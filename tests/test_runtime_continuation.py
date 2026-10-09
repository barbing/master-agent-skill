"""Stopped native turns are settled from observed exit, checkpointed and continued.

Scripted synthetic actors; the deadline, budget and transport paths use the
real controller, store and artifact code but no live model.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from master_agent_runtime.contracts import ConflictError, UnknownOutcome
from master_agent_runtime.controller import Controller

from runtime_fixtures import ScriptedActor, events, fixture_validator, make_project


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ScriptedActor.reset()
        self.seen = []

    async def run_project(self, store, pid):
        with patch('master_agent_runtime.controller.validate', fixture_validator):
            return await Controller(store, pid, actor_factory=ScriptedActor).run()

    def resuming_owner(self, first):
        """First turn misbehaves after writing partial work; later turns finish from the preserved file."""
        def owner(actor, number, message):
            self.seen.append(((actor.workspace / 'one.py').read_text(), 'Continuation:' in message, message))
            if number == 1:
                return {'write': {'one.py': 'value = 21  # partial\n'}, **first}
            return {'write': {'one.py': 'value = 42\n'}}
        return owner

    async def test_deadline_interrupts_checkpoints_and_the_same_owner_continues(self):
        store, pid = make_project(self.root, [('one', ())], policy={'owner_turn_timeout_seconds': 1,
                                                                    'interrupt_grace_seconds': 1})
        ScriptedActor.owner_script = staticmethod(self.resuming_owner({'hang': True}))
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(self.seen[1][0], 'value = 21  # partial\n')   # preserved owned work
        self.assertTrue(self.seen[1][1])                               # explicit continuation note
        opens = [existing for key, existing in ScriptedActor.opens if key == 'one']
        self.assertEqual(opens, [None, 'thread-' + pid + ':module:one'])
        work = [op for op in store.status(pid)['operations'] if op['kind'] == 'work']
        self.assertEqual([(op['status'], op['outcome'], op['exit_observed']) for op in work],
                         [('failed', 'deadline', 1), ('done', None, 0)])
        row = store.modules(pid)[0]
        self.assertEqual((row['continuations'], row['checkpoint']), (0, None))   # chain ended by submission
        stopped = events(store, pid, 'native-turn-stopped')
        self.assertEqual(stopped[0][1]['module_state'], 'continuing')
        self.assertIsNotNone(stopped[0][1]['checkpoint'])
        self.assertEqual(store.status(pid)['reserved_tokens'], 0)

    async def test_continuation_limit_blocks_then_operator_rearm_resumes_from_checkpoint(self):
        store, pid = make_project(self.root, [('one', ())], policy={'owner_turn_timeout_seconds': 1,
                                                                    'interrupt_grace_seconds': 1,
                                                                    'max_continuations': 0})
        ScriptedActor.owner_script = staticmethod(self.resuming_owner({'hang': True}))
        first = await self.run_project(store, pid)
        self.assertEqual(first['state'], 'blocked_or_reconciliation_required')
        row = store.modules(pid)[0]
        self.assertEqual((row['state'], row['failure_kind']), ('blocked', 'deadline'))
        self.assertIsNotNone(row['checkpoint'])
        self.assertEqual(store.operator_rearm_module(pid, 'one', authorization='Human: allow one more attempt'), 'continuing')
        second = await self.run_project(store, pid)
        self.assertEqual(second['state'], 'accepted')
        self.assertEqual(self.seen[-1][0], 'value = 21  # partial\n')
        self.assertIn('Operator rearmed this module', self.seen[-1][2])

    async def test_budget_stop_preserves_work_and_resumes_after_operator_extends_envelope(self):
        store, pid = make_project(self.root, [('one', ())], budget=1000)
        ScriptedActor.owner_script = staticmethod(self.resuming_owner({'usage': 5000, 'hang': True}))
        first = await self.run_project(store, pid)
        self.assertEqual(first['state'], 'budget_exhausted')
        row = store.modules(pid)[0]
        self.assertEqual((row['state'], row['continuations']), ('continuing', 0))   # budget is not the owner's fault
        spent = store.status(pid)['spent_tokens']
        self.assertGreaterEqual(spent, 5000)
        with self.assertRaises(Exception):
            store.operator_set_budget(pid, spent - 1, authorization='Human: too small')
        store.operator_set_budget(pid, spent + 20000, authorization='Human: extend envelope for this module')
        second = await self.run_project(store, pid)
        self.assertEqual(second['state'], 'accepted')
        self.assertEqual(self.seen[-1][0], 'value = 21  # partial\n')
        self.assertGreaterEqual(store.status(pid)['spent_tokens'], spent)
        self.assertEqual(events(store, pid, 'operator-budget-revised')[0][1]['before'], 1000)

    async def test_missing_structured_receipt_continues_instead_of_wedging(self):
        store, pid = make_project(self.root, [('one', ())])
        ScriptedActor.owner_script = staticmethod(self.resuming_owner({'text': 'I am done (not JSON)'}))
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        work = [op for op in store.status(pid)['operations'] if op['kind'] == 'work']
        self.assertEqual(work[0]['outcome'], 'receipt_missing')

    async def test_lost_transport_with_observed_exit_continues_from_checkpoint(self):
        store, pid = make_project(self.root, [('one', ())])
        ScriptedActor.owner_script = staticmethod(self.resuming_owner({'raise': UnknownOutcome('synthetic dropped stream')}))
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        work = [op for op in store.status(pid)['operations'] if op['kind'] == 'work']
        self.assertEqual((work[0]['status'], work[0]['outcome'], work[0]['exit_observed']), ('failed', 'transport_lost', 1))
        self.assertEqual(self.seen[1][0], 'value = 21  # partial\n')

    async def test_scope_violation_returns_correction_with_owned_files_preserved(self):
        store, pid = make_project(self.root, [('one', ())])
        def owner(actor, number, message):
            self.seen.append(((actor.workspace / 'one.py').read_text(), (actor.workspace / 'stray.py').exists(), message))
            if number == 1:
                return {'write': {'one.py': 'value = 42\n', 'stray.py': 'value = 42\n'}}
            return {}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(self.seen[1][:2], ('value = 42\n', False))
        self.assertIn('could not be sealed', self.seen[1][2])
        self.assertIn('stray.py', self.seen[1][2])
        unsubmittable = events(store, pid, 'candidate-unsubmittable')
        self.assertEqual(unsubmittable[0][1]['module_state'], 'revision_requested')
        self.assertFalse((Path(result['path']) / 'stray.py').exists())


class StoppedTurnStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.pid = make_project(Path(self.temp.name), [('one', ())])
        self.master = self.store.bind(self.pid + ':master', 'm', 'm')
        self.owner = self.store.bind(self.pid + ':module:one', 'o', 'o')

    def test_settled_operation_cannot_be_settled_again_as_stopped(self):
        op = self.store.dispatch(self.master, self.owner.actor_id, 'Work')
        self.store.settle(op, evidence='fixture completion')
        with self.assertRaises(ConflictError):
            self.store.settle_interrupted(op, reason='deadline', evidence='late', instructions='continue')

    def test_started_turn_cannot_be_released_as_rejected_dispatch(self):
        op = self.store.dispatch(self.master, self.owner.actor_id, 'Work')
        self.store.mark_started(op, 'native-turn')
        with self.assertRaises(ConflictError):
            self.store.release_rejected_dispatch(op, reason='too late to claim nothing ran')
        self.assertEqual(self.store.status(self.pid)['reserved_tokens'], 100)

    def test_unknown_stop_reason_is_refused(self):
        op = self.store.dispatch(self.master, self.owner.actor_id, 'Work')
        with self.assertRaises(Exception):
            self.store.settle_interrupted(op, reason='invented', evidence='x', instructions='y')


class WorkspacePreparationTests(unittest.IsolatedAsyncioTestCase):
    """Found live with Codex owners: an exact write grant cannot create a missing parent directory."""

    async def test_parents_of_admitted_paths_exist_before_the_owner_turn(self):
        from master_agent_runtime import ModuleSpec, ProjectSpec, Store
        from runtime_fixtures import CHECK
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root / 'project'; project.mkdir()
            (project / 'README.md').write_text('base\n')
            spec = ProjectSpec(str(project), 'Add a module in a new package', (
                ModuleSpec('one', 'Provide value 42 in pkg/deep/one.py', ('pkg/deep/one.py',), token_reservation=100,
                           validation=CHECK),), 100000, integration_validation=CHECK)
            store = Store(root / 'state' / 'runtime.sqlite'); pid = store.create_project(spec)
            ScriptedActor.reset(); seen = []
            def owner(actor, number, message):
                seen.append((actor.workspace / 'pkg' / 'deep').is_dir())
                return {'write': {'pkg/deep/one.py': 'value = 42\n'}}
            ScriptedActor.owner_script = staticmethod(owner)
            with patch('master_agent_runtime.controller.validate', fixture_validator):
                result = await Controller(store, pid, actor_factory=ScriptedActor).run()
            self.assertEqual(seen, [True])
            self.assertEqual(result['state'], 'accepted')
