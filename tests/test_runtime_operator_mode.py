"""Operator mode with scripted synthetic actors; not live-model proof.

Owner questions answered by the Master or escalated to the user, the user as
final checker, host review tiers and the Master budget, preflight, interface
contracts and the user's private acceptance checks.
"""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master_agent_runtime import AcceptanceCheck, InterfaceSpec, ModuleSpec, ProjectSpec, Store, ValidationStep
from master_agent_runtime.admission import load_handoff_with_policy
from master_agent_runtime.contracts import (AuthorityError, ConflictError, ContractError, OPERATOR_POLICY,
                                            RuntimePolicy)
from master_agent_runtime.controller import (DECISION_OUTPUT, MODULE_OUTPUT, STRUCTURED_REVIEW_OUTPUT, Controller,
                                             lenient_schema)
from master_agent_runtime.health import inspect_project_state
from master_agent_runtime.recovery import reconcile

from runtime_fixtures import CHECK, ScriptedActor, events, fixture_validator, make_project

ROOT = Path(__file__).resolve().parents[1]


def asks(question='Which value should the module hold?', options=('42', '7'), recommendation='42'):
    return {'status': 'needs_decision', 'question': question, 'options': list(options),
            'recommendation': recommendation}


def is_decision(schema):
    return 'action' in (schema or {}).get('required', [])


class OperatorModeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ScriptedActor.reset()

    async def run_project(self, store, pid, validator=fixture_validator):
        with patch('master_agent_runtime.controller.validate', validator):
            return await Controller(store, pid, actor_factory=ScriptedActor).run()

    def record_master(self, decide=None):
        """Record every Master prompt; decide may override the default scripted answer."""
        prompts = []
        base = ScriptedActor.master_script
        def master(actor, number, message, schema):
            prompts.append((message, schema))
            result = decide(actor, number, message, schema) if decide else None
            return result if result is not None else base(actor, number, message, schema)
        ScriptedActor.master_script = staticmethod(master)
        return prompts


class DecisionTests(OperatorModeCase):
    async def test_master_answers_and_the_owner_resumes_with_its_files(self):
        store, pid = make_project(self.root, [('one', ())])
        seen = []
        def owner(actor, number, message):
            if number == 1:
                return {'write': {'one.py': 'value = 1  # draft\n'}, **asks()}
            seen.append(((actor.workspace / 'one.py').read_text(), message))
            return {'write': {'one.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        prompts = self.record_master()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        draft, message = seen[0]
        self.assertEqual(draft, 'value = 1  # draft\n')
        self.assertIn('Use the first option', message)
        questions = [message for message, schema in prompts if is_decision(schema)]
        self.assertEqual(len(questions), 1)
        self.assertIn('Which value should the module hold?', questions[0])
        self.assertEqual([kind for kind, _ in events(store, pid) if kind.startswith('decision-')],
                         ['decision-requested', 'decision-answered'])
        row = store.modules(pid)[0]
        self.assertEqual((row['decisions'], row['pending_question']), (1, None))
        self.assertEqual(len({existing for key, existing in ScriptedActor.opens if key == 'one' and existing}), 1)
        work = [op['outcome'] for op in store.status(pid)['operations'] if op['kind'] == 'work']
        self.assertEqual(work, ['needs_decision', None])

    async def test_an_escalated_question_waits_for_the_user_while_other_modules_finish(self):
        store, pid = make_project(self.root, [('one', ()), ('two', ())])
        def owner(actor, number, message):
            if actor.key == 'one' and number == 1:
                return asks('Metric or imperial units?', ('metric', 'imperial'), None)
            return {'write': {f'{actor.key}.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        def escalate(actor, number, message, schema):
            if is_decision(schema):
                return {'action': 'escalate', 'answer': '', 'rationale': 'A product choice the brief leaves open',
                        'question_for_user': 'Should reports use metric or imperial units?'}
        self.record_master(escalate)
        first = await self.run_project(store, pid)
        self.assertEqual(first['state'], 'awaiting_user')
        self.assertEqual([item['module'] for item in first['questions']], ['one'])
        self.assertEqual(first['questions'][0]['question_for_user'], 'Should reports use metric or imperial units?')
        self.assertEqual({row['key']: row['state'] for row in store.modules(pid)},
                         {'one': 'awaiting_user', 'two': 'accepted'})
        self.assertIn('answer ' + pid, inspect_project_state(store, pid)['next_action'])
        with self.assertRaises(AuthorityError):
            store.operator_answer(pid, 'one', answer='Metric', authorization=' ')
        store.operator_answer(pid, 'one', answer='Metric', authorization='user test answer')
        self.assertEqual(store.modules(pid)[0]['state'], 'continuing')
        seen = []
        def resumed(actor, number, message):
            seen.append(message)
            return {'write': {'one.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(resumed)
        second = await self.run_project(store, pid)
        self.assertEqual(second['state'], 'accepted')
        self.assertIn('Metric', seen[0])
        self.assertEqual(events(store, pid, 'decision-answered')[0][1]['source'], 'user')

    async def test_questions_past_the_limit_go_straight_to_the_user(self):
        store, pid = make_project(self.root, [('one', ())], policy={'max_decisions': 1})
        ScriptedActor.owner_script = staticmethod(lambda actor, number, message: asks(f'Question {number}?'))
        prompts = self.record_master()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'awaiting_user')
        self.assertEqual(len([message for message, schema in prompts if is_decision(schema)]), 1)
        row = store.modules(pid)[0]
        self.assertEqual((row['state'], row['decisions'], row['failure_kind']), ('awaiting_user', 2, 'decision_escalated'))
        self.assertEqual(json.loads(row['pending_question'])['escalated_by'], 'host')

    async def test_an_unanswerable_question_goes_to_the_user_after_the_retry_bound(self):
        store, pid = make_project(self.root, [('one', ())],
                                  policy={'max_transient_retries': 0, 'transient_backoff_seconds': 0})
        ScriptedActor.owner_script = staticmethod(lambda actor, number, message: asks())
        self.record_master(lambda actor, number, message, schema:
                           {'action': 'answer', 'answer': ' ', 'rationale': 'empty'} if is_decision(schema) else None)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'awaiting_user')
        row = store.modules(pid)[0]
        self.assertEqual((row['state'], row['failure_kind']), ('awaiting_user', 'decision_unavailable'))

    async def test_a_question_without_text_is_not_a_valid_receipt(self):
        store, pid = make_project(self.root, [('one', ())])
        def owner(actor, number, message):
            if number == 1:
                return {'write': {'one.py': 'value = 42\n'}, 'status': 'needs_decision', 'question': '  '}
            return {'write': {'one.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual([payload['reason'] for _, payload in events(store, pid, 'native-turn-stopped')],
                         ['receipt_missing'])
        self.assertEqual(events(store, pid, 'decision-requested'), [])

    async def test_blocked_owner_files_survive_an_operator_rearm(self):
        store, pid = make_project(self.root, [('one', ())])
        seen = []
        def owner(actor, number, message):
            if number == 1:
                return {'write': {'one.py': 'value = 1  # partial\n'}, 'status': 'blocked'}
            seen.append((actor.workspace / 'one.py').read_text())
            return {'write': {'one.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        first = await self.run_project(store, pid)
        self.assertEqual(first['state'], 'blocked_or_reconciliation_required')
        row = store.modules(pid)[0]
        self.assertEqual(row['state'], 'blocked')
        self.assertIsNotNone(row['checkpoint'])
        self.assertEqual(store.operator_rearm_module(pid, 'one', authorization='test rearm'), 'continuing')
        second = await self.run_project(store, pid)
        self.assertEqual(second['state'], 'accepted')
        self.assertEqual(seen, ['value = 1  # partial\n'])


class FinalAcceptanceTests(OperatorModeCase):
    async def test_the_user_gate_waits_with_a_report_until_the_user_accepts(self):
        store, pid = make_project(self.root, [('one', ())], policy={'final_acceptance': 'user'})
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'awaiting_acceptance')
        report = Path(result['report'])
        text = report.read_text()
        self.assertIn('Waiting for your acceptance', text)
        self.assertIn(f'--state {shlex.quote(str(store.path.parent))} accept {pid}', text)
        self.assertIn('Integration check `python frozen-oracle.py`: passed', text)
        self.assertIn('`one.py`', text)
        self.assertNotIn('public', report.parts)
        status = store.status(pid)
        self.assertEqual((status['state'], status['integration']['kind'], status['integration']['report']),
                         ('awaiting_acceptance', 'integration-passed', str(report)))
        self.assertIn('accept ' + pid, inspect_project_state(store, pid)['next_action'])
        starts = dict(ScriptedActor.starts)
        again = await self.run_project(store, pid)
        self.assertEqual(again['state'], 'awaiting_acceptance')
        self.assertEqual(ScriptedActor.starts, starts)
        accepted = store.operator_accept(pid, authorization='inspected the integrated files')
        self.assertEqual(accepted['path'], result['path'])
        self.assertEqual(store.status(pid)['state'], 'accepted')
        final = events(store, pid, 'integration-accepted')[-1][1]
        self.assertEqual((final['accepted_by'], final['report']), ('user', str(report)))
        resumed = await self.run_project(store, pid)
        self.assertTrue(resumed['resumed_completed_project'])
        with self.assertRaises(ConflictError):
            store.operator_accept(pid, authorization='again')

    async def test_rejecting_a_named_module_returns_the_feedback_to_it_and_its_dependents(self):
        store, pid = make_project(self.root, [('a', ()), ('b', ()), ('c', ('a',))],
                                  policy={'final_acceptance': 'user'}, max_parallel=3)
        self.assertEqual((await self.run_project(store, pid))['state'], 'awaiting_acceptance')
        before = dict(ScriptedActor.starts)
        affected = store.operator_reject(pid, feedback='a must also log its value', modules=['a'],
                                         authorization='user review')
        self.assertEqual(affected, ['a', 'c'])
        self.assertEqual(store.status(pid)['state'], 'active')
        messages = {}
        def owner(actor, number, message):
            messages[actor.key] = message
            return {'write': {f'{actor.key}.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        self.assertEqual((await self.run_project(store, pid))['state'], 'awaiting_acceptance')
        self.assertEqual(set(messages), {'a', 'c'})
        self.assertIn('a must also log its value', messages['a'])
        self.assertEqual(ScriptedActor.starts['b'], before['b'])

    async def test_unnamed_feedback_is_mapped_to_modules_by_the_master(self):
        store, pid = make_project(self.root, [('a', ()), ('b', ())], policy={'final_acceptance': 'user'})
        await self.run_project(store, pid)
        prompts = self.record_master(lambda actor, number, message, schema:
            {'affected_modules': ['b'], 'rationale': 'b renders the disputed totals'}
            if (schema or {}).get('required') == ['affected_modules', 'rationale'] else None)
        store.operator_reject(pid, feedback='The totals are wrong', authorization='user review')
        self.assertIsNotNone(store.untriaged_rejection(pid))
        messages = {}
        def owner(actor, number, message):
            messages[actor.key] = message
            return {'write': {f'{actor.key}.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        self.assertEqual((await self.run_project(store, pid))['state'], 'awaiting_acceptance')
        self.assertEqual(set(messages), {'b'})
        self.assertIn('The totals are wrong', messages['b'])
        self.assertIsNone(store.untriaged_rejection(pid))
        self.assertTrue(any('The totals are wrong' in message for message, _ in prompts))

    async def test_feedback_needing_no_change_returns_the_integration_to_the_user(self):
        store, pid = make_project(self.root, [('a', ())], policy={'final_acceptance': 'user'})
        await self.run_project(store, pid)
        store.operator_reject(pid, feedback='Only checking the process', authorization='user review')
        owners = {key: count for key, count in ScriptedActor.starts.items() if key != 'master'}
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'awaiting_acceptance')
        self.assertEqual(store.status(pid)['state'], 'awaiting_acceptance')
        self.assertEqual({key: count for key, count in ScriptedActor.starts.items() if key != 'master'}, owners)

    async def test_acceptance_commands_require_a_waiting_project(self):
        store, pid = make_project(self.root, [('a', ())], policy={'final_acceptance': 'user'})
        with self.assertRaises(ConflictError):
            store.operator_accept(pid, authorization='too early')
        with self.assertRaises(ConflictError):
            store.operator_reject(pid, feedback='too early', authorization='too early')
        await self.run_project(store, pid)
        with self.assertRaises(ContractError):
            store.operator_reject(pid, feedback='wrong module', modules=['zz'], authorization='review')
        with self.assertRaises(ContractError):
            store.operator_reject(pid, feedback='  ', authorization='review')
        self.assertEqual(store.status(pid)['state'], 'awaiting_acceptance')


class ReviewTierTests(OperatorModeCase):
    @staticmethod
    def module_reviews(prompts):
        return [message for message, _ in prompts if 'Review module' in message]

    async def test_checks_only_accepts_on_admitted_checks_without_master_reviews(self):
        store, pid = make_project(self.root, [('a', ()), ('b', ())], policy={'module_review': 'checks_only'})
        prompts = self.record_master()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(self.module_reviews(prompts), [])
        self.assertTrue(any('Review the complete integrated project' in message for message, _ in prompts))
        with closing(store.connect()) as con:
            sources = [json.loads(row['details'])['source'] for row in con.execute('SELECT details FROM review')]
        self.assertEqual(sources, ['host_policy', 'host_policy'])

    async def test_checks_only_returns_failing_checks_without_a_master_turn(self):
        store, pid = make_project(self.root, [('a', ())], policy={'module_review': 'checks_only'})
        ScriptedActor.owner_script = staticmethod(lambda actor, number, message:
                                                  {'write': {'a.py': 'value = 0\n' if number == 1 else 'value = 42\n'}})
        prompts = self.record_master()
        self.assertEqual((await self.run_project(store, pid))['state'], 'accepted')
        self.assertEqual(self.module_reviews(prompts), [])
        self.assertEqual(store.modules(pid)[0]['rounds'], 1)

    async def test_named_modules_keep_their_master_review(self):
        store, pid = make_project(self.root, [('a', ()), ('b', ())],
                                  policy={'module_review': 'checks_only', 'review_modules': ['a']})
        prompts = self.record_master()
        await self.run_project(store, pid)
        reviewed = [message.split('Review module ', 1)[1].split(':', 1)[0] for message in self.module_reviews(prompts)]
        self.assertEqual(reviewed, ['a'])
        with self.assertRaises(ContractError):
            RuntimePolicy.from_dict({'review_modules': ['zz']}).validate_for(store.project(pid)['contract'])

    async def test_a_spent_master_budget_falls_back_to_checks_and_the_user(self):
        store, pid = make_project(self.root, [('a', ())], policy={'master_token_budget': 5})
        prompts = self.record_master()
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'awaiting_acceptance')
        self.assertEqual(self.module_reviews(prompts), [])
        self.assertFalse(any('Review the complete integrated project' in message for message, _ in prompts))
        self.assertIn('budget', result['master_review']['rationale'])
        self.assertGreater(store.status(pid)['usage_by_role']['master'], 0)

    async def test_a_spent_master_budget_sends_questions_to_the_user(self):
        store, pid = make_project(self.root, [('a', ())], policy={'master_token_budget': 5})
        ScriptedActor.owner_script = staticmethod(lambda actor, number, message: asks())
        self.assertEqual((await self.run_project(store, pid))['state'], 'awaiting_user')
        self.assertEqual(json.loads(store.modules(pid)[0]['pending_question'])['escalated_by'], 'host')


class PreflightTests(OperatorModeCase):
    async def test_preflight_replaces_the_admission_turn_and_runs_once(self):
        store, pid = make_project(self.root, [('a', ())], policy={'admission_review': False, 'preflight': True})
        prompts = self.record_master()
        with patch('master_agent_runtime.controller.shutil.which', return_value='/usr/bin/true'):
            result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertFalse(any('Review initial project admission' in message for message, _ in prompts))
        passed = events(store, pid, 'preflight-passed')
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0][1]['baseline'], {'a': ['failed'], 'integration': ['failed']})
        self.assertEqual(events(store, pid, 'master-project-admission')[0][1]['source'], 'runtime_policy')
        with patch('master_agent_runtime.controller.validate', fixture_validator), \
             patch('master_agent_runtime.controller.shutil.which', return_value='/usr/bin/true'):
            await Controller(store, pid, actor_factory=ScriptedActor).preflight()
        self.assertEqual(len(events(store, pid, 'preflight-passed')), 1)

    async def test_a_budget_revision_does_not_repeat_preflight(self):
        store, pid = make_project(self.root, [('a', ())], policy={'preflight': True})
        with patch('master_agent_runtime.controller.validate', fixture_validator), \
             patch('master_agent_runtime.controller.shutil.which', return_value='/usr/bin/true'):
            await Controller(store, pid, actor_factory=ScriptedActor).preflight()
            store.operator_set_budget(pid, 200000, authorization='more room')
            await Controller(store, pid, actor_factory=ScriptedActor).preflight()
        self.assertEqual(len(events(store, pid, 'preflight-passed')), 1)

    async def test_preflight_refuses_a_missing_executable_before_any_session(self):
        store, pid = make_project(self.root, [('a', ())], policy={'preflight': True})
        with patch('master_agent_runtime.controller.shutil.which', return_value=None):
            with self.assertRaisesRegex(ContractError, 'Preflight refused'):
                await self.run_project(store, pid)
        self.assertEqual(ScriptedActor.opens, [])
        self.assertEqual(store.status(pid)['operations'], [])

    async def test_preflight_warns_about_a_check_that_already_passes(self):
        store, pid = make_project(self.root, [('a', ())], policy={'preflight': True})
        (Path(store.project(pid)['contract'].root) / 'a.py').write_text('value = 42\n')
        with patch('master_agent_runtime.controller.shutil.which', return_value='/usr/bin/true'):
            await self.run_project(store, pid)
        warnings = events(store, pid, 'preflight-passed')[0][1]['warnings']
        self.assertTrue(any(item.startswith('a check 0 already passes') for item in warnings))


class InterfaceAndAcceptanceTests(OperatorModeCase):
    def spec(self, **fields):
        project = self.root / 'project'
        project.mkdir(exist_ok=True)
        modules = tuple(ModuleSpec(key, f'Provide {key}', (f'{key}.py',), token_reservation=100, validation=CHECK)
                        for key in ('a', 'b'))
        return ProjectSpec(str(project), 'Deliver', modules, 100000, integration_validation=CHECK, **fields)

    def test_interface_contracts_are_validated_and_keep_old_identities(self):
        plain = self.spec()
        self.assertNotIn('interfaces', plain.to_dict())
        api = InterfaceSpec('api.totals', 'a', 'totals(rows) -> int', ('b',))
        spec = self.spec(interfaces=(api,))
        self.assertNotEqual(spec.fingerprint, plain.fingerprint)
        self.assertEqual(ProjectSpec.from_dict(spec.to_dict()), spec)
        with self.assertRaises(ContractError):
            self.spec(interfaces=(InterfaceSpec('x', 'zz', 'contract'),))
        with self.assertRaises(ContractError):
            self.spec(interfaces=(api, api))
        with self.assertRaises(ContractError):
            InterfaceSpec('x', 'a', 'contract', ('a',))
        with self.assertRaises(ContractError):
            InterfaceSpec('x', 'a', '  ')

    async def test_owners_get_their_contracts_and_build_in_parallel(self):
        store, pid = make_project(self.root, [('a', ()), ('b', ())],
                                  interfaces=(InterfaceSpec('api.totals', 'a', 'totals(rows) -> int', ('b',)),))
        messages = {}
        def owner(actor, number, message):
            messages[actor.key] = message
            return {'write': {f'{actor.key}.py': 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        prompts = self.record_master()
        self.assertEqual((await self.run_project(store, pid))['state'], 'accepted')
        for key in ('a', 'b'):
            self.assertIn('totals(rows) -> int', messages[key])
        self.assertTrue(any("['api.totals']" in message for message, _ in prompts if 'Review module' in message))
        dispatched = [payload['actor'].rsplit(':', 1)[-1] for _, payload in events(store, pid, 'dispatch-intent')
                      if ':module:' in payload['actor']]
        self.assertEqual(sorted(dispatched[:2]), ['a', 'b'])

    def test_private_checks_must_live_outside_the_project(self):
        check = AcceptanceCheck('totals', ('python', '{acceptance}/check.py'))
        inside = self.root / 'project' / 'private'
        inside.mkdir(parents=True)
        with self.assertRaisesRegex(ContractError, 'outside the project'):
            self.spec(acceptance_directory=str(inside), acceptance_checks=(check,))
        outside = self.root / 'checks'
        outside.mkdir()
        with self.assertRaises(ContractError):
            self.spec(acceptance_directory=str(outside))
        with self.assertRaises(ContractError):
            self.spec(acceptance_checks=(check,))
        spec = self.spec(acceptance_directory=str(outside), acceptance_checks=(check,))
        self.assertEqual(ProjectSpec.from_dict(spec.to_dict()), spec)

    def private_project(self, status):
        checks = self.root / 'checks'
        checks.mkdir(exist_ok=True)
        (checks / 'check.py').write_text('assert totals_add_up()\n')
        check = AcceptanceCheck('totals', ('python', '{acceptance}/check.py'), description='totals add up')
        store, pid = make_project(self.root, [('a', ())], acceptance_directory=str(checks), acceptance_checks=(check,))
        calls = []
        def validator(steps, candidate, context, evidence, **kwargs):
            if kwargs.get('private') is not None:
                calls.append((Path(kwargs['private']), Path(evidence)))
                return [{'status': status, 'argv': list(step.argv)} for step in steps]
            return fixture_validator(steps, candidate, context, evidence)
        return store, pid, checks, calls, validator

    async def test_private_checks_reach_only_the_user_and_a_failure_forces_the_gate(self):
        store, pid, checks, calls, validator = self.private_project('failed')
        prompts = self.record_master()
        result = await self.run_project(store, pid, validator)
        self.assertEqual(result['state'], 'awaiting_acceptance')
        self.assertEqual(result['acceptance_checks'], [{'name': 'totals', 'status': 'failed'}])
        private, evidence = calls[0]
        state_root = store.path.parent / 'projects' / pid
        self.assertEqual(private, state_root / 'private' / 'acceptance')
        self.assertTrue((private / 'check.py').is_file())
        self.assertIn(state_root / 'private', evidence.parents)
        self.assertFalse(any('totals' in message or str(checks) in message for message, _ in prompts))
        report = Path(result['report']).read_text()
        for expected in ('totals', 'failed', 'totals add up'):
            self.assertIn(expected, report)
        self.assertEqual(len(events(store, pid, 'acceptance-checks-sealed')), 1)

    async def test_passing_private_checks_keep_master_acceptance(self):
        store, pid, _, calls, validator = self.private_project('passed')
        result = await self.run_project(store, pid, validator)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(len(calls), 1)


class LenientSchemaTests(OperatorModeCase):
    def test_host_optional_fields_become_optional_and_tolerated(self):
        schema = lenient_schema(STRUCTURED_REVIEW_OUTPUT)
        self.assertNotIn('advisories', schema['required'])
        self.assertNotIn('previous_issues', schema['required'])
        self.assertIn('blocking_issues', schema['required'])
        owner = lenient_schema(MODULE_OUTPUT)
        self.assertEqual(owner['required'], ['status', 'summary', 'issues'])
        self.assertEqual(lenient_schema(DECISION_OUTPUT), DECISION_OUTPUT)
        integration = {'type': 'object', 'additionalProperties': False, 'required': ['decision', 'advisories'],
                       'properties': {'decision': {'type': 'string'}, 'blocking_issues': {'type': 'array'},
                                      'advisories': {'type': 'array'}}}
        self.assertIn('previous_issues', lenient_schema(integration)['properties'])

    async def test_only_claude_actors_receive_the_lenient_schemas(self):
        schemas = {}
        class ClaudeScripted(ScriptedActor):
            provider = 'claude'
            async def start_turn(self, message, output_schema=None, **kwargs):
                schemas.setdefault(self.kind, []).append(output_schema)
                return await super().start_turn(message, output_schema=output_schema, **kwargs)
        store, pid = make_project(self.root, [('a', ())])
        with patch('master_agent_runtime.controller.validate', fixture_validator):
            result = await Controller(store, pid, actor_factory=ClaudeScripted).run()
        self.assertEqual(result['state'], 'accepted')
        self.assertTrue(all('question' not in schema['required'] for schema in schemas['module']))
        reviews = [schema for schema in schemas['master'] if 'blocking_issues' in schema['properties']]
        self.assertTrue(reviews and all('advisories' not in schema['required'] for schema in reviews))
        ScriptedActor.reset()
        strict = self.root / 'strict'
        strict.mkdir()
        store, pid = make_project(strict, [('a', ())])
        captured = []
        class CodexScripted(ScriptedActor):
            async def start_turn(self, message, output_schema=None, **kwargs):
                captured.append(output_schema)
                return await super().start_turn(message, output_schema=output_schema, **kwargs)
        with patch('master_agent_runtime.controller.validate', fixture_validator):
            await Controller(store, pid, actor_factory=CodexScripted).run()
        self.assertTrue(any(schema and 'question' in schema['required'] for schema in captured))


class CommandTests(OperatorModeCase):
    def cli(self, state, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, '-B', '-m', 'master_agent_runtime', '--state', str(state), *args],
                              text=True, capture_output=True, env=env)

    def test_init_admits_new_handoffs_in_operator_mode(self):
        project = self.root / 'project'
        project.mkdir()
        (project / 'a.py').write_text('value = -1\n')
        step = [{'argv': ['python', 'check.py']}]
        handoff = {'root': str(project), 'objective': 'Deliver', 'token_budget': 1000, 'integration_validation': step,
                   'modules': [{'key': 'a', 'objective': 'Provide a', 'paths': ['a.py'], 'validation': step}]}
        path = self.root / 'handoff.json'
        path.write_text(json.dumps(handoff))
        result = self.cli(self.root / 'state', 'init', str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['runtime_policy'], OPERATOR_POLICY)
        handoff['runtime_policy'] = {'final_acceptance': 'master'}
        path.write_text(json.dumps(handoff))
        _, policy = load_handoff_with_policy(path, defaults=OPERATOR_POLICY)
        self.assertEqual((policy.final_acceptance, policy.preflight, policy.admission_review), ('master', True, False))
        self.assertFalse(load_handoff_with_policy(path)[1].preflight)

    async def test_answer_reject_and_accept_commands_are_journaled(self):
        store, pid = make_project(self.root, [('a', ())], policy={'final_acceptance': 'user'})
        ScriptedActor.owner_script = staticmethod(lambda actor, number, message:
                                                  asks() if number == 1 else {'write': {'a.py': 'value = 42\n'}})
        self.record_master(lambda actor, number, message, schema:
            {'action': 'escalate', 'answer': '', 'rationale': 'user call', 'question_for_user': 'Which value?'}
            if is_decision(schema) else None)
        self.assertEqual((await self.run_project(store, pid))['state'], 'awaiting_user')
        state = store.path.parent
        answered = await asyncio.to_thread(self.cli, state, 'answer', pid, '--module', 'a', '--text', '42',
                                           '--authorization', 'cli test')
        self.assertEqual(answered.returncode, 0, answered.stderr)
        self.assertEqual(json.loads(answered.stdout)['status']['modules'][0]['state'], 'continuing')
        self.assertEqual((await self.run_project(store, pid))['state'], 'awaiting_acceptance')
        rejected = await asyncio.to_thread(self.cli, state, 'reject', pid, '--feedback', 'log it', '--module', 'a',
                                           '--authorization', 'cli test')
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        self.assertEqual(json.loads(rejected.stdout)['state'], 'active')
        self.assertEqual((await self.run_project(store, pid))['state'], 'awaiting_acceptance')
        accepted = await asyncio.to_thread(self.cli, state, 'accept', pid, '--authorization', 'cli test')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)['state'], 'accepted')
        self.assertNotEqual((await asyncio.to_thread(self.cli, state, 'accept', pid)).returncode, 0)


class DecisionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_routes_a_persisted_question_and_keeps_the_owner_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input'
            source.mkdir()
            (source / 'one.py').write_text('value = 0\n')
            store = Store(root / 'control' / 'runtime.sqlite')
            steps = (ValidationStep(('python', 'fixed-check.py')),)
            pid = store.create_project(ProjectSpec(str(source), 'Deliver', (
                ModuleSpec('one', 'Deliver', ('one.py',), validation=steps, token_reservation=100),), 10000,
                integration_validation=steps))
            master = store.bind(pid + ':master', 'master', 'master')
            owner = store.bind(pid + ':module:one', 'owner', 'owner')
            controller = Controller(store, pid, cli='synthetic-peer')
            controller.prepare()
            workspace = controller.workspaces / 'one' / 'activation'
            workspace.mkdir(parents=True)
            (workspace / 'one.py').write_text('value = 7  # draft\n')
            binding = store.binding_intent(owner.actor_id, {'workspace': str(workspace)})
            store.settle(binding, evidence='Synthetic binding acknowledgement')
            operation = store.dispatch(master, owner.actor_id, 'Deliver')
            store.mark_started(operation, 'native-turn')
            store.mark_unknown(operation, 'Synthetic lost completion response')
            peer = AsyncMock()
            peer.call.return_value = {'thread': {'id': 'owner', 'sessionId': 'owner', 'turns': [
                {'id': 'native-turn', 'status': 'completed', 'items': [
                    {'type': 'agentMessage', 'phase': 'final_answer',
                     'text': json.dumps({**asks(), 'summary': 'Needs a value', 'issues': []})}]}]}}
            with patch('master_agent_runtime.recovery.JsonRpcClient', return_value=peer):
                report = await reconcile(store, pid, cli='synthetic-peer')
            self.assertEqual(report['reconciled'][0]['module_state'], 'awaiting_decision')
            row = store.modules(pid)[0]
            self.assertEqual(row['state'], 'awaiting_decision')
            self.assertEqual((Path(row['checkpoint']) / 'one.py').read_text(), 'value = 7  # draft\n')
            self.assertEqual(store.status(pid)['reserved_tokens'], 0)
            requests = [item for item in store.pending_messages(pid + ':master') if item['kind'] == 'decision_request']
            self.assertEqual(len(requests), 1)
