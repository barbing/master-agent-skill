from pathlib import Path
import tempfile
import unittest
import json

from master_agent_runtime import ValidationStep
from master_agent_runtime.controller import Controller
from master_agent_runtime.contracts import ContractError

from master_agent_runtime import ModuleSpec,ProjectSpec,Store
from master_agent_runtime.contracts import ConflictError


class EmptyMasterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);project=root/'project';project.mkdir()
        self.store=Store(root/'state'/'runtime.sqlite')
        self.pid=self.store.create_project(ProjectSpec(str(project),'Complete one scope',(ModuleSpec('one','One',('one.py',)),),100000))
        self.master=self.store.bind(self.pid+':master','unused-master','unused-master')
        self.owner=self.store.bind(self.pid+':module:one','owner','owner')

    def test_unused_missing_root_is_explicitly_recovered_without_replacing_worker(self):
        self.store.recover_unused_master_binding(self.pid,evidence='native definitive no rollout found')
        self.assertIsNone(self.store.master(self.pid)['thread_id'])
        self.assertEqual(self.store.master(self.pid)['epoch'],2)
        self.assertEqual(self.store.actor(self.owner.actor_id)['thread_id'],'owner')

    def test_master_with_any_admitted_model_work_cannot_be_replaced(self):
        op=self.store.dispatch(self.master,self.master.actor_id,'Actual admission',kind='admission')
        self.store.settle(op,evidence='terminal model turn')
        with self.assertRaises(ConflictError):
            self.store.recover_unused_master_binding(self.pid,evidence='missing rollout')
        self.assertEqual(self.store.master(self.pid)['thread_id'],'unused-master')

    def test_unresolved_binding_cannot_be_discarded(self):
        op=self.store.binding_intent(self.master.actor_id,{'existing_thread':'unused-master'})
        self.store.mark_unknown(op,'unknown')
        with self.assertRaises(ConflictError):
            self.store.recover_unused_master_binding(self.pid,evidence='missing rollout')


class AdmissionRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_master_admission_is_not_bypassed_by_restart(self):
        from test_runtime_controller import FakeActor
        class RejectingMaster(FakeActor):
            async def await_turn(self,turn_id,*,on_usage=None):
                if self.kind=='master':
                    return {'text':json.dumps({'decision':'request_changes','rationale':'Unresolved frozen admission criterion'}),'turn':{'id':turn_id,'status':'completed'}}
                raise AssertionError('No module work admitted after Master rejection')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';project.mkdir()
            validation=(ValidationStep(('python','fixed-oracle.py')),)
            store=Store(root/'state'/'runtime.sqlite')
            spec=ProjectSpec(str(project),'Complete one module',(ModuleSpec('one','One',('one.py',),validation=validation),),100000,integration_validation=validation)
            pid=store.create_project(spec)
            for _ in range(2):
                with self.assertRaises(ContractError):
                    await Controller(store,pid,actor_factory=RejectingMaster).run()
            self.assertFalse(any(op['kind']=='work' for op in store.status(pid)['operations']))
