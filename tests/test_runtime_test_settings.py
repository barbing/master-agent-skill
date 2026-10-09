from pathlib import Path
import tempfile
import unittest

from master_agent_runtime import ModuleSpec,ProjectSpec,Store
from master_agent_runtime.contracts import AuthorityError,BudgetError,ConflictError


class OperatorTestConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);project=root/'project';project.mkdir()
        self.store=Store(root/'state'/'runtime.sqlite')
        self.spec=ProjectSpec(str(project),'Preserve full objective',(ModuleSpec('one','Full module',('one.py',)),),200000)
        self.pid=self.store.create_project(self.spec)
        self.master=self.store.bind(self.pid+':master','m','m')
        self.owner=self.store.bind(self.pid+':module:one','o','o')

    def test_luna_test_revision_preserves_criteria_identity_and_accumulated_usage(self):
        self.store.record_usage(self.owner.actor_id,150000)
        before=self.store.status(self.pid)
        self.store.configure_test_runtime(self.pid,model='gpt-6-luna',token_budget=250000,authorization='Human explicitly requests Luna tests')
        after=self.store.status(self.pid);contract=self.store.project(self.pid)['contract']
        self.assertEqual(after['spent_tokens'],150000)
        self.assertEqual(before['actors'],after['actors']);self.assertEqual(before['modules'],after['modules'])
        self.assertEqual(contract.objective,self.spec.objective);self.assertEqual(contract.modules,self.spec.modules)
        self.assertEqual(contract.forbidden_effects,self.spec.forbidden_effects)
        self.assertEqual(contract.integration_validation,self.spec.integration_validation)
        self.assertEqual(contract.model,'gpt-6-luna')

    def test_unknown_work_cannot_be_erased_by_test_reconfiguration(self):
        op=self.store.dispatch(self.master,self.owner.actor_id,'Full work')
        self.store.mark_unknown(op,'Lost ack')
        with self.assertRaises(ConflictError):
            self.store.configure_test_runtime(self.pid,model='gpt-6-luna',token_budget=300000,authorization='Human testing request')
        self.assertEqual(self.store.operation(op)['status'],'unknown')
        self.assertEqual(self.store.status(self.pid)['reserved_tokens'],20000)

    def test_explicit_authorization_and_retention_of_incurred_usage_are_required(self):
        with self.assertRaises(AuthorityError):
            self.store.configure_test_runtime(self.pid,model='gpt-6-luna',token_budget=300000,authorization='')
        self.store.record_usage(self.owner.actor_id,100000)
        with self.assertRaises(BudgetError):
            self.store.configure_test_runtime(self.pid,model='gpt-6-luna',token_budget=99999,authorization='Human testing request')
