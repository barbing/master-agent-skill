"""Readiness checks for this machine; no model turn, no project, nothing installed.

`doctor` exercises the platform-specific mechanisms a run depends on against
the CLIs actually installed: the project lease, ending an owned process tree,
the Codex app-server transport, the native sandbox contract, the write and
read conformance probes for check- and owner-shaped profiles, and one
sandboxed check. Its probe evidence stays under STATE/doctor/ for inspection.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from . import oscompat
from .contracts import ConflictError, RuntimeErrorBase, ValidationStep
from .lease import project_lease

# The child waits for the host's go-ahead, so the grandchild starts only
# after the child is inside its group.
CHILD_TREE = '''import subprocess,sys,time
sys.stdin.readline()
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
print(child.pid,flush=True)
time.sleep(120)
'''
CHECK = '''import pathlib,sys
print('sandboxed check read', pathlib.Path(sys.argv[1], 'marker.txt').read_text(encoding='utf-8'))
'''


def _alive(pid: int) -> bool:
    if oscompat.WINDOWS:
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                                encoding="utf-8", errors="replace", timeout=30)
        return f'"{pid}"' in result.stdout
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_process_tree() -> dict:
    """Start a child that starts a grandchild, end the group, and confirm both are gone."""
    process = subprocess.Popen([sys.executable, "-c", CHILD_TREE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               encoding="utf-8", **oscompat.process_group_options())
    group = oscompat.ProcessGroup(process.pid)
    try:
        process.stdin.write("go\n")
        process.stdin.flush()
        grandchild = int(process.stdout.readline().strip() or 0)
        group.signal(force=True)
        process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while grandchild and _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        survivor = bool(grandchild) and _alive(grandchild)
    finally:
        group.reap()
        if process.poll() is None:
            process.kill()
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass
    return {"status": "failed" if survivor or not grandchild else "passed", "contained": group.contained,
            "detail": ("the child and its grandchild ended together" if not survivor and grandchild
                       else "a grandchild outlived its group" if survivor else "the child did not start a grandchild")}


def check_lease(root: Path) -> dict:
    path = root / "controller.lock"
    with project_lease(path):
        try:
            with project_lease(path):
                return {"status": "failed", "detail": "a second holder took the lease"}
        except ConflictError:
            pass
    with project_lease(path):
        pass
    return {"status": "passed", "detail": "exclusive, refused a second holder and was released"}


async def check_app_server(cli: str) -> dict:
    from .native import JsonRpcClient
    client = JsonRpcClient((cli, "app-server", "--stdio"), timeout=30)
    try:
        await client.start()
        info = await client.initialize()
        config = (await client.call("config/read", {"includeLayers": False})).get("config", {})
    finally:
        shutdown = await client.close()
    platform = {key: info.get(key) for key in ("platformOs", "platformFamily") if isinstance(info, dict)}
    return {"status": "passed" if shutdown.get("process_exited") else "failed",
            "detail": "initialized and read its configuration; the owned service exited on close",
            "native": platform, "configured_model": config.get("model"),
            "windows_sandbox": (config.get("windows") or {}).get("sandbox") if oscompat.WINDOWS else None,
            "effect_coverage": shutdown.get("effect_coverage")}


def check_codex(cli: str, run: Path) -> list[tuple[str, dict]]:
    from .native import permission_config
    from .permissions import filesystem_profile
    from .sandbox import check_scope, sandbox_prefix
    from .validation import validate
    results = []
    version = subprocess.run([cli, "--version"], encoding="utf-8", errors="replace", capture_output=True, timeout=30)
    results.append(("codex version", {"status": "passed" if version.returncode == 0 else "failed",
                                      "detail": (version.stdout or version.stderr).strip()[:200], "cli": cli}))
    try:
        results.append(("codex app-server", asyncio.run(check_app_server(cli))))
    except (RuntimeErrorBase, OSError, asyncio.TimeoutError) as error:
        results.append(("codex app-server", {"status": "failed", "detail": f"{type(error).__name__}: {error}"}))
    try:
        prefix = sandbox_prefix(cli)
    except (RuntimeErrorBase, OSError, subprocess.SubprocessError) as error:
        results.append(("codex sandbox contract", {"status": "failed", "detail": str(error)}))
        return results
    results.append(("codex sandbox contract", {"status": "passed", "detail": " ".join(prefix[1:])}))
    evidence = run / "evidence"
    # A check-shaped profile: write its own scratch, read nothing else.
    scratch = run / "check" / "scratch"
    scratch.mkdir(parents=True)
    probes = [("check profile probe", lambda: check_scope(
        cli, "runtime-validator", filesystem_profile(scratch, (), writable=True), scratch,
        evidence / "check-profile.json", writable=True, protected=scratch.parent))]
    # An owner-shaped profile: the control root denied, the owner's workspace
    # and the public view re-opened inside it, as for a real module owner.
    control = run / "control"
    workspace, public = control / "workspaces" / "owner" / uuid.uuid4().hex, control / "public"
    workspace.mkdir(parents=True)
    public.mkdir(parents=True)
    name, config = permission_config(workspace, public, control, cli=cli)
    probes.append(("owner profile probe", lambda: check_scope(
        cli, name, config["permissions"][name], workspace / ".tmp", evidence / "owner-profile.json",
        writable=True, protected=control)))
    for label, probe in probes:
        try:
            report = probe()
            results.append((label, {"status": "passed", "detail": _effects(report), "evidence": str(_evidence(label, evidence))}))
        except (RuntimeErrorBase, OSError, subprocess.SubprocessError) as error:
            results.append((label, {"status": "failed", "detail": str(error), "observed": _observed(label, evidence)}))
    candidate, context = run / "candidate", run / "context"
    candidate.mkdir()
    context.mkdir()
    (candidate / "marker.txt").write_text("doctor", encoding="utf-8")
    (context / "check.py").write_text(CHECK, encoding="utf-8")
    step = ValidationStep((sys.executable, "{context}/check.py", "{candidate}"), 60)
    try:
        [check] = validate((step,), candidate, context, run / "check-evidence", cli=cli)
        log = Path(check["log"]).read_text(encoding="utf-8", errors="replace")
        ok = check["status"] == "passed" and "sandboxed check read doctor" in log
        results.append(("sandboxed check", {"status": "passed" if ok else "failed",
                                            "detail": log.strip().splitlines()[0] if ok else log[-2000:],
                                            "log": check["log"]}))
    except (RuntimeErrorBase, OSError, subprocess.SubprocessError) as error:
        results.append(("sandboxed check", {"status": "failed", "detail": str(error)}))
    return results


def _evidence(label, directory):
    return directory / ("check-profile.json" if label.startswith("check") else "owner-profile.json")


def _observed(label, directory):
    try:
        return json.loads(_evidence(label, directory).read_text(encoding="utf-8")).get("observations")
    except (OSError, ValueError):
        return None


def _effects(report):
    return ", ".join(f"{row.get('case')}: {row.get('effect')}" for row in report.get("observations") or [])


def check_claude(claude: str | None) -> dict:
    if not oscompat.claude_sandbox_supported():
        return {"status": "skipped", "detail": oscompat.CLAUDE_WINDOWS_REFUSAL}
    from .claude_native import MINIMUM_VERSION, parse_version
    path = claude or next((item for item in oscompat.claude_candidates() if item and Path(item).is_file()), None)
    if not path:
        return {"status": "skipped", "detail": "Claude Code is not installed; it is needed only for Claude actors"}
    result = subprocess.run([path, "--version"], encoding="utf-8", errors="replace", capture_output=True, timeout=30)
    version = parse_version(result.stdout)
    ok = result.returncode == 0 and version is not None and version >= MINIMUM_VERSION
    return {"status": "passed" if ok else "failed", "cli": path, "detail": (result.stdout or result.stderr).strip()[:200]
            + ("" if ok else f" (needs {'.'.join(map(str, MINIMUM_VERSION))} or later)")}


def run_doctor(state: Path, codex: str | None = None, claude: str | None = None) -> dict:
    state = Path(state).absolute()
    run = state / "doctor" / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    checks = []

    def record(name, outcome):
        checks.append({"check": name, **outcome})

    version_ok = sys.version_info >= (3, 12)
    record("python", {"status": "passed" if version_ok else "failed", "version": sys.version.split()[0],
                      "executable": sys.executable,
                      "detail": "3.12 or later" if version_ok else "the runtime needs Python 3.12 or later"})
    if oscompat.WINDOWS:
        if "windowsapps" in sys.executable.lower():
            record("python install", {"status": "warning", "detail": "This interpreter is a Microsoft Store app "
                   "alias, which a sandboxed check may be unable to start; prefer a python.org install."})
        enabled = oscompat.long_paths_enabled()
        record("long paths", {"status": "passed" if enabled else "warning",
                              "detail": "enabled" if enabled else
                              f"Windows limits paths to 260 characters; enable LongPathsEnabled or keep the state "
                              f"directory short (this one is {len(str(state))} characters)"})
    record("state directory", {"status": "passed", "detail": str(state)})
    record("project lease", check_lease(run))
    record("process tree", check_process_tree())
    if codex:
        cli = oscompat.native_executable(codex)
        if cli is None:
            record("codex", {"status": "failed", "detail": oscompat.shim_refusal(codex)})
    else:
        cli = next((item for item in oscompat.codex_candidates() if item and Path(item).is_file()), None)
        if cli is None:
            found = shutil.which("codex")
            record("codex", {"status": "failed", "detail": oscompat.shim_refusal(found) if found else
                             "No Codex CLI found on PATH or in the standard install locations; pass --codex PATH"})
    if cli:
        for name, outcome in check_codex(cli, run):
            record(name, outcome)
    record("claude", check_claude(claude))
    ready = all(item["status"] in ("passed", "skipped", "warning") for item in checks)
    return {"state": "ready" if ready else "not_ready", "ready": ready, "platform": oscompat.platform_name(),
            "checks": checks, "evidence": str(run),
            "scope": "machine readiness only; no model turn, project or installation; probes cover owned disposable files"}
