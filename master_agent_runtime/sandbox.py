"""Native command contract and effect conformance; no unsandboxed fallback."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from .contracts import ContractError
from . import oscompat
from .oscompat import minimal_environment, outside_temp_parent, platform_name

def toml_literal(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(k) + " = " + toml_literal(v) for k, v in value.items()) + " }"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    return json.dumps(str(value))


@lru_cache(maxsize=8)
def sandbox_prefix(cli):
    """Use the installed CLI contract; old and new builds differ here."""
    result = subprocess.run([cli, "help", "sandbox"], encoding="utf-8", errors="replace", capture_output=True,
                            timeout=10)
    help_text = result.stdout
    if result.returncode != 0:
        raise ContractError("Unable to verify installed native sandbox command")
    if "<COMMAND>" in help_text and "macos" in help_text:
        platform = platform_name()
        if platform not in help_text:
            raise ContractError(f"Installed CLI has no {platform} sandbox command")
        prefix = [cli, "sandbox", platform]
        detail = subprocess.run([cli, "help", "sandbox", platform], encoding="utf-8", errors="replace",
                                capture_output=True, timeout=10)
        if detail.returncode:
            raise ContractError("Installed CLI cannot describe its platform sandbox")
        help_text = detail.stdout
    elif "[COMMAND]..." in help_text:
        prefix = [cli, "sandbox"]
    else:
        raise ContractError("Unsupported installed sandbox CLI contract")
    for option in ("--permission-profile", "--permissions-profile"):
        if option in help_text:
            if "--include-managed-config" not in help_text:
                raise ContractError("Native sandbox cannot confirm managed requirements for a named profile")
            return tuple(prefix + ["--include-managed-config",option, "runtime-validator"])
    raise ContractError("Native validator requires an explicit named permission selector")


# The probe runs from a file beside the workspace rather than from -c: its
# lines then reach the sandboxed interpreter intact, whatever launcher the
# platform's sandbox puts in between. A write must leave a file; a read must
# return the host's sentinel bytes; anything else is not an established effect.
PROBE = '''import json,pathlib,sys
cases=('own_workspace_write','outside_temp_write','outside_temp_read','protected_read')
rows=[]
for name,path in zip(cases,sys.argv[1:]):
    target=pathlib.Path(path)
    try:
        if name.endswith('_write'):
            target.write_text('owned disposable conformance fixture',encoding='utf-8');rows.append({'case':name,'effect':'succeeded'})
        else:
            rows.append({'case':name,'effect':'succeeded','bytes':len(target.read_bytes())})
    except PermissionError:rows.append({'case':name,'effect':'blocked'})
    except FileNotFoundError:rows.append({'case':name,'effect':'blocked' if name.endswith('_write') else 'hidden'})
    except OSError as e:rows.append({'case':name,'effect':'not_established','errno':e.errno})
print(json.dumps(rows))
'''
UNREADABLE = ("blocked", "hidden")


def check_scope(cli, name, profile, workspace, evidence, *, writable, protected=None):
    """Probe actual backend effects before any model or candidate-code execution.

    The probe writes inside its own workspace and outside it, and reads two
    sentinels the host plants: one in a fresh shared temporary directory and,
    when given, one inside protected, a directory the profile must keep
    unreadable (the control state, or the unadmitted parent of a validator's
    scratch). These owned-file command probes are necessary conformance, not
    proof of native editing, hosted tools, arbitrary process escape or semantic safety.
    """
    evidence = Path(evidence)
    evidence.parent.mkdir(parents=True,exist_ok=True)
    workspace = Path(workspace)
    token = uuid.uuid4().hex
    owned = workspace / (".scope-probe-" + token)
    script = workspace / (".scope-probe-" + token + ".py")
    guarded = Path(protected) / (".scope-probe-" + token) if protected is not None else None
    with tempfile.TemporaryDirectory(prefix="outside-runtime-scope-",dir=outside_temp_parent()) as outside:
        outside = Path(outside).resolve()
        forbidden, sentinel = outside / "forbidden.txt", outside / "secret.txt"
        sentinel.write_text(token, encoding="utf-8")
        targets = [owned, forbidden, sentinel]
        expected = [{"case":"own_workspace_write","effect":"succeeded" if writable else "blocked"},
                    {"case":"outside_temp_write","effect":"blocked"},
                    {"case":"outside_temp_read","effect":UNREADABLE}]
        prefix = list(sandbox_prefix(cli));prefix[-1] = name
        try:
            if guarded is not None:
                guarded.mkdir(parents=True)
                (guarded / "secret.txt").write_text(token, encoding="utf-8")
                targets.append(guarded / "secret.txt")
                expected.append({"case":"protected_read","effect":UNREADABLE})
            script.write_text(PROBE, encoding="utf-8")
            launch = prefix + ["-c","permissions=" + toml_literal({name:profile}),"--",sys.executable,
                               str(script),*map(str,targets)]
            result = subprocess.run(launch,cwd=workspace,encoding="utf-8",errors="replace",capture_output=True,
                                    timeout=20,env=minimal_environment())
            try:rows = json.loads(result.stdout)
            except ValueError:rows = []
            matched = (isinstance(rows,list) and len(rows) == len(expected)
                       and all(isinstance(row,dict) and row.get("case") == want["case"]
                               and (row.get("effect") in want["effect"] if isinstance(want["effect"],tuple)
                                    else row.get("effect") == want["effect"])
                               for row,want in zip(rows,expected)))
            passed = result.returncode == 0 and matched and not forbidden.exists() and owned.exists()==writable
            report = {"passed":passed,"profile":profile,"exit_code":result.returncode,"platform":platform_name(),
                      "observations":rows,"stderr":result.stderr,
                      "scope":"owned disposable command effects only; additional native routes require conformance"}
        except subprocess.TimeoutExpired:
            report = {"passed":False,"error":"native scope probe timed out","platform":platform_name()}
        finally:
            for path in (owned, script):
                if path.exists():path.unlink()
            if guarded is not None and guarded.exists():
                for child in guarded.iterdir():child.unlink()
                guarded.rmdir()
        evidence.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
        if not report["passed"]:
            hint = (" On Windows the Codex sandbox must be MXC or elevated (unelevated cannot enforce read "
                    "restrictions), and the runtime state must sit in a folder only your account can read."
                    if oscompat.WINDOWS else "")
            raise ContractError(f"Native command scope is not enforced; execution refused.{hint} Evidence: {evidence}")
        return report
