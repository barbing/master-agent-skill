"""Typed recovery preserves code/usage and cannot bypass admission gates."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master_agent_runtime import ModuleSpec, ProjectSpec, Store, ValidationStep
from master_agent_runtime.contracts import AuthorityError, ConflictError
from master_agent_runtime.controller import Controller
from master_agent_runtime.health import inspect_project_state


class ReviewRecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); project=root/'input';project.mkdir()
        self.store=Store(root/'state'/'runtime.sqlite')
        self.pid=self.store.create_project(ProjectSpec(str(project),'Deliver',
            (ModuleSpec('one','Deliver',('one.py',),token_reservation=100),),10000,max_evidence_rounds=1))
        self.master=self.store.bind(self.pid+':master','master','master')
        self.owner=self.store.bind(self.pid+':module:one','owner','owner')
        op=self.store.dispatch(self.master,self.owner.actor_id,'Admitted work')
        self.store.mark_started(op,'turn');self.store.record_usage(self.owner.actor_id,71)
        self.store.settle(op,evidence='Synthetic fixture terminal acknowledgement')
        self.store.submit(self.owner,'candidate',tree_hash='code-sha',snapshot='sealed/code',inputs={},
            validation=[{'status':'passed','argv':['python','locked-check']}],summary='Fixture',operation_id=op)

    def reject(self, kind='evidence'):
        self.store.review(self.master,'candidate','request_changes','Attach executed concurrency results',
                          verified_tree_hash='code-sha',failure_kind=kind)

    def test_evidence_repair_retains_candidate_context_usage_and_code_allowance(self):
        before=self.store.status(self.pid);self.reject()
        row=self.store.modules(self.pid)[0]
        self.assertEqual(row['state'],'awaiting_evidence');self.assertEqual(row['rounds'],0)
        self.assertEqual(row['evidence_rounds'],1);self.assertEqual(self.store.pending_messages(self.owner.actor_id),[])
        self.store.attach_evidence(self.master,'candidate',path='sealed/evidence',evidence_hash='evidence-sha',recovered=True)
        self.assertEqual(self.store.modules(self.pid)[0]['state'],'awaiting_review')
        self.assertEqual(self.store.status(self.pid)['spent_tokens'],before['spent_tokens'])
        self.assertEqual(self.store.actor(self.owner.actor_id)['thread_id'],'owner')
        self.assertEqual(self.store.candidate('candidate')['tree_hash'],'code-sha')

    def test_evidence_recovery_is_finite_and_cannot_reopen_exhausted_work(self):
        self.reject();self.store.attach_evidence(self.master,'candidate',path='sealed/evidence',evidence_hash='first',recovered=True)
        self.reject();row=self.store.modules(self.pid)[0]
        self.assertEqual(row['state'],'blocked');self.assertEqual(row['rounds'],0)
        self.assertEqual(row['evidence_rounds'],2)
        with self.assertRaises(ConflictError):
            self.store.attach_evidence(self.master,'candidate',path='another',evidence_hash='forged',recovered=True)
        with self.assertRaises(ConflictError): self.store.invalidate_module(self.master,'one','Bypass through a new revision')
        health=inspect_project_state(self.store,self.pid)
        for expected in ('evidence_recovery_exhausted','Attach executed concurrency results',self.owner.actor_id,'2/1'):
            self.assertIn(expected,health['next_action'])
        self.assertFalse(health['safe_to_run'])

    def test_owner_cannot_attach_forged_evidence_or_remove_locked_checks(self):
        with self.assertRaises(AuthorityError):
            self.store.attach_evidence(self.owner,'candidate',path='forged',evidence_hash='forged')
        self.reject()
        with self.assertRaises(ConflictError):
            self.store.attach_evidence(self.master,'candidate',path='forged',evidence_hash='forged',validation=[],recovered=True)
        with self.assertRaises(ConflictError):
            self.store.attach_evidence(self.master,'candidate',path='forged',evidence_hash='forged',
                validation=[{'status':'passed','argv':['python','easier-check']}],recovered=True)

    def test_failed_check_cannot_be_laundered_into_an_evidence_only_rejection(self):
        with self.store.transaction() as con:
            con.execute('UPDATE candidate SET validation=? WHERE id=?',
                        (json.dumps([{'status':'failed','argv':['python','locked-check']}]),'candidate'))
        self.reject();row=self.store.modules(self.pid)[0]
        self.assertEqual(row['state'],'revision_requested');self.assertEqual(row['failure_kind'],'implementation')
        self.assertEqual(row['rounds'],1);self.assertEqual(row['evidence_rounds'],0)

    def test_uncertain_review_cannot_dispatch_another_writer(self):
        self.reject('uncertain_execution')
        row=self.store.modules(self.pid)[0]
        self.assertEqual(row['state'],'blocked');self.assertEqual(row['rounds'],0)
        with self.assertRaises(ConflictError): self.store.dispatch(self.master,self.owner.actor_id,'Blind retry')


class EvidenceRecoveryControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_master_admission_can_inspect_actual_project_before_any_owner_work(self):
        from test_runtime_controller import FakeActor, actual_fixture_validator
        class SourceInspectingMaster(FakeActor):
            inspected=False
            async def await_turn(self,turn_id,*,on_usage=None):
                if self.kind=='master' and 'Review initial project admission:' in self.message:
                    line=next((line for line in self.message.splitlines() if line.startswith('Readonly actual project source and acceptance: ')),None)
                    if line is None:
                        return {'text':json.dumps({'decision':'request_changes','rationale':'Actual project context unavailable'}),'turn':{'id':turn_id,'status':'completed'}}
                    source=Path(line.split(': ',1)[1])
                    assert (source/'b.py').read_text()=='value = -1\n'
                    type(self).inspected=True
                return await super().await_turn(turn_id,on_usage=on_usage)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';project.mkdir();(project/'b.py').write_text('value = -1\n')
            step=(ValidationStep(('python','fixed-oracle.py')),)
            store=Store(root/'state/runtime.sqlite')
            pid=store.create_project(ProjectSpec(str(project),'Deliver',(
                ModuleSpec('b','Provide 42',('b.py',),validation=step),),100000,integration_validation=step))
            with patch('master_agent_runtime.controller.validate',actual_fixture_validator):
                result=await Controller(store,pid,actor_factory=SourceInspectingMaster).run()
            self.assertEqual(result['state'],'accepted');self.assertTrue(SourceInspectingMaster.inspected)

    async def test_missing_proof_is_collected_and_rerun_without_owner_reimplementation(self):
        from test_runtime_controller import FakeActor, actual_fixture_validator
        class ProofRequestingMaster(FakeActor):
            requests=0
            async def await_turn(self,turn_id,*,on_usage=None):
                if self.kind=='master' and 'Review module' in self.message:
                    type(self).requests+=1
                    if type(self).requests==1:
                        return {'text':json.dumps({'decision':'request_changes','failure_kind':'evidence',
                            'rationale':'Missing command receipt; retain the code'}),'turn':{'id':turn_id,'status':'completed'}}
                return await super().await_turn(turn_id,on_usage=on_usage)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';project.mkdir();(project/'b.py').write_text('value = -1\n')
            validation=(ValidationStep(('python','fixed-oracle.py')),)
            store=Store(root/'state'/'runtime.sqlite')
            pid=store.create_project(ProjectSpec(str(project),'Deliver',(
                ModuleSpec('b','Provide 42',('b.py',),validation=validation),),100000,integration_validation=validation))
            controller=Controller(store,pid,actor_factory=ProofRequestingMaster)
            controller.collect_candidate_commands=AsyncMock(return_value=([], 'Synthetic read-only collector'))
            with patch('master_agent_runtime.controller.validate',actual_fixture_validator):
                result=await controller.run()
            self.assertEqual(result['state'],'accepted')
            self.assertEqual(ProofRequestingMaster.executions[pid+':module:b'],1)
            self.assertEqual(controller.collect_candidate_commands.await_count,1)
            row=store.modules(pid)[0]
            self.assertEqual(row['rounds'],0);self.assertEqual(row['evidence_rounds'],1)
            self.assertEqual(store.status(pid)['reserved_tokens'],0)
            candidate=store.candidate(row['accepted_candidate'])
            self.assertTrue(Path(candidate['evidence'],'index.json').is_file())
