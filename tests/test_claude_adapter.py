"""Claude adapter tests against a labelled fake `claude` executable (no model, no network).

The fake speaks the documented stream-json message shapes. These tests prove
the adapter's protocol handling, configuration and controller integration;
they are not conformance evidence for the real Claude Code sandbox.
"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from master_agent_runtime.claude_native import (ClaudeActor, StreamState, claude_preflight, claude_trace_commands,
                                                claude_trace_result)
from master_agent_runtime.contracts import AuthorityError, ContractError
from master_agent_runtime.controller import Controller
from master_agent_runtime.native import NativeRejected
from master_agent_runtime.recovery import reconcile

from runtime_fixtures import events, fixture_validator, make_project

FAKE = r'''import json, os, pathlib, re, signal, sys, time
argv = sys.argv[1:]
if argv == ['--version']:
    print('2.1.263 (Claude Code)'); sys.exit(0)
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
prompt = sys.stdin.read()
session = opt('--session-id') or opt('--resume')
config = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
with open(os.environ['FAKE_CLAUDE_LOG'], 'a') as log:
    log.write(json.dumps({'argv': argv, 'cwd': os.getcwd(), 'resumed': '--resume' in argv,
                          'tmp': os.environ.get('CLAUDE_CODE_TMPDIR'), 'scrub': os.environ.get('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB')}) + '\n')
emit = lambda message: print(json.dumps(message), flush=True)
result = lambda **fields: emit({'type': 'result', 'session_id': session, 'usage': {}, 'modelUsage': {}, 'num_turns': 1,
                                'duration_ms': 1, 'duration_api_ms': 1, 'permission_denials': [], 'stop_reason': None, **fields})
if '--resume' in argv and not list(config.glob(f'projects/*/{session}.jsonl')):
    result(subtype='error_during_execution', is_error=True, errors=['No conversation found'], total_cost_usd=0); sys.exit(1)
transcript = config / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', os.getcwd()) / f'{session}.jsonl'
if '--resume' in argv:
    transcript = next(config.glob(f'projects/*/{session}.jsonl'))
transcript.parent.mkdir(parents=True, exist_ok=True)
with transcript.open('a') as stream:
    stream.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': prompt[:120]}}) + '\n')
runs = len(transcript.read_text().splitlines())
mode = os.environ.get('FAKE_CLAUDE_MODE', 'normal')
tools = (opt('--tools') or '').split(',') + ['EndConversation'] + (['WebFetch'] if mode == 'forbidden_tools' else [])
emit({'type': 'system', 'subtype': 'init', 'uuid': 'u0', 'session_id': session, 'cwd': os.getcwd(), 'tools': tools,
      'mcp_servers': [],
      # A logged-in CLI reports its built-in plugins; 'plugins' mode adds an installed one.
      'plugins': [{'name': 'cc-plugin-agents-md', 'path': 'builtin', 'source': 'cc-plugin-agents-md@builtin'}]
                 + ([{'name': 'helper', 'path': '/plugins/helper', 'source': 'helper@market'}] if mode == 'plugins' else []),
      'model': opt('--model') or 'claude-opus-5-5',
      # Like the real CLI: the subprocess credential scrub forces default mode.
      'permissionMode': 'acceptEdits' if mode == 'accept_edits' else
                        ('default' if os.environ.get('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB') == '1' else opt('--permission-mode')),
      'claude_code_version': '2.1.263', 'apiKeySource': 'none'})
if mode == 'hang':
    def stop(*_):
        result(subtype='error_during_execution', is_error=True, errors=['interrupted'], total_cost_usd=0.01,
               terminal_reason='aborted_streaming'); sys.exit(130)
    signal.signal(signal.SIGINT, stop)
    while True: time.sleep(0.02)
if mode == 'error':
    result(subtype='error_during_execution', is_error=True, errors=['model_error'], total_cost_usd=0); sys.exit(1)
schema = json.loads(opt('--json-schema') or '{}')
usage = {'input_tokens': 10, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 50, 'output_tokens': 40}
emit({'type': 'assistant', 'uuid': 'a1', 'session_id': session, 'parent_tool_use_id': None,
      'message': {'id': 'msg_1', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'Working.'}], 'usage': usage}})
if 'status' in schema.get('properties', {}):
    paths = json.loads(re.search(r"Write only admitted module paths in your current workspace: (\[.*?\])", prompt).group(1).replace("'", '"'))
    emit({'type': 'assistant', 'uuid': 'a2', 'session_id': session, 'parent_tool_use_id': None,
          'message': {'id': 'msg_1', 'role': 'assistant', 'usage': usage,
                      'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash', 'input': {'command': 'python3 -c "print(42)"'}}]}})
    emit({'type': 'user', 'uuid': 'u1', 'session_id': session, 'parent_tool_use_id': None,
          'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': '42', 'is_error': False}]},
          'tool_use_result': {'stdout': '42', 'stderr': '', 'interrupted': False}})
    for path in paths:
        target = pathlib.Path(os.getcwd()) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('value = 42\n')
    output = {'status': 'submitted', 'summary': 'Fake owner wrote value 42', 'issues': []}
else:
    marker = os.environ.get('FAKE_CLAUDE_REJECT_FIRST')
    reject = bool(marker) and 'Review module' in prompt and not os.path.exists(marker)
    if reject:
        open(marker, 'w').write('rejected once')
    output = {}
    for key in schema.get('required', []):
        output[key] = {'decision': 'request_changes' if reject else 'approve',
                       'rationale': 'Rename the helper to match the interface' if reject else 'Fake review of sealed files',
                       'failure_kind': 'implementation' if reject else 'none',
                       'blocking_issues': [{'obligation': 'interface', 'evidence': 'a.py:1', 'required_change': 'Rename the helper'}] if reject else []
                       }.get(key, [])
emit({'type': 'assistant', 'uuid': 'a3', 'session_id': session, 'parent_tool_use_id': None,
      'message': {'id': 'msg_2', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'Done.'}],
                  'usage': {'input_tokens': 5, 'cache_read_input_tokens': 1100, 'cache_creation_input_tokens': 0, 'output_tokens': 20}}})
result(subtype='success', is_error=False, result='Done.', structured_output=output, total_cost_usd=0.05 * runs, stop_reason='end_turn')
'''


class FakeClaude:
    def __init__(self, root):
        self.root = Path(root)
        self.cli = self.root / 'bin' / 'claude'
        self.cli.parent.mkdir(parents=True)
        # A shell wrapper with a quoted interpreter path also works when the
        # interpreter or checkout path contains spaces, which a #! line cannot.
        body = self.root / 'bin' / 'claude_fake.py'
        body.write_text(FAKE)
        self.cli.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(body))} "$@"\n')
        self.cli.chmod(0o755)
        self.log = self.root / 'calls.jsonl'
        self.config = self.root / 'claude-config'
        self.env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config), 'FAKE_CLAUDE_LOG': str(self.log),
                                           'MASTER_AGENT_CLAUDE_ENV': 'FAKE_CLAUDE_LOG,FAKE_CLAUDE_MODE,FAKE_CLAUDE_REJECT_FIRST'})

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


class ClaudeActorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeClaude(self.root)
        self.fake.env.start()
        self.addCleanup(self.fake.env.stop)
        self.control = self.root / 'control'
        self.public = self.control / 'projects' / 'p' / 'public'
        self.public.mkdir(parents=True)

    def actor(self, kind='module', key='w1', **kwargs):
        workspace = self.control / 'projects' / 'p' / 'workspaces' / key
        return ClaudeActor('p:module:a' if kind == 'module' else 'p:master', workspace, self.public, self.control,
                           kind=kind, cli=str(self.fake.cli), write_paths=('src', 'a.py') if kind == 'module' else None,
                           trace_dir=self.control / 'traces', timeout=30, **kwargs)

    async def test_configuration_confines_writes_tools_network_and_settings_sources(self):
        owner, master = self.actor(), self.actor('master', 'master')
        rules = owner.settings['permissions']['allow']
        workspace = owner.workspace.as_posix().lstrip('/')
        self.assertEqual(rules, [f'Edit(//{workspace}/src)', f'Edit(//{workspace}/src/**)', f'Edit(//{workspace}/a.py)',
                                 f'Edit(//{workspace}/a.py/**)', f'Edit(//{workspace}/.tmp/**)', 'Bash'])
        sandbox = owner.settings['sandbox']
        self.assertTrue(sandbox['enabled'] and sandbox['failIfUnavailable'])
        self.assertFalse(sandbox['allowUnsandboxedCommands'])
        self.assertEqual(sandbox['network']['allowedDomains'], [])
        denied = sandbox['filesystem']['denyRead']
        for path in (Path.home().resolve(), Path.home().resolve() / '.ssh', self.control.resolve()):
            self.assertIn(str(path), denied)
        self.assertNotIn('blockReadsOutsideWorkingDirectories', owner.settings['permissions'])
        reads = sandbox['filesystem']['allowRead']
        self.assertEqual(reads[0], str(owner.workspace))
        self.assertNotIn(str(self.public.resolve()), reads)                 # owners never read the shared view
        self.assertIn(str(self.public.resolve()), master.settings['sandbox']['filesystem']['allowRead'])
        self.assertNotIn(str(Path.home().resolve()), reads)
        python = self.actor(key='w2', runtime_executables=(sys.executable,))
        self.assertIn(str(Path(sys.executable).resolve().parents[1]), python.settings['sandbox']['filesystem']['allowRead'])
        self.assertEqual(master.settings['permissions']['allow'], ['Bash'])
        self.assertEqual(master.settings['sandbox']['filesystem']['denyWrite'], [str(self.public.resolve())])
        flags = owner.static_flags()
        for flag in ('--restricted', '--strict-mcp-config', '--disable-slash-commands'):
            self.assertIn(flag, flags)
        self.assertEqual(flags[flags.index('--tools') + 1], 'Read,Edit,Write,Glob,Grep,Bash')
        self.assertEqual(flags[flags.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(flags[flags.index('--permission-prompts') + 1], 'none')
        self.assertNotIn('--add-dir', flags)
        self.assertEqual(master.static_flags()[master.static_flags().index('--add-dir') + 1], str(self.public.resolve()))
        receipt = self.root / 'receipt.json'
        self.assertTrue(claude_preflight(owner, receipt)['passed'])
        exposed = self.actor(key='w3')
        exposed.settings['sandbox']['filesystem']['denyRead'].remove(str(Path.home().resolve()))
        with self.assertRaisesRegex(ContractError, 'not read-denied'):
            claude_preflight(exposed, self.root / 'exposed.json')
        owner.settings['sandbox']['network']['allowedDomains'] = ['example.com']
        with self.assertRaises(ContractError):
            claude_preflight(owner, receipt)
        self.assertFalse(json.loads(receipt.read_text())['passed'])

    async def test_actor_environment_never_carries_the_launching_host_session(self):
        host = {'CLAUDECODE': '1', 'CLAUDE_CODE_MESSAGING_SOCKET': '/tmp/host.sock', 'CLAUDE_CODE_MESSAGING_TOKEN': 'secret',
                'CLAUDE_CODE_SESSION_ID': 'host', 'CLAUDE_CODE_CHILD_SESSION': '1', 'CLAUDE_PID': '1',
                'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9/proxy', 'HTTPS_PROXY': 'http://127.0.0.1:9',
                'CLAUDE_CODE_OAUTH_TOKEN': 'token', 'OPENAI_API_KEY': 'other', 'UNRELATED': 'x'}
        named = 'FAKE_CLAUDE_LOG,CLAUDE_CODE_MESSAGING_TOKEN,CLAUDE_CODE_SESSION_ID,ANTHROPIC_API_KEY'
        with patch.dict(os.environ, {**host, 'ANTHROPIC_API_KEY': 'operator-key', 'MASTER_AGENT_CLAUDE_ENV': named}):
            owner = self.actor()
            env = owner.environment()
            receipt = claude_preflight(owner, self.root / 'environment-receipt.json')
        for name in host:
            self.assertNotIn(name, env)
        self.assertEqual(env['ANTHROPIC_API_KEY'], 'operator-key')          # deliberately forwarded by name
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.fake.config))
        self.assertEqual(env['CLAUDE_CODE_TMPDIR'], str(owner.workspace / '.tmp'))
        self.assertEqual(env['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'], '1')
        self.assertEqual(receipt['environment_names'], sorted(env))
        self.assertNotIn('operator-key', json.dumps(receipt))                # names only, never values

    async def test_host_chosen_session_is_created_then_resumed_with_usage_receipts_and_trace(self):
        owner = self.actor()
        await owner.open(instructions='Owner instructions')
        session = owner.session_id
        self.assertFalse(owner.session_started)
        owner.set_usage_base(500, {'outputTokens': 7})
        seen = []
        turn = await owner.start_turn('Write only admitted module paths in your current workspace: [\'a.py\']',
                                      output_schema={'type': 'object', 'properties': {'status': {}}, 'required': ['status']},
                                      client_message_id='op-1')
        result = await owner.await_turn(turn, on_usage=lambda total, detail=None, cost=None, **flags: seen.append((total, detail, cost)))
        self.assertEqual(json.loads(result['text'])['status'], 'submitted')
        self.assertEqual((owner.workspace / 'a.py').read_text(), 'value = 42\n')
        final_total, final_detail, cost = seen[-1]
        # msg_1 is counted once although two stream messages repeat its usage.
        self.assertEqual(final_total, 500 + (10 + 1000 + 50 + 40) + (5 + 1100 + 20))
        self.assertEqual(final_detail['cachedInputTokens'], 2100)
        self.assertEqual(final_detail['outputTokens'], 7 + 60)
        self.assertAlmostEqual(cost, 0.05)
        receipt = owner.command_receipts['toolu_1']
        self.assertEqual((receipt['command'], receipt['output'], receipt['exit_code'], receipt['thread_id'], receipt['turn_id']),
                         ('python3 -c "print(42)"', '42', 0, session, 'op-1'))
        trace = self.control / 'traces' / 'op-1.stream.jsonl'
        self.assertEqual(claude_trace_result(trace)[0], 'completed')
        commands, error = claude_trace_commands(trace, thread_id=session, turn_id='op-1')
        self.assertIsNone(error)
        self.assertEqual([c['item_id'] for c in commands], ['toolu_1'])
        self.assertTrue((await owner.close())['process_exited'])
        resumed = self.actor(key='w2')
        await resumed.open(existing_thread=session, instructions='Owner instructions')
        self.assertTrue(resumed.session_started)
        await resumed.await_turn(await resumed.start_turn("Write only admitted module paths in your current workspace: ['a.py']",
            output_schema={'type': 'object', 'properties': {'status': {}}, 'required': ['status']}, client_message_id='op-2'))
        calls = self.fake.calls()
        self.assertEqual([(c['resumed'], c['argv'][c['argv'].index('--resume' if c['resumed'] else '--session-id') + 1]) for c in calls],
                         [(False, session), (True, session)])
        self.assertTrue(all(c['scrub'] == '1' and c['tmp'].endswith('.tmp') for c in calls))

    async def test_runtime_that_contradicts_the_admitted_tools_is_refused_and_stopped(self):
        owner = self.actor()
        await owner.open()
        with patch.dict(os.environ, {'FAKE_CLAUDE_MODE': 'forbidden_tools'}):
            turn = await owner.start_turn('x', client_message_id='op-x')
            with self.assertRaises(AuthorityError) as caught:
                await owner.await_turn(turn)
        self.assertIn('WebFetch', str(caught.exception))
        self.assertTrue((await owner.close())['process_exited'])

    async def test_any_other_reported_permission_mode_is_refused(self):
        owner = self.actor()
        await owner.open(instructions='Owner instructions')
        with patch.dict(os.environ, {'FAKE_CLAUDE_MODE': 'accept_edits'}):
            turn = await owner.start_turn('Work')
            with self.assertRaisesRegex(AuthorityError, 'acceptEdits'):
                await owner.await_turn(turn)
        await owner.close()

    def test_result_usage_replaces_partial_stream_counts(self):
        # Observed on 2.1.295: assistant messages report running partial output
        # counts; the result carries the run's real usage.
        state = StreamState('t', 'u', '/')
        state.feed({'type': 'assistant', 'message': {'id': 'm1', 'content': [], 'usage': {
            'input_tokens': 6, 'cache_read_input_tokens': 29038, 'cache_creation_input_tokens': 6842, 'output_tokens': 37}}})
        self.assertEqual(state.usage_total()['outputTokens'], 37)
        state.feed({'type': 'result', 'subtype': 'success', 'usage': {
            'input_tokens': 6, 'cache_read_input_tokens': 29038, 'cache_creation_input_tokens': 6842, 'output_tokens': 2512}})
        total = state.usage_total()
        self.assertEqual((total['inputTokens'], total['outputTokens'], total['totalTokens']), (35886, 2512, 38398))

    async def test_installed_plugins_refuse_the_run_but_built_in_ones_do_not(self):
        owner = self.actor()
        await owner.open(instructions='Owner instructions')
        with patch.dict(os.environ, {'FAKE_CLAUDE_MODE': 'plugins'}):
            turn = await owner.start_turn('Work')
            with self.assertRaisesRegex(AuthorityError, 'helper@market'):
                await owner.await_turn(turn)
        await owner.close()

    async def test_interrupt_ends_the_turn_and_exit_is_observed(self):
        owner = self.actor()
        await owner.open()
        with patch.dict(os.environ, {'FAKE_CLAUDE_MODE': 'hang'}):
            turn = await owner.start_turn('x', client_message_id='op-h')
            waiting = asyncio.create_task(owner.await_turn(turn))
            await asyncio.sleep(0.5)
            await owner.interrupt(turn)
            with self.assertRaises(NativeRejected):
                await waiting
        self.assertTrue((await owner.close())['process_exited'])

    async def test_error_result_is_a_definitive_native_failure(self):
        owner = self.actor()
        await owner.open()
        with patch.dict(os.environ, {'FAKE_CLAUDE_MODE': 'error'}):
            with self.assertRaises(NativeRejected):
                await owner.await_turn(await owner.start_turn('x', client_message_id='op-e'))


class ClaudeControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_master_and_owners_deliver_with_same_session_rework(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = FakeClaude(root / 'fake')
            marker = root / 'rejected-once'
            store, pid = make_project(root, [('a', ()), ('b', ('a',))], policy={'provider': 'claude'})
            with fake.env, patch.dict(os.environ, {'FAKE_CLAUDE_REJECT_FIRST': str(marker)}), \
                    patch('master_agent_runtime.controller.validate', fixture_validator):
                result = await Controller(store, pid, cli='codex-not-needed-for-claude-actors',
                                          claude_cli=str(fake.cli)).run()
            self.assertEqual(result['state'], 'accepted')
            self.assertTrue(marker.exists())
            owner = store.actor(pid + ':module:a')
            owner_calls = [c for c in fake.calls() if c['argv'][c['argv'].index('--tools') + 1].startswith('Read,Edit')
                           and (owner['thread_id'] in c['argv'])]
            self.assertEqual([c['resumed'] for c in owner_calls], [False, True])   # rework resumed the same session
            runtime = [payload for _, payload in events(store, pid, 'native-runtime-observed')]
            self.assertTrue(runtime and all(item['provider'] == 'claude' and item['cli_version'] for item in runtime))
            status = store.status(pid)
            self.assertGreater(status['spent_tokens'], 0)
            self.assertGreater(status['estimated_cost_usd'], 0)
            self.assertEqual(status['reserved_tokens'], 0)
            accepted = store.candidate(next(m for m in status['modules'] if m['key'] == 'a')['accepted_candidate'])
            index = json.loads((Path(accepted['evidence']) / 'index.json').read_text())
            self.assertEqual([c['command_preview'] for c in index['commands']], ['python3 -c "print(42)"'])
            preflights = list((store.path.parent / 'projects' / pid / 'public' / 'validation').glob('scope-*.json'))
            self.assertTrue(preflights and all(json.loads(p.read_text())['passed'] for p in preflights))

    async def test_reconcile_uses_the_host_trace_and_settles_only_observed_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = make_project(Path(tmp), [('one', ())], policy={'provider': 'claude'})
            master = store.bind(pid + ':master', '00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000001')
            owner = store.bind(pid + ':module:one', '00000000-0000-4000-8000-000000000002', '00000000-0000-4000-8000-000000000002')
            op = store.dispatch(master, owner.actor_id, 'Work'); store.mark_started(op, op); store.mark_unknown(op, 'lost')
            report = await reconcile(store, pid, cli='codex-unused')
            self.assertEqual(report['held'][0]['operation'], op)
            store.note_exit_observed(op)
            report = await reconcile(store, pid, cli='codex-unused')
            self.assertEqual(report['reconciled'][0]['state'], 'failed')
            self.assertEqual(store.modules(pid)[0]['state'], 'continuing')
