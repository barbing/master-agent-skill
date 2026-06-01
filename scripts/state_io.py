"""Small file-backed state IO helpers for Master Agent state packs."""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def atomic_write_text(path: Path, text: str) -> None:
    """Write text via a sibling temp file and atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _process_is_alive(pid_value: object) -> bool:
    try:
        pid = int(str(pid_value).strip())
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return pid == os.getpid()
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_lock_metadata(lock_path: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return metadata
    for token in text.replace("\n", " ").split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def lock_is_recoverable(lock_path: Path, stale_seconds: float | None) -> bool:
    metadata = _read_lock_metadata(lock_path)
    owner_pid = metadata.get("pid")
    try:
        locked_at = float(metadata.get("time") or lock_path.stat().st_mtime)
    except (OSError, ValueError):
        locked_at = 0.0
    age_seconds = time.time() - locked_at
    dead_owner_probe_seconds = 5.0 if stale_seconds is None else min(stale_seconds, 5.0)
    if owner_pid and age_seconds >= dead_owner_probe_seconds and not _process_is_alive(owner_pid):
        return True
    if stale_seconds is None:
        return False
    if age_seconds < stale_seconds:
        return False
    return not _process_is_alive(owner_pid)


def unlink_with_retry(path: Path, attempts: int = 100, delay_seconds: float = 0.02) -> None:
    last_error: OSError | None = None
    for _ in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(delay_seconds)
    if last_error:
        raise last_error


@contextmanager
def with_lock(
    lock_path: Path,
    timeout_seconds: float = 5.0,
    stale_seconds: float | None = 600.0,
) -> Iterator[None]:
    """Acquire a portable exclusive lock file with timeout."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} time={time.time()}\n".encode("utf-8"))
            break
        except FileExistsError as exc:
            if lock_is_recoverable(lock_path, stale_seconds):
                try:
                    unlink_with_retry(lock_path)
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_path}") from exc
            time.sleep(0.02)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        unlink_with_retry(lock_path)


def append_jsonl_locked(
    path: Path,
    entry: dict,
    timeout_seconds: float = 5.0,
    stale_seconds: float | None = 600.0,
) -> None:
    path = Path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    line = json.dumps(entry, sort_keys=True) + "\n"
    with with_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        stale_seconds=stale_seconds,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
