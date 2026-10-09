"""Run admitted validators against readonly candidate bytes in a native sandbox."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import shutil

from .contracts import ContractError, ValidationStep
from .sandbox import check_scope, sandbox_prefix, toml_literal
from .permissions import filesystem_profile



def validate(steps: tuple[ValidationStep, ...], candidate: Path, context: Path, evidence: Path, *, cli="codex") -> list[dict]:
    evidence.mkdir(parents=True, exist_ok=True)
    checks = []
    for index, step in enumerate(steps):
        with tempfile.TemporaryDirectory(prefix="validator-", dir=evidence) as tmp:
            scratch = Path(tmp).resolve()
            private_temp = scratch/'.tmp';private_temp.mkdir()
            argv = [x.replace("{candidate}", str(candidate)).replace("{context}", str(context)) for x in step.argv]
            executable_name = shutil.which(argv[0])
            if not executable_name:
                raise ContractError("Admitted validation executable is unavailable")
            executable = Path(executable_name).resolve()
            profile = filesystem_profile(scratch,(candidate,context),writable=True,executables=(argv[0],))
            check_scope(cli,"runtime-validator",profile,scratch,
                        evidence / f"scope-{index}-{time.time_ns()}.json",writable=True)
            launch = [*sandbox_prefix(cli),
                      "-c", "permissions=" + toml_literal({"runtime-validator": profile}), "--", *argv]
            started = time.monotonic()
            try:
                result = subprocess.run(launch, cwd=scratch, stdin=subprocess.DEVNULL,
                    text=True, capture_output=True, timeout=step.timeout_seconds,
                    env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                         "LANG": "en_US.UTF-8",'TMPDIR':str(private_temp),'TMP':str(private_temp),'TEMP':str(private_temp)})
                stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
                status = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired as error:
                stdout, stderr, exit_code, status = str(error.stdout or ""), str(error.stderr or ""), None, "timeout"
            text = stdout + "\nSTDERR\n" + stderr
            log = evidence / f"check-{index}-{time.time_ns()}.log"
            log.write_text(text)
            checks.append({"argv": argv, "status": status, "exit_code": exit_code,
                           "seconds": time.monotonic() - started, "log": str(log),
                           "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                           "scope": "admitted argv under native filesystem/network sandbox; semantic oracle adequacy requires Master review"})
    return checks
