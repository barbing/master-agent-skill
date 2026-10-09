import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from master_agent_runtime import ModuleSpec,ProjectSpec,Store
from master_agent_runtime.contracts import ConflictError,ProjectCancelled
from master_agent_runtime.controller import Controller
from master_agent_runtime.lease import project_lease
from master_agent_runtime.recovery import reconcile


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);project=root/'project';project.mkdir()
        self.store=Store(root/'state'/'runtime.sqlite')
        spec=ProjectSpec(str(project),'Complete module',(ModuleSpec('one','One',('one.py',),token_reservation=100),),10000)
        self.pid=self.store.create_project(spec)
        self.master=self.store.bind(self.pid+':master','master','master')
        self.owner=self.store.bind(self.pid+':module:one','one','one')

    async def test_unacknowledged_turn_keeps_budget_and_never_creates_or_retries_native_work(self):
        operation=self.store.dispatch(self.master,self.owner.actor_id,'Work')
        self.store.mark_unknown(operation,'Lost start acknowledgement')
        client=AsyncMock()
        with patch('master_agent_runtime.recovery.JsonRpcClient',return_value=client):
            report=await reconcile(self.store,self.pid,cli='fake-labelled-peer')
        self.assertEqual(len(report['held']),1)
        self.assertEqual(self.store.status(self.pid)['reserved_tokens'],100)
        client.call.assert_not_awaited()

    async def test_known_interrupted_turn_reconciles_without_model_respawn(self):
        operation=self.store.dispatch(self.master,self.owner.actor_id,'Work')
        self.store.mark_started(operation,'turn-one');self.store.mark_unknown(operation,'Disconnected')
        client=AsyncMock();client.call.return_value={'thread':{'id':'one','sessionId':'one','turns':[{'id':'turn-one','status':'interrupted'}]}}
        with patch('master_agent_runtime.recovery.JsonRpcClient',return_value=client):
            report=await reconcile(self.store,self.pid,cli='fake-labelled-peer')
        self.assertEqual(report['reconciled'][0]['state'],'failed')
        self.assertEqual(self.store.status(self.pid)['reserved_tokens'],0)
        self.assertEqual([c.args[0] for c in client.call.call_args_list],['thread/read'])

    async def test_cancellation_interrupts_owned_turn_and_settles_only_after_exit(self):
        operation=self.store.dispatch(self.master,self.owner.actor_id,'Work')
        actor=AsyncMock();actor.start_turn.return_value='turn';actor.close.return_value={'process_exited':True}
        async def pending(*args,**kwargs):await asyncio.Future()
        actor.await_turn.side_effect=pending;actor.events=[];actor.effective=None
        controller=Controller(self.store,self.pid,actor_factory=lambda:None)
        task=asyncio.create_task(controller.run_native_operation(actor,self.owner,operation,'Work',{}))
        await asyncio.sleep(.01);self.store.cancel_project(self.master)
        with self.assertRaises(ProjectCancelled):await task
        actor.interrupt.assert_awaited_once_with('turn')
        self.assertEqual(self.store.operation(operation)['status'],'cancelled')
        self.assertTrue(self.store.finalize_cancellation(self.pid))


class LeaseTests(unittest.TestCase):
    def test_second_controller_refused_and_crash_release_equivalent_is_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'project.lock'
            with project_lease(path):
                with self.assertRaises(ConflictError):
                    with project_lease(path):pass
            with project_lease(path):pass
