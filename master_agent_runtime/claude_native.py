"""Claude Code adapter: one persistent session per actor, one restricted process per turn.

Each turn runs `claude -p` in stream-json mode with:
  * --restricted: only managed settings and the host's --settings load; user,
    project and local settings files (hooks, plugins, permissions) are ignored,
    and the built-in file tools are confined to the working directories;
  * --tools naming the exact built-in tools, with no subagent, web, MCP,
    monitor or workflow tools;
  * --permission-mode dontAsk and --permission-prompts none, so anything not
    explicitly allowed is denied instead of waiting for a person;
  * an OS sandbox for Bash (strict: no unsandboxed retry, no network, refuse
    to start without the sandbox) and Edit allow rules naming only the
    module's admitted paths.

The host chooses the session id before the first turn and resumes it later,
so the owner's context continues across activations. The init message of
every run is checked against this configuration before any tool result is
trusted. Claude's file tools are governed by Claude Code's permission rules
(harness-level), not by the OS sandbox; that coverage difference is recorded
in every conformance receipt.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import time
import uuid
from pathlib import Path

from .contracts import AuthorityError, ContractError, UnknownOutcome
from .evidence import MAX_COMMANDS, MAX_COMMAND_OUTPUT, command_observation
from .native import NativeRejected


# The read block, the forced default mode under the credential scrub and the
# built-in plugin report were observed live on 2.1.263 and 2.1.295.
MINIMUM_VERSION = (2, 1, 263)
OWNER_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash")
MASTER_TOOLS = ("Read", "Glob", "Grep", "Bash")
# Tools that run outside the sandbox, reach the network, delegate to other
# agents or outlive a turn. Their presence in a run refuses the run.
FORBIDDEN_TOOLS = frozenset({"Agent", "Task", "WebFetch", "WebSearch", "Monitor", "Workflow", "RemoteTrigger",
                             "CronCreate", "CronDelete", "ScheduleWakeup", "SendMessage", "PowerShell",
                             "TeamCreate", "TeamDelete", "BashOutput", "KillShell"})
SENSITIVE_READS = (".ssh", ".aws", ".codex", ".config/gh", ".netrc", ".docker", ".gnupg", ".kube")
# Shared temp roots stay readable: Claude Code points every sandboxed command's
# TMPDIR at /tmp/claude-<uid> and ignores an inherited override, so denying them
# breaks ordinary test code. They must never be re-opened wholesale either.
SHARED_TEMP_ROOTS = ("/private/tmp", "/private/var/folders", "/private/var/tmp")
ENVIRONMENT = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    "CLAUDE_CODE_RETRY_WATCHDOG": "1",
}
# Claude Code forces permission mode "default" whenever the subprocess
# credential scrub is set (its allowed_non_write_users hardening, observed in
# 2.1.263 and 2.1.295). With --permission-prompts none, default mode denies everything not
# pre-approved, as dontAsk would; any other reported mode refuses the run.
EXPECTED_PERMISSION_MODE = "default" if ENVIRONMENT.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB") == "1" else "dontAsk"
# Owner processes start from a minimal environment. The launching process may
# itself run inside an agent host whose session identity, messaging channel,
# proxies or API routing must never reach a model-driven process.
INHERITED_ENVIRONMENT = ("HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
                         "CLAUDE_CONFIG_DIR")
# Comma-separated extra variable names an operator deliberately forwards, for
# example API-key or cloud-provider authentication. Values are never logged.
PASSTHROUGH_VARIABLE = "MASTER_AGENT_CLAUDE_ENV"
HOST_SESSION_MARKERS = ("CLAUDECODE", "MESSAGING", "_SESSION", "CLAUDE_PID")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EXIT_RE = re.compile(r"(?:^|\n)\s*(?:Exit code|exit code)[: ]\s*(-?\d+)")


def parse_version(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _gitignore_escape(path):
    return re.sub(r"([\[\]*?!#\\])", r"\\\1", path)


def _anchor(path):
    """Permission-rule absolute anchor (//path) for a concrete path."""
    return "//" + _gitignore_escape(Path(path).resolve().as_posix().lstrip("/"))


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcript_path(session_id):
    if not UUID_RE.match(session_id or ""):
        return None
    projects = config_dir() / "projects"
    if not projects.is_dir():
        return None
    return next(iter(sorted(projects.glob(f"*/{session_id}.jsonl"))), None)


def executable_reads(executables):
    """Install roots the sandboxed commands must read to run admitted executables."""
    import shutil
    from .permissions import executable_read_roots
    roots = set()
    for executable in executables or ():
        roots.update(executable_read_roots(executable))
        resolved = Path(shutil.which(str(executable))).resolve()
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", resolved.name):
            roots.add(str(resolved.parents[1]))  # the interpreter's stdlib and site-packages
    return sorted(roots)


def claude_settings(workspace, public, control, *, kind, write_paths=(), executables=()):
    """Settings for one actor activation; passed with --settings under --restricted.

    Bash is allowed explicitly and always runs inside the OS sandbox, which
    enforces the boundary as the Codex owner profile does: writes reach only
    the admitted paths and private .tmp, the network is off, and reads of the
    home folder and the control state are denied at the kernel. Only the
    workspace, the Master's public view, admitted executables and the CLI's
    own shell snapshots and session environment are re-opened. Unlike the
    Codex allowlist, world-readable files in the shared temp roots remain
    readable, because the CLI's sandboxed temp directory lives there. Claude Code
    (2.1.263, 2.1.295) forces permission mode default while
    the credential scrub is set and then refuses any Bash command it cannot
    prove safe, so the explicit allow is what lets unattended owners run
    tests. Its read block (blockReadsOutsideWorkingDirectories) is not used:
    with nobody to approve, it refuses commands that name the interpreter or
    compute a path at run time.
    """
    workspace = Path(workspace).resolve()
    public = Path(public).resolve()
    control = Path(control).resolve()
    home = Path.home().resolve()
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").resolve()
    allow = []
    if kind == "module":
        for relative in write_paths or ():
            target = workspace / relative
            allow += [f"Edit({_anchor(target)})", f"Edit({_anchor(target)}/**)"]
        allow.append(f"Edit({_anchor(workspace / '.tmp')}/**)")
    allow.append("Bash")
    deny_read = [str(home)] + [str(home / name) for name in SENSITIVE_READS] + [str(control)]
    allow_read = ([str(workspace)] + ([str(public)] if kind == "master" else []) + executable_reads(executables)
                  + [str(config_dir / "shell-snapshots"), str(config_dir / "session-env")])
    deny_write = [str(public)] if kind == "master" else []
    return {
        "permissions": {"defaultMode": "dontAsk", "allow": allow, "deny": [], "ask": []},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "excludedCommands": [],
            "filesystem": {"denyRead": deny_read, "allowRead": allow_read, "denyWrite": deny_write, "allowWrite": []},
            "network": {"allowedDomains": [], "deniedDomains": [], "strictAllowlist": True,
                        "allowLocalBinding": False, "allowUnixSockets": []},
        },
        "disableAllHooks": True,
        "enableAllProjectMcpServers": False,
        "autoMemoryEnabled": False,
    }


def claude_preflight(actor, receipt_path):
    """Static conformance of the generated configuration, recorded before any turn.

    This proves what is requested, not what the OS enforces; the init message of
    every run is checked again, and the live route probe is a separate experiment.
    """
    settings = actor.settings
    sandbox = settings["sandbox"]
    problems = []
    if not (sandbox["enabled"] and sandbox["failIfUnavailable"]) or sandbox["allowUnsandboxedCommands"]:
        problems.append("Bash sandbox is not strict")
    if sandbox["network"]["allowedDomains"] or not sandbox["network"]["strictAllowlist"]:
        problems.append("network egress is admitted")
    if sandbox["excludedCommands"]:
        problems.append("commands are excluded from the sandbox")
    allowed_roots = [actor.workspace / relative for relative in (actor.write_paths or ())] + [actor.workspace / ".tmp"]
    # The bare Bash rule admits commands that always run inside the strict
    # sandbox above; every other rule must be an Edit grant on admitted paths.
    edits = [rule for rule in settings["permissions"]["allow"] if rule != "Bash"]
    for rule in edits:
        if not rule.startswith("Edit(//") or not any(rule.startswith(f"Edit({_anchor(root)}") for root in allowed_roots):
            problems.append(f"allow rule outside admitted write paths: {rule}")
    if actor.kind == "master" and edits:
        problems.append("the Master has write rules")
    home = str(Path.home().resolve())
    denied = sandbox["filesystem"]["denyRead"]
    if home not in denied or str(actor.control) not in denied:
        problems.append("the home folder and control state are not read-denied")
    broad = {str(Path("/")), home, str(actor.control), *SHARED_TEMP_ROOTS}
    reopened = [path for path in sandbox["filesystem"]["allowRead"] if path in broad]
    if reopened:
        problems.append(f"overbroad read re-open: {reopened}")
    tools = set(actor.tools)
    if tools & FORBIDDEN_TOOLS:
        problems.append(f"forbidden tools requested: {sorted(tools & FORBIDDEN_TOOLS)}")
    forwarded = sorted(actor.environment())
    leaked = [name for name in forwarded if any(marker in name for marker in HOST_SESSION_MARKERS)]
    if leaked:
        problems.append(f"host session variables would reach the actor: {leaked}")
    report = {"passed": not problems, "problems": problems, "provider": "claude", "kind": actor.kind,
              "argv_flags": actor.static_flags(), "settings": settings, "environment_names": forwarded,
              "settings_sha256": hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest(),
              "scope": ("generated configuration only; Bash is OS-sandboxed, file tools are confined by Claude Code "
                        "permission rules (harness-level); init conformance is checked per run; live route probes "
                        "are a separate admitted experiment")}
    receipt_path = Path(receipt_path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(report, indent=2) + "\n")
    if problems:
        raise ContractError("Claude actor configuration refused: " + "; ".join(problems))
    return report


def _tool_result_text(block, extra=None):
    content = block.get("content")
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = content if isinstance(content, str) else ""
    if not text and isinstance(extra, dict):
        text = "\n".join(str(extra.get(name) or "") for name in ("stdout", "stderr")).strip()
    return text


def _command_item(tool_use, block, extra, cwd):
    text = _tool_result_text(block, extra)
    is_error = bool(block.get("is_error"))
    code = None
    match = EXIT_RE.search(text)
    if match:
        code = int(match.group(1))
    elif not is_error and not (isinstance(extra, dict) and extra.get("interrupted")):
        code = 0
    return {"id": tool_use["id"], "type": "commandExecution", "command": (tool_use.get("input") or {}).get("command"),
            "cwd": cwd, "status": "failed" if is_error else "completed", "exitCode": code, "aggregatedOutput": text}


class StreamState:
    """Incremental interpretation of one run's stream-json messages."""

    def __init__(self, thread_id, turn_id, cwd):
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.cwd = cwd
        self.tool_uses = {}
        self.commands = {}
        self.usage_by_message = {}
        self.result = None
        self.init = None
        self.compactions = 0
        self.messages = []

    def usage_total(self):
        totals = {"inputTokens": 0, "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "outputTokens": 0}
        # Assistant messages carry running partial output counts; the result
        # message carries this run's authoritative usage, so prefer it.
        final = (self.result or {}).get("usage")
        records = ([final] if isinstance(final, dict) and ("input_tokens" in final or "output_tokens" in final)
                   else list(self.usage_by_message.values()))
        for usage in records:
            fresh = usage.get("input_tokens") or 0
            read = usage.get("cache_read_input_tokens") or 0
            written = usage.get("cache_creation_input_tokens") or 0
            totals["inputTokens"] += fresh + read + written
            totals["cachedInputTokens"] += read
            totals["cacheWriteInputTokens"] += written
            totals["outputTokens"] += usage.get("output_tokens") or 0
        totals["totalTokens"] = totals["inputTokens"] + totals["outputTokens"]
        return totals

    def feed(self, message):
        kind, subtype = message.get("type"), message.get("subtype")
        if kind == "system" and subtype == "init":
            self.init = message
        elif kind == "system" and subtype == "compact_boundary":
            self.compactions += 1
        elif kind == "assistant":
            body = message.get("message") or {}
            identity = body.get("id") or message.get("uuid") or str(len(self.usage_by_message))
            if isinstance(body.get("usage"), dict) and message.get("parent_tool_use_id") is None:
                # Partial assistant messages repeat the same API response id;
                # count each response once.
                self.usage_by_message[identity] = body["usage"]
            for block in body.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    self.tool_uses[block["id"]] = block
                elif isinstance(block, dict) and block.get("type") == "text":
                    self.messages.append(block.get("text", ""))
        elif kind == "user":
            body = message.get("message") or {}
            content = body.get("content") if isinstance(body.get("content"), list) else []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use = self.tool_uses.get(block.get("tool_use_id"))
                if tool_use and tool_use.get("name") == "Bash" and len(self.commands) < MAX_COMMANDS:
                    item = _command_item(tool_use, block, message.get("tool_use_result"), self.cwd)
                    self.commands[item["id"]] = command_observation(item, thread_id=self.thread_id, turn_id=self.turn_id)
        elif kind == "result":
            self.result = message


def claude_trace_result(path):
    """(status, final_text) from a host-written stream trace, or (None, None)."""
    path = Path(path)
    if not path.is_file():
        return None, None
    result = None
    for line in path.read_text(errors="replace").splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("type") == "result":
            result = message
    if result is None:
        return None, None
    if result.get("subtype") == "success" and not result.get("is_error"):
        output = result.get("structured_output")
        return "completed", json.dumps(output) if output is not None else result.get("result", "")
    return "failed", None


def claude_trace_commands(path, *, thread_id, turn_id):
    """Command observations from a host-written stream trace, without inference."""
    path = Path(path)
    if not path.is_file():
        return [], f"host stream trace unavailable: {path.name}"
    state = StreamState(thread_id, turn_id, None)
    for line in path.read_text(errors="replace").splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            if message.get("type") == "system" and message.get("subtype") == "init":
                state.cwd = message.get("cwd")
            state.feed(message)
    if state.result is None or state.result.get("subtype") != "success":
        return list(state.commands.values()), "completed result absent from the host stream trace"
    return list(state.commands.values()), None


class ClaudeActor:
    """One host-chosen Claude Code session per registered actor."""
    provider = "claude"

    def __init__(self, actor_id, workspace: Path, context: Path, control: Path, *, kind, cli="claude",
                 model=None, effort=None, timeout=180, write_paths=None, runtime_executables=(), trace_dir=None):
        self.actor_id = actor_id
        self.workspace = Path(workspace).resolve()
        self.context = Path(context).resolve()
        self.control = Path(control).resolve()
        self.kind = kind
        self.cli = cli
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.write_paths = tuple(write_paths or ())
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.tools = OWNER_TOOLS if kind == "module" else MASTER_TOOLS
        self.settings = claude_settings(self.workspace, self.context, self.control, kind=kind, write_paths=self.write_paths,
                                        executables=runtime_executables)
        self.profile = f"claude-restricted-{kind}"
        self.config = {"settings": self.settings}
        self.thread_id = None
        self.session_id = None
        self.session_started = False
        self.instructions = ""
        self.effective = None
        self.events = []
        self.command_receipts = {}
        self.compactions = 0
        self.usage_base = 0
        self.usage_detail_base = {}
        self.process = None
        self.state = None
        self.trace_file = None
        self.version = None

    def static_flags(self):
        flags = ["-p", "--output-format", "stream-json", "--verbose", "--restricted", "--tools", ",".join(self.tools),
                 "--permission-mode", "dontAsk", "--permission-prompts", "none", "--strict-mcp-config",
                 "--mcp-config", json.dumps({"mcpServers": {}}), "--disable-slash-commands"]
        if self.kind == "master":
            flags += ["--add-dir", str(self.context)]
        return flags

    def set_usage_base(self, total, detail):
        self.usage_base = total if isinstance(total, int) else 0
        self.usage_detail_base = detail if isinstance(detail, dict) else {}

    async def _version(self):
        process = await asyncio.create_subprocess_exec(self.cli, "--version", stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 20)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ContractError("Claude CLI did not report its version")
        return out.decode(errors="replace").strip()

    async def open(self, *, existing_thread=None, instructions=""):
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.kind == "module":
            (self.workspace / ".tmp").mkdir(parents=True, exist_ok=True)
        try:
            text = await self._version()
        except OSError as error:
            raise ContractError(f"Claude CLI unavailable: {error}") from error
        version = parse_version(text)
        if version is None or version < MINIMUM_VERSION:
            raise ContractError(f"Claude CLI {text!r} is older than the required {'.'.join(map(str, MINIMUM_VERSION))}")
        session = existing_thread or str(uuid.uuid4())
        if not UUID_RE.match(session):
            raise AuthorityError("A Claude actor binding must be a host-chosen UUID session")
        # A bound session without a transcript never started its first turn;
        # the next turn creates it under the same host-chosen identity.
        self.session_started = bool(existing_thread) and transcript_path(session) is not None
        self.thread_id = self.session_id = session
        self.instructions = instructions
        self.version = text
        self.effective = {"provider": "claude", "cli_version": text, "model": self.model, "effort": self.effort,
                          "permission_mode": "dontAsk", "restricted": True, "tools": list(self.tools),
                          "session_started": self.session_started,
                          "settings_sha256": hashlib.sha256(json.dumps(self.settings, sort_keys=True).encode()).hexdigest()}
        return {"thread": {"id": session, "sessionId": session}}

    def argv(self, output_schema=None):
        argv = [self.cli, *self.static_flags(), "--settings", json.dumps(self.settings),
                "--append-system-prompt", self.instructions]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if output_schema:
            argv += ["--json-schema", json.dumps(output_schema)]
        argv += ["--resume", self.session_id] if self.session_started else ["--session-id", self.session_id]
        return argv

    def environment(self):
        names = set(INHERITED_ENVIRONMENT)
        names.update(name.strip() for name in os.environ.get(PASSTHROUGH_VARIABLE, "").split(",") if name.strip())
        env = {name: os.environ[name] for name in sorted(names)
               if name in os.environ and not any(marker in name for marker in HOST_SESSION_MARKERS)}
        env.setdefault("PATH", os.defpath)
        env.update(ENVIRONMENT)
        env["CLAUDE_CODE_TMPDIR"] = str(self.workspace / ".tmp") if self.kind == "module" else str(self.workspace)
        return env

    async def _reap(self):
        if self.process and self.process.returncode is None:
            await self.close()

    async def start_turn(self, message, *, output_schema=None, client_message_id=None):
        if not self.session_id:
            raise AuthorityError("No Claude session binding")
        await self._reap()
        turn_id = client_message_id or uuid.uuid4().hex
        self.command_receipts = {}
        self.state = StreamState(self.session_id, turn_id, str(self.workspace))
        if self.trace_dir:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            self.trace_file = self.trace_dir / f"{turn_id}.stream.jsonl"
        try:
            self.process = await asyncio.create_subprocess_exec(*self.argv(output_schema), cwd=str(self.workspace),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.environment(), start_new_session=True, limit=16 * 1024 * 1024)
        except OSError as error:
            raise NativeRejected(f"Claude CLI could not start: {error}") from error
        prompt = f"[runtime operation {turn_id}]\n{message}"
        try:
            self.process.stdin.write(prompt.encode())
            await self.process.stdin.drain()
            self.process.stdin.close()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise UnknownOutcome(f"Claude prompt delivery failed: {error}") from error
        return turn_id

    def _record(self, message):
        if self.trace_file is not None:
            with self.trace_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(message, separators=(",", ":")) + "\n")
        if len(self.events) < 2000:
            body = message.get("message") if isinstance(message.get("message"), dict) else {}
            blocks = body.get("content") if isinstance(body.get("content"), list) else []
            self.events.append({"type": message.get("type"), "subtype": message.get("subtype"),
                                "tool_uses": [b.get("name") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"],
                                "is_error": message.get("is_error")})

    def _check_init(self, init):
        problems = []
        if init.get("session_id") != self.session_id:
            problems.append(f"session {init.get('session_id')!r} replaced the bound session")
        if init.get("permissionMode") != EXPECTED_PERMISSION_MODE:
            problems.append(f"permission mode {init.get('permissionMode')!r} (expected {EXPECTED_PERMISSION_MODE!r})")
        tools = set(init.get("tools") or [])
        forbidden = sorted(name for name in tools if name in FORBIDDEN_TOOLS or name.startswith("mcp__"))
        if forbidden:
            problems.append(f"forbidden tools present: {forbidden}")
        if init.get("mcp_servers"):
            problems.append("MCP servers connected")
        # Plugins shipped inside the CLI binary report path 'builtin'; any
        # installed, marketplace or account plugin refuses the run.
        foreign = [plugin.get("source") or plugin.get("name") if isinstance(plugin, dict) else plugin
                   for plugin in init.get("plugins") or []
                   if not (isinstance(plugin, dict) and plugin.get("path") == "builtin"
                           and str(plugin.get("source", "")).endswith("@builtin"))]
        if foreign:
            problems.append(f"plugins loaded: {foreign}")
        if init.get("cwd") and Path(init["cwd"]).resolve() != self.workspace:
            problems.append(f"working directory {init.get('cwd')!r}")
        if self.model and self.model.startswith("claude-") and init.get("model") \
                and not str(init["model"]).startswith(self.model):
            # Aliases such as 'opus' resolve to full ids; explicit ids must not be substituted.
            problems.append(f"model {init.get('model')!r} substituted for {self.model!r}")
        return problems

    async def await_turn(self, turn_id, *, on_usage=None):
        if self.process is None or self.state is None:
            raise AuthorityError("No running Claude turn")
        deadline = time.monotonic() + self.timeout
        stderr_task = asyncio.create_task(self.process.stderr.read())
        reported = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UnknownOutcome("Claude turn exceeded its admitted deadline")
                try:
                    line = await asyncio.wait_for(self.process.stdout.readline(), remaining)
                except asyncio.TimeoutError as error:
                    raise UnknownOutcome("Claude turn did not report completion") from error
                except (ValueError, asyncio.LimitOverrunError) as error:
                    raise UnknownOutcome(f"Claude stream line exceeded the admitted size: {error}") from error
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                self._record(message)
                self.state.feed(message)
                if message.get("type") == "system" and message.get("subtype") == "init":
                    problems = self._check_init(message)
                    if problems:
                        await self.close()
                        raise AuthorityError("Claude runtime did not confirm the admitted configuration: " + "; ".join(problems))
                    self.session_started = True
                    self.effective.update(model=message.get("model"), claude_code_version=message.get("claude_code_version"),
                                          init_tools=sorted(message.get("tools") or []), api_key_source=message.get("apiKeySource"))
                usage = self.state.usage_total()
                if on_usage and usage["totalTokens"] != reported:
                    reported = usage["totalTokens"]
                    detail = {name: self.usage_detail_base.get(name, 0) + value for name, value in usage.items()}
                    # The provider reports cost with the result; until then the
                    # tokens are priced later, not unpriced.
                    on_usage(self.usage_base + usage["totalTokens"], detail, cost_pending=True)
                if self.state.result is not None:
                    break
            try:
                await asyncio.wait_for(self.process.wait(), 30)
            except asyncio.TimeoutError:
                pass  # A lingering process is reaped by close(); the result already arrived.
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        self.command_receipts = dict(self.state.commands)
        self.compactions += self.state.compactions
        result = self.state.result
        if result is None:
            raise NativeRejected(f"Claude exited with status {self.process.returncode} before a result")
        cost = result.get("total_cost_usd")
        if on_usage and isinstance(cost, (int, float)):
            usage = self.state.usage_total()
            detail = {name: self.usage_detail_base.get(name, 0) + value for name, value in usage.items()}
            on_usage(self.usage_base + usage["totalTokens"], detail, cost)
        if result.get("subtype") != "success" or result.get("is_error"):
            raise NativeRejected(f"Claude turn ended {result.get('subtype')}: {result.get('errors') or result.get('terminal_reason')}")
        output = result.get("structured_output")
        text = json.dumps(output) if output is not None else result.get("result", "")
        return {"turn": {"id": turn_id, "status": "completed"}, "text": text, "messages": self.state.messages,
                "cost_usd": cost}

    async def interrupt(self, turn_id):
        # SIGINT ends the running turn and records it; the session stays resumable.
        if self.process and self.process.returncode is None:
            self.process.send_signal(signal.SIGINT)
        return {}

    async def read(self):
        path = transcript_path(self.session_id)
        if path is None:
            return {"thread": {"id": self.session_id, "sessionId": self.session_id, "turns": []}}
        return {"thread": {"id": self.session_id, "sessionId": self.session_id,
                           "transcript": str(path), "entries": len(path.read_text(errors="replace").splitlines())}}

    async def set_goal(self, objective, token_budget):
        raise ContractError("Claude actors have no native goal primitive; the host owns the envelope")

    async def close(self):
        process = self.process
        if process is None:
            return {"process_exited": True, "reason": "no process started"}
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        await asyncio.wait_for(process.wait(), wait)
                        break
                    except asyncio.TimeoutError:
                        continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        return {"process_exited": process.returncode is not None, "exit_code": process.returncode,
                "effect_coverage": "owned claude process group; arbitrary escaped processes not certified"}
