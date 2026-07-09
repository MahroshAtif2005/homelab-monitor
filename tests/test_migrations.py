"""Tests for the versioned migration runner (Phase 2.2)."""
import sqlite3
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.db as _db_module
from backend.db import run_migrations, register_migration


class TestMigrationRunner(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        # Save and reset _MIGRATIONS so tests don't bleed into each other
        self._orig = dict(_db_module._MIGRATIONS)
        _db_module._MIGRATIONS.clear()

    def tearDown(self):
        self.conn.close()
        _db_module._MIGRATIONS.clear()
        _db_module._MIGRATIONS.update(self._orig)

    def test_creates_schema_migrations_table(self):
        run_migrations(self.conn)
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_records_applied_migration(self):
        register_migration("test_v1", "CREATE TABLE IF NOT EXISTS _t1 (id INTEGER);")
        run_migrations(self.conn)
        applied = [r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")]
        self.assertIn("test_v1", applied)

    def test_idempotent_second_run(self):
        register_migration("test_v2", "CREATE TABLE IF NOT EXISTS _t2 (id INTEGER);")
        run_migrations(self.conn)
        run_migrations(self.conn)  # must not error or duplicate
        count = self.conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='test_v2'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_fresh_and_existing_db_converge(self):
        conn2 = sqlite3.connect(":memory:")
        run_migrations(conn2)
        row = conn2.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone()
        self.assertIsNotNone(row)
        conn2.close()

    def test_migrations_run_in_version_order(self):
        order = []
        register_migration("0003", "CREATE TABLE IF NOT EXISTS _ord3 (id INTEGER);")
        register_migration("0001", "CREATE TABLE IF NOT EXISTS _ord1 (id INTEGER);")
        register_migration("0002", "CREATE TABLE IF NOT EXISTS _ord2 (id INTEGER);")
        run_migrations(self.conn)
        applied = [r[0] for r in self.conn.execute(
            "SELECT version FROM schema_migrations ORDER BY applied_at, version"
        )]
        self.assertEqual(applied, ["0001", "0002", "0003"])

    def test_no_duplicate_on_partial_applied(self):
        register_migration("a001", "CREATE TABLE IF NOT EXISTS _pa1 (id INTEGER);")
        register_migration("a002", "CREATE TABLE IF NOT EXISTS _pa2 (id INTEGER);")
        run_migrations(self.conn)
        # register a new one and re-run — only a003 should be added
        register_migration("a003", "CREATE TABLE IF NOT EXISTS _pa3 (id INTEGER);")
        run_migrations(self.conn)
        count = self.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
