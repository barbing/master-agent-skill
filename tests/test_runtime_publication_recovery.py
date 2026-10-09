"""Recover publication gaps without duplicate inference or false submissions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master_agent_runtime import ModuleSpec, ProjectSpec, Store, ValidationStep
from master_agent_runtime.controller import Controller
from master_agent_runtime.recovery import reconcile


class PublicationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);source=root/'input';source.mkdir();(source/'one.py').write_text('value = 0\n')
        self.store=Store(root/'control/runtime.sqlite')
        steps=(ValidationStep(('python','fixed-check.py')),)
        self.pid=self.store.create_project(ProjectSpec(str(source),'Deliver',(
            ModuleSpec('one','Deliver',('one.py',),validation=steps,token_reservation=100),),10000,integration_validation=steps))
        self.master=self.store.bind(self.pid+':master','master','master')
        self.owner=self.store.bind(self.pid+':module:one','owner','owner')
        self.controller=Controller(self.store,self.pid,cli='synthetic-peer');self.controller.prepare()
        self.workspace=self.controller.workspaces/'one'/'activation';self.workspace.mkdir(parents=True)
        (self.workspace/'one.py').write_text('value = 42\n')
        binding=self.store.binding_intent(self.owner.actor_id,{'workspace':str(self.workspace)})
        self.store.settle(binding,evidence='Synthetic binding acknowledgement')
        self.operation=self.store.dispatch(self.master,self.owner.actor_id,'Deliver')
        self.store.mark_started(self.operation,'native-turn')
        self.peer=AsyncMock()

    def native_result(self,status='submitted'):
        self.peer.call.return_value={'thread':{'id':'owner','sessionId':'owner','turns':[
            {'id':'native-turn','status':'completed','items':[
                {'type':'agentMessage','phase':'final_answer','text':json.dumps({'status':status,'summary':'Synthetic real-file fixture','issues':[]})}]}]}}

    async def recover(self):
        with patch('master_agent_runtime.recovery.JsonRpcClient',return_value=self.peer), \
             patch('master_agent_runtime.controller.validate',return_value=[{'status':'passed','oracle':'independent synthetic file check'}]):
            return await reconcile(self.store,self.pid,cli='synthetic-peer')

    async def test_already_settled_completed_producer_is_submitted_once(self):
        self.native_result();self.store.settle(self.operation,evidence='Native completion observed before publication crash')
        first=await self.recover()
        self.assertEqual(len(first['reconciled']),1)
        self.assertEqual(self.store.modules(self.pid)[0]['state'],'awaiting_review')
        self.assertEqual(self.store.status(self.pid)['reserved_tokens'],0)
        self.assertEqual(self.store.actor(self.owner.actor_id)['thread_id'],'owner')
        second=await self.recover();self.assertEqual(second['reconciled'],[])
        self.assertEqual([call.args[0] for call in self.peer.call.call_args_list],['thread/read'])

    async def test_completed_but_blocked_receipt_never_becomes_a_submission(self):
        self.native_result('blocked');self.store.mark_unknown(self.operation,'Synthetic lost completion response')
        report=await self.recover()
        self.assertEqual(report['reconciled'][0]['state'],'failed')
        self.assertEqual(self.store.modules(self.pid)[0]['state'],'blocked')
        self.assertIsNone(self.store.modules(self.pid)[0]['current_candidate'])
        self.assertEqual(self.store.status(self.pid)['reserved_tokens'],0)

    async def test_missing_producer_receipt_retains_uncertainty_and_budget(self):
        self.native_result();self.peer.call.return_value['thread']['turns'][0]['items']=[]
        self.store.mark_unknown(self.operation,'Synthetic lost receipt')
        report=await self.recover()
        self.assertEqual(len(report['held']),1)
        self.assertEqual(self.store.status(self.pid)['reserved_tokens'],100)
        self.assertEqual(self.store.operation(self.operation)['status'],'unknown')
