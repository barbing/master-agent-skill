"""Windows portability of the runtime.

Three layers:
  * static guards over the source: POSIX-only APIs stay in oscompat, and
    every text read, write and subprocess pipe names its encoding;
  * simulated Windows: oscompat.WINDOWS is patched on any OS, which proves
    the Windows branches' logic, not Windows itself;
  * real-platform checks, which run on every OS, plus Windows-only checks that
    run where the runtime is ported (junctions, final paths, job objects).
"""
import ast
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from master_agent_runtime import oscompat
from master_agent_runtime.artifacts import capture, verify
from master_agent_runtime.contracts import ConflictError, ContractError, relative_path
from master_agent_runtime.lease import project_lease

from runtime_fixtures import make_junction

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "master_agent_runtime"
WINDOWS = os.name == "nt"


def simulate_windows():
    return patch.multiple(oscompat, WINDOWS=True, MACOS=False)


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr, func.value.id if isinstance(func.value, ast.Name) else None
    if isinstance(func, ast.Name):
        return func.id, None
    return None, None


class StaticPortabilityTests(unittest.TestCase):
    POSIX_ONLY = {("os", "killpg"), ("os", "uname"), ("os", "O_DIRECTORY"), ("os", "O_NOFOLLOW"),
                  ("os", "getuid"), ("os", "geteuid"), ("os", "setsid"), ("os", "fork"),
                  ("signal", "SIGKILL"), ("signal", "SIGTERM"), ("signal", "SIGINT")}

    def test_posix_only_apis_stay_in_the_platform_module(self):
        found = []
        for path in sorted(RUNTIME.glob("*.py")):
            if path.name == "oscompat.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) else [])
                    found += [f"{path.name}:{node.lineno} imports {name}" for name in names
                              if name in ("fcntl", "msvcrt", "winreg", "_winapi", "pwd", "grp", "termios")]
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                        and (node.value.id, node.attr) in self.POSIX_ONLY:
                    found.append(f"{path.name}:{node.lineno} uses {node.value.id}.{node.attr}")
                elif isinstance(node, ast.keyword) and node.arg in ("dir_fd", "start_new_session", "preexec_fn"):
                    found.append(f"{path.name}:{node.value.lineno} passes {node.arg}")
        self.assertEqual(found, [])

    def test_text_io_and_subprocess_pipes_name_their_encoding(self):
        """Windows decodes with its ANSI code page unless told otherwise; the runtime always says UTF-8."""
        found = []
        for path in sorted(RUNTIME.glob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name, owner = _call_name(node)
                keywords = {keyword.arg for keyword in node.keywords}
                where = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                if name in ("read_text", "write_text") and "encoding" not in keywords:
                    found.append(f"{where} {name} without encoding")
                if name == "open" and owner != "os" and "encoding" not in keywords:
                    mode = node.args[0 if owner else 1] if len(node.args) > (0 if owner else 1) else None
                    mode = next((keyword.value for keyword in node.keywords if keyword.arg == "mode"), mode)
                    literal = mode.value if isinstance(mode, ast.Constant) else ("r" if mode is None and not owner else None)
                    if isinstance(literal, str) and "b" not in literal:
                        found.append(f"{where} open({literal!r}) without encoding")
                if keywords & {"text", "universal_newlines"} and "encoding" not in keywords:
                    found.append(f"{where} text-mode subprocess without encoding")
        self.assertEqual(found, [])


class SimulatedWindowsTests(unittest.TestCase):
    def test_lease_takes_a_byte_lock_through_msvcrt(self):
        held, calls = {}, []

        def locking(fd, mode, size):
            calls.append((mode, size))
            if mode == fake.LK_NBLCK:
                if held and fd not in held:
                    raise OSError(13, "locked by another handle")
                held[fd] = True
            else:
                held.pop(fd, None)

        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with tempfile.TemporaryDirectory() as tmp, simulate_windows(), patch.dict(sys.modules, {"msvcrt": fake}):
            path = Path(tmp) / "controller.lock"
            with project_lease(path):
                with self.assertRaises(ConflictError):
                    with project_lease(path):
                        pass
            with project_lease(path):
                pass
            self.assertEqual(path.read_text(encoding="utf-8").strip(), str(os.getpid()))
        self.assertEqual(calls[0], (2, 1))
        self.assertIn((0, 1), calls)
        self.assertEqual(held, {})

    def test_children_get_their_own_windows_process_group(self):
        with simulate_windows():
            self.assertEqual(oscompat.process_group_options(), {"creationflags": 0x200})
        if not WINDOWS:
            self.assertEqual(oscompat.process_group_options(), {"start_new_session": True})

    def test_process_group_ends_its_job_or_falls_back_to_taskkill(self):
        ended, closed = [], []
        with simulate_windows(), patch.object(oscompat, "_windows_job", return_value="job"), \
                patch.object(oscompat, "_windows_terminate_job", side_effect=lambda job: ended.append(job) or True), \
                patch.object(oscompat, "_windows_close", side_effect=closed.append):
            group = oscompat.ProcessGroup(4321)
            self.assertTrue(group.contained)
            self.assertTrue(group.signal())
            self.assertTrue(group.signal(force=True))
            group.reap()
            group.reap()
        self.assertEqual(ended, ["job", "job", "job"])
        self.assertEqual(closed, ["job"])
        with simulate_windows(), patch.object(oscompat, "_windows_job", return_value=None), \
                patch.object(oscompat, "_taskkill", return_value=False) as taskkill:
            group = oscompat.ProcessGroup(4321)
            self.assertFalse(group.contained)
            self.assertFalse(group.signal(force=True))
        taskkill.assert_called_once_with(4321)

    def test_taskkill_ends_the_whole_tree_forcefully(self):
        with patch.dict(os.environ, {"SYSTEMROOT": "C:\\Windows"}), \
                patch.object(oscompat.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertTrue(oscompat._taskkill(4321))
        argv = run.call_args.args[0]
        self.assertTrue(argv[0].endswith("taskkill.exe"))
        self.assertEqual(argv[1:], ["/PID", "4321", "/T", "/F"])

    def test_minimal_environment_keeps_what_windows_children_need(self):
        host = {"PATH": "C:\\Windows\\System32", "SYSTEMROOT": "C:\\Windows", "COMSPEC": "C:\\Windows\\System32\\cmd.exe",
                "PATHEXT": ".COM;.EXE;.BAT;.CMD", "USERPROFILE": "C:\\Users\\Default", "LOCALAPPDATA": "C:\\Local",
                "OPENAI_API_KEY": "not-for-children", "CLAUDECODE": "1"}
        with simulate_windows(), patch.dict(os.environ, host, clear=True):
            env = oscompat.minimal_environment(TMP="C:\\private\\tmp")
        for name in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "LOCALAPPDATA"):
            self.assertEqual(env[name], host[name])
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["TMP"], "C:\\private\\tmp")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CLAUDECODE", env)
        if not WINDOWS:
            with patch.dict(os.environ, host, clear=True):
                self.assertEqual(set(oscompat.minimal_environment()), {"PATH", "LANG"})

    def test_probes_use_the_user_temp_folder_on_windows(self):
        with simulate_windows():
            self.assertEqual(oscompat.outside_temp_parent(), tempfile.gettempdir())

    def test_sandbox_contract_selects_the_windows_or_unified_command(self):
        from master_agent_runtime import sandbox
        unified = "Usage: codex sandbox [OPTIONS] [COMMAND]...\n --permission-profile <NAME>\n --include-managed-config"
        legacy = "Usage: codex sandbox <COMMAND>\nCommands:\n  macos\n  linux\n  windows\n"
        detail = "Usage: codex sandbox windows [OPTIONS] [COMMAND]...\n --permission-profile\n --include-managed-config"

        def helper(pages):
            def run(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, pages[tuple(argv[2:])], "")
            return run

        cases = (({("sandbox",): unified}, ("codex", "sandbox")),
                 ({("sandbox",): legacy, ("sandbox", "windows"): detail}, ("codex", "sandbox", "windows")))
        for pages, prefix in cases:
            sandbox.sandbox_prefix.cache_clear()
            with simulate_windows(), patch.object(sandbox.subprocess, "run", side_effect=helper(pages)):
                self.assertEqual(sandbox.sandbox_prefix("codex"),
                                 prefix + ("--include-managed-config", "--permission-profile", "runtime-validator"))
        sandbox.sandbox_prefix.cache_clear()
        with simulate_windows(), patch.object(sandbox.subprocess, "run",
                                              side_effect=helper({("sandbox",): legacy.replace("windows", "")})):
            with self.assertRaises(ContractError):
                sandbox.sandbox_prefix("codex")
        sandbox.sandbox_prefix.cache_clear()

    def test_npm_shims_resolve_to_the_packaged_codex_binary(self):
        with tempfile.TemporaryDirectory() as tmp, simulate_windows(), \
                patch.object(oscompat.platform, "machine", return_value="AMD64"):
            npm = Path(tmp) / "npm"
            shim = npm / "codex.cmd"
            npm.mkdir()
            shim.write_text("@node codex.js %*\n", encoding="utf-8")
            self.assertIsNone(oscompat.native_executable(str(shim)))
            for vendor in (npm / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64",
                           npm / "node_modules" / "@openai" / "codex-win32-x64"):
                binary = vendor / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"")
                self.assertEqual(oscompat.native_executable(str(shim)), str(binary))
                binary.unlink()
            native = Path(tmp) / "codex.exe"
            self.assertEqual(oscompat.native_executable(str(native)), str(native))
            self.assertTrue(oscompat.is_shim(str(npm / "codex")))
        if not WINDOWS:
            self.assertEqual(oscompat.native_executable("/usr/local/bin/codex"), "/usr/local/bin/codex")
            self.assertFalse(oscompat.is_shim("/usr/local/bin/codex"))

    def test_codex_candidates_include_the_windows_install_locations(self):
        with tempfile.TemporaryDirectory() as tmp, simulate_windows(), \
                patch.object(oscompat.shutil, "which", return_value=None), \
                patch.dict(os.environ, {"LOCALAPPDATA": str(Path(tmp) / "local"), "CODEX_HOME": str(Path(tmp) / "home")}):
            candidates = oscompat.codex_candidates()
        self.assertIn(str(Path(tmp) / "local" / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"), candidates)
        self.assertIn(str(Path(tmp) / "home" / "packages" / "standalone" / "current" / "bin" / "codex.exe"), candidates)

    def test_a_shim_is_never_launched(self):
        import asyncio
        from master_agent_runtime.backend import native_cli, select_cli
        from master_agent_runtime.native import ModelUnavailable
        with tempfile.TemporaryDirectory() as tmp, simulate_windows():
            shim = Path(tmp) / "codex.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")
            with self.assertRaises(ContractError) as refused:
                native_cli(str(shim))
            self.assertIn(".exe", str(refused.exception))
            with patch("master_agent_runtime.backend.JsonRpcClient") as client, \
                    self.assertRaises(ModelUnavailable) as unavailable:
                asyncio.run(select_cli(candidates=[str(shim)]))
            client.assert_not_called()
            self.assertIn("launcher script", str(unavailable.exception))

    def test_claude_actors_are_refused_on_native_windows(self):
        import asyncio
        from master_agent_runtime.backend import select_claude_cli
        from master_agent_runtime.claude_native import ClaudeActor, claude_preflight
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actor = ClaudeActor("p:alpha", root / "work", root / "public", root, kind="module", write_paths=("a.py",))
            with simulate_windows():
                with self.assertRaises(ContractError) as refused:
                    asyncio.run(select_claude_cli(candidates=[]))
                self.assertIn("WSL2", str(refused.exception))
                with self.assertRaises(ContractError) as preflight:
                    claude_preflight(actor, root / "receipt.json")
            self.assertIn("native Windows", str(preflight.exception))
            self.assertFalse(json.loads((root / "receipt.json").read_text(encoding="utf-8"))["passed"])

    def test_windows_reserved_and_redirecting_names_are_refused(self):
        with simulate_windows():
            for value in ("D:evil.py", "src/a.py:stream", "src/nul.txt", "src/CON", "src/com1.log", "src/file.",
                          "src/name ", 'src/"quoted"', "src/what?.py"):
                with self.subTest(value=value), self.assertRaises(ContractError):
                    relative_path(value)
            self.assertEqual(relative_path("src/console.py"), "src/console.py")
        if not WINDOWS:
            self.assertEqual(relative_path("src/a:b.py"), "src/a:b.py")

    def test_protected_names_match_regardless_of_case(self):
        for value in (".GIT/config", "src/Claude.md", "src/.Claude/settings.json", ".Codex/config.toml"):
            with self.subTest(value=value), self.assertRaises(ContractError):
                relative_path(value)

    def test_commands_for_the_user_are_quoted_for_powershell(self):
        with simulate_windows():
            self.assertEqual(oscompat.shell_quote("C:\\runtime state\\it's"), "'C:\\runtime state\\it''s'")
            self.assertEqual(oscompat.shell_fence(), "powershell")
        if not WINDOWS:
            self.assertEqual(oscompat.shell_quote("/runtime state"), shlex.quote("/runtime state"))
            self.assertEqual(oscompat.shell_fence(), "bash")

    def test_python_library_roots_follow_windows_layouts(self):
        from master_agent_runtime.permissions import is_python, python_library_roots
        with tempfile.TemporaryDirectory() as tmp, simulate_windows():
            base, venv = Path(tmp) / "Python312", Path(tmp) / "venv"
            (venv / "Scripts").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text(f"home = {base}\ninclude-system-site-packages = false\n", encoding="utf-8")
            self.assertEqual(python_library_roots(base / "python.exe"), [str(base)])
            self.assertEqual(python_library_roots(venv / "Scripts" / "python.exe"), [str(venv), str(base.resolve())])
            for name, expected in (("python.exe", True), ("python3.12.exe", True), ("pythonw.exe", True),
                                   ("py.exe", False), ("pytest.exe", False)):
                self.assertIs(is_python(Path(name)), expected, name)

    def test_link_detection_reads_the_windows_reparse_tag(self):
        def lstat(tag, attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT):
            return types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=attributes, st_reparse_tag=tag)
        cases = ((lstat(0xA0000003), True),   # junction / mount point
                 (lstat(0xA000000C), True),   # symlink
                 (lstat(0x9000701A), False),  # OneDrive placeholder: data in place
                 (lstat(0, 0), False))
        for info, expected in cases:
            with simulate_windows(), patch.object(oscompat.os, "lstat", return_value=info):
                self.assertIs(oscompat.is_link_like("C:\\x"), expected, hex(info.st_reparse_tag))

    def test_rename_retries_while_windows_holds_a_file(self):
        attempts = []

        def rename(source, target):
            attempts.append(source)
            if len(attempts) < 3:
                raise PermissionError(13, "in use")

        with tempfile.TemporaryDirectory() as tmp, simulate_windows(), patch.object(oscompat.os, "rename", side_effect=rename):
            oscompat.rename_directory(Path(tmp) / "staging", Path(tmp) / "target", delay=0)
        self.assertEqual(len(attempts), 3)
        if not WINDOWS:
            with patch.object(oscompat.os, "rename", side_effect=PermissionError(13, "denied")) as denied:
                with self.assertRaises(PermissionError):
                    oscompat.rename_directory("staging", "target", delay=0)
            self.assertEqual(denied.call_count, 1)

    def test_tree_removal_clears_read_only_files_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sealed.txt"
            target.write_text("sealed", encoding="utf-8")
            target.chmod(0o444)
            seen = {}

            def rmtree(path, **kwargs):
                seen.update(kwargs)
                handler = kwargs.get("onexc") or kwargs.get("onerror")
                handler(os.unlink, str(target), PermissionError(13, "read-only"))

            with simulate_windows(), patch.object(oscompat.shutil, "rmtree", side_effect=rmtree):
                oscompat.remove_tree(tmp)
            self.assertFalse(target.exists(), "the handler cleared the read-only bit and retried the delete")
            self.assertTrue({"onexc", "onerror"} & set(seen))


def _posix_final_path(fd):
    if sys.platform == "darwin":
        import fcntl
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode()
    return os.readlink(f"/proc/self/fd/{fd}")


@unittest.skipIf(WINDOWS, "the real Windows reads are covered by WindowsOnlyTests")
class SimulatedWindowsReadTests(unittest.TestCase):
    """The Windows anchored-read algorithm, run against this OS's real files and links."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "workspace"
        (self.work / "src").mkdir(parents=True)
        (self.work / "src" / "code.py").write_text("answer = 42\n", encoding="utf-8")
        for patcher in (simulate_windows(), patch.object(oscompat, "final_path", side_effect=_posix_final_path)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ordinary_files_are_captured_and_verified(self):
        artifact = capture(self.work, self.root / "snapshots", ("src",), bindings={"revision": 1})
        manifest = verify(Path(artifact["path"]), ("src",), expected_hash=artifact["tree_hash"])
        self.assertEqual([row["path"] for row in manifest["files"]], ["src/code.py"])

    def test_link_components_are_refused(self):
        secret = self.root / "private.py"
        secret.write_text("protected", encoding="utf-8")
        (self.work / "src" / "escape.py").symlink_to(secret)
        with self.assertRaises(OSError):
            capture(self.work, self.root / "snapshots", ("src",), bindings={})
        (self.work / "src" / "escape.py").unlink()
        (self.work / "src" / "linked").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ContractError):
            capture(self.work, self.root / "snapshots", ("src",), bindings={})

    def test_a_handle_that_resolved_elsewhere_is_refused(self):
        anchor = oscompat.open_directory(self.work)
        with patch.object(oscompat, "final_path", return_value=str(self.root / "elsewhere.py")):
            with self.assertRaises(OSError):
                oscompat.open_beneath(anchor, ["src", "code.py"])
        fd = oscompat.open_beneath(anchor, ["src", "code.py"])
        os.close(fd)


class RealPlatformTests(unittest.TestCase):
    def test_a_process_group_ends_a_child_and_its_grandchild(self):
        from master_agent_runtime.doctor import check_process_tree
        outcome = check_process_tree()
        self.assertEqual(outcome["status"], "passed", outcome)

    def test_minimal_environment_starts_python_and_keeps_utf8_output(self):
        result = subprocess.run([sys.executable, "-c", "print('完成 — ok')"], capture_output=True,
                                env=oscompat.minimal_environment(), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8").strip(), "完成 — ok")

    def test_lease_is_exclusive_across_handles(self):
        from master_agent_runtime.doctor import check_lease
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check_lease(Path(tmp))["status"], "passed")


@unittest.skipUnless(WINDOWS, "native Windows behaviour")
class WindowsOnlyTests(unittest.TestCase):
    def test_final_path_of_an_open_file_matches_its_real_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.txt"
            path.write_text("x", encoding="utf-8")
            fd = os.open(path, os.O_RDONLY)
            try:
                self.assertEqual(os.path.normcase(oscompat.final_path(fd)),
                                 os.path.normcase(oscompat.strip_extended_prefix(os.path.realpath(path))))
            finally:
                os.close(fd)

    def test_junctions_are_links_and_anchored_reads_refuse_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outside").mkdir()
            (root / "outside" / "secret.txt").write_text("secret", encoding="utf-8")
            (root / "work").mkdir()
            make_junction(root / "work" / "linked", root / "outside")
            self.assertTrue(oscompat.is_link_like(root / "work" / "linked"))
            anchor = oscompat.open_directory(root / "work")
            with self.assertRaises(OSError):
                oscompat.open_beneath(anchor, ["linked", "secret.txt"])
            with self.assertRaises(OSError):
                oscompat.open_directory(root / "work" / "linked")

    def test_runtime_children_are_held_in_a_job_object(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   **oscompat.process_group_options())
        group = oscompat.ProcessGroup(process.pid)
        try:
            self.assertTrue(group.contained, "Windows refused a job object for the child")
            self.assertTrue(group.signal(force=True))
            self.assertEqual(process.wait(timeout=30), 1)
        finally:
            group.reap()
            if process.poll() is None:
                process.kill()

    def test_long_path_setting_is_readable(self):
        self.assertIn(oscompat.long_paths_enabled(), (True, False))

    def test_discovered_codex_candidates_are_native_executables(self):
        for candidate in oscompat.codex_candidates():
            if candidate:
                self.assertEqual(Path(candidate).suffix.lower(), ".exe", candidate)
