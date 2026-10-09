import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from master_agent_runtime import ModuleSpec, ProjectSpec, Store, ValidationStep
from master_agent_runtime.admission import describe_handoff, load_handoff
from master_agent_runtime.contracts import ContractError, FORBIDDEN_EFFECTS
from master_agent_runtime.native import JsonRpcClient


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="admission-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.spec = ProjectSpec(
            str(self.project), "Deliver admission and readonly health — 完成", (
                ModuleSpec("zeta", "Deliver the dependent module", ("pkg/zeta.py",),
                           dependencies=("alpha",), token_reservation=7000,
                           validation=(ValidationStep(("python", "{candidate}/check.py"), 120),)),
                ModuleSpec("alpha", "Deliver the first module", ("pkg/alpha.py",),
                           token_reservation=5000)),
            token_budget=54000, max_parallel=3, turn_timeout_seconds=300,
            max_revision_rounds=4, model="admitted-model", effort="high",
            forbidden_effects=tuple(sorted(FORBIDDEN_EFFECTS | {"additional_restriction"})),
            integration_validation=(ValidationStep(("python", "{context}/check.py"), 150),))
        self.path = self.root / "handoff.json"
        self.write(self.spec.to_dict())

    def write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def assert_rejected(self, data, *details):
        self.write(data)
        return self.assert_rejected_file(*details)

    def assert_rejected_file(self, *details):
        before = self.path.read_bytes()
        with self.assertRaises(ContractError) as caught:
            load_handoff(self.path)
        message = str(caught.exception).lower()
        self.assertIn("handoff", message)
        self.assertIn(str(self.path).lower(), message)
        for detail in details:
            self.assertIn(detail.lower(), message)
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertEqual(self.path.read_bytes(), before)
        return caught.exception

    def test_complete_contract_roundtrip_preserves_authority_and_resources(self):
        before = self.path.read_bytes()
        loaded = load_handoff(self.path)
        self.assertIsInstance(loaded, ProjectSpec)
        self.assertEqual(loaded, self.spec)
        self.assertEqual(loaded.fingerprint, self.spec.fingerprint)
        self.assertEqual(loaded.to_dict(), self.spec.to_dict())
        self.assertEqual(load_handoff(str(self.path)), loaded)
        self.assertEqual(self.path.read_bytes(), before)

    def test_uses_existing_factory_once_without_mutating_decoded_input(self):
        data = self.spec.to_dict()
        before = copy.deepcopy(data)
        with mock.patch("master_agent_runtime.admission.json.loads", return_value=data), \
                mock.patch.object(ProjectSpec, "from_dict", wraps=ProjectSpec.from_dict) as factory:
            loaded = load_handoff(self.path)
        factory.assert_called_once_with(data)
        self.assertEqual(data, before)
        self.assertEqual(loaded, self.spec)

    def test_minimal_handoff_uses_existing_contract_defaults(self):
        data = {"root": str(self.project), "objective": "Deliver", "token_budget": 10000,
                "modules": [{"key": "one", "objective": "Complete", "paths": ["one.py"]}]}
        self.write(data)
        loaded = load_handoff(self.path)
        self.assertEqual(loaded, ProjectSpec.from_dict(data))
        self.assertEqual(set(loaded.forbidden_effects), FORBIDDEN_EFFECTS)

    def test_summary_is_deterministic_sorted_and_preserves_resource_evidence(self):
        before = self.spec.to_dict()
        summary = describe_handoff(self.spec)
        self.assertEqual(summary["modules"], ["alpha", "zeta"])
        self.assertEqual(summary["module_count"], 2)
        self.assertEqual(summary["spec_hash"], self.spec.fingerprint)
        self.assertEqual(summary["native_calls"], 0)
        for field in ("root", "objective", "token_budget", "max_parallel",
                      "turn_timeout_seconds", "max_revision_rounds", "model", "effort"):
            self.assertEqual(summary[field], getattr(self.spec, field))
        self.assertEqual(summary["token_reservations"], {"alpha": 5000, "zeta": 7000})
        self.assertEqual(summary["forbidden_effects"], list(self.spec.forbidden_effects))
        self.assertEqual(describe_handoff(self.spec), summary)
        self.assertEqual(self.spec.to_dict(), before)
        self.assertEqual(json.loads(json.dumps(summary)), summary)

    def test_json_formatting_and_field_order_do_not_change_identity(self):
        first = describe_handoff(load_handoff(self.path))
        self.path.write_text(json.dumps(self.spec.to_dict(), indent=4, sort_keys=True),
                             encoding="utf-8")
        self.assertEqual(describe_handoff(load_handoff(self.path)), first)

    def test_summary_containers_are_detached_from_spec_and_later_results(self):
        expected = describe_handoff(self.spec)
        modified = describe_handoff(self.spec)
        modified["modules"].append("invented")
        modified["token_reservations"]["alpha"] = 0
        modified["forbidden_effects"].clear()
        self.assertEqual(describe_handoff(self.spec), expected)

    def test_summary_does_not_reinspect_root_or_read_handoff(self):
        with mock.patch.object(Path, "stat", side_effect=AssertionError("filesystem inspection")), \
                mock.patch.object(Path, "read_text", side_effect=AssertionError("file read")):
            summary = describe_handoff(self.spec)
        self.assertEqual(summary["spec_hash"], self.spec.fingerprint)

    def test_no_filesystem_writes_native_calls_or_runtime_admission(self):
        (self.project / "preserved.txt").write_text("existing project evidence", encoding="utf-8")

        def snapshot():
            return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in self.root.rglob("*") if path.is_file()}

        before = snapshot()
        original_open = io.open

        def readonly_open(file, mode="r", *args, **kwargs):
            self.assertFalse(set(mode) & set("wax+"), f"write attempted: {mode}")
            return original_open(file, mode, *args, **kwargs)

        with mock.patch("io.open", side_effect=readonly_open), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("directory write")), \
                mock.patch.object(Store, "create_project") as create, \
                mock.patch.object(JsonRpcClient, "start") as start, \
                mock.patch.object(JsonRpcClient, "call") as call, \
                mock.patch("subprocess.Popen") as process, \
                mock.patch("asyncio.create_subprocess_exec") as async_process:
            first = describe_handoff(load_handoff(self.path))
            self.assertEqual(describe_handoff(load_handoff(self.path)), first)
        for operation in (create, start, call, process, async_process):
            operation.assert_not_called()
        self.assertEqual(snapshot(), before)

    def test_non_object_json_is_rejected(self):
        for data in ([], None, True, 10, "text"):
            with self.subTest(data=data):
                self.assert_rejected(data, "JSON object")

    def test_invalid_json_retains_parser_failure_location(self):
        for text in ("{invalid", "", '{"root":}', "{} trailing", "\ufeff{}"):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                error = self.assert_rejected_file("line", "column")
                self.assertIsInstance(error.__cause__, json.JSONDecodeError)

    def test_invalid_encoding_is_an_actionable_handoff_error(self):
        self.path.write_bytes(b"\xff")
        self.assert_rejected_file("utf-8", "decode")

    def test_missing_file_directory_and_denied_read_are_wrapped(self):
        for path, detail in ((self.root / "missing.json", "No such file"),
                             (self.project, "directory")):
            with self.subTest(path=path), self.assertRaises(ContractError) as caught:
                load_handoff(path)
            self.assertIn("handoff", str(caught.exception).lower())
            self.assertIn(detail.lower(), str(caught.exception).lower())
            self.assertIn(str(path), str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, OSError)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("permission denied")):
            self.assert_rejected_file("permission denied")
        self.assertFalse((self.root / "missing.json").exists())

    def test_missing_required_fields_at_every_contract_level_are_named(self):
        cases = [((), name) for name in ("root", "objective", "modules", "token_budget")]
        cases += [(("modules", 0), name) for name in ("key", "objective", "paths")]
        cases += [(("modules", 0, "validation", 0), "argv"),
                  (("integration_validation", 0), "argv")]
        for location, field in cases:
            data = copy.deepcopy(self.spec.to_dict())
            target = data
            for part in location:
                target = target[part]
            del target[field]
            with self.subTest(location=location, field=field):
                self.assert_rejected(data, "missing required", field)

    def test_unknown_fields_including_nested_validation_are_rejected(self):
        for location, detail in (((), "handoff"), (("modules", 0), "modules[0]"),
                                 (("modules", 0, "validation", 0), "modules[0].validation[0]"),
                                 (("integration_validation", 0), "integration_validation[0]")):
            data = copy.deepcopy(self.spec.to_dict())
            target = data
            for part in location:
                target = target[part]
            target["unrecognized"] = True
            with self.subTest(location=location):
                self.assert_rejected(data, "unknown", "unrecognized", detail)

    def test_malformed_nested_objects_and_types_do_not_leak_builtin_errors(self):
        cases = (("objective", None), ("token_budget", "many"), ("modules", None),
                 ("modules", [None]), ("integration_validation", [None]))
        for field, value in cases:
            data = self.spec.to_dict()
            data[field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected(data)
        for field, value in (("objective", None), ("paths", None), ("validation", [None]),
                             ("dependencies", None), ("token_reservation", "many")):
            data = self.spec.to_dict()
            data["modules"][0][field] = value
            with self.subTest(module_field=field):
                self.assert_rejected(data)

    def test_invalid_roots_preserve_contract_failure(self):
        link = self.root / "linked-project"
        link.symlink_to(self.project, target_is_directory=True)
        for root in ("relative", str(self.root / "absent"), str(self.path), str(link)):
            data = self.spec.to_dict()
            data["root"] = root
            with self.subTest(root=root):
                self.assert_rejected(data, "root", "existing absolute real directory")

    def test_objectives_modules_and_resource_bounds_remain_enforced(self):
        cases = (("objective", " ", "objective"), ("modules", [], "modules"),
                 ("token_budget", 0, "resource"), ("token_budget", -1, "resource"),
                 ("max_parallel", 0, "parallelism"), ("max_parallel", 17, "parallelism"),
                 ("turn_timeout_seconds", 0, "deadline"),
                 ("turn_timeout_seconds", 3601, "deadline"),
                 ("max_revision_rounds", -1, "revision"),
                 ("max_revision_rounds", 11, "revision"))
        for field, value, failure in cases:
            data = self.spec.to_dict()
            data[field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected(data, failure)
        for field, value, failure in (("key", "bad key", "keys"),
                                      ("objective", " ", "objective"),
                                      ("paths", [], "paths"),
                                      ("token_reservation", 0, "reservation")):
            data = self.spec.to_dict()
            data["modules"][0][field] = value
            with self.subTest(module_field=field):
                self.assert_rejected(data, failure)

    def test_validation_contracts_remain_enforced_at_both_levels(self):
        for location in (("modules", 0, "validation"), ("integration_validation",)):
            for step, failure in (({"argv": []}, "argv"),
                                  ({"argv": [""]}, "argv"),
                                  ({"argv": [1]}, "argv"),
                                  ({"argv": ["python"], "timeout_seconds": 0}, "timeout"),
                                  ({"argv": ["python"], "timeout_seconds": 601}, "timeout")):
                data = self.spec.to_dict()
                target = data
                for part in location[:-1]:
                    target = target[part]
                target[location[-1]] = [step]
                with self.subTest(location=location, step=step):
                    self.assert_rejected(data, failure)

    def test_protected_absolute_and_non_normalized_paths_remain_rejected(self):
        for path in ("/outside.py", "../escape.py", "pkg\\escape.py", "pkg/../escape.py",
                     "./code.py", "pkg//code.py", ".git/config", ".gitignore",
                     ".codex/config.toml", ".agents/control.json", ".aws/config"):
            data = self.spec.to_dict()
            data["modules"][0]["paths"] = [path]
            with self.subTest(path=path):
                self.assert_rejected(data, "path")
        data = self.spec.to_dict()
        data["modules"][0]["paths"] = ["pkg/zeta.py", "pkg/zeta.py"]
        self.assert_rejected(data, "paths", "unique")

    def test_duplicate_module_keys_overlap_and_dependency_violations_are_rejected(self):
        for violation in ("duplicate", "overlap", "unknown", "self", "cycle"):
            data = self.spec.to_dict()
            if violation == "duplicate":
                data["modules"][0]["key"] = "alpha"
                failure = "unique"
            elif violation == "overlap":
                data["modules"][1]["paths"] = ["pkg"]
                failure = "overlap"
            elif violation in ("unknown", "self"):
                data["modules"][0]["dependencies"] = ["absent" if violation == "unknown" else "zeta"]
                failure = "dependency"
            else:
                data["modules"][1]["dependencies"] = ["zeta"]
                failure = "cycle"
            with self.subTest(violation=violation):
                self.assert_rejected(data, failure)

    def test_no_prohibited_effect_can_be_removed_via_handoff(self):
        data = self.spec.to_dict()
        data["forbidden_effects"] = []
        self.assert_rejected(data, "irreversible/external effects")
        for effect in sorted(FORBIDDEN_EFFECTS):
            data = self.spec.to_dict()
            data["forbidden_effects"] = sorted(FORBIDDEN_EFFECTS - {effect})
            with self.subTest(effect=effect):
                self.assert_rejected(data, "irreversible/external effects")

    def test_non_finite_json_numbers_and_numeric_overflow_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            data = self.spec.to_dict()
            data["token_budget"] = value
            with self.subTest(value=value):
                self.assert_rejected(data, "non-finite JSON number")
        text = json.dumps(self.spec.to_dict()).replace('"token_budget": 54000', '"token_budget": 1e999')
        self.path.write_text(text, encoding="utf-8")
        self.assert_rejected_file("JSON", "range")

    def test_invalid_unicode_is_wrapped_before_an_unhashable_contract_is_returned(self):
        data = self.spec.to_dict()
        data["objective"] = "\ud800"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        error = self.assert_rejected_file("encode")
        self.assertIsInstance(error.__cause__, UnicodeEncodeError)

    def test_excessively_nested_json_decoder_errors_are_wrapped(self):
        self.path.write_text("[" * 10000 + "0" + "]" * 10000, encoding="utf-8")
        decoder_error = RecursionError("maximum recursion depth exceeded while decoding JSON")
        with mock.patch("master_agent_runtime.admission.json.loads", side_effect=decoder_error):
            error = self.assert_rejected_file("recursion")
        self.assertIsInstance(error.__cause__, RecursionError)
        self.assertIs(error.__cause__, decoder_error)

    def test_deep_non_object_json_is_rejected_before_contract_or_native_work(self):
        self.path.write_text("[" * 10000 + "0" + "]" * 10000, encoding="utf-8")
        with mock.patch.object(ProjectSpec, "from_dict") as factory, \
                mock.patch.object(Store, "create_project") as create, \
                mock.patch.object(JsonRpcClient, "start") as start:
            error = self.assert_rejected_file()
        self.assertIsInstance(error.__cause__, (RecursionError, ContractError))
        for operation in (factory, create, start):
            operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
