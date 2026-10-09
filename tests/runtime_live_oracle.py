"""Independent frozen oracle for real module delivery; producers cannot edit it."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

candidate=Path(sys.argv[1]);context=Path(sys.argv[2]);mode=sys.argv[3]
sys.path.insert(0,str(context))
from master_agent_runtime import ModuleSpec,ProjectSpec,Store
from master_agent_runtime.contracts import ContractError

def load(name):
    path=candidate/'master_agent_runtime'/(name+'.py')
    spec=importlib.util.spec_from_file_location('master_agent_runtime.'+name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module

class AdmissionOracle(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.cwd());self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);project=self.root/'project';project.mkdir()
        self.spec=ProjectSpec(str(project),'Deliver the complete module',
            (ModuleSpec('one','Complete one module',('one.py',)),),10000)
        self.path=self.root/'handoff.json';self.path.write_text(json.dumps(self.spec.to_dict()))
        self.module=load('admission')

    def test_exact_roundtrip_and_deterministic_summary_without_model_or_fs_effects(self):
        spec=self.module.load_handoff(self.path)
        self.assertEqual(spec.fingerprint,self.spec.fingerprint)
        description=self.module.describe_handoff(spec)
        self.assertEqual(description['module_count'],1)
        self.assertEqual(description['modules'],['one'])
        self.assertEqual(description['spec_hash'],spec.fingerprint)
        self.assertEqual(description['native_calls'],0)
        self.assertEqual(self.module.describe_handoff(spec),description)

    def test_missing_objective_unknown_fields_and_bad_json_are_actionable_contract_errors(self):
        for data in ({**self.spec.to_dict(),'objective':''},{**self.spec.to_dict(),'extra':True},{'root':self.spec.root}):
            self.path.write_text(json.dumps(data))
            with self.assertRaises(ContractError) as caught:self.module.load_handoff(self.path)
            self.assertIn('handoff',str(caught.exception).lower())
        self.path.write_text('{invalid')
        with self.assertRaises(ContractError):self.module.load_handoff(self.path)

    def test_missing_file_and_non_object_inputs_preserve_original(self):
        with self.assertRaises(ContractError):self.module.load_handoff(self.root/'missing.json')
        self.path.write_text('[]');before=self.path.read_bytes()
        with self.assertRaises(ContractError):self.module.load_handoff(self.path)
        self.assertEqual(self.path.read_bytes(),before)

    def test_policy_prohibition_cannot_be_removed_via_handoff(self):
        data=self.spec.to_dict();data['forbidden_effects']=[];self.path.write_text(json.dumps(data))
        with self.assertRaises(ContractError):self.module.load_handoff(self.path)

class HealthOracle(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.cwd());self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);project=root/'project';project.mkdir()
        self.store=Store(root/'state'/'runtime.sqlite')
        spec=ProjectSpec(str(project),'Complete',(
            ModuleSpec('one','One',('one.py',),token_reservation=100),),10000)
        self.pid=self.store.create_project(spec)
        self.master=self.store.bind(self.pid+':master','master','master')
        self.owner=self.store.bind(self.pid+':module:one','owner','owner')
        self.module=load('health')

    def test_ready_diagnostic_is_readonly_and_does_not_claim_hard_billing(self):
        before=self.store.status(self.pid)
        report=self.module.inspect_project_state(self.store,self.pid)
        self.assertTrue(report['safe_to_run']);self.assertFalse(report['reconciliation_required'])
        self.assertEqual(report['project_id'],self.pid)
        self.assertEqual(report['active_operations'],[])
        self.assertFalse(report['usage']['hard_billing_cap'])
        self.assertEqual(self.store.status(self.pid),before)

    def test_unknown_operation_blocks_resume_without_refunding_or_writing(self):
        op=self.store.dispatch(self.master,self.owner.actor_id,'Complete module')
        self.store.mark_unknown(op,'Lost acknowledgement')
        before=self.store.status(self.pid)
        report=self.module.inspect_project_state(self.store,self.pid)
        self.assertFalse(report['safe_to_run']);self.assertTrue(report['reconciliation_required'])
        self.assertEqual(report['active_operations'][0]['id'],op)
        self.assertEqual(report['usage']['reserved_tokens'],100)
        self.assertEqual(self.store.status(self.pid),before)

    def test_cancellation_and_blocked_modules_are_not_reported_ready(self):
        op=self.store.dispatch(self.master,self.owner.actor_id,'Work')
        self.store.settle(op,state='failed',evidence='fixture process exited')
        report=self.module.inspect_project_state(self.store,self.pid)
        self.assertFalse(report['safe_to_run']);self.assertEqual(report['blocked_modules'],['one'])
        self.store.cancel_project(self.master)
        self.assertFalse(self.module.inspect_project_state(self.store,self.pid)['safe_to_run'])

    def test_accepted_project_and_unknown_project_are_explicit(self):
        with self.store.transaction() as con:con.execute("UPDATE project SET state='accepted' WHERE id=?",(self.pid,))
        report=self.module.inspect_project_state(self.store,self.pid)
        self.assertFalse(report['safe_to_run']);self.assertEqual(report['project_state'],'accepted')
        with self.assertRaises(ContractError):self.module.inspect_project_state(self.store,'missing')

suite=unittest.TestSuite()
if mode in ('admission','integration'):suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(AdmissionOracle))
if mode in ('health','integration'):suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(HealthOracle))
if not suite.countTestCases():raise SystemExit('Unknown oracle mode')
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
