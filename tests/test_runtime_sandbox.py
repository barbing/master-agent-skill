"""Fail-closed control tests; simulated backend responses are not OS conformance."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from master_agent_runtime.contracts import ContractError
from master_agent_runtime.sandbox import check_scope


class ScopePreflightTests(unittest.TestCase):
    def test_unexpected_temporary_write_cannot_be_hidden_by_claimed_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);work = root/'work';work.mkdir()
            def forged_backend(argv,**kwargs):
                # Mutate the disposable target while claiming the policy blocked it.
                Path(argv[-1]).write_text('actual outside effect')
                rows=[{'case':'own_workspace_write','effect':'succeeded'},
                      {'case':'outside_temp_write','effect':'blocked'}]
                return subprocess.CompletedProcess(argv,0,json.dumps(rows),'')
            with patch('master_agent_runtime.sandbox.sandbox_prefix',return_value=('codex','sandbox','-P','runtime-validator')), \
                 patch('master_agent_runtime.sandbox.subprocess.run',side_effect=forged_backend):
                with self.assertRaises(ContractError):
                    check_scope('codex','scoped',{},work,root/'scope.json',writable=True)
            self.assertFalse(json.loads((root/'scope.json').read_text())['passed'])

    def test_native_execution_failure_is_not_permission_conformance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);work = root/'work';work.mkdir()
            with patch('master_agent_runtime.sandbox.sandbox_prefix',return_value=('codex','sandbox','-P','runtime-validator')), \
                 patch('master_agent_runtime.sandbox.subprocess.run',return_value=subprocess.CompletedProcess([],71,'','sandbox failed')):
                with self.assertRaises(ContractError):
                    check_scope('codex','scoped',{},work,root/'scope.json',writable=False)
            self.assertFalse(json.loads((root/'scope.json').read_text())['passed'])
