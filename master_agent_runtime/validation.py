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

from . import oscompat
from .contracts import ContractError, ValidationStep
from .sandbox import check_scope, sandbox_prefix, toml_literal
from .permissions import filesystem_profile

# cmd.exe re-reads these inside arguments it passes to a batch file.
BATCH_UNSAFE = frozenset('"%^&|<>!\r\n')


def validate(steps: tuple[ValidationStep, ...], candidate: Path, context: Path, evidence: Path, *, cli="codex",
             private: Path | None = None) -> list[dict]:
    """private is the user's sealed acceptance directory: readable by these validators only, as {acceptance}."""
    evidence.mkdir(parents=True, exist_ok=True)
    checks = []
    readable = (candidate, context) + ((private,) if private is not None else ())
    for index, step in enumerate(steps):
        with tempfile.TemporaryDirectory(prefix="validator-", dir=evidence) as tmp:
            scratch = Path(tmp).resolve()
            private_temp = scratch/'.tmp';private_temp.mkdir()
            argv = [x.replace("{candidate}", str(candidate)).replace("{context}", str(context)) for x in step.argv]
            if private is not None:
                argv = [x.replace("{acceptance}", str(private)) for x in argv]
            executable_name = shutil.which(argv[0])
            if not executable_name:
                raise ContractError("Admitted validation executable is unavailable")
            command = list(argv)
            if oscompat.WINDOWS:
                # The sandbox launches argv[0] without PATHEXT search, so it gets
                # the resolved file; a batch file must not receive arguments cmd.exe re-reads.
                command[0] = executable_name
                if Path(executable_name).suffix.lower() in (".cmd", ".bat") and any(
                        set(arg) & BATCH_UNSAFE for arg in argv[1:]):
                    raise ContractError(f"Validation command {argv[0]!r} is a batch file and an argument holds a "
                                        "character cmd.exe would re-read; use a native executable")
            profile = filesystem_profile(scratch,readable,writable=True,executables=(argv[0],))
            check_scope(cli,"runtime-validator",profile,scratch,
                        evidence / f"scope-{index}-{time.time_ns()}.json",writable=True,protected=evidence)
            launch = [*sandbox_prefix(cli),
                      "-c", "permissions=" + toml_literal({"runtime-validator": profile}), "--", *command]
            started = time.monotonic()
            try:
                result = subprocess.run(launch, cwd=scratch, stdin=subprocess.DEVNULL,
                    encoding="utf-8", errors="replace", capture_output=True, timeout=step.timeout_seconds,
                    env=oscompat.minimal_environment(PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(private_temp),
                                                     TMP=str(private_temp), TEMP=str(private_temp)))
                stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
                status = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired as error:
                stdout, stderr, exit_code, status = _text(error.stdout), _text(error.stderr), None, "timeout"
            text = stdout + "\nSTDERR\n" + stderr
            log = evidence / f"check-{index}-{time.time_ns()}.log"
            log.write_text(text, encoding="utf-8")
            checks.append({"argv": argv, "status": status, "exit_code": exit_code,
                           "seconds": time.monotonic() - started, "log": str(log),
                           "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                           "scope": "admitted argv under native filesystem/network sandbox; semantic oracle adequacy requires Master review"})
    return checks


def _text(value) -> str:
    """Partial output of a timed-out run; bytes when the stream had not been decoded yet."""
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
