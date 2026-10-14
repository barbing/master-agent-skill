"""Fail-closed control tests; simulated backend responses are not OS conformance."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from master_agent_runtime.contracts import ContractError
from master_agent_runtime.sandbox import check_scope

PREFIX = ('codex','sandbox','-P','runtime-validator')


def probe_targets(argv):
    """The probe's targets after `-- python script`: own write, outside write, outside read[, protected read]."""
    return [Path(arg) for arg in argv[argv.index('--') + 3:]]


def rows(*effects):
    names = ('own_workspace_write','outside_temp_write','outside_temp_read','protected_read')
    return [{'case':name,'effect':effect} for name,effect in zip(names,effects)]


class ScopePreflightTests(unittest.TestCase):
    def test_unexpected_temporary_write_cannot_be_hidden_by_claimed_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);work = root/'work';work.mkdir()
            def forged_backend(argv,**kwargs):
                # Mutate the disposable target while claiming the policy blocked it.
                owned, forbidden, _ = probe_targets(argv)
                owned.write_text('owned');forbidden.write_text('actual outside effect')
                return subprocess.CompletedProcess(argv,0,json.dumps(rows('succeeded','blocked','blocked')),'')
            with patch('master_agent_runtime.sandbox.sandbox_prefix',return_value=PREFIX), \
                 patch('master_agent_runtime.sandbox.subprocess.run',side_effect=forged_backend):
                with self.assertRaises(ContractError):
                    check_scope('codex','scoped',{},work,root/'scope.json',writable=True)
            self.assertFalse(json.loads((root/'scope.json').read_text(encoding='utf-8'))['passed'])

    def test_native_execution_failure_is_not_permission_conformance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);work = root/'work';work.mkdir()
            with patch('master_agent_runtime.sandbox.sandbox_prefix',return_value=PREFIX), \
                 patch('master_agent_runtime.sandbox.subprocess.run',return_value=subprocess.CompletedProcess([],71,'','sandbox failed')):
                with self.assertRaises(ContractError):
                    check_scope('codex','scoped',{},work,root/'scope.json',writable=False)
            self.assertFalse(json.loads((root/'scope.json').read_text(encoding='utf-8'))['passed'])

    def test_a_readable_protected_sentinel_refuses_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);work = root/'work';work.mkdir();control = root/'control';control.mkdir()
            seen = {}
            def leaking_backend(argv,**kwargs):
                owned, _, _, protected = probe_targets(argv)
                owned.write_text('owned');seen['secret'] = protected.read_text(encoding='utf-8')
                return subprocess.CompletedProcess(argv,0,json.dumps(rows('succeeded','blocked','blocked','succeeded')),'')
            with patch('master_agent_runtime.sandbox.sandbox_prefix',return_value=PREFIX), \
                 patch('master_agent_runtime.sandbox.subprocess.run',side_effect=leaking_backend):
                with self.assertRaises(ContractError):
                    check_scope('codex','scoped',{},work,root/'scope.json',writable=True,protected=control)
            self.assertTrue(seen['secret'])
            self.assertEqual(list(control.iterdir()), [], 'the host removes its sentinel')
            self.assertEqual(sorted(path.name for path in work.iterdir()), [], 'probe files are removed')

    def test_blocked_and_hidden_reads_are_both_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);work = root/'work';work.mkdir();control = root/'control';control.mkdir()
            def enforcing_backend(argv,**kwargs):
                owned = probe_targets(argv)[0]
                owned.write_text('owned')
                return subprocess.CompletedProcess(argv,0,json.dumps(rows('succeeded','blocked','hidden','blocked')),'')
            with patch('master_agent_runtime.sandbox.sandbox_prefix',return_value=PREFIX), \
                 patch('master_agent_runtime.sandbox.subprocess.run',side_effect=enforcing_backend) as run:
                report = check_scope('codex','scoped',{},work,root/'scope.json',writable=True,protected=control)
            self.assertTrue(report['passed'])
            argv = run.call_args.args[0]
            self.assertTrue(probe_targets(argv)[0].name.startswith('.scope-probe-'))
            self.assertTrue(argv[argv.index('--') + 2].endswith('.py'), 'the probe runs from a script file, not -c')
