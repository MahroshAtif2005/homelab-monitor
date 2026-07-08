"""Unit tests for backend.db connection factory (Phase 2.1)."""
import threading
import unittest
import tempfile
import os
import sys
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.db


class TestConnFactory(unittest.TestCase):
    def setUp(self):
        # Each test gets a fresh thread-local state via a temp DB file.
        self._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmp.close()
        self._patcher = patch.object(backend.db, 'DB_PATH', self._tmp.name)
        self._patcher.start()
        # Reset thread-local so tests don't share connections.
        backend.db._local.conn = None

    def tearDown(self):
        self._patcher.stop()
        os.unlink(self._tmp.name)
        backend.db._local.conn = None

    def test_same_thread_returns_same_conn(self):
        c1 = backend.db.connection()
        c2 = backend.db.connection()
        self.assertIs(c1, c2)

    def test_different_threads_return_different_conns(self):
        results = {}
        errors = {}

        def get_conn(key):
            try:
                results[key] = backend.db.connection()
            except Exception as e:
                errors[key] = e

        t1 = threading.Thread(target=get_conn, args=('t1',))
        t2 = threading.Thread(target=get_conn, args=('t2',))
        t1.start(); t2.start(); t1.join(); t2.join()
        if errors:
            self.fail(f"Thread(s) raised exceptions: {errors}")
        self.assertIsNot(results['t1'], results['t2'])

    def test_connection_is_alive(self):
        conn = backend.db.connection()
        row = conn.execute("SELECT 1").fetchone()
        self.assertEqual(row[0], 1)
