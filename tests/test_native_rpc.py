"""Transport tests use a real subprocess with a labelled fake protocol peer."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from master_agent_runtime.contracts import AuthorityError, UnknownOutcome
from master_agent_runtime.native import JsonRpcClient, ModelUnavailable, NativeActor, permission_config


PEER = '''import json,sys,time
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m:continue
 method=m['method']
 if method=='slow':time.sleep(.10)
 if method=='event':
  print(json.dumps({'method':'thread/started','params':{'threadId':'root'}}),flush=True)
 if method=='approval':
  print(json.dumps({'id':'native-approval','method':'item/commandExecution/requestApproval','params':{}}),flush=True)
  reply=json.loads(sys.stdin.readline())
  result={'decision':reply['result']['decision']}
 else:result={'method':method,'request':m['id']}
 print(json.dumps({'id':m['id'],'result':result}),flush=True)
'''


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.peer = self.root / 'fake_native_peer.py'
        self.peer.write_text(PEER)
        self.client = JsonRpcClient((sys.executable, str(self.peer)), timeout=2)
        await self.client.start()
        self.addAsyncCleanup(self.client.close)

    async def test_response_and_notification_are_demultiplexed(self):
        result = await self.client.call('event')
        event = await self.client.notifications.get()
        self.assertEqual(result['method'], 'event')
        self.assertEqual(event['method'], 'thread/started')

    async def test_lost_ack_is_unknown_and_late_response_remains_reconcilable(self):
        with self.assertRaises(UnknownOutcome):
            await self.client.call('slow', timeout=.01)
        await asyncio.sleep(.15)
        self.assertEqual(len(self.client.late_responses), 1)
        self.assertEqual(next(iter(self.client.late_responses.values()))['result']['method'], 'slow')

    async def test_native_approval_request_is_declined_not_auto_accepted(self):
        result = await self.client.call('approval')
        self.assertEqual(result['decision'], 'decline')
        event = await self.client.notifications.get()
        self.assertEqual(event['method'], 'runtime/request-denied')

    async def test_owned_service_shutdown_is_observed(self):
        result = await self.client.close()
        self.assertTrue(result['process_exited'])
        self.assertIsNotNone(self.client.process.returncode)


class NativeProfileTests(unittest.TestCase):
    def test_generated_profile_has_no_broad_root_or_global_temp_write(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            name, config = permission_config(root/'work', root/'public', root, cli=sys.executable)
            profile = config['permissions'][name]
            self.assertFalse(profile['network']['enabled'])
            self.assertNotIn(':root', profile['filesystem'])
            self.assertNotIn(':tmpdir', profile['filesystem'])
            self.assertEqual(profile['filesystem'][str(root.resolve())], 'deny')
            self.assertEqual(profile['filesystem'][str((root/'work').resolve())], 'write')
            self.assertEqual(profile['filesystem'][str((root/'public').resolve())], 'read')
            self.assertEqual(config['shell_environment_policy']['inherit'],'none')
            self.assertFalse(config['allow_login_shell'])
            self.assertFalse(config['agents']['enabled'])

    def test_master_profile_cannot_write_production_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            name, config = permission_config(root/'master', root/'public', root, readonly=True, cli=sys.executable)
            self.assertEqual(config['permissions'][name]['filesystem'][str((root/'master').resolve())], 'read')


class NativeBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.actor = NativeActor('owner',root/'work',root/'public',root,kind='module',cli=sys.executable)
        self.actor.client = AsyncMock()
        self.reply = {'thread':{'id':'independent-root','sessionId':'independent-root','ephemeral':False},
                      'activePermissionProfile':{'id':self.actor.profile},'approvalPolicy':'never'}
        async def reply(method,params=None):
            if method=='config/read':return {'config':{'model':'permitted-model','model_reasoning_effort':'max'}}
            if method=='model/list':return {'data':[{'model':name,'supportedReasoningEfforts':[{'reasoningEffort':'max'}]} for name in ('permitted-model','explicit-admitted-model')]}
            return self.reply
        self.actor.client.call.side_effect = reply

    async def test_backend_cannot_silently_broaden_profile_before_execution(self):
        self.reply['activePermissionProfile']['id'] = ':workspace'
        self.actor.client.call.return_value = self.reply
        with self.assertRaises(AuthorityError):
            await self.actor.open()
        self.assertIsNone(self.actor.thread_id)
        self.assertNotIn('turn/start',[call.args[0] for call in self.actor.client.call.call_args_list])

    async def test_backend_cannot_replace_owner_with_fork_or_other_resume_root(self):
        self.reply['thread']['sessionId'] = 'master-root'
        self.actor.client.call.return_value = self.reply
        with self.assertRaises(AuthorityError):
            await self.actor.open()
        self.reply['thread']['sessionId'] = 'independent-root'
        with self.assertRaises(AuthorityError):
            await self.actor.open(existing_thread='original-owner-root')

    async def test_backend_model_substitution_is_rejected_before_turn_dispatch(self):
        self.actor.model = 'explicit-admitted-model'
        self.reply['model'] = 'other-model'
        self.actor.client.call.return_value = self.reply
        with self.assertRaises(AuthorityError):
            await self.actor.open()
        self.assertIsNone(self.actor.thread_id)

    async def test_unavailable_model_is_rejected_before_creating_uncertain_native_root(self):
        self.actor.model='unavailable-model'
        with self.assertRaises(ModelUnavailable):await self.actor.open()
        calls=[call.args[0] for call in self.actor.client.call.call_args_list]
        self.assertNotIn('thread/start',calls)
        self.assertNotIn('turn/start',calls)
