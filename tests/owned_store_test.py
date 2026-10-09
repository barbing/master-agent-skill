"""Owner-maintained Store checks for readonly freshness and stop behavior."""
from contextlib import closing
import sqlite3
from pathlib import Path
import unittest

from acceptance.oracle import StoreReadOnlyAcceptance


class OwnedStoreChecks(StoreReadOnlyAcceptance):
    def test_new_readonly_observation_tracks_live_wal_without_source_mutation(self):
        writer = self.store.connect()
        self.addCleanup(writer.close)
        writer.execute('UPDATE project SET spent=41 WHERE id=?', (self.pid,))
        before = {item.name: item.read_bytes() for item in self.db.parent.iterdir()}
        observed = self.Store.open_readonly(self.db).status(self.pid)
        self.assertEqual(observed['spent_tokens'], 41)
        self.assertEqual({item.name: item.read_bytes() for item in self.db.parent.iterdir()}, before)

    def test_open_connection_reports_retry_if_source_changes_before_query(self):
        reader = self.Store.open_readonly(self.db).connect()
        self.addCleanup(reader.close)
        writer = self.store.connect()
        try:
            writer.execute('UPDATE project SET spent=52 WHERE id=?', (self.pid,))
            with self.assertRaisesRegex(sqlite3.OperationalError, 'changed.*retry'):
                reader.execute('SELECT spent FROM project WHERE id=?', (self.pid,))
        finally:
            writer.close()

    def test_cursor_reports_retry_if_source_changes_between_execute_and_fetch(self):
        reader = self.Store.open_readonly(self.db).connect()
        self.addCleanup(reader.close)
        cursor = reader.execute('SELECT spent FROM project WHERE id=?', (self.pid,))
        writer = self.store.connect()
        try:
            writer.execute('UPDATE project SET spent=63 WHERE id=?', (self.pid,))
            with self.assertRaisesRegex(sqlite3.OperationalError, 'changed.*retry'):
                cursor.fetchone()
        finally:
            writer.close()

    def test_existing_readonly_constructor_remains_a_query_only_connection(self):
        legacy = self.Store(self.db, readonly=True)
        with closing(legacy.connect()) as con:
            self.assertEqual(con.execute('SELECT spent FROM project WHERE id=?', (self.pid,)).fetchone()[0], 0)
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute('UPDATE project SET spent=1 WHERE id=?', (self.pid,))

    def test_stop_unknown_project_does_not_create_or_change_a_project(self):
        with closing(self.store.connect()) as con:
            before = list(con.iterdump())
        with self.assertRaises(Exception):
            self.store.request_stop('unknown-project')
        with closing(self.store.connect()) as con:
            self.assertEqual(list(con.iterdump()), before)


def additional_suite():
    loader = unittest.TestLoader()
    inherited = set(loader.getTestCaseNames(StoreReadOnlyAcceptance))
    return unittest.TestSuite(
        OwnedStoreChecks(name) for name in loader.getTestCaseNames(OwnedStoreChecks)
        if name not in inherited
    )
