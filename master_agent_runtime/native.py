"""Real Codex app-server transport and independent native root bindings.

No API keys, global configuration edits, parent forks, automatic escalation or
model/effort substitutions. JSON-RPC timeouts are unknown outcomes, not retries.
"""
from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
import json
import os
import signal
import shutil
import sys
import time

from .contracts import AuthorityError, ContractError, RuntimeErrorBase, UnknownOutcome
from .permissions import filesystem_profile


class NativeRejected(RuntimeErrorBase):
    pass


class ModelUnavailable(ContractError):
    """No native thread/turn has been requested; never silently substitute."""


class JsonRpcClient:
    def __init__(self, argv=("codex", "app-server", "--stdio"), *, cwd=None, timeout=30):
        self.argv = tuple(argv)
        self.cwd = cwd
        self.timeout = timeout
        self.process = None
        self._reader = None
        self._errors = None
        self._serial = 0
        self._pending = {}
        self.notifications = asyncio.Queue()
        self.stderr = deque(maxlen=100)
        self.late_responses = {}
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    async def start(self):
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        self.process = await asyncio.create_subprocess_exec(*self.argv, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024, **kwargs)
        self._reader = asyncio.create_task(self._read())
        self._errors = asyncio.create_task(self._read_errors())
        return self

    async def _send(self, message):
        if not self.process or self.process.returncode is not None:
            raise UnknownOutcome("Native transport is not available")
        async with self._write_lock:
            self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            await self.process.stdin.drain()

    async def call(self, method, params=None, *, timeout=None):
        self._serial += 1
        request_id = self._serial
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"id": request_id, "method": method, "params": params or {}})
        try:
            # Shield retains late replies for actual reconciliation rather than
            # cancelling a future that may describe an already-started effect.
            return await asyncio.wait_for(asyncio.shield(future), timeout or self.timeout)
        except asyncio.TimeoutError as error:
            raise UnknownOutcome(f"Native {method} acknowledgement timed out (request {request_id})") from error

    async def notify(self, method, params=None):
        await self._send({"method": method, "params": params or {}})

    async def _read_errors(self):
        while line := await self.process.stderr.readline():
            self.stderr.append(line.decode(errors="replace").rstrip())

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "id" in message and "method" not in message:
                    future = self._pending.pop(message["id"], None)
                    self.late_responses[message["id"]] = message
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(NativeRejected(str(message["error"])))
                        else:
                            future.set_result(message.get("result"))
                elif "id" in message:
                    # This initial adapter never grants extra permissions or
                    # answers an approval by escalating another execution path.
                    method = message.get("method", "")
                    if "requestApproval" in method:
                        await self._send({"id": message["id"], "result": {"decision": "decline"}})
                    else:
                        await self._send({"id": message["id"], "error": {
                            "code": -32601, "message": "Unadmitted native client request"}})
                    await self.notifications.put({"method": "runtime/request-denied", "params": {"method": method}})
                else:
                    await self.notifications.put(message)
        except (ValueError, ConnectionError, OSError) as error:
            await self.notifications.put({"method": "runtime/transport-failed", "params": {"error": str(error)}})
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(UnknownOutcome("Native connection ended before acknowledgement"))
            self._pending.clear()
            await self.notifications.put({"method": "runtime/transport-ended", "params": {}})

    async def initialize(self):
        result = await self.call("initialize", {"clientInfo": {
            "name": "master-agent-runtime", "title": "Independent module runtime", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}})
        await self.notify("initialized")
        return result

    async def close(self):
        async with self._close_lock:
            return await self._close_owned()

    async def _close_owned(self):
        if not self.process:
            return {"process_exited": True, "reason": "not started"}
        process = self.process
        if process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    await process.wait()
        if os.name=='posix':
            # Reap remaining members of the process group we created; closing
            # only its leader can leave background writers holding the pipes.
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
        tasks=[task for task in (self._reader,self._errors) if task]
        if tasks:
            done,pending=await asyncio.wait(tasks,timeout=2)
            for task in pending:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        return {"process_exited": process.returncode is not None, "exit_code": process.returncode,
                "effect_coverage": "owned native service/process group; arbitrary escaped processes not certified"}


def permission_config(workspace: Path, context: Path, control: Path, *, readonly=False, cli="codex",write_paths=None,runtime_executables=()) -> tuple[str, dict]:
    """Ephemeral, narrow profile; no mutation of the operator's config file."""
    name = "master-runtime-review" if readonly else "master-runtime-module"
    profile = filesystem_profile(workspace,(context,),writable=not readonly,executables=(cli,*runtime_executables),
        denied=(control,Path.home()/'.codex'/'auth.json',Path.home()/'.aws',Path.home()/'.ssh'),write_paths=write_paths)
    fs = profile['filesystem']
    global_instructions = Path.home() / ".codex" / "AGENTS.md"
    if global_instructions.exists():
        fs[str(global_instructions)] = "read"
    # Do not extend :workspace: its implicit global temp writes would broaden
    # the actor beyond its own writable namespace. Workspace git/config policy
    # subtrees are explicitly read-only, including a future created subtree.
    for part in (".git", ".gitignore", ".codex", ".agents"):
        fs[str(workspace.resolve() / part)] = "read"
    private_temp = workspace.resolve()/'.tmp'
    if not readonly:
        private_temp.mkdir(parents=True,exist_ok=True)
    config = {"permissions": {name: profile},
              "web_search": "disabled", "features": {name:False for name in (
                  'apps','plugins','remote_plugin','hooks','browser_use','browser_use_external',
                  'browser_use_full_cdp_access','computer_use','in_app_browser','image_generation',
                  'in_app_local_automation','goals','skill_mcp_dependency_install')},
              "agents": {"enabled": False}, "allow_login_shell": False,
              "shell_environment_policy": {"inherit": "none", "ignore_default_excludes": False,
                  "set": {"PATH": os.environ.get("PATH", os.defpath), "LANG": "en_US.UTF-8",
                          "PYTHONDONTWRITEBYTECODE": "1",'TMPDIR':str(private_temp),
                          'TMP':str(private_temp),'TEMP':str(private_temp)}}}
    # Names only are examined, never credential values. Existing MCP transports
    # are separate effects and are not admitted by this local-code first slice.
    import tomllib
    config_path = Path.home() / ".codex" / "config.toml"
    if config_path.is_file():
        with config_path.open("rb") as stream:
            operator = tomllib.load(stream)
            names = operator.get("mcp_servers", {}).keys()
        for server in names:
            config[f"mcp_servers.{server}.enabled"] = False
        config['plugins']={name:{'enabled':False} for name in operator.get('plugins',{})}
    return name, config


class NativeActor:
    """One independently launched/resumable root session per registered actor."""
    def __init__(self, actor_id, workspace: Path, context: Path, control: Path, *, kind,
                 cli="codex", model=None, effort=None, timeout=180,write_paths=None,runtime_executables=()):
        self.actor_id = actor_id
        self.workspace = workspace.resolve()
        self.context = context.resolve()
        self.control = control.resolve()
        self.kind = kind
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.client = JsonRpcClient((cli, "app-server", "--stdio"), cwd=str(self.workspace))
        self.thread_id = None
        self.session_id = None
        self.effective = None
        self.events = []
        self.command_receipts = {}
        self.command_output = {}
        self.command_output_truncated = set()
        self.profile, self.config = permission_config(self.workspace, self.context, self.control,
                                                      readonly=kind == "master", cli=cli,write_paths=write_paths,runtime_executables=runtime_executables)

    async def open(self, *, existing_thread=None, instructions=""):
        self.workspace.mkdir(parents=True, exist_ok=True)
        await self.client.start()
        await self.client.initialize()
        config = (await self.client.call('config/read',{'includeLayers':False})).get('config',{})
        selected = self.model or config.get('model')
        provider = config.get('model_provider') or 'openai'
        if provider == 'openai':
            catalog = await self.client.call('model/list',{})
            models = catalog.get('data',[])
            available = {item.get('model') or item.get('id'):item for item in models}
            if selected and selected not in available:
                raise ModelUnavailable(f"Configured model {selected!r} is not in this native account/client catalog; "
                    f"choose an explicit project model from {sorted(available)}. Global settings were preserved.")
            effort = self.effort or config.get('model_reasoning_effort')
            if selected and effort:
                allowed = {item['reasoningEffort'] for item in available[selected].get('supportedReasoningEfforts',[])}
                if allowed and effort not in allowed:
                    raise ModelUnavailable(f"Reasoning effort {effort!r} is unsupported for the selected model")
        params = {"cwd": str(self.workspace), "permissions": self.profile,
                  "approvalPolicy": "never", "config": self.config}
        if self.model:
            params["model"] = self.model
        if existing_thread:
            params["threadId"] = existing_thread
            result = await self.client.call("thread/resume", params)
        else:
            params["ephemeral"] = False
            params["developerInstructions"] = instructions
            params["serviceName"] = "master-agent-runtime"
            result = await self.client.call("thread/start", params)
        thread = result["thread"]
        if thread.get("ephemeral") or thread.get("sessionId") != thread.get("id"):
            raise AuthorityError("Native backend did not create/resume an independent durable root")
        if existing_thread and thread["id"] != existing_thread:
            raise AuthorityError("Native resume replaced module context")
        actual = result.get("activePermissionProfile")
        if not actual or actual.get("id") != self.profile or result.get("approvalPolicy") != "never":
            raise AuthorityError("Native runtime did not confirm the exact scoped profile and non-escalating policy")
        if self.model and result.get("model") != self.model:
            raise AuthorityError("Native runtime substituted the explicitly admitted model")
        self.thread_id = thread["id"]
        self.session_id = thread["sessionId"]
        self.effective = {"model": result.get("model"), "model_provider": result.get("modelProvider"),
                          "reasoning_effort": result.get("reasoningEffort"),
                          "explicit_turn_effort": self.effort,
                          "approval_policy": result.get("approvalPolicy"),
                          "permission_profile": actual}
        return result

    async def start_turn(self, message, *, output_schema=None, client_message_id=None):
        if not self.thread_id:
            raise AuthorityError("No native binding")
        params = {"threadId": self.thread_id, "input": [{"type": "text", "text": message}]}
        if self.effort:
            params["effort"] = self.effort
        if output_schema:
            params["outputSchema"] = output_schema
        if client_message_id:
            params["clientUserMessageId"] = client_message_id
        result = await self.client.call("turn/start", params)
        return result["turn"]["id"]

    async def await_turn(self, turn_id, *, on_usage=None):
        deadline = time.monotonic() + self.timeout
        final = []
        active = False
        self.command_receipts = {}
        self.command_output = {}
        self.command_output_truncated = set()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UnknownOutcome("Native turn exceeded its admitted deadline")
            try:
                event = await asyncio.wait_for(self.client.notifications.get(), remaining)
            except asyncio.TimeoutError as error:
                raise UnknownOutcome("Native turn did not report completion") from error
            method = event.get("method", "")
            params = event.get("params", {})
            if method.startswith("runtime/transport"):
                raise UnknownOutcome("Native connection ended with unresolved work")
            if params.get("threadId") != self.thread_id:
                continue
            if params.get('turnId') == turn_id:
                from .evidence import command_observation, MAX_COMMAND_OUTPUT, MAX_COMMANDS
                if method == 'item/commandExecution/outputDelta':
                    key = params.get('itemId')
                    previous = self.command_output.get(key, '')
                    combined = previous + params.get('delta', '')
                    if len(combined.encode('utf-8')) > MAX_COMMAND_OUTPUT: self.command_output_truncated.add(key)
                    self.command_output[key] = combined.encode('utf-8')[:MAX_COMMAND_OUTPUT].decode('utf-8', errors='replace')
                elif method == 'item/completed' and params.get('item', {}).get('type') == 'commandExecution':
                    item = params['item']
                    if isinstance(item.get('id'), str) and len(self.command_receipts) < MAX_COMMANDS:
                        self.command_receipts[item['id']] = command_observation(item,
                            thread_id=self.thread_id, turn_id=turn_id,
                            streamed_output=self.command_output.get(item['id'], ''))
                        if item.get('aggregatedOutput') is None and item['id'] in self.command_output_truncated:
                            self.command_receipts[item['id']]['output_truncated'] = True
            if len(self.events) < 2000:
                item = params.get('item', {})
                self.events.append({'method':method,'turn_id':params.get('turnId'),
                    'item_type':item.get('type'),'item_id':item.get('id'),
                    'status':item.get('status'),'exit_code':item.get('exitCode'),
                    'command':item.get('command'),'changes':item.get('changes'),
                    'phase':item.get('phase')})
            if method == "turn/started" and params.get("turn", {}).get("id") == turn_id:
                active = True
            if method == "thread/tokenUsage/updated" and on_usage and active:
                total = params.get("tokenUsage", {}).get("total", {}).get("totalTokens")
                if isinstance(total, int):
                    on_usage(total)
            if method == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    final.append(item.get("text", ""))
            if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                turn = params["turn"]
                if turn.get("status") != "completed":
                    raise NativeRejected(str(turn.get("error") or turn.get("status")))
                return {"turn": turn, "text": final[-1] if final else "", "messages": final}

    async def interrupt(self, turn_id):
        return await self.client.call("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id})

    async def read(self):
        return await self.client.call("thread/read", {"threadId": self.thread_id, "includeTurns": True})

    async def set_goal(self, objective, token_budget):
        # Public native primitive available to an explicitly admitted module
        # goal. Not auto-enabled for an indefinitely waiting Master.
        return await self.client.call("thread/goal/set", {"threadId": self.thread_id,
            "objective": objective, "tokenBudget": token_budget, "status": "active"})

    async def close(self):
        return await self.client.close()
