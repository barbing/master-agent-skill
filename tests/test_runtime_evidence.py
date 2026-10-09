"""Proof transport and tampering checks; synthetic notifications are labelled."""
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from master_agent_runtime import ModuleSpec, ProjectSpec, ValidationStep
from master_agent_runtime.contracts import ConflictError, ContractError, digest
from master_agent_runtime.evidence import command_observation, seal_review_evidence, verify_review_evidence, MAX_COMMAND_OUTPUT
from master_agent_runtime.native import NativeActor


class EvidenceBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bindings = {'candidate_id':'sealed-code', 'tree_hash':'code-sha', 'thread_id':'owner',
                         'turn_id':'turn', 'inputs':{'dependency':{'candidate':'dep', 'revision':2}}, 'mandate_revision':3}
        self.log = self.root / 'check.log'; self.log.write_text('Independent assertion FAILED\n')
        self.validation = [{'status':'failed', 'log':str(self.log), 'log_sha256':hashlib.sha256(self.log.read_bytes()).hexdigest()}]
        self.command = command_observation({'id':'item', 'command':'printf PASS', 'cwd':'private',
            'status':'completed', 'exitCode':0, 'aggregatedOutput':'PASS\n'}, thread_id='owner', turn_id='turn')

    def seal(self, **changes):
        args = {'bindings':self.bindings, 'commands':[self.command], 'validation':self.validation,
                'test_paths':('owned_test.py',), 'manifest':{'files':[{'path':'owned_test.py', 'sha256':'test-sha', 'bytes':42}]}}
        args.update(changes)
        return seal_review_evidence(self.root/'sealed', **args)

    def test_native_pass_text_never_overrides_failed_independent_check(self):
        result = self.seal(); body = verify_review_evidence(Path(result['path']), expected_hash=result['hash'], bindings=self.bindings)
        self.assertEqual(body['validation'][0]['status'], 'failed')
        self.assertEqual(body['owner_tests'][0]['sha256'], 'test-sha')
        receipt = json.loads((Path(result['path'])/body['commands'][0]['receipt']).read_text())
        self.assertEqual(receipt['output'], 'PASS\n')
        self.assertIn('observation', receipt['trust'])
        self.assertIn('may precede later edits', receipt['code_version_binding'])
        self.assertEqual(body['bindings']['inputs'], self.bindings['inputs'])

    def test_wrong_root_turn_or_candidate_binding_cannot_be_replayed(self):
        for field in ('thread_id', 'turn_id'):
            wrong = dict(self.command, **{field:'foreign'})
            with self.assertRaises(ConflictError): self.seal(commands=[wrong])
        result = self.seal()
        with self.assertRaises(ConflictError):
            verify_review_evidence(Path(result['path']), expected_hash=result['hash'], bindings=dict(self.bindings, tree_hash='another-code-version'))

    def test_tampered_output_and_replaced_receipt_cannot_validate(self):
        result = self.seal(); root = Path(result['path']); index = json.loads((root/'index.json').read_text())
        target = root / index['commands'][0]['receipt']; target.chmod(0o644); target.write_text('forged output')
        with self.assertRaises(ConflictError): verify_review_evidence(root, expected_hash=result['hash'], bindings=self.bindings)

    def test_validation_log_cannot_change_between_check_and_seal(self):
        self.log.write_text('faked success')
        with self.assertRaises(ConflictError): self.seal()

    def test_missing_test_and_output_truncation_are_explicit(self):
        clipped = command_observation({'id':'long', 'aggregatedOutput':'x'*(MAX_COMMAND_OUTPUT+1)}, thread_id='owner', turn_id='turn')
        result = self.seal(commands=[clipped], manifest={'files':[]})
        body = verify_review_evidence(Path(result['path']), expected_hash=result['hash'], bindings=self.bindings)
        self.assertEqual(body['missing_owner_tests'], ['owned_test.py'])
        self.assertTrue(body['commands'][0]['output_truncated'])

    def test_old_contract_identity_is_preserved_and_test_scope_is_explicit(self):
        spec = ProjectSpec(str(self.root), 'Deliver', (ModuleSpec('one','Deliver',('one.py',)),), 10000)
        old = asdict(spec); old.pop('max_evidence_rounds')
        for module in old['modules']: module.pop('test_paths'); module.pop('test_validation')
        self.assertEqual(spec.fingerprint, digest(old))
        with self.assertRaises(ContractError): ModuleSpec('one','Deliver',('one.py',),test_paths=('outside.py',),test_validation=(ValidationStep(('python','tests.py')),))
        with self.assertRaises(ContractError): ModuleSpec('one','Deliver',('one.py','owned_test.py'),test_paths=('owned_test.py',))


class NativeCommandReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_command_survives_metadata_cap_and_ignores_foreign_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); actor = NativeActor('owner',root/'work',root/'public',root,kind='module',cli=sys.executable)
            actor.thread_id = 'owner'; actor.events = [{}]*2000
            queue = asyncio.Queue(); actor.client = SimpleNamespace(notifications=queue)
            def completed(thread, turn, item):
                queue.put_nowait({'method':'item/completed','params':{'threadId':thread,'turnId':turn,'item':item}})
            item = {'id':'proof','type':'commandExecution','command':'real fixture command','cwd':str(root),
                    'status':'completed','exitCode':7,'aggregatedOutput':'actual failing assertion'}
            completed('foreign','turn',dict(item,id='foreign'))
            completed('owner','old-turn',dict(item,id='old'))
            completed('owner','turn',item)
            queue.put_nowait({'method':'turn/completed','params':{'threadId':'owner','turn':{'id':'turn','status':'completed'}}})
            await actor.await_turn('turn')
            self.assertEqual(set(actor.command_receipts), {'proof'})
            self.assertEqual(actor.command_receipts['proof']['exit_code'], 7)
            self.assertEqual(actor.command_receipts['proof']['output'], 'actual failing assertion')

    async def test_stream_fallback_never_claims_clipped_output_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); actor=NativeActor('owner',root/'work',root/'public',root,kind='module',cli=sys.executable)
            actor.thread_id='owner'; queue=asyncio.Queue(); actor.client=SimpleNamespace(notifications=queue)
            queue.put_nowait({'method':'item/commandExecution/outputDelta','params':{'threadId':'owner','turnId':'turn','itemId':'proof','delta':'x'*(MAX_COMMAND_OUTPUT+10)}})
            queue.put_nowait({'method':'item/completed','params':{'threadId':'owner','turnId':'turn','item':{'id':'proof','type':'commandExecution','status':'completed','exitCode':0}}})
            queue.put_nowait({'method':'turn/completed','params':{'threadId':'owner','turn':{'id':'turn','status':'completed'}}})
            await actor.await_turn('turn')
            self.assertTrue(actor.command_receipts['proof']['output_truncated'])
            self.assertEqual(actor.command_receipts['proof']['output_source'], 'streamed_deltas')
