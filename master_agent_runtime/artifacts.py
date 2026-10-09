"""Content-addressed, scope-checked snapshots; mutable paths are never approvals."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import shutil
import stat
import tempfile

from .contracts import ConflictError, ContractError, PROTECTED_PARTS, canonical, digest, in_scope, relative_path


IGNORED_GENERATED = {"__pycache__", ".pytest_cache", ".tmp"}


def read_regular(root_fd: int, relative: str, *, metadata_read=False) -> bytes:
    """Anchor every component to directory FDs; no symlink/parent-race reads."""
    if metadata_read:
        if Path(relative).is_absolute() or '..' in Path(relative).parts or '\\' in relative:
            raise ContractError('Invalid readonly context path')
        parts=relative.split('/')
    else:parts = relative_path(relative).split("/")
    directory = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ContractError("Artifacts must be ordinary single-link files")
            if before.st_size > 16 * 1024 * 1024:
                raise ContractError("Artifact file exceeds the admitted 16 MiB limit")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read()
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ConflictError("Artifact changed during capture; finish/fence its writer")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def file_names(root: Path) -> list[str]:
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                raise ContractError("Symlink directories cannot be submitted")
            if name in IGNORED_GENERATED:
                dirs.remove(name)
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            result.append((Path(directory) / name).relative_to(root).as_posix())
    return sorted(result)


def owned_path(name,paths):
    return not set(Path(name).parts)&PROTECTED_PARTS and any(name==p or name.startswith(p+'/') for p in paths)


def context_hashes(root,paths):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:return {name:hashlib.sha256(read_regular(fd,name,metadata_read=True)).hexdigest()
                for name in file_names(root) if not owned_path(name,paths)}
    finally:os.close(fd)


def tree(root: Path, paths: tuple[str, ...] | list[str], *, context_inputs=None) -> tuple[list[dict], dict[str, bytes]]:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ContractError("Artifact root must be a real directory")
    names = file_names(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    data = {}
    rows = []
    total = 0
    try:
        for name in names:
            if not owned_path(name,paths):
                if context_inputs is None:
                    raise ContractError(f"Candidate writes outside module namespace: {name}")
                content=read_regular(fd,name,metadata_read=True)
                if context_inputs.get(name)!=hashlib.sha256(content).hexdigest():
                    raise ConflictError(f'Readonly project context was modified or added: {name}')
                continue
            content = read_regular(fd, name)
            total += len(content)
            if total > 64 * 1024 * 1024:
                raise ContractError("Candidate exceeds the admitted 64 MiB limit")
            data[name] = content
            rows.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
    finally:
        os.close(fd)
    if names != file_names(root):
        raise ConflictError("Candidate membership changed during capture")
    if context_inputs is not None and set(context_inputs)-set(names):
        raise ConflictError('Readonly project context was deleted')
    return rows, data


def capture(workspace: Path, destination: Path, paths, *, bindings: dict, baseline_files=(),context_inputs=None) -> dict:
    rows, data = tree(workspace, paths,context_inputs=context_inputs)
    if "candidate-manifest.json" in data:
        raise ContractError("The snapshot manifest is controller-owned")
    baseline = {relative_path(name) for name in baseline_files}
    if any(not in_scope(name, paths) for name in baseline):
        raise ContractError("Deletion baseline exceeds the module grant")
    deleted = sorted(baseline - data.keys())
    if not rows and not deleted:
        raise ContractError("A candidate must contain actual files or admitted deletions")
    manifest = {"bindings": bindings, "files": rows, "deleted": deleted}
    content_hash = digest(manifest)
    target = destination / content_hash
    destination.mkdir(parents=True, exist_ok=True)
    if target.exists():
        verify(target, paths, expected_hash=content_hash)
        return {"path": str(target), "tree_hash": content_hash, "manifest": manifest}
    staging = Path(tempfile.mkdtemp(prefix="capture-", dir=destination))
    try:
        for name, content in data.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (staging / "candidate-manifest.json").write_text(canonical(manifest) + "\n")
        os.rename(staging, target)
        for path in target.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"path": str(target), "tree_hash": content_hash, "manifest": manifest}


def verify(snapshot: Path, paths, *, expected_hash: str) -> dict:
    manifest = json.loads((snapshot / "candidate-manifest.json").read_text())
    if digest(manifest) != expected_hash:
        raise ConflictError("Immutable candidate manifest changed after capture/review")
    expected = {x["path"]: x for x in manifest["files"]}
    deleted = manifest.get("deleted", [])
    if (len(expected) != len(manifest["files"]) or len(set(deleted)) != len(deleted)
            or set(deleted) & expected.keys()
            or any(not in_scope(name, paths) for name in deleted)):
        raise ContractError("Snapshot paths/deletions are inconsistent or ungranted")
    actual = [x for x in file_names(snapshot) if x != "candidate-manifest.json"]
    if sorted(expected) != actual:
        raise ConflictError("Immutable candidate membership changed")
    fd = os.open(snapshot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name, info in expected.items():
            if not in_scope(name, paths):
                raise ContractError("Snapshot contains an ungranted production path")
            content = read_regular(fd, name)
            if hashlib.sha256(content).hexdigest() != info["sha256"] or len(content) != info["bytes"]:
                raise ConflictError("Candidate bytes changed after capture/review")
    finally:
        os.close(fd)
    return manifest
