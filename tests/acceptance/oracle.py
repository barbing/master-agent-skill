"""Frozen acceptance checks for a real CLI improvement; no model mocks.

Run with CONTEXT CANDIDATE MODE. Candidate files overlay a complete original
tree in validator-private scratch, so partial module snapshots remain usable.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def fingerprint(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


class StoreReadOnlyAcceptance(unittest.TestCase):
    def setUp(self):
        from master_agent_runtime import ModuleSpec, ProjectSpec, Store
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.input = self.base / 'input'; self.input.mkdir()
        self.db = self.base / 'state' / 'runtime.sqlite'
        self.Store = Store
        self.store = Store(self.db)
        self.spec = ProjectSpec(str(self.input), 'Inspect an admitted task', (
            ModuleSpec('one', 'Deliver the module', ('one.py',)),), 1000000)
        self.pid = self.store.create_project(self.spec)
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.assertEqual(con.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')

    def test_missing_readonly_store_never_creates_state(self):
        target = self.base / 'absent' / 'nested' / 'runtime.sqlite'
        with self.assertRaises(Exception):
            self.Store.open_readonly(target)
        self.assertFalse(target.parent.exists())

    def test_existing_store_is_physically_unchanged_and_rejects_writes(self):
        expected = self.store.status(self.pid)
        # Restore DELETE after the ordinary writer API's status observation.
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('PRAGMA journal_mode=DELETE')
        before = fingerprint(self.db.parent)
        read = self.Store.open_readonly(self.db)
        self.assertEqual(read.status(self.pid), expected)
        with closing(read.connect()) as con:
            self.assertEqual(con.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("UPDATE project SET spent=999999 WHERE id=?", (self.pid,))
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute('CREATE TABLE unauthorized (value TEXT)')
        with self.assertRaises(Exception):
            read.create_project(self.spec)
        self.assertEqual(fingerprint(self.db.parent), before)

    def test_live_wal_observation_sees_committed_changes(self):
        writer = self.store.connect()
        self.addCleanup(writer.close)
        writer.execute('UPDATE project SET spent=237 WHERE id=?', (self.pid,))
        before = {p.name: p.read_bytes() for p in self.db.parent.iterdir()
                  if p.name.endswith(('.sqlite', '-wal'))}
        report = self.Store.open_readonly(self.db).status(self.pid)
        self.assertEqual(report['spent_tokens'], 237)
        self.assertEqual({p.name: p.read_bytes() for p in self.db.parent.iterdir()
                          if p.name.endswith(('.sqlite', '-wal'))}, before)

    def test_invalid_database_is_not_initialized_or_repaired(self):
        for payload in (b'', b'this is not SQLite'):
            with self.subTest(payload=payload):
                path = self.base / ('empty.db' if not payload else 'corrupt.db')
                path.write_bytes(payload)
                before = path.read_bytes()
                with self.assertRaises(Exception):
                    self.Store.open_readonly(path).status('missing')
                self.assertEqual(path.read_bytes(), before)


class CliUserAcceptance(StoreReadOnlyAcceptance):
    def cli(self, command, *args, state=None):
        argv = [sys.executable, '-B', '-m', 'master_agent_runtime', '--state',
                str(state or self.db.parent), command, *args]
        result = subprocess.run(argv, cwd=Path.cwd(), capture_output=True,
                                text=True, timeout=30)
        self.assertNotIn('Traceback (most recent call last)', result.stderr)
        stream = result.stdout if result.returncode == 0 else (result.stderr or result.stdout)
        payload = json.loads(stream)
        return result, payload

    def test_all_existing_state_commands_reject_absent_storage_without_creating_it(self):
        for command in ('status', 'health', 'run', 'reconcile', 'stop'):
            with self.subTest(command=command):
                root = self.base / ('missing-' + command) / 'nested'
                result, payload = self.cli(command, self.pid, state=root)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(payload.get('detail') or payload.get('error'))
                self.assertFalse(root.exists())

    def test_bad_handoff_cannot_initialize_control_storage(self):
        handoff = self.base / 'invalid-handoff.json'; handoff.write_text('{"root": 4}')
        state = self.base / 'bad-admission' / 'nested'
        result, payload = self.cli('init', str(handoff), state=state)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('error', payload)
        self.assertFalse(state.exists())

    def test_inspection_preserves_database_bytes_journal_and_existing_interfaces(self):
        expected = self.store.status(self.pid)
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('PRAGMA journal_mode=DELETE')
        before = fingerprint(self.db.parent)
        result, status = self.cli('status', self.pid)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(status, expected)
        result, health = self.cli('health', self.pid)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(health['project_id'], self.pid)
        self.assertTrue(health['safe_to_run'])
        self.assertEqual(health['usage']['spent_tokens'], 0)
        self.assertEqual(fingerprint(self.db.parent), before)

    def test_inspection_does_not_require_the_old_input_checkout_to_still_exist(self):
        shutil.rmtree(self.input)
        for command in ('status', 'health'):
            with self.subTest(command=command):
                result, payload = self.cli(command, self.pid)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(payload['project_id'], self.pid)

    def test_live_wal_cli_observation_is_current_without_logical_mutation(self):
        writer = self.store.connect(); self.addCleanup(writer.close)
        writer.execute('UPDATE project SET spent=237 WHERE id=?', (self.pid,))
        before = list(writer.iterdump())
        for command in ('status', 'health'):
            result, payload = self.cli(command, self.pid)
            self.assertEqual(result.returncode, 0)
            spent = payload['spent_tokens'] if command == 'status' else payload['usage']['spent_tokens']
            self.assertEqual(spent, 237)
            self.assertEqual(list(writer.iterdump()), before)

    def test_corrupt_and_empty_state_return_structured_errors_without_repair(self):
        for data in (b'', b'not a database'):
            root = self.base / ('empty-state' if not data else 'corrupt-state'); root.mkdir()
            db = root / 'runtime.sqlite'; db.write_bytes(data)
            for command in ('status', 'health'):
                with self.subTest(data=data, command=command):
                    before = fingerprint(root)
                    result, payload = self.cli(command, self.pid, state=root)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('error', payload)
                    self.assertEqual(fingerprint(root), before)

    def test_unknown_project_is_an_error_without_writes(self):
        before = fingerprint(self.db.parent)
        for command in ('status', 'health'):
            result, payload = self.cli(command, 'unknown-project')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('error', payload)
            self.assertEqual(fingerprint(self.db.parent), before)

    def test_nondelivered_run_states_are_nonzero_without_contacting_native_backend(self):
        for state in ('blocked', 'paused', 'cancelled', 'cancelling', 'integration_failed'):
            with closing(sqlite3.connect(self.db)) as con:
                con.execute('UPDATE project SET state=? WHERE id=?', (state, self.pid)); con.commit()
            result, payload = self.cli('run', self.pid, '--codex', '/no-such-runtime-allowed')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(payload['state'], state)
            self.assertNotIn('error', payload)

    def test_accepted_run_retrieves_exact_original_delivery_without_new_work(self):
        from master_agent_runtime.contracts import canonical
        bundle = self.base / 'accepted-delivery'; bundle.mkdir()
        (bundle / 'one.py').write_text('answer = 42\n')
        receipt = {'path': str(bundle), 'modules': {'one': {'candidate': 'sealed-original', 'revision': 1}},
                   'validation': [{'status': 'passed', 'log': 'original-check'}]}
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE project SET state='accepted' WHERE id=?", (self.pid,))
            con.execute('INSERT INTO event(project_id,kind,payload,at) VALUES(?,?,?,?)',
                        (self.pid, 'integration-accepted', canonical(receipt), 1)); con.commit()
        before = fingerprint(self.db.parent)
        for _ in range(2):
            result, payload = self.cli('run', self.pid, '--codex', '/no-such-runtime-allowed')
            self.assertEqual(result.returncode, 0)
            self.assertEqual(payload['state'], 'accepted')
            for key, value in receipt.items(): self.assertEqual(payload[key], value)
            self.assertTrue(payload['resumed_completed_project'])
            self.assertEqual(fingerprint(self.db.parent), before)

    def test_user_can_stop_an_admitted_project_before_any_native_root_is_bound(self):
        result, payload = self.cli('stop', self.pid)
        self.assertEqual(result.returncode, 0)
        result, status = self.cli('status', self.pid)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(status['state'], 'cancelled')
        self.assertEqual(status['spent_tokens'], 0)
        self.assertEqual(status['reserved_tokens'], 0)
        self.assertEqual(status['operations'], [])
        self.assertTrue(all(actor['thread_id'] is None for actor in status['actors']))
        before = fingerprint(self.db.parent)
        result, payload = self.cli('stop', self.pid)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(fingerprint(self.db.parent), before)

    def test_stop_of_an_accepted_project_preserves_the_delivery(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE project SET state='accepted' WHERE id=?", (self.pid,)); con.commit()
        before = fingerprint(self.db.parent)
        result, payload = self.cli('stop', self.pid)
        self.assertEqual(result.returncode, 0)
        result, status = self.cli('status', self.pid)
        self.assertEqual(status['state'], 'accepted')
        self.assertEqual(fingerprint(self.db.parent), before)


def run_checks(mode):
    loader = unittest.TestLoader()
    # Avoid counting inherited store tests twice in the CLI suite.
    suite = loader.loadTestsFromTestCase(StoreReadOnlyAcceptance)
    if mode != 'store':
        cli_names = [name for name in loader.getTestCaseNames(CliUserAcceptance)
                     if name not in loader.getTestCaseNames(StoreReadOnlyAcceptance)]
        suite.addTests(CliUserAcceptance(name) for name in cli_names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    acceptance = {'tests': result.testsRun, 'failures': len(result.failures),
                  'errors': len(result.errors), 'skipped': len(result.skipped)}
    regression = None
    if mode in ('store', 'integration'):
        old_suite = unittest.TestSuite()
        for pattern in ('test_module_runtime.py', 'test_native_rpc.py', 'test_runtime*.py'):
            old_suite.addTests(loader.discover('tests', pattern=pattern))
        # Original unit fixtures inspect a home config and seed a disposable
        # global-temp target before mocking the backend. Relocate fixture setup
        # into validator-private scratch; assertions and source stay unchanged.
        # Real native scope preflights remain separate, unmocked runtime gates.
        original_mkdtemp = tempfile.mkdtemp
        with tempfile.TemporaryDirectory(prefix='unit-fixtures-') as unit_tmp:
            unit_root = Path(unit_tmp); fixture_home = unit_root / 'home'; fixture_home.mkdir()
            fixture_bin = unit_root / 'bin'; fixture_bin.mkdir()
            (fixture_bin / 'python').symlink_to(sys.executable)
            def private_fixture_temp(*args, **kwargs):
                args = list(args)
                parent = kwargs.get('dir', args[2] if len(args) > 2 else None)
                if str(parent) in ('/tmp', '/private/tmp'):
                    if len(args) > 2: args[2] = str(unit_root)
                    else: kwargs['dir'] = str(unit_root)
                return original_mkdtemp(*args, **kwargs)
            with patch('pathlib.Path.home', return_value=fixture_home), \
                 patch('master_agent_runtime.sandbox.tempfile.mkdtemp', side_effect=private_fixture_temp), \
                 patch.dict(os.environ, {'PATH': str(fixture_bin) + os.pathsep + os.environ.get('PATH', '')}):
                old_result = unittest.TextTestRunner(verbosity=1).run(old_suite)
        regression = {'tests': old_result.testsRun, 'failures': len(old_result.failures),
                      'errors': len(old_result.errors), 'skipped': len(old_result.skipped)}
    print('USER_ACCEPTANCE_RECEIPT=' + json.dumps({'mode': mode, 'acceptance': acceptance,
                                                'regression': regression}, sort_keys=True))
    return 0 if result.wasSuccessful() and (regression is None or old_result.wasSuccessful()) else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--inside':
        return run_checks(sys.argv[2])
    context, candidate, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    with tempfile.TemporaryDirectory(prefix='user-oracle-') as tmp:
        staged = Path(tmp) / 'project'
        shutil.copytree(context, staged, ignore=shutil.ignore_patterns('__pycache__', '.tmp', '.git'))
        for path in candidate.rglob('*'):
            if path.is_file() and not any(p in ('.tmp', '__pycache__', '.git') for p in path.relative_to(candidate).parts):
                target = staged / path.relative_to(candidate); target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        env = dict(os.environ, PYTHONPATH=str(staged), PYTHONDONTWRITEBYTECODE='1')
        proc = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--inside', mode],
                              cwd=staged, env=env, timeout=300)
        return proc.returncode


if __name__ == '__main__':
    raise SystemExit(main())
