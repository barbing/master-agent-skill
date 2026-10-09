"""Shared least-privilege profiles for native actors and independent checks."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import re

from .contracts import ContractError


MACOS_AMBIENT_TEMPS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp")


def executable_read_roots(executable):
    """Admit loader directories and symlink traversal, without home-wide reads.

    The native backend canonicalizes read rules. A rule for a launcher target
    therefore does not preserve access to intermediate directory symlinks.
    Their real parent directories are required for executable traversal.
    """
    found = shutil.which(str(executable))
    if not found:
        raise ContractError(f"Required executable is unavailable: {executable}")
    lexical = Path(found).absolute()
    roots = {str(lexical.parent.resolve()), str(lexical.resolve().parent)}
    current = lexical
    for _ in range(32):
        link = next((path for path in reversed(current.parents) if path.is_symlink()), None)
        if link is None and current.is_symlink():
            link = current
        if link is None:
            return roots
        parent = link.parent.resolve()
        system_alias = link in {Path('/var'),Path('/tmp'),Path('/etc')}
        if not system_alias and parent in {Path('/'), Path.home().resolve(), Path.home().resolve().parent}:
            raise ContractError("Executable symlink requires an overbroad directory read grant")
        if not system_alias:
            roots.add(str(parent))
        destination = Path(os.readlink(link))
        if not destination.is_absolute():
            destination = link.parent / destination
        current = destination / current.relative_to(link)
    raise ContractError("Executable symlink chain exceeds the supported limit")


def filesystem_profile(workspace, readable, *, writable, executables=(), denied=(),write_paths=None):
    workspace = Path(workspace).resolve()
    if os.uname().sysname == 'Darwin':
        for base in (Path('/private/tmp'), Path('/private/var/tmp')):
            if workspace == base or base in workspace.parents:
                raise ContractError("Use a private runtime directory outside ambient system temp trees")
    rules = {":minimal": "read", str(workspace): "write" if writable else "read"}
    if writable and write_paths is not None:
        rules[str(workspace)]='read'
        for relative in write_paths:
            target=workspace/relative
            rules[str(target)]='write'
            for part in ('.git','.gitignore','.codex','.agents'):
                rules[str(target/part)]='read'
        rules[str(workspace/'.tmp')]='write'
    rules.update({str(Path(path).resolve()): "read" for path in readable})
    for executable in (sys.executable, *executables):
        for root in executable_read_roots(executable):
            rules[root] = "read"
        # The embedded Python runtime also needs its sibling library tree.
        resolved=Path(shutil.which(str(executable))).resolve()
        if resolved == Path(sys.executable).resolve() or re.fullmatch(r'python(?:\d+(?:\.\d+)*)?',resolved.name):
            # Secondary admitted Python interpreters need their own stdlib and
            # site-packages as well; admitting bin alone breaks encodings init.
            rules[str(resolved.parents[1])] = "read"
    if os.uname().sysname == 'Darwin':
        # Exact denies do not override this build's Process platform scratch
        # allowances. Native deny globs compile to final Seatbelt read/write
        # denies, covering future files as well as existing temporary siblings.
        rules.update({path + '/**': 'deny' for path in MACOS_AMBIENT_TEMPS})
    for part in (".git", ".gitignore", ".codex", ".agents"):
        rules[str(workspace / part)] = "read"
    # An executable located in control storage cannot reopen the control grant.
    rules.update({str(Path(path).resolve()): "deny" for path in denied})
    return {"filesystem": rules, "network": {"enabled": False}}
