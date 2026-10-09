"""Journaled operator actions: explicit, bounded, never refunding or inferring results."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from master_agent_runtime.contracts import AuthorityError, BudgetError, ConflictError, ContractError
from master_agent_runtime.health import inspect_project_state
from master_agent_runtime.lease import project_lease

from unittest.mock import patch

from master_agent_runtime.controller import Controller
from runtime_fixtures import ScriptedActor, events, fixture_validator, make_project

PACKAGE = Path(__file__).resolve().parents[1]


class OperatorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.pid = make_project(Path(self.temp.name), [('one', ()), ('two', ())])
        self.master = self.store.bind(self.pid + ':master', 'm', 'm')
        self.owner = self.store.bind(self.pid + ':module:one', 'o', 'o')

    def held_operation(self):
        op = self.store.dispatch(self.master, self.owner.actor_id, 'Work')
        self.store.mark_started(op, 'native-turn')
        self.store.record_usage(self.owner.actor_id, 777)
        self.store.mark_unknown(op, 'Controller crashed; native outcome unknowable')
        return op

    def test_resolve_closes_unknowable_work_without_inferring_a_submission(self):
        op = self.held_operation()
        with self.assertRaises(AuthorityError):
            self.store.operator_resolve_operation(self.pid, op, outcome='failed', authorization='  ')
        with self.assertRaises(ContractError):
            self.store.operator_resolve_operation(self.pid, op, outcome='done', authorization='Human checked')
        self.store.operator_resolve_operation(self.pid, op, outcome='failed', authorization='Human checked: process gone')
        status = self.store.status(self.pid)
        self.assertEqual(status['reserved_tokens'], 0)
        self.assertEqual(status['spent_tokens'], 777)
        row = next(m for m in status['modules'] if m['key'] == 'one')
        self.assertEqual((row['state'], row['failure_kind'], row['current_candidate']), ('blocked', 'uncertain_execution', None))
        self.assertEqual(self.store.operation(op)['outcome'], 'operator_resolved')
        journal = events(self.store, self.pid, 'operator-resolved-operation')[0][1]
        self.assertEqual((journal['previous_status'], journal['usage_retained']), ('unknown', 777))
        with self.assertRaises(ConflictError):
            self.store.operator_resolve_operation(self.pid, op, outcome='failed', authorization='again')

    def test_rearm_grants_exactly_the_admitted_extra_attempts(self):
        for attempt in range(3):
            op = self.store.dispatch(self.master, self.owner.actor_id, 'Work')
            self.store.mark_started(op, f'turn-{attempt}')
            self.store.settle(op, evidence='fixture completion')
            self.store.submit(self.owner, f'c{attempt}', tree_hash='t', snapshot='s', inputs={},
                              validation=[{'status': 'passed'}], summary='x', operation_id=op)
            self.store.review(self.master, f'c{attempt}', 'request_changes', 'Still incomplete', verified_tree_hash='t')
        self.assertEqual(self.store.modules(self.pid)[0]['state'], 'blocked')
        live = self.store.dispatch(self.master, self.store.bind(self.pid + ':module:two', 't', 't').actor_id, 'Other')
        self.assertEqual(self.store.operator_rearm_module(self.pid, 'one', authorization='Human: one more try'),
                         'revision_requested')
        with self.assertRaises(ConflictError):
            self.store.operator_rearm_module(self.pid, 'one', authorization='not blocked any more')
        op = self.store.dispatch(self.master, self.owner.actor_id, 'Rearmed work')
        self.store.mark_started(op, 'turn-r'); self.store.settle(op, evidence='fixture completion')
        self.store.submit(self.owner, 'c3', tree_hash='t', snapshot='s', inputs={},
                          validation=[{'status': 'passed'}], summary='x', operation_id=op)
        self.store.review(self.master, 'c3', 'request_changes', 'Still incomplete', verified_tree_hash='t')
        self.assertEqual(self.store.modules(self.pid)[0]['state'], 'blocked')   # the one extra attempt was used
        self.assertEqual(self.store.operation(live)['status'], 'intent')       # other work untouched by rearm
        self.store.settle(live, state='failed', evidence='fixture: other module settled before health check')
        self.assertIn('rearm ' + self.pid, inspect_project_state(self.store, self.pid)['next_action'])

    def test_rearm_refuses_while_the_owner_has_unsettled_work(self):
        op = self.held_operation()
        with self.store.transaction() as con:
            con.execute("UPDATE module SET state='blocked' WHERE key='one'")
        with self.assertRaises(ConflictError):
            self.store.operator_rearm_module(self.pid, 'one', authorization='Human: retry')
        self.assertIn('resolve ' + self.pid, inspect_project_state(self.store, self.pid)['next_action'])
        self.store.operator_resolve_operation(self.pid, op, outcome='failed', authorization='Human: confirmed dead')
        self.assertEqual(self.store.operator_rearm_module(self.pid, 'one', authorization='Human: retry'), 'revision_requested')

    def test_admission_rerun_and_override_are_journaled_decisions(self):
        self.store.record_admission(self.pid, decision='request_changes', rationale='Master saw a harness defect')
        self.assertEqual(self.store.admission_state(self.pid), 'rejected')
        self.store.operator_admission(self.pid, decision='rerun', authorization='Human: harness fixed')
        self.assertEqual(self.store.admission_state(self.pid), 'pending')
        self.store.record_admission(self.pid, decision='request_changes', rationale='Still unhappy')
        self.store.operator_admission(self.pid, decision='override', authorization='Human accepts the contract')
        self.assertEqual(self.store.admission_state(self.pid), 'admitted')
        with self.assertRaises(ConflictError):
            self.store.operator_admission(self.pid, decision='override', authorization='already admitted')

    def test_budget_and_policy_changes_keep_usage_and_bound_contexts(self):
        self.store.record_usage(self.owner.actor_id, 5000)
        with self.assertRaises(BudgetError):
            self.store.operator_set_budget(self.pid, 4999, authorization='Human: shrink')
        before = self.store.project(self.pid)['contract']
        self.store.operator_set_budget(self.pid, 250000, authorization='Human: extend')
        after = self.store.project(self.pid)['contract']
        self.assertEqual((after.token_budget, after.modules, after.objective), (250000, before.modules, before.objective))
        policy = self.store.operator_set_policy(self.pid, {'owner_turn_timeout_seconds': 5400},
                                                authorization='Human: long module')
        self.assertEqual(policy.owner_turn_timeout_seconds, 5400)
        with self.assertRaises(ConflictError):
            self.store.operator_set_policy(self.pid, {'provider': 'claude'}, authorization='switch bound actors')
        self.store.operator_set_policy(self.pid, {'modules': {'two': {'provider': 'claude'}}},
                                       authorization='Human: unbound module uses Claude')
        self.assertEqual(self.store.project(self.pid)['policy'].runtime_for(after, 'two').provider, 'claude')
        with self.assertRaises(ContractError):
            self.store.operator_set_policy(self.pid, {'max_continuations': 99}, authorization='out of bounds')


class OperatorCliTests(unittest.TestCase):
    def test_operator_commands_are_refused_while_a_controller_holds_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = make_project(Path(tmp), [('one', ())])
            master = store.bind(pid + ':master', 'm', 'm')
            owner = store.bind(pid + ':module:one', 'o', 'o')
            op = store.dispatch(master, owner.actor_id, 'Work'); store.mark_unknown(op, 'lost')
            argv = [sys.executable, '-B', '-m', 'master_agent_runtime', '--state', str(store.path.parent),
                    'resolve', pid, '--operation', op, '--authorization', 'Human confirmed the process is gone']
            lock = store.path.parent / 'projects' / pid / 'controller.lock'
            with project_lease(lock):
                refused = subprocess.run(argv, cwd=PACKAGE, capture_output=True, text=True, timeout=60)
            self.assertEqual(refused.returncode, 1)
            self.assertIn('Another controller owns this project', refused.stderr)
            self.assertEqual(store.operation(op)['status'], 'unknown')
            done = subprocess.run(argv + ['--rearm'], cwd=PACKAGE, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(done.stdout)
            self.assertEqual(result['operator_action'], 'resolve')
            self.assertEqual(store.operation(op)['status'], 'failed')
            self.assertEqual(store.modules(pid)[0]['state'], 'revision_requested')



class StopRaceTests(unittest.IsolatedAsyncioTestCase):
    """Advisories raised by a Haiku Master reviewing the historical Store/CLI replay."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ScriptedActor.reset()

    async def test_stop_between_dispatch_and_start_acknowledgement_interrupts_and_settles(self):
        store, pid = make_project(self.root, [('one', ())])
        actors = []
        def owner(actor, number, message):
            actors.append(actor)
            store.request_stop(pid)          # the stop lands while turn/start is in flight
            return {'hang': True}
        ScriptedActor.owner_script = staticmethod(owner)
        with patch('master_agent_runtime.controller.validate', fixture_validator):
            result = await Controller(store, pid, actor_factory=ScriptedActor).run()
        self.assertEqual(result['state'], 'cancelled')
        work = [op for op in store.status(pid)['operations'] if op['kind'] == 'work']
        self.assertEqual([(op['status'], op['remote_id']) for op in work], [('cancelled', 'one-turn-1')])
        self.assertTrue(actors[0].interrupted)
        self.assertFalse([op for op in store.status(pid)['operations'] if op['status'] in ('intent', 'running', 'unknown', 'stopping')])

    def test_operator_resolution_of_a_crashed_binding_finishes_a_pending_stop(self):
        store, pid = make_project(self.root, [('one', ())])
        binding = store.binding_intent(pid + ':module:one', {'existing_thread': None})
        store.request_stop(pid)
        self.assertEqual(store.project(pid)['state'], 'cancelling')
        store.operator_resolve_operation(pid, binding, outcome='cancelled', authorization='Human: binding process crashed')
        self.assertEqual(store.project(pid)['state'], 'cancelled')
