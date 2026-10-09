"""Health checks against real Store fixtures, with no native/model execution."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from master_agent_runtime import ModuleSpec, ProjectSpec, Store
from master_agent_runtime.contracts import ContractError
from master_agent_runtime.health import inspect_project_state


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.source = self.project_root / "original.txt"
        self.source.write_text("Original project source\n")
        self.store = Store(self.root / "state" / "runtime.sqlite")
        self.spec = ProjectSpec(str(self.project_root), "Deliver the admitted project", (
            ModuleSpec("zeta", "Implement Zeta", ("zeta.py",), token_reservation=100),
            ModuleSpec("alpha", "Implement Alpha", ("alpha.py",), token_reservation=100),
            ModuleSpec("beta", "Implement Beta", ("beta.py",), token_reservation=100),
        ), 10000, max_parallel=3)
        self.project = self.store.create_project(self.spec)
        self.master = self.store.bind(self.project + ":master", "master", "master")
        self.owners = {key: self.store.bind(self.project + ":module:" + key, key, key)
                       for key in ("zeta", "alpha", "beta")}

    def database_state(self):
        # Include events, messages, candidates, reviews and usage, which are not
        # all present in Store.status. Open only a readonly connection here.
        with closing(sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True)) as con:
            return tuple(con.iterdump())

    def report(self, project_id=None):
        project_id = self.project if project_id is None else project_id
        before = self.store.status(project_id)
        database_before = self.database_state()
        database_bytes = self.store.path.read_bytes()
        source_before = self.source.read_bytes()
        report = inspect_project_state(self.store, project_id)
        self.assertEqual(report, inspect_project_state(self.store, project_id))
        self.assertEqual(self.store.status(project_id), before)
        self.assertEqual(self.database_state(), database_before)
        self.assertEqual(self.store.path.read_bytes(), database_bytes)
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertEqual(set(report), {
            "project_id", "project_state", "safe_to_run", "reconciliation_required",
            "active_operations", "blocked_modules", "pending_reviews", "next_action", "usage",
        })
        self.assertEqual(report["project_id"], project_id)
        self.assertEqual(report["project_state"], before["state"])
        self.assertIsInstance(report["safe_to_run"], bool)
        self.assertIsInstance(report["reconciliation_required"], bool)
        self.assertIsInstance(report["next_action"], str)
        self.assertTrue(report["next_action"].strip())
        self.assertEqual(report["usage"], {
            "spent_tokens": before["spent_tokens"],
            "reserved_tokens": before["reserved_tokens"],
            "hard_billing_cap": False,
        })
        # Reports must remain usable as JSON without leaking Store contracts.
        self.assertEqual(json.loads(json.dumps(report)), report)
        return report

    def dispatch(self, key):
        return self.store.dispatch(self.master, self.owners[key].actor_id, "Fixture module work")

    def submit(self, key, *, passed=True):
        # Simulated native outcomes are fixture setup only. Health must never
        # infer or create settlement, submissions, reviews or refunds itself.
        operation = self.dispatch(key)
        self.store.mark_started(operation, "fixture-turn-" + operation)
        self.store.settle(operation, evidence="Test fixture simulates observed completion")
        candidate = "candidate-" + key
        self.store.submit(self.owners[key], candidate, tree_hash="tree-" + key,
                          snapshot="fixture/" + candidate, inputs={},
                          validation=[{"status": "passed" if passed else "failed"}],
                          summary="Fixture submission", operation_id=operation)
        return candidate

    def test_ready_project_has_complete_readonly_diagnostic(self):
        report = self.report()
        self.assertTrue(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])
        self.assertEqual(report["active_operations"], [])
        self.assertEqual(report["blocked_modules"], [])
        self.assertEqual(report["pending_reviews"], [])
        self.assertIn("scheduling", report["next_action"].lower())

    def test_registered_project_can_resume_before_native_binding(self):
        project = self.store.create_project(self.spec)
        report = self.report(project)
        self.assertTrue(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])

    def test_intent_running_unknown_and_stopping_keep_actual_records(self):
        operation = self.dispatch("alpha")
        self.store.record_usage(self.owners["alpha"].actor_id, 17)
        for state, reconcile in (("intent", True), ("running", False),
                                 ("unknown", True), ("stopping", True)):
            with self.subTest(state=state):
                if state == "running":
                    self.store.mark_started(operation, "native-fixture-turn")
                elif state == "unknown":
                    self.store.mark_unknown(operation, "Fixture lost acknowledgement")
                elif state == "stopping":
                    self.store.cancel_project(self.master)
                report = self.report()
                self.assertFalse(report["safe_to_run"])
                self.assertEqual(report["reconciliation_required"], reconcile)
                self.assertEqual(report["active_operations"], [self.store.operation(operation)])
                self.assertEqual(report["active_operations"][0]["status"], state)
                self.assertEqual(report["usage"]["spent_tokens"], 17)
                self.assertEqual(report["usage"]["reserved_tokens"], 100)
                self.assertIn(operation, report["next_action"])
        self.assertEqual(report["project_state"], "cancelling")
        self.assertIn("cancelling", report["next_action"])

    def test_mixed_live_operations_include_master_and_are_project_scoped(self):
        running = self.dispatch("alpha")
        self.store.mark_started(running, "fixture-running")
        unknown = self.dispatch("zeta")
        self.store.mark_unknown(unknown, "Fixture uncertain outcome")
        intent = self.dispatch("beta")
        master_operation = self.store.dispatch(self.master, self.master.actor_id,
                                               "Fixture review", kind="review")
        # Fixture for a host stopping only its Master turn, without cancelling
        # this still-active project or changing the other operation statuses.
        with self.store.transaction() as con:
            con.execute("UPDATE operation SET status='stopping' WHERE id=?", (master_operation,))
        other_project = self.store.create_project(self.spec)
        other_master = self.store.bind(other_project + ":master", "other-master", "other-master")
        foreign = self.store.dispatch(other_master, other_master.actor_id, "Foreign fixture work")
        report = self.report()
        self.assertFalse(report["safe_to_run"])
        self.assertTrue(report["reconciliation_required"])
        expected = self.store.status(self.project)["operations"]
        self.assertEqual(report["active_operations"], expected)
        self.assertEqual({op["id"] for op in report["active_operations"]},
                         {running, unknown, intent, master_operation})
        self.assertNotIn(foreign, {op["id"] for op in report["active_operations"]})

    def test_terminal_operations_are_excluded_without_settling_work(self):
        for key, state in (("alpha", "done"), ("beta", "failed"), ("zeta", "cancelled")):
            operation = self.dispatch(key)
            self.store.settle(operation, state=state, evidence="Fixture observed terminal outcome")
        report = self.report()
        self.assertEqual(report["active_operations"], [])
        self.assertEqual(report["blocked_modules"], ["beta", "zeta"])
        self.assertFalse(report["safe_to_run"])
        self.assertTrue(report["reconciliation_required"])
        self.assertIn("alpha", report["next_action"])

    def test_completed_execution_with_unsubmitted_work_requires_reconciliation(self):
        operation = self.dispatch("alpha")
        self.store.mark_started(operation, "fixture-completed-turn")
        self.store.settle(operation, evidence="Fixture observed completed turn")
        report = self.report()
        self.assertEqual(report["active_operations"], [])
        self.assertEqual(report["blocked_modules"], [])
        self.assertFalse(report["safe_to_run"])
        self.assertTrue(report["reconciliation_required"])
        self.assertIn("alpha", report["next_action"])
        module = next(m for m in self.store.modules(self.project) if m["key"] == "alpha")
        self.assertEqual(module["state"], "working")
        self.assertIsNone(module["current_candidate"])

    def test_other_module_running_does_not_own_orphaned_work(self):
        orphan = self.dispatch("alpha")
        self.store.settle(orphan, evidence="Fixture completed without a submission")
        running = self.dispatch("beta")
        self.store.mark_started(running, "fixture-other-owner")
        report = self.report()
        self.assertTrue(report["reconciliation_required"])
        self.assertFalse(report["safe_to_run"])
        self.assertEqual(report["active_operations"], [self.store.operation(running)])
        self.assertIn("alpha", report["next_action"])

    def test_master_operation_does_not_own_orphaned_module_work(self):
        orphan = self.dispatch("alpha")
        self.store.settle(orphan, evidence="Fixture completed without submission")
        running = self.store.dispatch(self.master, self.master.actor_id, "Fixture review", kind="review")
        self.store.mark_started(running, "fixture-master-review")
        report = self.report()
        self.assertFalse(report["safe_to_run"])
        self.assertTrue(report["reconciliation_required"])
        self.assertIn("alpha", report["next_action"])

    def test_root_binding_is_not_ownership_of_working_module_execution(self):
        orphan = self.dispatch("alpha")
        self.store.settle(orphan, evidence="Fixture completed without submission")
        binding = self.store.binding_intent(self.owners["alpha"].actor_id, {"fixture": "root binding"})
        self.store.mark_started(binding, "fixture-binding-turn")
        report = self.report()
        self.assertEqual(report["active_operations"], [self.store.operation(binding)])
        self.assertFalse(report["safe_to_run"])
        self.assertTrue(report["reconciliation_required"])
        self.assertIn("alpha", report["next_action"])

    def test_pending_reviews_resume_master_without_rerunning_producers(self):
        self.submit("beta")
        self.submit("alpha")
        report = self.report()
        self.assertTrue(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])
        self.assertEqual(report["active_operations"], [])
        self.assertEqual(report["pending_reviews"], ["alpha", "beta"])
        self.assertIn("Master review", report["next_action"])
        self.assertEqual(len(self.store.pending_messages(self.master.actor_id)), 2)

    def test_pending_reviews_do_not_override_running_or_blocked_work(self):
        self.submit("alpha")
        operation = self.dispatch("beta")
        self.store.mark_started(operation, "fixture-pending-work")
        report = self.report()
        self.assertEqual(report["pending_reviews"], ["alpha"])
        self.assertFalse(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])
        self.assertIn(operation, report["next_action"])
        self.store.settle(operation, state="failed", evidence="Fixture observed failure")
        report = self.report()
        self.assertEqual(report["pending_reviews"], ["alpha"])
        self.assertEqual(report["blocked_modules"], ["beta"])
        self.assertFalse(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])
        self.assertIn("blocked", report["next_action"])

    def test_active_master_review_blocks_duplicate_resume(self):
        self.submit("alpha")
        operation = self.store.dispatch(self.master, self.master.actor_id, "Fixture candidate review", kind="review")
        self.store.mark_started(operation, "fixture-master-running")
        report = self.report()
        self.assertEqual(report["pending_reviews"], ["alpha"])
        self.assertEqual(report["active_operations"], [self.store.operation(operation)])
        self.assertFalse(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])

    def test_requested_revision_can_resume_with_original_owner(self):
        candidate = self.submit("alpha", passed=False)
        self.store.review(self.master, candidate, "request_changes", "Fixture missing behavior",
                          verified_tree_hash="tree-alpha")
        report = self.report()
        self.assertTrue(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])
        self.assertEqual(report["pending_reviews"], [])
        module = next(m for m in self.store.modules(self.project) if m["key"] == "alpha")
        self.assertEqual(module["owner"], self.owners["alpha"].actor_id)
        self.assertEqual(module["state"], "revision_requested")
        self.assertIn("requested revisions", report["next_action"])
        self.assertEqual(len(self.store.pending_messages(self.owners["alpha"].actor_id)), 1)

    def test_all_accepted_modules_resume_integration_then_project_is_unsafe(self):
        bindings = {}
        for key in self.owners:
            candidate = self.submit(key)
            self.store.review(self.master, candidate, "approve", "Fixture independently checked candidate",
                              verified_tree_hash="tree-" + key)
            bindings[key] = {"candidate": candidate, "revision": 1}
        report = self.report()
        self.assertTrue(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])
        self.assertEqual(report["pending_reviews"], [])
        self.assertIn("integration", report["next_action"].lower())
        self.store.record_integration(self.master, "fixture/integration", bindings, [{"status": "passed"}])
        report = self.report()
        self.assertEqual(report["project_state"], "accepted")
        self.assertFalse(report["safe_to_run"])
        self.assertFalse(report["reconciliation_required"])

    def test_nonactive_project_states_never_admit_execution(self):
        for state in ("accepted", "completed", "cancelled", "cancelling", "blocked", "paused"):
            with self.subTest(state=state):
                with self.store.transaction() as con:
                    con.execute("UPDATE project SET state=? WHERE id=?", (state, self.project))
                report = self.report()
                self.assertFalse(report["safe_to_run"])
                self.assertFalse(report["reconciliation_required"])
                self.assertEqual(report["project_state"], state)
                self.assertIn(state, report["next_action"])

    def test_nonactive_state_does_not_hide_reconciliation(self):
        operation = self.dispatch("alpha")
        self.store.settle(operation, evidence="Fixture completed without submission")
        with self.store.transaction() as con:
            con.execute("UPDATE project SET state='blocked' WHERE id=?", (self.project,))
        report = self.report()
        self.assertFalse(report["safe_to_run"])
        self.assertTrue(report["reconciliation_required"])
        self.assertIn("alpha", report["next_action"])
        self.assertIn("blocked", report["next_action"])

    def test_observed_counter_resets_are_preserved_without_refunds_or_exact_billing(self):
        operation = self.dispatch("alpha")
        self.store.mark_unknown(operation, "Fixture unknown completion")
        self.store.record_usage(self.owners["alpha"].actor_id, 37)
        self.store.record_usage(self.owners["alpha"].actor_id, 4)
        report = self.report()
        self.assertEqual(report["usage"], {
            "spent_tokens": 41, "reserved_tokens": 100, "hard_billing_cap": False,
        })
        self.assertEqual(report["active_operations"][0]["usage"], 41)
        self.assertEqual(self.store.actor(self.owners["alpha"].actor_id)["last_usage"], 4)
        self.assertTrue(report["reconciliation_required"])

    def test_one_status_observation_preserves_order_records_and_source_snapshot(self):
        for key in ("alpha", "zeta"):
            operation = self.dispatch(key)
            self.store.settle(operation, state="failed", evidence="Fixture observed failure")
        operation = self.dispatch("beta")
        self.store.mark_unknown(operation, "Fixture unresolved acknowledgement")
        snapshot = self.store.status(self.project)
        snapshot["modules"].reverse()
        # Verify records remain detached even if Store later exposes structured
        # evidence fields, as well as preserving every current operation field.
        snapshot["operations"][-1]["evidence"] = {"observations": ["Fixture uncertainty"]}
        before = deepcopy(snapshot)
        status_only = Mock(spec_set=["status"])
        status_only.status.return_value = snapshot
        report = inspect_project_state(status_only, self.project)
        status_only.status.assert_called_once_with(self.project)
        self.assertEqual(snapshot, before)
        self.assertEqual(report["blocked_modules"], ["alpha", "zeta"])
        self.assertEqual(report["active_operations"], [snapshot["operations"][-1]])
        report["active_operations"][0]["status"] = "done"
        report["active_operations"][0]["evidence"]["observations"].clear()
        report["active_operations"].clear()
        self.assertEqual(snapshot, before)

    def test_inspection_works_when_all_store_connections_are_readonly(self):
        self.submit("alpha")
        operation = self.dispatch("beta")
        self.store.mark_unknown(operation, "Fixture uncertain work")

        def connect_readonly():
            con = sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True,
                                  isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            return con

        with patch.object(self.store, "connect", side_effect=connect_readonly):
            report = self.report()
        self.assertFalse(report["safe_to_run"])
        self.assertTrue(report["reconciliation_required"])
        self.assertEqual(report["pending_reviews"], ["alpha"])
        self.assertEqual(report["usage"]["reserved_tokens"], 100)

    def test_unknown_project_propagates_contract_error_without_writes(self):
        before = self.database_state()
        with self.assertRaisesRegex(ContractError, "Unknown project"):
            inspect_project_state(self.store, "missing-project")
        self.assertEqual(self.database_state(), before)
        error = ContractError("Fixture status failure")
        status_only = Mock(spec_set=["status"])
        status_only.status.side_effect = error
        with self.assertRaises(ContractError) as caught:
            inspect_project_state(status_only, "missing-project")
        self.assertIs(caught.exception, error)
        status_only.status.assert_called_once_with("missing-project")


if __name__ == "__main__":
    unittest.main()
