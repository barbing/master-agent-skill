"""Event-driven scheduling with scripted synthetic actors; not live-model proof."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from master_agent_runtime.controller import Controller

from runtime_fixtures import ScriptedActor, events, fixture_validator, make_project


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ScriptedActor.reset()

    async def run_project(self, store, pid):
        with patch('master_agent_runtime.controller.validate', fixture_validator):
            return await Controller(store, pid, actor_factory=ScriptedActor).run()

    async def test_fast_module_is_reviewed_while_slow_owner_still_works(self):
        store, pid = make_project(self.root, [('slow', ()), ('fast', ()), ('quick', ())], max_parallel=3)
        timeline = []
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': 'value = 42\n'}, 'sleep': 0.6 if actor.key == 'slow' else 0.01}
        base_master = ScriptedActor.master_script
        def master(actor, number, message, schema):
            if 'Review module' in message:
                timeline.append(('review', message.split('Review module ', 1)[1].split(':', 1)[0], time.monotonic()))
            return base_master(actor, number, message, schema)
        ScriptedActor.owner_script, ScriptedActor.master_script = staticmethod(owner), staticmethod(master)
        started = time.monotonic()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        reviews = {key: moment - started for _, key, moment in timeline}
        self.assertLess(reviews['fast'], 0.5)
        self.assertLess(reviews['quick'], 0.5)
        self.assertGreater(reviews['slow'], 0.5)

    async def test_blocked_module_stops_only_its_dependents(self):
        # Under the batch scheduler a blocked module halted the whole project.
        store, pid = make_project(self.root, [('bad', ()), ('good', ()), ('child', ('bad',))],
                                  max_parallel=1, max_revision_rounds=0)
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': ('value = 0\n' if actor.key == 'bad' else 'value = 42\n')}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'blocked_or_reconciliation_required')
        states = {row['key']: row['state'] for row in store.modules(pid)}
        self.assertEqual(states, {'bad': 'blocked', 'good': 'accepted', 'child': 'ready'})
        self.assertNotIn('child', ScriptedActor.starts)
        self.assertEqual(store.status(pid)['reserved_tokens'], 0)

    async def test_isolated_job_failure_is_journaled_and_other_modules_finish(self):
        store, pid = make_project(self.root, [('broken', ()), ('fine', ())])
        def owner(actor, number, message):
            if actor.key == 'broken':
                return {'raise': RuntimeError('synthetic controller-side defect')}
            return {'write': {'fine.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'blocked_or_reconciliation_required')
        states = {row['key']: row['state'] for row in store.modules(pid)}
        self.assertEqual(states['fine'], 'accepted')
        self.assertEqual(states['broken'], 'working')
        failures = events(store, pid, 'scheduler-job-failed')
        self.assertEqual([payload['module'] for _, payload in failures], ['broken'])
        broken_ops = [op for op in store.status(pid)['operations']
                      if op['actor_id'].endswith(':broken') and op['kind'] == 'work']
        self.assertEqual([op['status'] for op in broken_ops], ['unknown'])

    async def test_definitive_start_rejection_backs_off_and_retries_the_same_owner(self):
        store, pid = make_project(self.root, [('one', ())], policy={'transient_backoff_seconds': 0})
        def owner(actor, number, message):
            if number == 1:
                return {'reject_start': True}
            return {'write': {'one.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        work = [op for op in store.status(pid)['operations'] if op['kind'] == 'work']
        self.assertEqual([(op['status'], op['outcome']) for op in work], [('failed', 'dispatch_rejected'), ('done', None)])
        self.assertEqual(work[0]['usage'], 0)
        self.assertEqual(len({existing for key, existing in ScriptedActor.opens if key == 'one' and existing}), 1)

    async def test_repeated_start_rejection_blocks_after_the_bound(self):
        store, pid = make_project(self.root, [('one', ())],
                                  policy={'transient_backoff_seconds': 0, 'max_transient_retries': 1})
        ScriptedActor.owner_script = staticmethod(lambda actor, number, message: {'reject_start': True})
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'blocked_or_reconciliation_required')
        row = store.modules(pid)[0]
        self.assertEqual((row['state'], row['failure_kind'], row['transient_failures']), ('blocked', 'native_rejected', 2))

    async def test_critical_path_module_is_dispatched_first(self):
        store, pid = make_project(self.root, [('leaf', ()), ('hub', ()), ('left', ('hub',)), ('right', ('hub',))],
                                  max_parallel=1)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        dispatched = [payload['actor'].rsplit(':', 1)[-1] for _, payload in events(store, pid, 'dispatch-intent')
                      if ':module:' in payload['actor']]
        self.assertEqual(dispatched[0], 'hub')
        self.assertEqual(set(dispatched), {'leaf', 'hub', 'left', 'right'})
