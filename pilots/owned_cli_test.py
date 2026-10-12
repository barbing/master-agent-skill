"""User-command coverage for the runtime CLI safety and lifecycle behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from master_agent_runtime import ModuleSpec, ProjectSpec, Store


class RuntimeCliCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project_root = self.root / 'project'
        self.project_root.mkdir()
        self.state = self.root / 'state'
        self.store = Store(self.state / 'runtime.sqlite')
        spec = ProjectSpec(str(self.project_root), 'deliver',
                           (ModuleSpec('module', 'implement', ('module.py',), token_reservation=10),), 100)
        self.project_id = self.store.create_project(spec)

    def cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        return subprocess.run([sys.executable, '-m', 'master_agent_runtime', '--state', str(self.state), *args],
                              text=True, capture_output=True, env=env)

    def test_status_and_health_read_existing_state_and_unknown_project_is_structured(self):
        status = self.cli('status', self.project_id)
        health = self.cli('health', self.project_id)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)['state'], 'active')
        self.assertEqual(health.returncode, 0, health.stderr)
        missing = self.cli('status', 'not-a-project')
        self.assertEqual(missing.returncode, 1)
        self.assertIn('"error"', missing.stderr)

    def test_missing_database_inspection_does_not_create_storage(self):
        self.state.joinpath('runtime.sqlite').unlink()
        result = self.cli('health', self.project_id)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.state.joinpath('runtime.sqlite').exists())
        self.assertIn('"error"', result.stderr)

    def test_stop_before_binding_settles_and_repeats_idempotently(self):
        first = self.cli('stop', self.project_id)
        second = self.cli('stop', self.project_id)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['state'], 'cancelled')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['state'], 'cancelled')

    def test_run_nonactive_returns_state_without_backend_and_nonzero(self):
        self.store.request_stop(self.project_id)
        result = self.cli('run', self.project_id, '--codex', '/definitely/not/a/backend')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['state'], 'cancelled')
        self.assertNotIn('ModelUnavailable', result.stderr)

    def test_invalid_handoff_does_not_create_runtime_state(self):
        handoff = self.root / 'bad.json'
        handoff.write_text('{broken')
        self.state.joinpath('runtime.sqlite').unlink()
        result = self.cli('init', str(handoff))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.state.joinpath('runtime.sqlite').exists())
        self.assertIn('"error"', result.stderr)

    def test_reconcile_unknown_project_does_not_change_database(self):
        database=self.state / 'runtime.sqlite'
        before=database.read_bytes()
        result=self.cli('reconcile', 'not-a-project', '--codex', '/no-backend')
        self.assertEqual(result.returncode, 1)
        self.assertIn('"error"', result.stderr)
        self.assertEqual(database.read_bytes(), before)

    def test_reconcile_empty_or_corrupt_database_does_not_repair_it(self):
        database=self.state / 'runtime.sqlite'
        for contents in (b'', b'not a SQLite database'):
            with self.subTest(contents=contents):
                database.write_bytes(contents)
                result=self.cli('reconcile', self.project_id, '--codex', '/no-backend')
                self.assertEqual(result.returncode, 1)
                self.assertIn('"error"', result.stderr)
                self.assertEqual(database.read_bytes(), contents)


if __name__ == '__main__':
    unittest.main()
