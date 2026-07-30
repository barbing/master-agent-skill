#!/usr/bin/env python3
"""Run release-readiness checks for the Master Agent System skill pack."""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".txt"}
PERSONAL_PATH_RE = re.compile(r"\b[A-Za-z]:\\Users\\[^\\\s]+")
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|password|private[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"
    r"|sk-[A-Za-z0-9]{20,}"
)


def run_step(name: str, command: list[str], cwd: Path = ROOT) -> tuple[str, int, str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=240,
    )
    detail = (result.stdout + result.stderr).strip()
    return name, result.returncode, detail


def command_is_available(command: str) -> bool:
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return False
    if not argv:
        return False
    executable = argv[0].strip('"')
    return shutil.which(executable) is not None or Path(executable).exists()


def plugin_eval_step(command: str, require: bool) -> tuple[str, int, str]:
    if "{skill}" in command:
        rendered = command.replace("{skill}", str(ROOT))
    else:
        rendered = f"{command} {ROOT}"
    if not command_is_available(rendered):
        if require:
            return "plugin-eval", 1, f"plugin-eval command unavailable: {command}"
        return "plugin-eval", 0, f"skipped: plugin-eval command unavailable: {command}"
    try:
        argv = shlex.split(rendered, posix=os.name != "nt")
    except ValueError as exc:
        return "plugin-eval", 1, f"plugin-eval command could not be parsed: {exc}"
    return run_step("plugin-eval", argv)


def iter_release_files(root: Path) -> list[Path]:
    ignored_parts = {".git", "__pycache__", ".pytest_cache"}
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in ignored_parts for part in path.parts):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        files.append(path)
    return sorted(files)


def scan_text_files(root: Path) -> tuple[list[str], list[str]]:
    personal_hits: list[str] = []
    secret_hits: list[str] = []
    for path in iter_release_files(root):
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root).as_posix()
        for pattern, hits in ((PERSONAL_PATH_RE, personal_hits), (SECRET_RE, secret_hits)):
            for match in pattern.finditer(text):
                hits.append(f"{rel}: {match.group(0)[:80]}")
    return personal_hits, secret_hits


def compare_installed_copy(installed: Path) -> list[str]:
    mismatches: list[str] = []
    source_files = {
        path.relative_to(ROOT).as_posix(): path
        for path in iter_release_files(ROOT)
        if path.relative_to(ROOT).parts[0] in {"SKILL.md", "agents", "assets", "references", "role-skills", "scripts"}
    }
    installed_files = {
        path.relative_to(installed).as_posix(): path
        for path in iter_release_files(installed)
        if path.relative_to(installed).parts[0] in {"SKILL.md", "agents", "assets", "references", "role-skills", "scripts"}
    }
    for rel in sorted(set(source_files) | set(installed_files)):
        if rel not in source_files:
            mismatches.append(f"extra installed file: {rel}")
        elif rel not in installed_files:
            mismatches.append(f"missing installed file: {rel}")
        elif not filecmp.cmp(source_files[rel], installed_files[rel], shallow=False):
            mismatches.append(f"installed file differs: {rel}")
    return mismatches


def worktree_control_step() -> tuple[str, int, str]:
    required_files = [
        ROOT / "assets" / "templates" / "worktree-control.md",
        ROOT / "scripts" / "master_agent_tool.py",
        ROOT / "scripts" / "validate_state_pack.py",
        ROOT / "references" / "master-agent-system.md",
        ROOT / "README.md",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_files if not path.exists()]
    if missing:
        return "worktree control surface", 1, "missing files: " + ", ".join(missing)
    tool_text = (ROOT / "scripts" / "master_agent_tool.py").read_text(encoding="utf-8")
    docs_text = "\n".join(
        [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "SKILL.md").read_text(encoding="utf-8"),
            (ROOT / "references" / "master-agent-system.md").read_text(encoding="utf-8"),
            (ROOT / "assets" / "templates" / "worktree-control.md").read_text(encoding="utf-8"),
        ]
    )
    required_terms = [
        "worktree-plan",
        "worktree-confirm-create",
        "worktree-assign-session",
        "worktree-reconcile",
        "worktree-close",
        "worktree-confirm-close",
        "validate-worktreeinclude",
        "state/worktrees.jsonl",
        ".worktreeinclude",
    ]
    missing_terms = [
        term
        for term in required_terms
        if term not in tool_text and term not in docs_text
    ]
    if missing_terms:
        return "worktree control surface", 1, "missing terms: " + ", ".join(missing_terms)
    return "worktree control surface", 0, ""


def governance_control_step() -> tuple[str, int, str]:
    required_files = [
        ROOT / "assets" / "templates" / "authority-envelope.md",
        ROOT / "assets" / "templates" / "obstacle-recovery-packet.md",
        ROOT / "assets" / "templates" / "acceptance-gate.md",
        ROOT / "assets" / "templates" / "guard-obligation.md",
        ROOT / "assets" / "templates" / "implementation-guard-adapter.md",
        ROOT / "scripts" / "master_agent_tool.py",
        ROOT / "references" / "master-agent-system.md",
        ROOT / "README.md",
        ROOT / "SKILL.md",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_files if not path.exists()]
    if missing:
        return "governance control surface", 1, "missing files: " + ", ".join(missing)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in required_files)
    required_terms = [
        "governance-lint",
        "record-authority-required",
        "record-governance-status",
        "record-acceptance-gate",
        "state/governance-events.jsonl",
        "state/acceptance-gates.jsonl",
        "authority_required",
        "authorization_invalid",
        "in_root_transition_required",
        "external_mutation_domain_identified",
        "validation_support_required",
        "loop_type",
        "diagnostic",
        "live_seam_green",
        "production_accepted",
    ]
    missing_terms = [term for term in required_terms if term not in combined]
    if missing_terms:
        return "governance control surface", 1, "missing terms: " + ", ".join(missing_terms)
    return "governance control surface", 0, ""


def round_log_control_step() -> tuple[str, int, str]:
    required_files = [
        ROOT / "assets" / "templates" / "round-log-control.md",
        ROOT / "assets" / "templates" / "coding-receipt.md",
        ROOT / "assets" / "templates" / "work-order.md",
        ROOT / "scripts" / "master_agent_tool.py",
        ROOT / "references" / "master-agent-system.md",
        ROOT / "README.md",
        ROOT / "SKILL.md",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_files if not path.exists()]
    if missing:
        return "round-log control surface", 1, "missing files: " + ", ".join(missing)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in required_files)
    required_terms = [
        "round-log-status",
        "record-round-log-evidence",
        "require-round-log-evidence",
        "round-log-export",
        "state/round-log-events.jsonl",
        "round-log-control.md",
        "Round Log Evidence",
        ".codex-round-log",
        "Snapshot id",
        "Manifest path",
    ]
    missing_terms = [term for term in required_terms if term not in combined]
    if missing_terms:
        return "round-log control surface", 1, "missing terms: " + ", ".join(missing_terms)
    return "round-log control surface", 0, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Master Agent System release readiness.")
    parser.add_argument("--quick-validate", help="Path to Codex skill quick_validate.py")
    parser.add_argument("--installed-skill-dir", help="Optional installed skill copy to compare")
    parser.add_argument(
        "--plugin-eval-command",
        default="plugin-eval evaluate-skill {skill}",
        help="Optional plugin-eval command. Use {skill} as the skill path placeholder.",
    )
    parser.add_argument("--require-plugin-eval", action="store_true")
    args = parser.parse_args()

    checks: list[tuple[str, int, str]] = []
    python_files = [
        ROOT / "scripts" / "bootstrap_project_state.py",
        ROOT / "scripts" / "file_session_provider.py",
        ROOT / "scripts" / "validate_state_pack.py",
        ROOT / "scripts" / "master_agent_tool.py",
        ROOT / "scripts" / "state_io.py",
        ROOT / "scripts" / "release_validate.py",
        ROOT / "scripts" / "soak_validate.py",
        ROOT / "tests" / "test_master_agent_system.py",
    ]
    skip_core = os.environ.get("MASTER_AGENT_RELEASE_VALIDATE_SKIP_CORE") == "1"
    if skip_core:
        checks.append(("core command checks", 0, "skipped by MASTER_AGENT_RELEASE_VALIDATE_SKIP_CORE"))
    else:
        checks.append(run_step("unit tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]))
        checks.append(run_step("py_compile", [sys.executable, "-m", "py_compile", *map(str, python_files)]))
        checks.append(run_step("template state pack", [sys.executable, "scripts/validate_state_pack.py", "assets/templates"]))
        checks.append(run_step("operating-system soak", [sys.executable, "scripts/soak_validate.py", "--quick"]))
        checks.append(worktree_control_step())
        checks.append(governance_control_step())
        checks.append(round_log_control_step())

        with tempfile.TemporaryDirectory(prefix="master-agent-release-") as tmp:
            tmp_path = Path(tmp)
            checks.append(run_step("fresh bootstrap", [sys.executable, "scripts/bootstrap_project_state.py", "--project-root", str(tmp_path)]))
            checks.append(
                run_step(
                    "fresh bootstrap validate",
                    [sys.executable, "scripts/validate_state_pack.py", str(tmp_path / "docs" / "master-agent")],
                )
            )

    quick_validate = Path(args.quick_validate).resolve() if args.quick_validate else None
    if quick_validate:
        checks.append(run_step("root skill quick_validate", [sys.executable, str(quick_validate), str(ROOT)]))
        for skill in sorted((ROOT / "role-skills").glob("*/SKILL.md")):
            checks.append(
                run_step(
                    f"{skill.parent.name} quick_validate",
                    [sys.executable, str(quick_validate), str(skill.parent)],
                )
            )
    else:
        checks.append(("quick_validate", 0, "skipped: pass --quick-validate to run Codex skill validation"))

    personal_hits, secret_hits = scan_text_files(ROOT)
    checks.append(("personal path scan", 1 if personal_hits else 0, "\n".join(personal_hits)))
    checks.append(("secret scan", 1 if secret_hits else 0, "\n".join(secret_hits)))

    if args.installed_skill_dir:
        installed = Path(args.installed_skill_dir).resolve()
        if not installed.exists():
            checks.append(("installed copy comparison", 1, f"missing installed skill dir: {installed}"))
        else:
            mismatches = compare_installed_copy(installed)
            checks.append(("installed copy comparison", 1 if mismatches else 0, "\n".join(mismatches)))

    checks.append(plugin_eval_step(args.plugin_eval_command, args.require_plugin_eval))

    failed = [(name, detail) for name, code, detail in checks if code != 0]
    print("Release validation summary")
    for name, code, detail in checks:
        label = "PASS" if code == 0 else "FAIL"
        print(f"- {label}: {name}")
        if detail and (code != 0 or detail.startswith("skipped")):
            print(detail)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
