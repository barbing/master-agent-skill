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
    result = subprocess.run([cli, "help", "sandbox"], text=True, capture_output=True, timeout=10)
    help_text = result.stdout
    if result.returncode != 0:
        raise ContractError("Unable to verify installed native sandbox command")
    if "<COMMAND>" in help_text and "macos" in help_text:
        platform = "macos" if os.uname().sysname == "Darwin" else "linux"
        prefix = [cli, "sandbox", platform]
        detail = subprocess.run([cli, "help", "sandbox", platform], text=True, capture_output=True, timeout=10)
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


def check_scope(cli, name, profile, workspace, evidence, *, writable):
    """Probe actual backend effects before any model or candidate-code execution.

    These owned-file command probes are necessary conformance, not proof of
    native editing, hosted tools, arbitrary process escape or semantic safety.
    """
    evidence = Path(evidence)
    evidence.parent.mkdir(parents=True,exist_ok=True)
    workspace = Path(workspace)
    owned = workspace / (".scope-probe-" + uuid.uuid4().hex)
    temporary_parent = "/private/tmp" if os.uname().sysname == "Darwin" else tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="outside-runtime-scope-",dir=temporary_parent) as outside:
        outside = Path(outside).resolve() / "forbidden.txt"
        prefix = list(sandbox_prefix(cli));prefix[-1] = name
        code = '''import json,pathlib,sys
rows=[]
for name,path in zip(('own_workspace_write','outside_temp_write'),sys.argv[1:]):
    try:pathlib.Path(path).write_text('owned disposable conformance fixture');rows.append({'case':name,'effect':'succeeded'})
    except PermissionError:rows.append({'case':name,'effect':'blocked'})
    except OSError as e:rows.append({'case':name,'effect':'not_established','errno':e.errno})
print(json.dumps(rows))
'''
        launch = prefix + ["-c","permissions=" + toml_literal({name:profile}),"--",sys.executable,
                           "-c",code,str(owned),str(outside)]
        try:
            result = subprocess.run(launch,cwd=workspace,text=True,capture_output=True,timeout=20,
                                    env={"PATH":os.environ.get("PATH",os.defpath),"LANG":"en_US.UTF-8"})
            try:rows = json.loads(result.stdout)
            except ValueError:rows = []
            expected = [{"case":"own_workspace_write","effect":"succeeded" if writable else "blocked"},
                        {"case":"outside_temp_write","effect":"blocked"}]
            passed = result.returncode == 0 and rows == expected and not outside.exists() and owned.exists()==writable
            report = {"passed":passed,"profile":profile,"exit_code":result.returncode,
                      "observations":rows,"stderr":result.stderr,
                      "scope":"owned disposable command effects only; additional native routes require conformance"}
        except subprocess.TimeoutExpired:
            report = {"passed":False,"error":"native scope probe timed out"}
        finally:
            if owned.exists():owned.unlink()
        evidence.write_text(json.dumps(report,indent=2)+"\n")
        if not report["passed"]:
            raise ContractError(f"Native command scope is not enforced; execution refused. Evidence: {evidence}")
        return report
