import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from master_agent_runtime import ModuleSpec, Principal, ProjectSpec, Store, ValidationStep
from master_agent_runtime.contracts import AuthorityError, BudgetError, ConflictError, ContractError
from master_agent_runtime.artifacts import capture, context_hashes, verify


class ModuleRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.store = Store(self.root / "control" / "runtime.sqlite")
        self.spec = ProjectSpec(str(self.project_root), "Deliver the admitted project", (
            ModuleSpec("a", "Implement module A", ("a.py",), token_reservation=100),
            ModuleSpec("b", "Implement module B", ("b.py",), token_reservation=100)), 1000)
        self.project = self.store.create_project(self.spec)
        self.master = self.store.bind(self.project + ":master", "master-thread", "master-thread")
        self.a = self.store.bind(self.project + ":module:a", "a-thread", "a-thread")
        self.b = self.store.bind(self.project + ":module:b", "b-thread", "b-thread")

    def submission(self, principal, candidate_id, *, passed=True, tree_hash="tree"):
        op = self.store.dispatch(self.master, principal.actor_id, "Complete your module")
        self.store.mark_started(op, "native-turn-" + op)
        inputs = json.loads(self.store.operation(op)["payload"])["inputs"]
        self.store.settle(op, evidence="native turn ended and owned worker was settled/fenced")
        self.store.submit(principal, candidate_id, tree_hash=tree_hash, snapshot="sealed/" + candidate_id,
                          inputs=inputs, validation=[{"status": "passed" if passed else "failed"}],
                          summary="Actual implementation", operation_id=op)
        return op

    def test_independent_sessions_and_stable_owner_across_master_adoption(self):
        op = self.store.dispatch(self.master, self.a.actor_id, "Full module objective")
        old = self.master
        self.master = self.store.adopt_master(self.project)
        self.assertEqual(self.store.actor(self.a.actor_id)["thread_id"], "a-thread")
        self.assertEqual(self.store.operation(op)["status"], "intent")
        with self.assertRaises(AuthorityError):
            self.store.dispatch(old, self.b.actor_id, "stale issuer")
        self.store.dispatch(self.master, self.b.actor_id, "Independent module")

    def test_forked_session_cannot_be_renamed_as_independent_actor(self):
        with self.assertRaises(AuthorityError):
            self.store.bind(self.a.actor_id, "new-child-thread", "master-thread")
        with self.assertRaises(ConflictError):
            self.store.bind(self.a.actor_id, "replacement-thread", "replacement-thread")

    def test_producer_cannot_dispatch_or_review_by_claiming_master_role(self):
        with self.assertRaises(AuthorityError):
            self.store.dispatch(self.a, self.b.actor_id, "I am the master")
        self.submission(self.a, "candidate")
        with self.assertRaises(AuthorityError):
            self.store.review(self.a, "candidate", "approve", "PASS", verified_tree_hash="tree")
        forged = Principal(self.master.actor_id, self.a.thread_id, self.master.epoch)
        with self.assertRaises(AuthorityError):
            self.store.review(forged, "candidate", "approve", "PASS", verified_tree_hash="tree")

    def test_unknown_start_keeps_reservation_and_blocks_duplicate_writer(self):
        op = self.store.dispatch(self.master, self.a.actor_id, "Run")
        self.store.mark_unknown(op, "start reached backend but ack lost")
        self.assertEqual(self.store.status(self.project)["reserved_tokens"], 100)
        with self.assertRaises(ConflictError):
            self.store.dispatch(self.master, self.a.actor_id, "blind retry")
        self.store.mark_started(op, "discovered-native-turn")
        self.store.settle(op, evidence="observed runtime completion and fenced workspace")
        self.assertEqual(self.store.status(self.project)["reserved_tokens"], 0)

    def test_dispatch_is_atomic_under_concurrent_requests(self):
        def attempt(_):
            try:
                return self.store.dispatch(self.master, self.a.actor_id, "Run")
            except ConflictError:
                return None
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(attempt, range(5)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(self.store.status(self.project)["reserved_tokens"], 100)

    def test_max_parallel_includes_unknown_owned_operations(self):
        spec = ProjectSpec(str(self.project_root), "Deliver", self.spec.modules, 1000, max_parallel=1)
        project = self.store.create_project(spec)
        master = self.store.bind(project + ":master", "m2", "m2")
        a = self.store.bind(project + ":module:a", "a2", "a2")
        b = self.store.bind(project + ":module:b", "b2", "b2")
        op = self.store.dispatch(master, a.actor_id, "Run")
        self.store.mark_unknown(op, "unknown external writer")
        with self.assertRaises(ConflictError):
            self.store.dispatch(master, b.actor_id, "No slot")

    def test_master_review_correction_returns_to_same_owner_context(self):
        self.submission(self.a, "first", passed=False)
        self.store.review(self.master, "first", "request_changes", "Fix the missing behavior", verified_tree_hash="tree")
        self.assertEqual(self.store.actor(self.a.actor_id)["thread_id"], "a-thread")
        message = self.store.pending_messages(self.a.actor_id)[0]
        self.assertEqual(message["kind"], "correction")
        with self.assertRaises(AuthorityError):
            self.store.acknowledge(self.b, message["id"])
        self.store.acknowledge(self.a, message["id"])
        self.submission(self.a, "second")
        self.store.review(self.master, "second", "approve", "Verified corrected implementation", verified_tree_hash="tree")
        module = next(m for m in self.store.modules(self.project) if m["key"] == "a")
        self.assertEqual(module["owner"], self.a.actor_id)
        self.assertEqual(module["accepted_candidate"], "second")

    def test_claimed_pass_cannot_override_failed_or_missing_evidence(self):
        self.submission(self.a, "failed", passed=False)
        with self.assertRaises(ConflictError):
            self.store.review(self.master, "failed", "approve", "The agent says PASS", verified_tree_hash="tree")
        with self.assertRaises(ConflictError):
            self.store.review(self.master, "failed", "request_changes", "Needs fix", verified_tree_hash="new-tree")

    def test_revision_budget_is_finite_and_does_not_reset_context(self):
        for n in range(3):
            candidate = "attempt" + str(n)
            self.submission(self.a, candidate, passed=False)
            self.store.review(self.master, candidate, "request_changes", "Still incomplete", verified_tree_hash="tree")
        module = next(m for m in self.store.modules(self.project) if m["key"] == "a")
        self.assertEqual(module["state"], "blocked")
        self.assertEqual(self.store.actor(self.a.actor_id)["thread_id"], "a-thread")
        with self.assertRaises(ConflictError):
            self.store.dispatch(self.master, self.a.actor_id, "reset by renaming task")

    def test_usage_survives_reopen_and_observed_counter_reset(self):
        self.assertEqual(self.store.record_usage(self.a.actor_id, 60), 60)
        self.assertEqual(self.store.record_usage(self.a.actor_id, 60), 0)
        self.store = Store(self.store.path)
        self.assertEqual(self.store.record_usage(self.a.actor_id, 70), 10)
        self.assertEqual(self.store.record_usage(self.a.actor_id, 5), 5)
        self.assertEqual(self.store.status(self.project)["spent_tokens"], 75)

    def test_cancel_is_not_settlement_and_rejects_new_control(self):
        op = self.store.dispatch(self.master, self.a.actor_id, "Run")
        self.store.cancel_project(self.master)
        self.assertEqual(self.store.operation(op)["status"], "stopping")
        self.assertEqual(self.store.status(self.project)["reserved_tokens"], 100)
        with self.assertRaises(AuthorityError):
            self.store.dispatch(self.master, self.b.actor_id, "Continue despite cancelled project")
        self.store.settle(op, state="cancelled", evidence="owned native runtime actually exited")
        self.assertEqual(self.store.status(self.project)["reserved_tokens"], 0)

    def test_dependencies_and_stale_input_acceptance(self):
        spec = ProjectSpec(str(self.project_root), "Deliver", (
            ModuleSpec("x", "Upstream", ("x.py",), token_reservation=100),
            ModuleSpec("y", "Downstream", ("y.py",), ("x",), 100)), 1000)
        project = self.store.create_project(spec)
        master = self.store.bind(project + ":master", "md", "md")
        x = self.store.bind(project + ":module:x", "xd", "xd")
        y = self.store.bind(project + ":module:y", "yd", "yd")
        with self.assertRaises(ConflictError):
            self.store.dispatch(master, y.actor_id, "Too early")
        op = self.store.dispatch(master, x.actor_id, "Upstream")
        self.store.settle(op, evidence="fenced completed work")
        self.store.submit(x, "x-candidate", tree_hash="tx", snapshot="sx", inputs={}, validation=[{"status":"passed"}], summary="x", operation_id=op)
        self.store.review(master, "x-candidate", "approve", "Verified", verified_tree_hash="tx")
        op = self.store.dispatch(master, y.actor_id, "Use accepted upstream")
        inputs = json.loads(self.store.operation(op)["payload"])["inputs"]
        self.store.invalidate_module(master, "x", "Interface changed")
        self.store.settle(op, evidence="old downstream work stopped/fenced")
        with self.assertRaises(ConflictError):
            self.store.submit(y, "stale", tree_hash="ty", snapshot="sy", inputs=inputs, validation=[{"status":"passed"}], summary="y", operation_id=op)

    def test_root_envelope_rejects_overlap_cycles_and_protected_policy(self):
        with self.assertRaises(ContractError):
            ModuleSpec("bad", "Change policy", (".gitignore",))
        with self.assertRaises(ContractError):
            ProjectSpec(str(self.project_root), "Deliver", (
                ModuleSpec("x", "x", ("src",)), ModuleSpec("y", "y", ("src/y.py",))), 100)
        with self.assertRaises(ContractError):
            ProjectSpec(str(self.project_root), "Deliver", (
                ModuleSpec("x", "x", ("x.py",), ("y",)), ModuleSpec("y", "y", ("y.py",), ("x",))), 100)
        with self.assertRaises(ContractError):
            ProjectSpec(str(self.project_root), "Deliver", self.spec.modules, 100, forbidden_effects=())


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "workspace"
        self.work.mkdir()
        (self.work / "src").mkdir()
        (self.work / "src" / "code.py").write_text("answer = 42\n")

    def test_immutable_bytes_not_mutable_reference_are_reviewed(self):
        artifact = capture(self.work, self.root / "snapshots", ("src",), bindings={"revision":1})
        (self.work / "src" / "code.py").write_text("answer = -1\n")
        verify(Path(artifact["path"]), ("src",), expected_hash=artifact["tree_hash"])
        self.assertEqual((Path(artifact["path"]) / "src" / "code.py").read_text(), "answer = 42\n")
        target = Path(artifact["path"]) / "src" / "code.py"
        target.chmod(0o644)
        target.write_text("tampered\n")
        with self.assertRaises(ConflictError):
            verify(Path(artifact["path"]), ("src",), expected_hash=artifact["tree_hash"])

    def test_ungranted_files_and_indirect_symlinks_are_rejected(self):
        (self.work / "other.py").write_text("outside = 1")
        with self.assertRaises(ContractError):
            capture(self.work, self.root / "snapshots", ("src",), bindings={})
        (self.work / "other.py").unlink()
        secret = self.root / "private.py"
        secret.write_text("protected")
        (self.work / "src" / "escape.py").symlink_to(secret)
        with self.assertRaises(OSError):
            capture(self.work, self.root / "snapshots", ("src",), bindings={})

    def test_protected_git_target_cannot_be_hidden_in_candidate_namespace(self):
        (self.work / "src" / ".git").mkdir()
        (self.work / "src" / ".git" / "config").write_text("changed")
        with self.assertRaises(ContractError):
            capture(self.work, self.root / "snapshots", ("src",), bindings={})

    def test_deletion_is_bound_to_owned_baseline_and_survives_sealing(self):
        (self.work / "src" / "code.py").unlink()
        artifact = capture(self.work, self.root / "snapshots", ("src",), bindings={"revision": 1},
                           baseline_files=("src/code.py",))
        manifest = verify(Path(artifact["path"]), ("src",), expected_hash=artifact["tree_hash"])
        self.assertEqual(manifest["deleted"], ["src/code.py"])
        self.assertEqual(manifest["files"], [])
        with self.assertRaises(ContractError):
            capture(self.work, self.root / "snapshots", ("src",), bindings={}, baseline_files=("outside.py",))

    def test_coherent_project_context_is_preserved_but_not_submitted_as_owned_code(self):
        (self.work/'readonly.py').write_text('protected = True\n')
        inputs=context_hashes(self.work,('src',))
        artifact=capture(self.work,self.root/'snapshots',('src',),bindings={},context_inputs=inputs)
        self.assertEqual([x['path'] for x in artifact['manifest']['files']],['src/code.py'])
        (self.work/'readonly.py').write_text('shortcut = True\n')
        with self.assertRaises(ConflictError):
            capture(self.work,self.root/'snapshots',('src',),bindings={},context_inputs=inputs)
