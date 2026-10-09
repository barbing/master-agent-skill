"""Structured Master review, re-review discipline, fail-fast and admission policy (synthetic actors)."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from master_agent_runtime.contracts import ContractError
from master_agent_runtime.controller import Controller

from runtime_fixtures import ScriptedActor, default_master, events, fixture_validator, make_project


def review_rows(store, pid):
    with closing(store.connect()) as con:
        rows = con.execute('SELECT decision,rationale,details FROM review ORDER BY at').fetchall()
    return [(row['decision'], row['rationale'], json.loads(row['details']) if row['details'] else None) for row in rows]


class ReviewCalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ScriptedActor.reset()
        self.messages = []

    async def run_project(self, store, pid):
        with patch('master_agent_runtime.controller.validate', fixture_validator):
            return await Controller(store, pid, actor_factory=ScriptedActor).run()

    def master_reviews(self, store, pid):
        return [op for op in store.status(pid)['operations'] if op['actor_id'].endswith(':master') and op['kind'] == 'review']

    async def test_unactionable_rejection_is_reasked_once_instead_of_consuming_a_revision(self):
        store, pid = make_project(self.root, [('one', ())])
        def master(actor, number, message, schema):
            self.messages.append(message)
            if 'Review module' in message and len([m for m in self.messages if 'Review module' in m]) == 1:
                return {'decision': 'request_changes', 'rationale': 'I would prefer a different style',
                        'failure_kind': 'implementation', 'blocking_issues': [], 'advisories': [], 'previous_issues': []}
            if 'is not actionable' in message:
                return {'decision': 'approve', 'rationale': 'No admitted obligation is violated',
                        'failure_kind': 'none', 'blocking_issues': [],
                        'advisories': ['Consider a different style'], 'previous_issues': []}
            return default_master(actor, number, message, schema)
        ScriptedActor.master_script = staticmethod(master)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(ScriptedActor.starts['one'], 1)        # no owner rework was spent
        self.assertEqual(store.modules(pid)[0]['rounds'], 0)
        self.assertEqual(len(self.master_reviews(store, pid)), 2)
        decision, _, details = review_rows(store, pid)[0]
        self.assertEqual(decision, 'approve')
        self.assertEqual(details['advisories'], ['Consider a different style'])
        self.assertEqual(events(store, pid, 'master-review-restructure-requested')[0][1]['problem'],
                         'an implementation change request listed no blocking issue')

    async def test_blocking_issues_reach_the_owner_and_are_rechecked_on_resubmission(self):
        store, pid = make_project(self.root, [('one', ())])
        issue = {'obligation': 'module objective', 'evidence': 'one.py:1 keeps the placeholder comment',
                 'required_change': 'Remove the placeholder marker'}
        def owner(actor, number, message):
            self.messages.append(('owner', message))
            return {'write': {'one.py': 'value = 42  # placeholder\n' if number == 1 else 'value = 42\n'}}
        def master(actor, number, message, schema):
            reviews = [m for m in self.messages if m[0] == 'review']
            if 'Review module' in message:
                self.messages.append(('review', message))
                if not reviews:
                    return {'decision': 'request_changes', 'rationale': 'Placeholder remains', 'failure_kind': 'implementation',
                            'blocking_issues': [issue], 'advisories': ['Add a docstring'], 'previous_issues': []}
                return {'decision': 'approve', 'rationale': 'Placeholder removed', 'failure_kind': 'none',
                        'blocking_issues': [], 'advisories': [],
                        'previous_issues': [{'issue': issue['required_change'], 'status': 'resolved'}]}
            return default_master(actor, number, message, schema)
        ScriptedActor.owner_script, ScriptedActor.master_script = staticmethod(owner), staticmethod(master)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        second_owner = [m for kind, m in self.messages if kind == 'owner'][1]
        self.assertIn('Blocking issues the Master will verify on resubmission', second_owner)
        self.assertIn('Remove the placeholder marker', second_owner)
        self.assertIn('Non-blocking advisories', second_owner)
        second_review = [m for kind, m in self.messages if kind == 'review'][1]
        self.assertIn('Your previous blocking issues for this module were', second_review)
        self.assertIn('Remove the placeholder marker', second_review)
        self.assertEqual(review_rows(store, pid)[1][2]['previous_issues'][0]['status'], 'resolved')

    async def test_fail_fast_is_opt_in_and_spends_no_master_turn_on_failed_checks(self):
        store, pid = make_project(self.root, [('one', ())], policy={'fail_fast_failed_checks': True})
        def owner(actor, number, message):
            self.messages.append(message)
            return {'write': {'one.py': 'value = 0\n' if number == 1 else 'value = 42\n'}}
        ScriptedActor.owner_script = staticmethod(owner)
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(len(self.master_reviews(store, pid)), 1)
        first = review_rows(store, pid)[0]
        self.assertEqual((first[0], first[2]['source']), ('request_changes', 'host_fail_fast'))
        self.assertIn('Host fail-fast', self.messages[1])
        self.assertIn('check:0', self.messages[1])

    async def test_admission_turn_can_be_disabled_by_policy(self):
        store, pid = make_project(self.root, [('one', ())], policy={'admission_review': False})
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        kinds = [op['kind'] for op in store.status(pid)['operations'] if op['actor_id'].endswith(':master')]
        self.assertNotIn('admission', kinds)
        self.assertEqual(events(store, pid, 'master-project-admission')[0][1]['source'], 'runtime_policy')

    async def test_operator_rerun_after_rejected_admission_reopens_exactly_one_new_admission(self):
        store, pid = make_project(self.root, [('one', ())])
        verdicts = iter(['request_changes', 'approve'])
        def master(actor, number, message, schema):
            if 'Review initial project admission' in message:
                return {'decision': next(verdicts), 'rationale': 'admission verdict'}
            return default_master(actor, number, message, schema)
        ScriptedActor.master_script = staticmethod(master)
        with self.assertRaises(ContractError):
            await self.run_project(store, pid)
        with self.assertRaises(ContractError):
            await self.run_project(store, pid)                 # restart alone cannot bypass the rejection
        store.operator_admission(pid, decision='rerun', authorization='Human: contract clarified')
        result = await self.run_project(store, pid)
        self.assertEqual(result['state'], 'accepted')
        admissions = [op for op in store.status(pid)['operations'] if op['kind'] == 'admission']
        self.assertEqual(len(admissions), 2)


class StructureCheckTests(unittest.TestCase):
    def test_obligation_categories_and_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = make_project(Path(tmp), [('one', ())])
            controller = Controller(store, pid, actor_factory=ScriptedActor)
            check = controller.review_structure_problem
            self.assertIsNone(check({'decision': 'request_changes', 'rationale': 'legacy output without the field'}))
            self.assertIsNone(check({'decision': 'request_changes', 'failure_kind': 'evidence', 'blocking_issues': []}))
            self.assertIsNotNone(check({'decision': 'approve', 'blocking_issues': [{'obligation': 'module'}]}))
            self.assertIsNotNone(check({'decision': 'request_changes', 'failure_kind': 'implementation',
                                        'blocking_issues': [{'obligation': 'taste', 'evidence': 'x', 'required_change': 'y'}]}))
            self.assertIsNone(check({'decision': 'request_changes', 'failure_kind': 'implementation',
                                     'blocking_issues': [{'obligation': 'check:0 frozen oracle', 'evidence': 'x',
                                                          'required_change': 'y'}]}))
