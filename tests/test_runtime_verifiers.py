"""Independent verifiers and the Master's ruling, with scripted synthetic actors; not live-model proof.

Live runs showed that checks derived from the Master's plan (owner tests, drafted probes, a review primed
by the plan) shared the plan's blind spots. A verifier is a readonly actor per module with its own
session: it never sees the plan, probes the actual code of each accepted version, and works beside the
owners and the Master. The Master rules on every confirmed failure before it reaches the owner; upheld
probes gate every later version, and dismissed ones are retired and reported.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from master_agent_runtime import Store
from master_agent_runtime.contracts import AuthorityError, ContractError, OPERATOR_POLICY, RuntimePolicy
from master_agent_runtime.controller import Controller, check_triage, triage_routes

from runtime_fixtures import ScriptedActor, events, make_project
from test_runtime_parallel_probes import STORE_API, ParallelCase, two_modules

VERIFY = {'module_review': 'checks_only', 'master_probes': True, 'probe_author': 'verifier'}
LEGACY_ACTOR_CHECK = "CHECK(kind IN ('master','module'))"


def probes(*items):
    return {'probes': [{'name': name, 'obligations': [], 'description': f'behaviour {name}',
                        'expected': f'expected {name}', 'code': code} for name, code in items], 'notes': ''}


def is_verifier_turn(message):
    return message.startswith('Find where the actual implementation of module')


class VerifierTests(ParallelCase):
    async def test_each_accepted_module_gets_its_own_verifier_session_beside_the_owners_without_the_plan(self):
        store, pid = two_modules(self.root, policy={**VERIFY, 'parallel_dependents': 'contract', 'planning': True},
                                 interfaces=(STORE_API,))
        plan = {'obligations': [{'id': 'O1', 'module': 'store', 'quote': 'Provide value 42 in store.py',
                                 'case': 'planned-case-marker', 'expected': 'planned-expected-marker'}],
                'interfaces': [], 'notes': ''}
        verifier_messages = {}
        def decide(actor, number, message, schema):
            if 'Plan the module owners' in message:
                return plan
            if is_verifier_turn(message):
                verifier_messages[actor.key] = message
                return probes(('holds', 'PASS'))
            return None
        self.master(decide)
        def owner(actor, number, message):
            text = 'value = 42\n# test_O1_case\n' if actor.key == 'store' else 'value = 42\n'
            return {'write': {f'{actor.key}.py': text}, **({'sleep': 0.4} if actor.key == 'cli' else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        status = store.status(pid)
        verifiers = {a['id']: a for a in status['actors'] if a['kind'] == 'verifier'}
        self.assertEqual(sorted(verifiers), [f'{pid}:verifier:cli', f'{pid}:verifier:store'])
        self.assertTrue(all(a['module_key'] is None and a['thread_id'] for a in verifiers.values()))
        # The verifier never sees the plan: it judges the code against the requirements.
        self.assertNotIn('planned-case-marker', verifier_messages['verifier:store'])
        self.assertNotIn('O1', verifier_messages['verifier:store'])
        self.assertIn('store-api', verifier_messages['verifier:cli'])
        self.assertIn('independent verifier of module store', ScriptedActor.instructions['verifier:store'])
        # The store's verifier works while the cli owner is still building.
        store_verified = next(i for i, entry in enumerate(ScriptedActor.log) if entry[0] == 'verifier:store')
        self.assertLess(store_verified, self.index(('cli', 1, 'finished')))
        # Master-issued turns on the verifiers' own actors, accounted as verifier usage.
        verify_ops = [op for op in status['operations'] if op['kind'] == 'verify']
        self.assertEqual(sorted(op['actor_id'] for op in verify_ops), sorted(verifiers))
        self.assertTrue(all(json.loads(op['payload'])['issuer'] == f'{pid}:master' for op in verify_ops))
        self.assertGreater(status['usage_by_role']['verifiers'], 0)
        recorded = events(store, pid, 'probes-recorded')
        self.assertEqual(sorted((p['module'], p['source']) for _, p in recorded),
                         [('cli', 'verifier'), ('store', 'verifier')])
        workspaces = ScriptedActor.workspaces['verifier:store']
        self.assertEqual(workspaces[0].relative_to(Path(store.path.parent)).parts[-2:], ('verifiers', 'store'))
        self.assertEqual(events(store, pid, 'probes-triaged'), [])

    async def test_the_master_rules_on_failures_and_only_upheld_ones_reach_the_owner(self):
        store, pid = make_project(self.root, [('one', ())], policy=VERIFY)
        order = []
        def decide(actor, number, message, schema):
            if is_verifier_turn(message):
                order.append('verify')
                return probes(('real', 'REQUIRE: marker'), ('strict', 'FAIL'), ('fine', 'PASS'))
            if 'rule on each failure' in message:
                order.append('ruling')
                self.assertIn('"code": "FAIL"', message)
                return {'findings': [{'probe': 'real', 'verdict': 'uphold', 'reason': 'the objective demands it'},
                                     {'probe': 'strict', 'verdict': 'dismiss',
                                      'reason': 'no requirement demands this behaviour'}], 'notes': ''}
            if 'Review the complete integrated project' in message:
                order.append('integration')
            return None
        self.master(decide)
        def owner(actor, number, message):
            order.append(f'owner-{number}')
            return {'write': {'one.py': 'value = 42\n' + ('# marker\n' if number > 1 else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(order, ['owner-1', 'verify', 'ruling', 'owner-2', 'integration'])
        recorded = events(store, pid, 'probes-recorded')
        self.assertEqual(recorded[0][1]['triage'], 'pending')
        ruling = events(store, pid, 'probes-triaged')[0][1]
        self.assertEqual((ruling['upheld'], ruling['dismissed']),
                         (['real'], {'strict': 'no requirement demands this behaviour'}))
        correction = messages['one'][1]
        self.assertIn('the Master upheld', correction)
        self.assertIn('every input and state of the same kind', correction)
        self.assertIn('behaviour real', correction)
        self.assertNotIn('behaviour strict', correction)
        self.assertNotIn('REQUIRE', correction)
        one = next(m for m in store.status(pid)['modules'] if m['key'] == 'one')
        later = store.candidate(one['accepted_candidate'])['validation']
        self.assertEqual(sorted(c['probe'].split('/')[1] for c in later if c.get('kind') == 'probe'), ['fine', 'real'])
        self.assertEqual(sorted(c['probe'].split('/')[1] for c in result['validation'] if c.get('kind') == 'probe'),
                         ['fine', 'real'])
        report = Path(result['report']).read_text()
        self.assertIn('| one | strict | dismissed by the Master | no requirement demands this behaviour |', report)
        self.assertIn('independent verifier per module', report)
        # One verifier turn per module by default; the fix is re-checked by the sealed probes.
        self.assertEqual(len(recorded), 1)

    async def test_without_a_usable_ruling_every_verifier_failure_stands(self):
        store, pid = make_project(self.root, [('one', ())], policy=VERIFY)
        def decide(actor, number, message, schema):
            if is_verifier_turn(message):
                return probes(('real', 'REQUIRE: marker'))
            if 'rule on each failure' in message:
                return {'findings': [{'probe': 'real', 'verdict': 'dismiss', 'reason': ' '}], 'notes': ''}
            return None
        self.master(decide)
        def owner(actor, number, message):
            return {'write': {'one.py': 'value = 42\n' + ('# marker\n' if number > 1 else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(events(store, pid, 'probes-triaged')[0][1]['upheld'], ['real'])
        self.assertEqual([p['source'] for _, p in events(store, pid, 'module-probes-failed')], ['verifier'])
        self.assertEqual(len([e for e in ScriptedActor.log if e[0] == 'one' and e[2] == 'started']), 2)

    async def test_an_upheld_failure_decides_on_the_version_accepted_now_when_the_module_moved_on(self):
        store, pid = two_modules(self.root, policy={**VERIFY, 'parallel_dependents': 'contract'}, interfaces=(STORE_API,))
        def decide(actor, number, message, schema):
            if is_verifier_turn(message) and 'module store' in message:
                return {**probes(('store-fix', 'REQUIRE: storemark')), '__sleep__': 0.5}
            if is_verifier_turn(message):
                return {**probes(('cli-fix', 'REQUIRE: climark')), '__sleep__': 1.5}
            if 'rule on each failure' in message:
                name = 'store-fix' if 'module store' in message else 'cli-fix'
                return {'findings': [{'probe': name, 'verdict': 'uphold', 'reason': 'required'}], 'notes': ''}
            return None
        self.master(decide)
        def owner(actor, number, message):
            mark = {'store': 'storemark', 'cli': 'climark'}[actor.key]
            return {'write': {f'{actor.key}.py': 'value = 42\n' + (f'# {mark}\n' if number > 1 else '')},
                    **({'sleep': 0.1} if actor.key == 'cli' and number == 1 else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        ruled = {p['module']: p['candidate'] for _, p in events(store, pid, 'probes-triaged')}
        returned = {p['module']: p['candidate'] for _, p in events(store, pid, 'module-probes-failed')}
        # The cli verifier probed a version the store's correction then rebased; the upheld probe went back
        # to the owner against the version accepted at the ruling.
        self.assertNotEqual(returned['cli'], ruled['cli'])
        self.assertEqual(store.candidate(returned['cli'])['module_key'], 'cli')
        self.assertEqual(len([e for e in ScriptedActor.log if e[0] == 'cli' and e[2] == 'started']), 2)
        self.assertEqual(events(store, pid, 'integration-failed'), [])

    async def test_a_failed_verifier_job_never_halts_the_project(self):
        store, pid = make_project(self.root, [('one', ())], policy=VERIFY)
        self.master(lambda actor, number, message, schema:
                    probes(('holds', 'PASS')) if is_verifier_turn(message) else None)
        original = Controller.run_probe_checks
        async def crash(self_, key, probes_, view, destination, **kwargs):
            if 'probe-results' in str(destination):  # the verifier job's own run, after its turn settled
                raise RuntimeError('synthetic verifier job crash')
            return await original(self_, key, probes_, view, destination, **kwargs)
        with patch.object(Controller, 'run_probe_checks', crash):
            result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        failed = [(p['lane'], p['module']) for _, p in events(store, pid, 'scheduler-job-failed')]
        self.assertEqual(failed, [('verify', 'one')])

    def test_the_ruling_keeps_a_failure_unless_dismissed_by_name_with_a_reason(self):
        names = ['a', 'b', 'c']
        output = {'findings': [{'probe': 'a', 'verdict': 'dismiss', 'reason': 'not required'},
                               {'probe': 'b', 'verdict': 'dismiss', 'reason': ''},
                               {'probe': 'z', 'verdict': 'dismiss', 'reason': 'unknown probe'}], 'notes': ''}
        self.assertEqual(check_triage(output, names), (['b', 'c'], {'a': 'not required'}))
        self.assertEqual(check_triage(None, names), (names, {}))
        self.assertEqual(check_triage({'findings': 'x'}, names), (names, {}))


class RoutingTests(ParallelCase):
    """Changes that belong to another module reach that module's owner (found live: a cli owner was told the
    store must change, the store owner never heard, and the cli blocked on a probe it could not satisfy)."""

    def answer_with_change(self, change):
        def decide(actor, number, message, schema):
            if 'asks:' in message and 'Question:' in message:
                return {'action': 'answer', 'answer': 'Yes; code against it', 'rationale': 'coordination',
                        'question_for_user': None, 'module_changes': [{'module': 'store', 'change': change}]}
            return None
        return decide

    async def test_a_decision_reopens_the_accepted_provider_with_the_master_s_change(self):
        store, pid = two_modules(self.root, policy={'parallel_dependents': 'contract', 'module_review': 'checks_only'},
                                 interfaces=(STORE_API,))
        self.master(self.answer_with_change('Add storemark to store.py'))
        def owner(actor, number, message):
            if actor.key == 'cli' and number == 1:
                return {'sleep': 0.3, 'status': 'needs_decision', 'question': 'Should the store add storemark?',
                        'options': ['yes', 'no'], 'recommendation': 'yes', 'write': {'cli.py': 'value = 42\n'}}
            return {'write': {f'{actor.key}.py': 'value = 42\n' + ('# storemark\n' if actor.key == 'store' and number > 1 else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        routed = events(store, pid, 'module-change-routed')
        self.assertEqual([(p['module'], p['source']['decision']) for _, p in routed], [('store', 'cli')])
        self.assertEqual([(p['module'], p['how']) for _, p in events(store, pid, 'module-change-applied')], [('store', 'reopened')])
        self.assertIn('Add storemark to store.py', messages['store'][1])
        self.assertIn('after the owner of module cli asked', messages['store'][1])
        accepted = store.candidate(next(m for m in store.status(pid)['modules'] if m['key'] == 'store')['accepted_candidate'])
        self.assertIn('storemark', (Path(accepted['snapshot']) / 'store.py').read_text())
        report = Path(result['report']).read_text() if result.get('report') else ''
        self.assertIn('Changes the Master routed between modules', report)

    async def test_a_change_for_a_provider_at_work_waits_until_its_turn_ends(self):
        store, pid = two_modules(self.root, policy={'parallel_dependents': 'contract', 'module_review': 'checks_only'},
                                 interfaces=(STORE_API,))
        self.master(self.answer_with_change('Add storemark to store.py'))
        def owner(actor, number, message):
            if actor.key == 'cli' and number == 1:
                return {'status': 'needs_decision', 'question': 'Should the store add storemark?', 'options': [],
                        'recommendation': 'yes', 'write': {'cli.py': 'value = 42\n'}}
            if actor.key == 'store' and number == 1:
                return {'sleep': 0.6, 'write': {'store.py': 'value = 42\n'}}
            return {'write': {f'{actor.key}.py': 'value = 42\n' + ('# storemark\n' if actor.key == 'store' else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        kinds = [kind for kind, _ in events(store, pid)]
        self.assertLess(kinds.index('module-change-routed'), kinds.index('module-change-applied'))
        first_store_submission = next(i for i, (kind, p) in enumerate(events(store, pid))
                                      if kind == 'module-submitted' and p['owner'].endswith(':store'))
        self.assertLess(first_store_submission, kinds.index('module-change-applied'))
        self.assertNotIn('Add storemark', messages['store'][0])
        self.assertIn('Add storemark', messages['store'][1])

    async def test_a_provider_defect_found_by_a_consumer_s_verifier_goes_to_the_provider(self):
        store, pid = two_modules(self.root, policy={**VERIFY, 'parallel_dependents': 'contract'}, interfaces=(STORE_API,))
        def decide(actor, number, message, schema):
            if is_verifier_turn(message) and 'module cli' in message:
                return probes(('needs-store', 'REQUIRE: storemark'), ('fine', 'PASS'))
            if is_verifier_turn(message):
                return probes(('holds', 'PASS'))
            if 'rule on each failure' in message:
                self.assertIn('(store)', message)
                return {'findings': [{'probe': 'needs-store', 'verdict': 'uphold', 'reason': 'store defect',
                                      'module': 'store'}], 'notes': ''}
            return None
        self.master(decide)
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': 'value = 42\n' + ('# storemark\n' if actor.key == 'store' and number > 1 else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(events(store, pid, 'probes-triaged')[0][1]['routed'], {'needs-store': 'store'})
        self.assertIn('behaviour needs-store', messages['store'][1])
        self.assertIn('independent verifier of module cli', messages['store'][1])
        self.assertEqual(len([e for e in ScriptedActor.log if e[0] == 'cli' and e[2] == 'started']), 1)
        self.assertEqual(events(store, pid, 'module-probes-failed'), [])
        cli = store.candidate(next(m for m in store.status(pid)['modules'] if m['key'] == 'cli')['accepted_candidate'])
        self.assertNotIn('needs-store', ' '.join(c.get('probe', '') for c in cli['validation'] if c.get('kind') == 'probe'))
        integrated = {c['probe'].split('/')[1]: (c['status'], c.get('responsible')) for c in result['validation']
                      if c.get('kind') == 'probe'}
        self.assertEqual(integrated['needs-store'], ('passed', 'store'))

    async def test_a_routed_probe_failing_at_integration_reopens_the_provider_not_the_consumer(self):
        store, pid = two_modules(self.root, policy={**VERIFY, 'parallel_dependents': 'contract'}, interfaces=(STORE_API,))
        def decide(actor, number, message, schema):
            if is_verifier_turn(message) and 'module cli' in message:
                return probes(('needs-store', 'REQUIRE: storemark'))
            if is_verifier_turn(message):
                return probes(('holds', 'PASS'))
            if 'rule on each failure' in message:
                return {'findings': [{'probe': 'needs-store', 'verdict': 'uphold', 'reason': 'store defect',
                                      'module': 'store'}], 'notes': ''}
            return None
        self.master(decide)
        def owner(actor, number, message):
            # The store owner ignores the routed correction once; integration names it again.
            return {'write': {f'{actor.key}.py': 'value = 42\n' + ('# storemark\n' if actor.key == 'store' and number > 2 else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        failed = events(store, pid, 'integration-failed')
        self.assertEqual(len(failed), 1)
        invalidated = [p for _, p in events(store, pid, 'module-inputs-invalidated')]
        self.assertEqual(invalidated[-1]['modules'], ['store'])
        self.assertIn('Sealed probes failed on the integrated project', invalidated[-1]['reason'])
        self.assertEqual(len([e for e in ScriptedActor.log if e[0] == 'cli' and e[2] == 'started']), 1)

    def test_only_upheld_failures_named_for_an_allowed_provider_are_routed(self):
        output = {'findings': [{'probe': 'a', 'verdict': 'uphold', 'reason': 'r', 'module': 'store'},
                               {'probe': 'b', 'verdict': 'uphold', 'reason': 'r', 'module': 'cli'},
                               {'probe': 'c', 'verdict': 'uphold', 'reason': 'r', 'module': 'elsewhere'},
                               {'probe': 'd', 'verdict': 'dismiss', 'reason': 'r', 'module': 'store'}], 'notes': ''}
        self.assertEqual(triage_routes(output, ['a', 'b', 'c'], allowed={'store'}, default='cli'), {'a': 'store'})
        self.assertEqual(triage_routes(None, ['a'], allowed={'store'}, default='cli'), {})


class VerifierStoreTests(ParallelCase):
    def bound_master(self, store, pid):
        return store.bind(f'{pid}:master', 'thread-master', 'thread-master')

    def test_a_verifier_is_a_readonly_actor_assigned_by_the_master(self):
        store, pid = make_project(self.root, [('one', ())], policy=VERIFY)
        master = self.bound_master(store, pid)
        actor_id = store.register_verifier(master, 'one')
        self.assertEqual(store.register_verifier(master, 'one'), actor_id)
        record = store.actor(actor_id)
        self.assertEqual((record['kind'], record['module_key']), ('verifier', None))
        self.assertEqual(len(events(store, pid, 'verifier-registered')), 1)
        with self.assertRaises(ContractError):
            store.register_verifier(master, 'missing')
        owner = store.bind(f'{pid}:module:one', 'thread-owner', 'thread-owner')
        with self.assertRaises(AuthorityError):
            store.register_verifier(owner, 'one')
        self.assertEqual(store.status(pid)['usage_by_role'], {'master': 0, 'owners': 0, 'verifiers': 0})

    def test_a_store_from_before_verifiers_is_widened_once_keeping_every_row_and_reference(self):
        store, pid = make_project(self.root, [('one', ()), ('two', ('one',))], policy=VERIFY)
        master = self.bound_master(store, pid)
        operation = store.dispatch(master, f'{pid}:module:one', 'work')
        # Recreate the pre-verifier actor table, as stores created before this change have it.
        with closing(sqlite3.connect(store.path, isolation_level=None)) as con:
            con.execute('PRAGMA foreign_keys=OFF')
            sql = con.execute("SELECT sql FROM sqlite_master WHERE name='actor'").fetchone()[0]
            con.execute('BEGIN')
            con.execute(sql.replace("CHECK(kind IN ('master','module','verifier'))", LEGACY_ACTOR_CHECK)
                        .replace('CREATE TABLE actor (', 'CREATE TABLE actor_old (', 1))
            con.execute('INSERT INTO actor_old SELECT * FROM actor')
            con.execute('DROP TABLE actor')
            con.execute('ALTER TABLE actor_old RENAME TO actor')
            con.execute('COMMIT')
        def snapshot():
            with closing(sqlite3.connect(store.path)) as con:
                return {table: con.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                        for table in ('actor', 'module', 'operation', 'project')}
        before = snapshot()
        reopened = Store(store.path)
        with closing(sqlite3.connect(store.path)) as con:
            self.assertIn(LEGACY_ACTOR_CHECK, con.execute("SELECT sql FROM sqlite_master WHERE name='actor'").fetchone()[0])
        actor_id = reopened.register_verifier(master, 'two')
        after = snapshot()
        self.assertEqual(after['module'], before['module'])
        self.assertEqual(after['operation'], before['operation'])
        self.assertEqual(after['actor'][:len(before['actor'])], before['actor'])
        self.assertEqual(len(after['actor']), len(before['actor']) + 1)
        with closing(sqlite3.connect(store.path)) as con:
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertIn("'verifier'", con.execute("SELECT sql FROM sqlite_master WHERE name='actor'").fetchone()[0])
        self.assertEqual(len(events(reopened, pid, 'store-actor-kinds-widened')), 1)
        reopened.register_verifier(master, 'one')
        self.assertEqual(len(events(reopened, pid, 'store-actor-kinds-widened')), 1)
        self.assertEqual(reopened.actor(actor_id)['kind'], 'verifier')
        self.assertEqual(reopened.operation(operation)['actor_id'], f'{pid}:module:one')


class VerifierPolicyTests(ParallelCase):
    def test_operator_mode_uses_independent_verifiers_on_accepted_versions(self):
        policy = RuntimePolicy.from_dict(OPERATOR_POLICY)
        self.assertEqual((policy.master_probes, policy.probe_author, policy.probe_timing, policy.max_probe_rounds),
                         (True, 'verifier', 'accepted', 1))
        self.assertEqual(RuntimePolicy().probe_author, 'master')
        with self.assertRaises(ContractError):
            RuntimePolicy.from_dict({'probe_author': 'verifier', 'probe_timing': 'draft'})
        with self.assertRaises(ContractError):
            RuntimePolicy.from_dict({'probe_author': 'tester'})


if __name__ == '__main__':
    import unittest
    unittest.main()
