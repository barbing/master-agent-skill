"""Parallel dependents, the Master's plan and sealed probes, with scripted synthetic actors; not live-model proof.

Owners of contract-covered dependents start before their providers are accepted and seal provisional
work that the host rebases onto the accepted inputs. The Master plans obligations (and contracts for
uncovered edges) before owners start, and probes accepted modules with executable checks that the host
seals, confirms and re-runs on later versions and on the integrated project.
"""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from master_agent_runtime import InterfaceSpec, ModuleSpec, ProjectSpec, Store, ValidationStep
from master_agent_runtime.contracts import ConflictError, ContractError, OPERATOR_POLICY, RuntimePolicy
from master_agent_runtime.controller import (PLAN_OUTPUT, PROBE_OUTPUT, Controller, check_plan, check_probes,
                                             lenient_schema, plan_message, probe_outcome)

from runtime_fixtures import CHECK, ScriptedActor, events, fixture_validator, make_project

CONTRACT = {'parallel_dependents': 'contract', 'module_review': 'checks_only'}
DRAFT = {'module_review': 'checks_only', 'master_probes': True, 'probe_timing': 'draft', 'max_probe_rounds': 0}
STORE_API = InterfaceSpec('store-api', 'store', 'store.py defines value = 42 and api names its version', ('cli',))


def probe_code(text):
    """Synthetic probe semantics read by the validator: PASS, FAIL, FLAKY (fails first run), ERROR (exit 2),
    or REQUIRE:<word> (passes when a .py file of the probed view contains the word)."""
    return text


def validator(rules=None):
    """Module checks: every sealed .py file holds 'value = 42', plus optional rules(step, candidate, context).
    Probe steps run the sealed probe text with the synthetic semantics above."""
    runs = {}
    def run(steps, candidate, context, evidence, **kwargs):
        results = []
        for step in steps:
            if len(step.argv) > 1 and step.argv[1].startswith('{acceptance}/'):
                code = (Path(kwargs['private']) / step.argv[1][len('{acceptance}/'):]).read_text()
                text = '\n'.join(path.read_text() for path in Path(candidate).rglob('*.py'))
                count = runs[step.argv[1]] = runs.get(step.argv[1], 0) + 1
                if code.startswith('REQUIRE:'):
                    exit_code = 0 if code.split(':', 1)[1].strip() in text else 1
                else:
                    exit_code = {'FAIL': 1, 'ERROR': 2, 'CRASH': 1}.get(code, 1 if code == 'FLAKY' and count == 1 else 0)
                stdout = ('Traceback: SyntaxError' if code == 'CRASH' else 'EXPECTED: required\nOBSERVED: other') \
                    if exit_code == 1 else 'ok'
                results.append({'argv': list(step.argv), 'status': 'passed' if exit_code == 0 else 'failed',
                                'exit_code': exit_code, 'stdout': stdout})
                continue
            result = fixture_validator((step,), candidate, context, evidence)[0]
            if result['status'] == 'passed' and rules and not rules(step, Path(candidate), Path(context)):
                result = {**result, 'status': 'failed', 'exit_code': 1}
            results.append({**result, 'argv': list(step.argv)})
        return results
    return run


def two_modules(root, *, policy, interfaces=(), with_tests=False, budget=100000):
    root = Path(root)
    project = root / 'project'
    project.mkdir(exist_ok=True)
    specs = []
    for key, dependencies in (('store', ()), ('cli', ('store',))):
        (project / f'{key}.py').write_text('value = -1\n')
        paths = (f'{key}.py',) + ((f'test_{key}.py',) if with_tests else ())
        specs.append(ModuleSpec(key, f'Provide value 42 in {key}.py', paths, dependencies, token_reservation=100,
                                validation=CHECK, test_paths=(f'test_{key}.py',) if with_tests else (),
                                test_validation=CHECK if with_tests else ()))
    spec = ProjectSpec(str(project), 'Deliver every fixture module', tuple(specs), budget,
                       integration_validation=CHECK, interfaces=tuple(interfaces))
    store = Store(root / 'state' / 'runtime.sqlite')
    return store, store.create_project(spec, RuntimePolicy.from_dict(policy))


class ParallelCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ScriptedActor.reset()

    async def run_project(self, store, pid, run=None):
        with patch('master_agent_runtime.controller.validate', run or validator()):
            return await Controller(store, pid, actor_factory=ScriptedActor).run()

    def master(self, decide):
        prompts = []
        base = ScriptedActor.master_script
        def script(actor, number, message, schema):
            prompts.append((message, schema))
            result = decide(actor, number, message, schema)
            return result if result is not None else base(actor, number, message, schema)
        ScriptedActor.master_script = staticmethod(script)
        return prompts

    def owner_messages(self):
        messages = {}
        base = ScriptedActor.owner_script
        def script(actor, number, message):
            messages.setdefault(actor.key, []).append(message)
            return base(actor, number, message)
        ScriptedActor.owner_script = staticmethod(script)
        return messages

    def index(self, entry):
        return ScriptedActor.log.index(entry)


class ParallelDependentTests(ParallelCase):
    async def test_contract_covered_dependent_starts_before_its_provider_and_is_rebased(self):
        store, pid = two_modules(self.root, policy=CONTRACT, interfaces=(STORE_API,))
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': 'value = 42\n'}, **({'sleep': 0.3} if actor.key == 'store' else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertLess(self.index(('cli', 1, 'started')), self.index(('store', 1, 'finished')))
        self.assertIn('Built in parallel', messages['cli'][0])
        submitted = [p for kind, p in events(store, pid, 'module-submitted') if p['owner'].endswith(':cli')]
        self.assertTrue(submitted[0].get('provisional'))
        rebased = events(store, pid, 'candidate-rebased')
        self.assertEqual([p['module'] for _, p in rebased], ['cli'])
        cli = next(m for m in store.status(pid)['modules'] if m['key'] == 'cli')
        bound = store.candidate(cli['accepted_candidate'])['inputs']
        store_row = next(m for m in store.status(pid)['modules'] if m['key'] == 'store')
        self.assertEqual(bound, {'store': {'revision': store_row['revision'], 'candidate': store_row['accepted_candidate']}})

    async def test_without_a_contract_the_dependent_waits_for_its_provider(self):
        store, pid = two_modules(self.root, policy=CONTRACT)
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': 'value = 42\n'}, **({'sleep': 0.2} if actor.key == 'store' else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertLess(self.index(('store', 1, 'finished')), self.index(('cli', 1, 'started')))
        self.assertEqual(events(store, pid, 'candidate-rebased'), [])

    async def test_policy_off_keeps_strict_ordering_even_with_a_contract(self):
        store, pid = two_modules(self.root, policy={'module_review': 'checks_only'}, interfaces=(STORE_API,))
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': 'value = 42\n'}, **({'sleep': 0.2} if actor.key == 'store' else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertLess(self.index(('store', 1, 'finished')), self.index(('cli', 1, 'started')))

    async def test_a_failed_rebase_returns_the_real_failure_with_the_provider_code_in_view(self):
        store, pid = two_modules(self.root, policy=CONTRACT, interfaces=(STORE_API,))
        seen = []
        def owner(actor, number, message):
            if actor.key == 'store':
                return {'write': {'store.py': "value = 42\napi = 'v2'\n"}, 'sleep': 0.2}
            provider = (actor.workspace / 'store.py').read_text()
            seen.append(provider)
            return {'write': {'cli.py': 'value = 42\n' + ('# uses v2\n' if "api = 'v2'" in provider else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        def rules(step, candidate, context):
            # The consumer must use the provider's accepted API version.
            if (candidate / 'cli.py').exists() and (context / 'store.py').exists():
                return "api = 'v2'" not in (context / 'store.py').read_text() or 'uses v2' in (candidate / 'cli.py').read_text()
            return True
        result = await self.run_project(store, pid, validator(rules))
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(seen[0], 'value = -1\n')
        self.assertIn("api = 'v2'", seen[1])
        self.assertIn('Host fail-fast', messages['cli'][1])
        self.assertEqual(len(events(store, pid, 'candidate-rebased')), 1)

    async def test_an_invalidated_provider_keeps_its_contract_dependent_s_sealed_work(self):
        store, pid = two_modules(self.root, policy=CONTRACT, interfaces=(STORE_API,))
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': 'value = 42\n'}, **({'sleep': 0.2} if actor.key == 'store' else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        def decide(actor, number, message, schema):
            if 'Review the complete integrated project' in message and not any(
                    'integration-failed' == kind for kind, _ in events(store, pid)):
                return {'decision': 'request_changes', 'rationale': 'store must change', 'failure_kind': 'implementation',
                        'affected_modules': ['store'], 'advisories': [],
                        'blocking_issues': [{'obligation': 'integration', 'evidence': 'x', 'required_change': 'y'}]}
            return None
        self.master(decide)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        cli_turns = [entry for entry in ScriptedActor.log if entry[0] == 'cli' and entry[2] == 'started']
        store_turns = [entry for entry in ScriptedActor.log if entry[0] == 'store' and entry[2] == 'started']
        self.assertEqual(len(cli_turns), 1)
        self.assertEqual(len(store_turns), 2)
        invalidated = events(store, pid, 'module-inputs-invalidated')
        self.assertEqual(invalidated[0][1]['modules'], ['store'])
        self.assertEqual(invalidated[0][1]['awaiting_inputs'], ['cli'])
        self.assertEqual([p['module'] for _, p in events(store, pid, 'candidate-rebased')], ['cli', 'cli'])


class StoreTransitionTests(ParallelCase):
    def principal(self, store, pid):
        return store.bind(f'{pid}:master', 'm', 'm')

    def test_dispatch_refuses_an_unaccepted_dependency_unless_it_is_provisional(self):
        store, pid = two_modules(self.root, policy=CONTRACT, interfaces=(STORE_API,))
        master = self.principal(store, pid)
        store.bind(f'{pid}:module:cli', 'c', 'c')
        with self.assertRaises(ConflictError):
            store.dispatch(master, f'{pid}:module:cli', 'work')
        operation = store.dispatch(master, f'{pid}:module:cli', 'work', provisional=('store',))
        payload = json.loads(store.operation(operation)['payload'])
        self.assertEqual(payload['inputs'], {'store': {'revision': 1, 'candidate': None}})

    def test_rebase_requires_waiting_work_and_accepted_inputs(self):
        store, pid = two_modules(self.root, policy=CONTRACT, interfaces=(STORE_API,))
        master = self.principal(store, pid)
        with self.assertRaises(ConflictError):
            store.rebase(master, 'cli', 'new', inputs={'store': {'revision': 1, 'candidate': None}}, validation=[])


class PlanTests(ParallelCase):
    def plan(self, obligations, interfaces=()):
        return {'obligations': obligations, 'interfaces': list(interfaces), 'notes': ''}

    async def test_the_plan_reaches_owners_and_coverage_is_enforced_by_the_host(self):
        store, pid = two_modules(self.root, policy={**CONTRACT, 'planning': True}, interfaces=(STORE_API,),
                                 with_tests=True)
        plan = self.plan([{'id': 'O1', 'module': 'store', 'quote': 'Provide value 42', 'case': 'fresh file',
                           'expected': 'value is 42'},
                          {'id': 'O2', 'module': 'cli', 'quote': 'Provide value 42 in cli.py', 'case': 'cli file',
                           'expected': 'value is 42'}])
        prompts = self.master(lambda actor, number, message, schema:
                              plan if 'Plan the module owners' in message else None)
        def owner(actor, number, message):
            named = '# O1\n' if actor.key == 'store' and number == 1 else ''
            test = 'value = 42\n' + ('# O2\n' if actor.key == 'cli' else ('# O1\n' if number > 1 else named))
            return {'write': {f'{actor.key}.py': 'value = 42\n', f'test_{actor.key}.py': test}}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertTrue(any('Plan the module owners' in message for message, _ in prompts))
        self.assertIn('"id": "O1"', messages['store'][0])
        self.assertIn('test_O1_', messages['store'][0])
        self.assertNotIn('"id": "O1"', messages['cli'][0])
        recorded = events(store, pid, 'plan-recorded')
        self.assertEqual([item['id'] for item in recorded[0][1]['obligations']], ['O1', 'O2'])
        # Owners start only after the plan.
        first_owner = min(self.index(entry) for entry in ScriptedActor.log if entry[0] != 'master')
        plan_turn = next(i for i, entry in enumerate(ScriptedActor.log) if entry[0] == 'master')
        self.assertLess(plan_turn, first_owner)

    async def test_missing_obligation_ids_send_the_owner_back_without_a_master_turn(self):
        store, pid = two_modules(self.root, policy={'module_review': 'checks_only', 'planning': True}, with_tests=True)
        plan = self.plan([{'id': 'O1', 'module': 'store', 'quote': 'Provide value 42', 'case': 'c', 'expected': 'e'}])
        self.master(lambda actor, number, message, schema: plan if 'Plan the module owners' in message else None)
        def owner(actor, number, message):
            test = 'value = 42\n' + ('# test_O1_value\n' if number > 1 or actor.key == 'cli' else '')
            return {'write': {f'{actor.key}.py': 'value = 42\n', f'test_{actor.key}.py': test}}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertIn('Owner tests do not name these assigned obligations: O1', messages['store'][1])
        reviews = [p for kind, p in events(store, pid, 'master-review')]
        self.assertEqual([p['decision'] for p in reviews][:1], ['request_changes'])

    async def test_a_refused_plan_is_retried_once_and_then_the_run_proceeds_without_it(self):
        store, pid = two_modules(self.root, policy={'module_review': 'checks_only', 'planning': True})
        bad = self.plan([{'id': 'O1', 'module': 'store', 'quote': 'invent a new feature', 'case': 'c', 'expected': 'e'}])
        prompts = self.master(lambda actor, number, message, schema:
                              bad if 'Plan the module owners' in message else None)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        planning = [message for message, _ in prompts if 'Plan the module owners' in message]
        self.assertEqual(len(planning), 2)
        self.assertIn('refused by the host', planning[1])
        self.assertTrue(events(store, pid, 'plan-unavailable'))

    async def test_the_plan_drafts_a_contract_so_an_uncovered_dependent_runs_in_parallel(self):
        store, pid = two_modules(self.root, policy={**CONTRACT, 'planning': True})
        plan = self.plan([], interfaces=[{'name': 'store-api', 'owner': 'store', 'consumers': ['cli'],
                                         'contract': 'value = 42 in store.py'}])
        prompts = self.master(lambda actor, number, message, schema:
                              plan if 'Plan the module owners' in message else None)
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': 'value = 42\n'}, **({'sleep': 0.3} if actor.key == 'store' else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertIn('Task 2, interfaces', next(message for message, _ in prompts if 'Plan the module owners' in message))
        self.assertLess(self.index(('cli', 1, 'started')), self.index(('store', 1, 'finished')))
        self.assertIn('store-api', messages['cli'][0])

    def test_plan_checks_guard_scope_ids_and_edges(self):
        store, pid = two_modules(self.root, policy=CONTRACT)
        spec = store.project(pid)['contract']
        good = {'id': 'O1', 'module': 'cli', 'quote': 'value 42 in cli.py', 'case': 'c', 'expected': 'e'}
        obligations, interfaces, problems = check_plan(spec, self.plan([good]), admitted_interfaces=(),
                                                       uncovered_edges=[('store', 'cli')], max_obligations=5)
        self.assertEqual((len(obligations), problems), (1, []))
        for change, fragment in (({'quote': 'add caching'}, 'verbatim'), ({'id': 'X1'}, 'form O<number>'),
                                 ({'module': 'nope'}, 'unknown module'), ({'case': ' '}, 'needs a case')):
            _, _, problems = check_plan(spec, self.plan([{**good, **change}]), admitted_interfaces=(),
                                        uncovered_edges=[], max_obligations=5)
            self.assertTrue(any(fragment in problem for problem in problems), (change, problems))
        _, _, problems = check_plan(spec, self.plan([good, {**good}]), admitted_interfaces=(), uncovered_edges=[],
                                    max_obligations=5)
        self.assertTrue(any('unique' in problem for problem in problems))
        wrong_edge = [{'name': 'x', 'owner': 'cli', 'consumers': ['store'], 'contract': 'c'}]
        _, _, problems = check_plan(spec, self.plan([], wrong_edge), admitted_interfaces=(),
                                    uncovered_edges=[('store', 'cli')], max_obligations=5)
        self.assertTrue(any('uncovered dependency edges' in problem for problem in problems))
        message = plan_message(spec, context='/ctx', interfaces=(), uncovered_edges=[('store', 'cli')], max_obligations=5)
        self.assertIn('"provider": "store"', message)


class ProbeTests(ParallelCase):
    def probes(self, *items):
        return {'probes': [{'name': name, 'obligations': [], 'description': f'behaviour {name}',
                            'expected': f'expected {name}', 'code': code} for name, code in items], 'notes': ''}

    async def test_a_confirmed_probe_failure_goes_back_to_the_owner_without_the_code_and_is_rechecked(self):
        store, pid = make_project(self.root, [('one', ())], policy={'module_review': 'checks_only', 'master_probes': True})
        self.master(lambda actor, number, message, schema:
                    self.probes(('marker', 'REQUIRE: marker'), ('fine', 'PASS'), ('shaky', 'FLAKY'), ('broken', 'ERROR'))
                    if 'Write executable probes' in message else None)
        def owner(actor, number, message):
            return {'write': {'one.py': 'value = 42\n' + ('# marker\n' if number > 1 else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        recorded = events(store, pid, 'probes-recorded')
        self.assertEqual({item['name']: item['status'] for item in recorded[0][1]['probes']},
                         {'marker': 'failed', 'fine': 'passed', 'shaky': 'flaky', 'broken': 'error'})
        self.assertEqual(recorded[0][1]['probes'][0]['runs'], [1, 1, 1])
        correction = messages['one'][1]
        self.assertIn('behaviour marker', correction)
        self.assertNotIn('REQUIRE', correction)
        self.assertEqual([p['failed'] for _, p in events(store, pid, 'module-probes-failed')], [['marker']])
        one = next(m for m in store.status(pid)['modules'] if m['key'] == 'one')
        later = store.candidate(one['accepted_candidate'])['validation']
        self.assertEqual(sorted(check['probe'].split('/')[1] for check in later if check.get('kind') == 'probe'),
                         ['fine', 'marker'])
        integrated = result['validation']
        self.assertEqual(sorted(check['probe'].split('/')[1] for check in integrated if check.get('kind') == 'probe'),
                         ['fine', 'marker'])
        sealed = list((Path(store.path.parent) / 'projects' / pid / 'private' / 'probes').rglob('*.py'))
        self.assertEqual(len(sealed), 4)
        self.assertFalse(list((Path(store.path.parent) / 'projects' / pid / 'public').rglob('marker.py')))
        # One probe turn per module by default: the fix is re-checked by the sealed probes, not a new turn.
        self.assertEqual(len(recorded), 1)

    async def test_host_reviews_do_not_wait_behind_a_master_probe_turn(self):
        store, pid = two_modules(self.root, policy={**CONTRACT, 'master_probes': True}, interfaces=(STORE_API,))
        def decide(actor, number, message, schema):
            if 'Write executable probes' in message:
                return {**self.probes(('ok', 'PASS')), '__sleep__': 0.6 if 'module store' in message else 0}
            return None
        self.master(decide)
        def owner(actor, number, message):
            return {'write': {f'{actor.key}.py': 'value = 42\n'}, **({'sleep': 0.2} if actor.key == 'store' else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        kinds = [(kind, payload) for kind, payload in events(store, pid)
                 if kind in ('candidate-rebased', 'master-review', 'probes-recorded')]
        rebased = next(i for i, (kind, _) in enumerate(kinds) if kind == 'candidate-rebased')
        cli_review = next(i for i, (kind, _) in enumerate(kinds) if kind == 'master-review' and i > rebased)
        store_probes = next(i for i, (kind, p) in enumerate(kinds) if kind == 'probes-recorded' and p['module'] == 'store')
        self.assertLess(cli_review, store_probes)

    async def test_integration_waits_for_probe_turns_and_strict_dependents_wait_for_their_provider_s_probes(self):
        store, pid = two_modules(self.root, policy={'module_review': 'checks_only', 'master_probes': True})
        order = []
        def decide(actor, number, message, schema):
            if 'Write executable probes' in message:
                order.append(('probe', message.split('accepted work of module ')[1].split('.')[0]))
                return self.probes(('ok', 'PASS'))
            if 'Review the complete integrated project' in message:
                order.append(('integration', None))
            return None
        self.master(decide)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(order, [('probe', 'store'), ('probe', 'cli'), ('integration', None)])
        probe_store = next(i for i, entry in enumerate(ScriptedActor.log) if entry[0] == 'master')
        self.assertLess(probe_store, self.index(('cli', 1, 'started')))

    async def test_a_sealed_probe_failing_on_the_integrated_project_reopens_its_module(self):
        store, pid = two_modules(self.root, policy={**CONTRACT, 'master_probes': True}, interfaces=(STORE_API,))
        self.master(lambda actor, number, message, schema:
                    self.probes(('no-conflict', 'REQUIRE: value'))
                    if 'Write executable probes' in message and 'module store' in message else
                    (self.probes() if 'Write executable probes' in message else None))
        with patch('master_agent_runtime.controller.Controller.run_probe_checks', autospec=True) as runner:
            calls = []
            async def fake(self_, key, probes, view, destination, *, python=None):
                calls.append(str(view))
                failing = 'integration-' in str(view) and len([c for c in calls if 'integration-' in c]) == 1
                return [{'argv': ['python', 'p'], 'kind': 'probe', 'module': key, 'probe': item['label'],
                         'status': 'failed' if failing else 'passed', 'outcome': 'failed' if failing else 'passed',
                         'exit_code': 1 if failing else 0, 'runs': [1 if failing else 0],
                         'description': item.get('description', ''), 'expected': item.get('expected', '')}
                        for item in probes]
            runner.side_effect = fake
            result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        failed = events(store, pid, 'integration-failed')
        self.assertTrue(failed)
        invalidated = events(store, pid, 'module-inputs-invalidated')
        self.assertEqual(invalidated[0][1]['modules'], ['store'])
        self.assertIn('Sealed probes failed on the integrated project', invalidated[0][1]['reason'])

    async def test_drafted_probes_written_during_owner_work_gate_the_first_submission(self):
        store, pid = make_project(self.root, [('one', ())], policy=DRAFT)
        self.master(lambda actor, number, message, schema:
                    self.probes(('marker', 'REQUIRE: marker'), ('fine', 'PASS'))
                    if 'while its owner is still building it' in message else None)
        def owner(actor, number, message):
            return {'write': {'one.py': 'value = 42\n' + ('# marker\n' if number > 1 else '')},
                    **({'sleep': 0.3} if number == 1 else {})}
        ScriptedActor.owner_script = staticmethod(owner)
        messages = self.owner_messages()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        draft_turn = next(i for i, entry in enumerate(ScriptedActor.log) if entry[0] == 'master')
        self.assertLess(draft_turn, self.index(('one', 1, 'finished')))
        drafted = events(store, pid, 'probes-drafted')
        self.assertEqual([item['name'] for item in drafted[0][1]['probes']], ['marker', 'fine'])
        with closing(store.connect()) as con:
            first = json.loads(con.execute("SELECT validation FROM candidate WHERE project_id=? ORDER BY at LIMIT 1",
                                           (pid,)).fetchone()[0])
        self.assertEqual({c['probe']: c['status'] for c in first if c.get('kind') == 'probe'},
                         {'draft/marker': 'failed', 'draft/fine': 'passed'})
        self.assertIn('Host fail-fast', messages['one'][1])
        self.assertNotIn('REQUIRE', messages['one'][1])
        self.assertEqual(events(store, pid, 'module-inputs-invalidated'), [])
        self.assertFalse([p for _, p in events(store, pid, 'probes-recorded') if p.get('turn')])
        integrated = {c['probe'] for c in result['validation'] if c.get('kind') == 'probe'}
        self.assertEqual(integrated, {'draft/marker', 'draft/fine'})

    async def test_drafts_that_arrive_after_acceptance_are_checked_on_the_accepted_version(self):
        store, pid = make_project(self.root, [('one', ())], policy=DRAFT)
        self.master(lambda actor, number, message, schema:
                    {**self.probes(('marker', 'REQUIRE: marker')), '__sleep__': 0.4}
                    if 'while its owner is still building it' in message else None)
        def owner(actor, number, message):
            return {'write': {'one.py': 'value = 42\n' + ('# marker\n' if number > 1 else '')}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        checked = [p for _, p in events(store, pid, 'probes-recorded') if p.get('source') == 'draft']
        self.assertEqual([item['status'] for item in checked[0]['probes']], ['failed'])
        self.assertEqual([p['source'] for _, p in events(store, pid, 'module-probes-failed')], ['draft'])
        self.assertEqual(len([e for e in ScriptedActor.log if e[0] == 'one' and e[2] == 'started']), 2)

    async def test_a_failed_draft_turn_never_halts_the_module(self):
        store, pid = make_project(self.root, [('one', ()), ('two', ())], policy=DRAFT)
        self.master(lambda actor, number, message, schema:
                    {'probes': 'not a list', 'notes': ''} if 'module one now' in message else
                    (self.probes(('fine', 'PASS')) if 'while its owner is still building it' in message else None))
        original = Controller.draft_probes
        async def flaky(self_, key):
            if key == 'two':
                raise RuntimeError('synthetic draft crash')
            return await original(self_, key)
        with patch.object(Controller, 'draft_probes', flaky):
            result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual([p['module'] for _, p in events(store, pid, 'probes-draft-skipped')], ['one'])
        failed = [p for _, p in events(store, pid, 'scheduler-job-failed')]
        self.assertEqual([(p['lane'], p['module']) for p in failed], [('draft', 'two')])

    def test_probe_checks_keep_valid_probes_and_refuse_broken_ones_one_by_one(self):
        output = self.probes(('a', 'PASS'), ('a', 'PASS'), ('broken', 'def f(:\n  pass'), ('b', 'PASS'), ('c', 'PASS'))
        output['probes'][0]['obligations'] = ['O1', 'O9']
        kept, rejected = check_probes(output, max_probes=2, obligation_ids={'O1'})
        self.assertEqual([item['name'] for item in kept], ['a', 'b'])
        self.assertEqual(kept[0]['obligations'], ['O1'])
        reasons = {item['probe']: item['reason'] for item in rejected}
        self.assertIn('used twice', reasons['a'])
        self.assertIn('does not compile', reasons['broken'])
        self.assertIn('beyond the limit', reasons['c'])
        self.assertEqual(check_probes({'probes': 'x'}, max_probes=2, obligation_ids=set())[0], [])

    def test_probe_outcomes_require_the_contract_markers_for_a_violation(self):
        marked = {'status': 'failed', 'exit_code': 1}
        self.assertEqual(probe_outcome([{'status': 'passed', 'exit_code': 0}]), 'passed')
        self.assertEqual(probe_outcome([marked] * 3, ['EXPECTED: a\nOBSERVED: b'] * 3), 'failed')
        self.assertEqual(probe_outcome([marked] * 3, ['Traceback: IndentationError'] * 3), 'error')
        self.assertEqual(probe_outcome([marked] * 2 + [{'status': 'passed', 'exit_code': 0}],
                                       ['EXPECTED: a\nOBSERVED: b'] * 2 + ['ok']), 'failed')
        self.assertEqual(probe_outcome([marked] + [{'status': 'passed', 'exit_code': 0}] * 2,
                                       ['EXPECTED: a\nOBSERVED: b', 'ok', 'ok']), 'flaky')
        self.assertEqual(probe_outcome([{'status': 'failed', 'exit_code': 2}] * 3), 'error')
        self.assertEqual(probe_outcome([{'status': 'timeout', 'exit_code': None}] * 3), 'timeout')

    async def test_a_crashing_or_miscited_drafted_probe_never_blocks_the_owner(self):
        store, pid = make_project(self.root, [('one', ())], policy={**DRAFT, 'planning': True})
        plan = {'obligations': [{'id': 'O1', 'module': 'one', 'quote': 'Provide value 42', 'case': 'c',
                                 'expected': 'e'}], 'interfaces': [], 'notes': ''}
        def decide(actor, number, message, schema):
            if 'Plan the module owners' in message:
                return plan
            if 'while its owner is still building it' in message:
                output = self.probes(('crash', 'CRASH'), ('syntax', 'def f(:'), ('fine', 'PASS'))
                output['probes'][2]['obligations'] = ['O7']
                return output
            return None
        self.master(decide)
        def owner(actor, number, message):
            return {'write': {'one.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        drafted = events(store, pid, 'probes-drafted')[0][1]
        self.assertEqual([item['name'] for item in drafted['probes']], ['crash', 'fine'])
        self.assertEqual([item['probe'] for item in drafted['rejected']], ['syntax'])
        one = next(m for m in store.status(pid)['modules'] if m['key'] == 'one')
        outcomes = {c['probe']: c['outcome'] for c in store.candidate(one['accepted_candidate'])['validation']
                    if c.get('kind') == 'probe'}
        self.assertEqual(outcomes, {'draft/crash': 'error', 'draft/fine': 'passed'})
        self.assertEqual(len([e for e in ScriptedActor.log if e[0] == 'one' and e[2] == 'started']), 1)


class PolicyTests(unittest.TestCase):
    def test_operator_mode_turns_on_parallel_contracts_planning_and_probes(self):
        policy = RuntimePolicy.from_dict(OPERATOR_POLICY)
        self.assertEqual((policy.parallel_dependents, policy.planning, policy.master_probes, policy.module_review,
                          policy.probe_timing, policy.max_probe_rounds, policy.probe_author),
                         ('contract', True, True, 'checks_only', 'accepted', 1, 'verifier'))
        default = RuntimePolicy()
        self.assertEqual((default.parallel_dependents, default.planning, default.master_probes, default.max_probe_rounds,
                          default.probe_timing), ('off', False, False, 1, 'accepted'))
        with self.assertRaises(ContractError):
            RuntimePolicy.from_dict({'parallel_dependents': 'always'})
        with self.assertRaises(ContractError):
            RuntimePolicy.from_dict({'max_obligations': 0})

    def test_claude_schemas_keep_notes_optional(self):
        self.assertNotIn('notes', lenient_schema(PLAN_OUTPUT)['required'])
        self.assertNotIn('notes', lenient_schema(PROBE_OUTPUT)['required'])
        self.assertIn('probes', lenient_schema(PROBE_OUTPUT)['required'])


if __name__ == '__main__':
    unittest.main()
