"""Usage breakdown, cost estimates, history fallback and runtime-policy admission."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from master_agent_runtime.admission import load_handoff, load_handoff_with_policy
from master_agent_runtime.contracts import ContractError, RuntimePolicy
from master_agent_runtime.native import NativeActor, NativeRejected, read_turn

from runtime_fixtures import make_project


class UsageDetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        price = {'input': 2.0, 'cached_input': 0.1, 'output': 10.0}
        self.store, self.pid = make_project(Path(self.temp.name), [('one', ())],
                                           policy={'prices': {'priced-model': price}})
        self.master = self.store.bind(self.pid + ':master', 'm', 'm')
        self.owner = self.store.bind(self.pid + ':module:one', 'o', 'o')
        self.price = self.store.project(self.pid)['policy'].price('priced-model')

    def detail(self, total, cached, output):
        return {'inputTokens': total - output, 'cachedInputTokens': cached, 'outputTokens': output,
                'reasoningOutputTokens': 0, 'totalTokens': total}

    def test_breakdown_deltas_survive_counter_reset_and_feed_cost(self):
        op = self.store.dispatch(self.master, self.owner.actor_id, 'Work')
        self.assertEqual(self.store.record_usage(self.owner.actor_id, 1_100_000, self.detail(1_100_000, 900_000, 100_000),
                                                 price=self.price), 1_100_000)
        # Counter reset (new native process): the new cumulative value is all new usage.
        self.assertEqual(self.store.record_usage(self.owner.actor_id, 10_000, self.detail(10_000, 0, 1_000),
                                                 price=self.price), 10_000)
        status = self.store.status(self.pid)
        self.assertEqual(status['usage_detail']['cachedInputTokens'], 900_000)
        self.assertEqual(status['usage_detail']['outputTokens'], 101_000)
        self.assertEqual(status['spent_tokens'], 1_110_000)
        expected = (100_000 * 2.0 + 900_000 * 0.1 + 100_000 * 10.0 + 9_000 * 2.0 + 1_000 * 10.0) / 1_000_000
        self.assertAlmostEqual(status['estimated_cost_usd'], expected)
        self.assertEqual(status['unpriced_tokens'], 0)
        self.assertEqual(json.loads(self.store.operation(op)['usage_detail'])['outputTokens'], 101_000)

    def test_provider_reported_cost_is_cumulative_per_actor(self):
        self.store.record_usage(self.owner.actor_id, 100, cost_usd=0.5)
        self.store.record_usage(self.owner.actor_id, 200, cost_usd=0.75)
        self.store.record_usage(self.master.actor_id, 50)
        status = self.store.status(self.pid)
        self.assertAlmostEqual(status['estimated_cost_usd'], 0.75)
        self.assertEqual(status['unpriced_tokens'], 50)
        self.assertEqual(status['spent_tokens'], 250)


class NativeUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_usage_notification_reports_breakdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actor = NativeActor('owner', root / 'work', root / 'public', root, kind='module', cli=sys.executable)
            actor.thread_id = 'owner'
            queue = asyncio.Queue(); actor.client = SimpleNamespace(notifications=queue)
            breakdown = {'inputTokens': 900, 'cachedInputTokens': 700, 'outputTokens': 100,
                         'reasoningOutputTokens': 40, 'totalTokens': 1000}
            queue.put_nowait({'method': 'turn/started', 'params': {'threadId': 'owner', 'turn': {'id': 'turn'}}})
            queue.put_nowait({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'owner', 'turnId': 'turn',
                              'tokenUsage': {'total': breakdown, 'last': breakdown}}})
            queue.put_nowait({'method': 'item/completed', 'params': {'threadId': 'owner', 'turnId': 'turn',
                              'item': {'id': 'c', 'type': 'contextCompaction'}}})
            queue.put_nowait({'method': 'turn/completed', 'params': {'threadId': 'owner', 'turn': {'id': 'turn', 'status': 'completed'}}})
            seen = []
            await actor.await_turn('turn', on_usage=lambda total, detail=None: seen.append((total, detail)))
            self.assertEqual(seen, [(1000, breakdown)])
            self.assertEqual(actor.compactions, 1)


class HistoryFallbackTests(unittest.IsolatedAsyncioTestCase):
    def client(self, replies):
        client = AsyncMock()
        async def call(method, params=None):
            reply = replies[method]
            if isinstance(reply, Exception):
                raise reply
            return reply(params) if callable(reply) else reply
        client.call.side_effect = call
        return client

    async def test_summary_turn_is_completed_from_paginated_items(self):
        items = [{'id': 'c1', 'type': 'commandExecution', 'status': 'completed'}, {'id': 'm', 'type': 'agentMessage', 'text': '{}'}]
        pages = iter([{'data': [{'turnId': 't', 'item': items[0]}], 'nextCursor': 'next'},
                      {'data': [{'turnId': 't', 'item': items[1]}, {'turnId': 'other', 'item': {'id': 'x'}}], 'nextCursor': None}])
        client = self.client({'thread/read': {'thread': {'id': 'r', 'sessionId': 'r', 'turns': [
                                  {'id': 't', 'status': 'completed', 'items': [], 'itemsView': 'summary'}]}},
                              'thread/items/list': lambda params: next(pages)})
        thread, turn, source = await read_turn(client, 'r', 't')
        self.assertEqual(turn['items'], items)
        self.assertEqual(turn['status'], 'completed')
        self.assertIn('thread/items/list', source)

    async def test_missing_turn_is_found_by_paging_turns_then_items(self):
        client = self.client({'thread/read': NativeRejected('full history unavailable'),
                              'thread/turns/list': {'data': [{'id': 't', 'status': 'interrupted', 'items': []}], 'nextCursor': None},
                              'thread/items/list': {'data': [], 'nextCursor': None}})
        thread, turn, source = await read_turn(client, 'r', 't')
        self.assertEqual(turn['status'], 'interrupted')
        self.assertEqual([c.args[0] for c in client.call.call_args_list], ['thread/read', 'thread/turns/list', 'thread/items/list'])

    async def test_full_thread_read_makes_no_extra_calls(self):
        client = self.client({'thread/read': {'thread': {'id': 'r', 'sessionId': 'r', 'turns': [{'id': 't', 'status': 'completed', 'items': []}]}}})
        _, turn, _ = await read_turn(client, 'r', 't')
        self.assertEqual(turn['status'], 'completed')
        self.assertEqual(len(client.call.call_args_list), 1)


class PolicyAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'project').mkdir()
        self.base = {'root': str(root / 'project'), 'objective': 'Deliver', 'token_budget': 10000,
                     'modules': [{'key': 'one', 'objective': 'Complete', 'paths': ['one.py']}]}
        self.path = root / 'handoff.json'

    def load(self, policy=None):
        data = dict(self.base, **({'runtime_policy': policy} if policy is not None else {}))
        self.path.write_text(json.dumps(data))
        return load_handoff_with_policy(self.path)

    def test_policy_rides_beside_the_contract_without_changing_its_identity(self):
        plain, default = self.load()
        spec, policy = self.load({'owner_turn_timeout_seconds': 7200, 'provider': 'claude',
                                  'master': {'provider': 'codex', 'model': 'gpt-6-luna', 'effort': 'medium'},
                                  'modules': {'one': {'provider': 'claude', 'model': 'claude-opus-5-5'}}})
        self.assertEqual(spec.fingerprint, plain.fingerprint)
        self.assertEqual(load_handoff(self.path), spec)
        self.assertEqual(default, RuntimePolicy())
        self.assertEqual(policy.runtime_for(spec).provider, 'codex')
        self.assertEqual(policy.runtime_for(spec, 'one').model, 'claude-opus-5-5')
        self.assertEqual(policy.turn_timeout(spec, 'module'), 7200)
        self.assertEqual(policy.turn_timeout(spec, 'master'), spec.turn_timeout_seconds)
        self.assertEqual(RuntimePolicy.from_dict(policy.to_dict()), policy)

    def test_invalid_policies_are_rejected_at_admission(self):
        for bad, detail in (({'unknown': 1}, 'unknown fields'), ({'modules': {'ghost': {'provider': 'claude'}}}, 'unknown modules'),
                            ({'provider': 'other'}, 'provider'), ({'max_continuations': -1}, 'max_continuations'),
                            ({'prices': {'m': {'input': 1}}}, 'price'), ({'admission_review': 'yes'}, 'admission_review')):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError) as caught:
                    self.load(bad)
                self.assertIn(detail.lower(), str(caught.exception).lower())


class DeferredCostTests(unittest.TestCase):
    def test_tokens_awaiting_a_provider_cost_are_not_unpriced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, pid = make_project(Path(tmp), [('one', ())])
            owner = store.bind(pid + ':module:one', 'one', 'one')
            store.record_usage(owner.actor_id, 1000, {'inputTokens': 990, 'outputTokens': 10}, cost_pending=True)
            self.assertEqual(store.status(pid)['unpriced_tokens'], 0)
            store.record_usage(owner.actor_id, 1200, {'inputTokens': 1000, 'outputTokens': 200}, cost_usd=0.003)
            status = store.status(pid)
            self.assertEqual((status['spent_tokens'], status['unpriced_tokens']), (1200, 0))
            self.assertAlmostEqual(status['estimated_cost_usd'], 0.003)
            store.record_usage(owner.actor_id, 1300, {'inputTokens': 1050, 'outputTokens': 250})
            self.assertEqual(store.status(pid)['unpriced_tokens'], 100)      # no price and no pending cost
