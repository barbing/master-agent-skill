"""Platform differences in one place: macOS, Linux and native Windows.

The rest of the runtime asks this module for the project lock, owned process
groups, minimal child environments, link checks, anchored file reads, tree
removal, CLI discovery and command quoting. POSIX behaviour is unchanged from
the original runtime; the Windows branches use only the standard library.
Every platform check reads WINDOWS/MACOS when called, so tests can simulate
another platform.
"""
from __future__ import annotations

import errno
from functools import lru_cache
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"

# Launchers that a shell re-parses. cmd.exe re-reads quotes, %, ^, & and |
# and stops at a newline, so JSON, TOML and probe code would not survive
# a shim. The runtime runs native executables only.
WINDOWS_SHIMS = (".cmd", ".bat", ".ps1")
WINDOWS_ESSENTIAL_ENVIRONMENT = (
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "CODEX_HOME")
WINDOWS_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$",
                                    *(f"com{n}" for n in "0123456789¹²³"), *(f"lpt{n}" for n in "0123456789¹²³")})
WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*') | {chr(code) for code in range(32)}
CLAUDE_WINDOWS_REFUSAL = (
    "Claude actors need Claude Code's Bash sandbox, which runs on macOS, Linux and WSL2 but not native Windows. "
    "Run the runtime inside WSL2, or route these actors to Codex with runtime_policy provider 'codex'.")
ELOOP = getattr(errno, "ELOOP", 62)
# Reparse tags with this bit redirect a path (symlinks, junctions, mount
# points, WSL links). Other tags hold data in place, such as OneDrive
# placeholders, and are ordinary files and directories for the runtime.
NAME_SURROGATE = 0x20000000


def platform_name() -> str:
    return "windows" if WINDOWS else "macos" if MACOS else "linux"


# ---- The project lease ------------------------------------------------------

def lock_file(stream) -> bool:
    """Take a non-blocking exclusive lock on an open file; False when another holder has it.

    The operating system releases the lock when its holder exits, so a crashed
    controller never leaves a stale lease. Windows locks are per open handle,
    like POSIX flock per open file, so a second open in one process conflicts too.
    """
    if WINDOWS:
        import msvcrt
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def unlock_file(stream) -> None:
    if WINDOWS:
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


# ---- Owned process groups ---------------------------------------------------

def process_group_options() -> dict:
    """Spawn options that give a child its own process group (POSIX session, Windows group)."""
    if WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


class ProcessGroup:
    """Every process a runtime-started child creates, so the runtime can end all of them.

    POSIX: the child leads a new session, so its process group id is its pid.
    Windows: the child is placed in a kill-on-close job object, which also
    holds its descendants unless they explicitly break away. When Windows
    refuses a job, termination falls back to `taskkill /T /F`, which reaches
    only descendants that are still linked to a live parent.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.contained = not WINDOWS
        self._job = None
        if WINDOWS:
            self._job = _windows_job(pid)
            self.contained = self._job is not None

    def signal(self, *, force=False) -> bool:
        """Ask (SIGTERM) or force (SIGKILL) every member to exit; False when none remain.

        Windows has no graceful signal for a console process tree, so both
        requests end the job (or the tree) at once.
        """
        if WINDOWS:
            if self._job is not None:
                return _windows_terminate_job(self._job)
            return _taskkill(self.pid)
        import signal
        try:
            os.killpg(self.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    def reap(self) -> None:
        """End members that outlived the leader, then release the group."""
        if WINDOWS:
            job, self._job = self._job, None
            if job is not None:
                _windows_terminate_job(job)
                _windows_close(job)
            return
        import signal
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def interrupt_process(process) -> None:
    """Deliver an interrupt (POSIX SIGINT, Windows CTRL_BREAK to the child's group)."""
    import signal
    try:
        process.send_signal(getattr(signal, "CTRL_BREAK_EVENT") if WINDOWS else signal.SIGINT)
    except (OSError, ValueError, AttributeError):
        pass


def _taskkill(pid: int) -> bool:
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    command = os.path.join(root, "System32", "taskkill.exe")
    try:
        result = subprocess.run([command, "/PID", str(pid), "/T", "/F"], capture_output=True,
                                encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@lru_cache(maxsize=1)
def _kernel32():
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    return kernel32


def _windows_job(pid: int):
    """A kill-on-close job holding pid, or None when Windows refuses one."""
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = ExtendedLimits()
        # Kill on close ends every member when the runtime releases the job or
        # exits. Breakaway stays possible for children that ask for it (as some
        # sandbox launchers do) instead of failing their process creation.
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        process = None
        try:
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not kernel32.AssignProcessToJobObject(job, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        except OSError:
            kernel32.CloseHandle(job)
            return None
        finally:
            if process:
                kernel32.CloseHandle(process)
        return job
    except (ImportError, AttributeError, OSError):
        return None


def _windows_terminate_job(job) -> bool:
    try:
        return bool(_kernel32().TerminateJobObject(job, 1))
    except (ImportError, AttributeError, OSError):
        return False


def _windows_close(handle) -> None:
    try:
        _kernel32().CloseHandle(handle)
    except (ImportError, AttributeError, OSError):
        pass


# ---- Child environments -----------------------------------------------------

def essential_environment() -> dict:
    """Variables a minimal child environment keeps on this platform (none outside Windows).

    Without SYSTEMROOT a Windows Python cannot seed its hash randomization and
    exits at startup; without COMSPEC, PATHEXT and the profile folders, shells
    and common tools fail. PYTHONUTF8 makes Python children write UTF-8, as the
    UTF-8 locale does elsewhere.
    """
    if not WINDOWS:
        return {}
    env = {name: os.environ[name] for name in WINDOWS_ESSENTIAL_ENVIRONMENT if name in os.environ}
    env["PYTHONUTF8"] = "1"
    return env


def minimal_environment(**overrides) -> dict:
    """PATH and a UTF-8 locale, plus the platform essentials and explicit overrides."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "en_US.UTF-8", **essential_environment()}
    env.update(overrides)
    return env


def outside_temp_parent() -> str:
    """A shared temporary root outside every admitted path, for conformance probes."""
    return "/private/tmp" if MACOS else tempfile.gettempdir()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


# ---- Links and anchored reads -----------------------------------------------

def is_link_like(path) -> bool:
    """A symlink, or on Windows any reparse point that redirects the path (junction, mount point).

    Python reports only true symlinks as links, and os.walk descends through
    Windows junctions, so the runtime checks the reparse tag itself.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    if not WINDOWS or not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return bool(getattr(info, "st_reparse_tag", 0) & NAME_SURROGATE)


class WindowsDirectory:
    """A directory anchor on Windows: its final path, checked again by every read beneath it."""
    __slots__ = ("path",)

    def __init__(self, path: str):
        self.path = path


def open_directory(root):
    """Anchor reads beneath root: a no-follow directory descriptor on POSIX, the final path on Windows."""
    if not WINDOWS:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root = os.path.abspath(root)
    if is_link_like(root):
        raise OSError(ELOOP, "Anchor directory is a link or reparse point", root)
    if not os.path.isdir(root):
        raise NotADirectoryError(errno.ENOTDIR, "Anchor is not a directory", root)
    return WindowsDirectory(strip_extended_prefix(os.path.realpath(root)))


def close_directory(anchor) -> None:
    if not isinstance(anchor, WindowsDirectory):
        os.close(anchor)


def open_beneath(anchor, parts) -> int:
    """Open one regular-file candidate beneath an anchor without following any link.

    POSIX walks directory descriptors with O_NOFOLLOW, which no rename race
    can redirect. Windows refuses link-like components, then confirms from the
    open handle that the file's final path is the expected one beneath the
    anchor, so a component swapped for a junction during the read is refused too.
    """
    if not isinstance(anchor, WindowsDirectory):
        directory = os.dup(anchor)
        try:
            for part in parts[:-1]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = nxt
            return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
    current = anchor.path
    for part in parts:
        current = os.path.join(current, part)
        if is_link_like(current):
            raise OSError(ELOOP, "Links and reparse points are not followed", current)
    fd = os.open(current, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
    try:
        if os.path.normcase(final_path(fd)) != os.path.normcase(current):
            raise OSError(ELOOP, "File resolved outside its anchored directory", current)
    except BaseException:
        os.close(fd)
        raise
    return fd


def final_path(fd: int) -> str:
    """The path an open file finally resolves to, without the \\\\?\\ prefix (Windows only)."""
    import ctypes
    import msvcrt
    kernel32 = _kernel32()
    handle = msvcrt.get_osfhandle(fd)
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if not length:
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
        if length < size:
            return strip_extended_prefix(buffer.value)
        size = length + 1


def strip_extended_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def windows_name_problem(part: str) -> str | None:
    """Why a path component cannot be an ordinary Windows file name, or None."""
    if not part:
        return None
    if set(part) & WINDOWS_INVALID_CHARACTERS:
        return "contains a character Windows reserves (<>:\"|?* or a control character)"
    if part[-1] in ". ":
        return "ends with a dot or space, which Windows strips"
    if part.split(".", 1)[0].rstrip(" ").casefold() in WINDOWS_RESERVED_NAMES:
        return "is a reserved Windows device name"
    return None


# ---- Moving and removing trees ----------------------------------------------

def rename_directory(source, target, *, attempts=20, delay=0.1) -> None:
    """os.rename, retried briefly on Windows while a scanner or indexer holds a file inside."""
    for attempt in range(attempts if WINDOWS else 1):
        try:
            os.rename(source, target)
            return
        except PermissionError:
            if attempt + 1 >= (attempts if WINDOWS else 1) or os.path.exists(target):
                raise
            time.sleep(delay)


def remove_tree(path, *, ignore_errors=False) -> None:
    """shutil.rmtree that also removes read-only files, which Windows refuses to delete."""
    if not WINDOWS:
        shutil.rmtree(path, ignore_errors=ignore_errors)
        return

    def clear_readonly(function, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            if not ignore_errors:
                raise

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear_readonly)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(path, onerror=clear_readonly)


def long_paths_enabled():
    """Whether Windows admits paths beyond 260 characters; None outside Windows."""
    if not WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
        return bool(value)
    except OSError:
        return False


# ---- Installed CLIs -----------------------------------------------------------

def _windows_arches():
    machine = (platform.machine() or "").lower()
    native = ("arm64", "aarch64") if machine in ("arm64", "aarch64") else ("x64", "x86_64")
    other = ("x64", "x86_64") if native[0] == "arm64" else ("arm64", "aarch64")
    return native, other


def is_shim(path) -> bool:
    """A Windows launcher script (or an extensionless npm sh shim) rather than a native executable."""
    return bool(path) and WINDOWS and Path(path).suffix.lower() in WINDOWS_SHIMS + ("",)


def native_executable(path):
    """The native binary behind a CLI path; Windows npm shims resolve to their packaged codex.exe.

    Returns None for a shim whose binary cannot be found. Paths on POSIX and
    native Windows executables are returned unchanged.
    """
    if not is_shim(path):
        return path
    candidate = Path(path)
    shim_dir = candidate.parent
    name = candidate.stem.lower()
    if name != "codex":
        sibling = shim_dir / f"{candidate.stem}.exe"
        return str(sibling) if sibling.is_file() else None
    package = shim_dir / "node_modules" / "@openai" / "codex"
    for arch, triple_arch in _windows_arches():
        triple = f"{triple_arch}-pc-windows-msvc"
        platform_package = f"codex-win32-{arch}"
        for root in (package / "node_modules" / "@openai" / platform_package / "vendor",
                     shim_dir / "node_modules" / "@openai" / platform_package / "vendor", package / "vendor"):
            for relative in (Path(triple) / "bin" / "codex.exe", Path(triple) / "codex" / "codex.exe",
                             Path(triple) / "codex.exe"):
                if (root / relative).is_file():
                    return str(root / relative)
    return None


def codex_candidates() -> list:
    """Installed Codex CLIs to try, in order; shims are replaced by their native binary."""
    found = shutil.which("codex")
    if not WINDOWS:
        return [found, "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                "/Applications/Codex.app/Contents/Resources/codex"]
    local = os.environ.get("LOCALAPPDATA")
    candidates = [native_executable(found) if found else None]
    if local:
        candidates.append(str(Path(local) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"))
    candidates.append(str(codex_home() / "packages" / "standalone" / "current" / "bin" / "codex.exe"))
    return candidates


def claude_candidates() -> list:
    found = shutil.which("claude")
    if WINDOWS:
        return [native_executable(found) if found else None, str(Path.home() / ".local" / "bin" / "claude.exe")]
    return [found, str(Path.home() / ".local" / "bin" / "claude")]


def claude_sandbox_supported() -> bool:
    """Claude Code's Bash sandbox covers macOS, Linux and WSL2 (which reports Linux), not native Windows."""
    return not WINDOWS


def shim_refusal(path) -> str:
    return (f"{path} is a launcher script that re-parses arguments through a shell; the runtime needs the "
            "native executable. Pass --codex (or --claude) with the full path of the .exe, for example the "
            "codex.exe inside the npm package's vendor folder or under %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin.")


# ---- Commands shown to the user ----------------------------------------------

def shell_quote(value: str) -> str:
    """Quote one argument for the user's shell: POSIX sh, or PowerShell on Windows."""
    if WINDOWS:
        return "'" + str(value).replace("'", "''") + "'"
    import shlex
    return shlex.quote(str(value))


def shell_fence() -> str:
    return "powershell" if WINDOWS else "bash"
