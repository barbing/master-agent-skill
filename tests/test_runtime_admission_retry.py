"""Admission robustness: no implicit approval, bounded retry, reconciled verdicts (synthetic actors)."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master_agent_runtime.contracts import ContractError
from master_agent_runtime.controller import Controller
from master_agent_runtime.health import inspect_project_state
from master_agent_runtime.recovery import reconcile

from runtime_fixtures import ScriptedActor, default_master, events, fixture_validator, make_project


MALFORMED = {'rationale': 'a turn that returned no decision field'}


def admission_script(outputs):
    """Admission turns return the queued outputs in order, then approve."""
    queue = list(outputs)
    def master(actor, number, message, schema):
        if 'Review initial project admission' in message:
            return queue.pop(0) if queue else {'decision': 'approve', 'rationale': 'Feasible bounded mandates'}
        return default_master(actor, number, message, schema)
    return master


def operations(store, pid, kind):
    return [op for op in store.status(pid)['operations'] if op['kind'] == kind]


class AdmissionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ScriptedActor.reset()

    async def run_project(self, store, pid):
        with patch('master_agent_runtime.controller.validate', fixture_validator):
            return await Controller(store, pid, actor_factory=ScriptedActor).run()

    async def test_failed_admission_turn_is_retried_and_never_counts_as_approval(self):
        store, pid = make_project(self.root, [('one', ())], policy={'transient_backoff_seconds': 0})
        ScriptedActor.master_script = staticmethod(admission_script([MALFORMED, MALFORMED]))
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual([op['status'] for op in operations(store, pid, 'admission')], ['failed', 'failed', 'done'])
        self.assertEqual([payload['decision'] for _, payload in events(store, pid, 'master-project-admission')], ['approve'])
        # No owner turn started before the Master actually approved.
        self.assertEqual(ScriptedActor.log[:4], [('master', 1, None), ('master', 2, None),
                                                 ('master', 3, 'approve'), ('one', 1, 'started')])

    async def test_exhausted_retries_dispatch_no_work_and_the_next_run_asks_again(self):
        store, pid = make_project(self.root, [('one', ())],
                                  policy={'transient_backoff_seconds': 0, 'max_transient_retries': 1})
        ScriptedActor.master_script = staticmethod(admission_script([MALFORMED, MALFORMED]))
        with self.assertRaisesRegex(ContractError, 'no decision in 2 attempts'):
            await self.run_project(store, pid)
        self.assertEqual(store.admission_state(pid), 'pending')
        self.assertEqual(operations(store, pid, 'work'), [])
        result = await self.run_project(store, pid)               # the scripted Master now approves
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual([op['status'] for op in operations(store, pid, 'admission')], ['failed', 'failed', 'done'])

    async def test_stop_during_admission_backoff_ends_the_run_as_cancelled(self):
        store, pid = make_project(self.root, [('one', ())], policy={'transient_backoff_seconds': 30})
        ScriptedActor.master_script = staticmethod(admission_script([MALFORMED] * 5))
        task = asyncio.create_task(self.run_project(store, pid))
        for _ in range(250):
            if any(op['status'] == 'failed' for op in operations(store, pid, 'admission')):
                break
            await asyncio.sleep(.02)
        store.request_stop(pid)
        result = await asyncio.wait_for(task, 5)                  # well inside the 30 s backoff
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(len(operations(store, pid, 'admission')), 1)
        self.assertEqual(operations(store, pid, 'work'), [])


class LegacyAdmissionStateTests(unittest.TestCase):
    def test_only_master_review_work_implies_a_legacy_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = make_project(Path(tmp), [('one', ())])
            master = store.bind(pid + ':master', 'master', 'master')
            operation = store.dispatch(master, master.actor_id, 'Admission', kind='admission')
            store.settle(operation, state='failed', evidence='native turn failed before a decision')
            self.assertEqual(store.admission_state(pid), 'pending')
            operation = store.dispatch(master, master.actor_id, 'Admission', kind='admission')
            store.settle(operation, evidence='settled, but no verdict was journaled')
            self.assertEqual(store.admission_state(pid), 'pending')
            operation = store.dispatch(master, master.actor_id, 'Review', kind='review')
            store.settle(operation, evidence='legacy review turn after an approved admission')
            self.assertEqual(store.admission_state(pid), 'admitted')


class ReconciledAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.pid = make_project(Path(self.temp.name), [('one', ())])
        self.master = self.store.bind(self.pid + ':master', 'master', 'master')

    async def reconcile_admission(self, text):
        operation = self.store.dispatch(self.master, self.master.actor_id, 'Admission', kind='admission')
        self.store.mark_started(operation, 'turn-admission')
        self.store.mark_unknown(operation, 'Disconnected after the turn completed')
        client = AsyncMock()
        client.call.return_value = {'thread': {'id': 'master', 'sessionId': 'master', 'turns': [
            {'id': 'turn-admission', 'status': 'completed', 'items': [{'type': 'agentMessage', 'text': text}]}]}}
        with patch('master_agent_runtime.recovery.JsonRpcClient', return_value=client):
            report = await reconcile(self.store, self.pid, cli='fake-labelled-peer')
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ['thread/read'])
        return operation, report

    async def test_completed_admission_turn_records_its_verdict_without_a_new_turn(self):
        operation, report = await self.reconcile_admission(json.dumps({'decision': 'approve', 'rationale': 'Consistent mandate'}))
        self.assertEqual(report['reconciled'], [{'operation': operation, 'state': 'done', 'admission': 'approve'}])
        self.assertEqual(self.store.admission_state(self.pid), 'admitted')
        self.assertEqual(events(self.store, self.pid, 'master-project-admission')[0][1]['source'], 'reconciled')

    async def test_reconciled_rejection_still_blocks_dispatch(self):
        await self.reconcile_admission(json.dumps({'decision': 'request_changes', 'rationale': 'Contradictory scope'}))
        self.assertEqual(self.store.admission_state(self.pid), 'rejected')

    async def test_reconciled_turn_without_a_valid_verdict_is_asked_again(self):
        operation, report = await self.reconcile_admission('Looks fine to me.')
        self.assertEqual(self.store.operation(operation)['status'], 'done')
        self.assertEqual(report['reconciled'], [{'operation': operation, 'state': 'done'}])
        self.assertEqual(self.store.admission_state(self.pid), 'pending')



class AdmissionHealthTests(unittest.TestCase):
    def test_health_names_the_admission_state_and_remedy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = make_project(Path(tmp), [('one', ())])
            master = store.bind(pid + ':master', 'master', 'master')
            report = inspect_project_state(store, pid)
            self.assertTrue(report['safe_to_run'])
            self.assertIn('asks the Master for project admission', report['next_action'])
            operation = store.dispatch(master, master.actor_id, 'Admission', kind='admission')
            store.settle(operation, state='failed', evidence='native turn returned no decision')
            self.assertIn('1 earlier admission turn(s) returned no decision', inspect_project_state(store, pid)['next_action'])
            store.record_admission(pid, decision='request_changes', rationale='Contradictory scope')
            report = inspect_project_state(store, pid)
            self.assertFalse(report['safe_to_run'])
            self.assertIn('admission ' + pid + ' --decision rerun|override', report['next_action'])
            store.operator_admission(pid, decision='override', authorization='Human accepts the contract')
            report = inspect_project_state(store, pid)
            self.assertTrue(report['safe_to_run'])
            self.assertNotIn('admission', report['next_action'])

if __name__ == '__main__':
    unittest.main()
