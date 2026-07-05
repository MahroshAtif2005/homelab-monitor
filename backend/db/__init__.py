"""Thread-local SQLite connection factory.

Each calling thread gets its own connection (WAL mode, busy_timeout=5000).
The module-level `connection()` function is the only public API for new code.
`app.py` re-exports `DB` and `LOCK` for backward compatibility with tests.

Concurrency model (conn-per-thread vs. the legacy global DB + LOCK):
- WAL mode allows concurrent readers alongside one writer — no reader is
  blocked by a writer and vice versa.
- `busy_timeout=5000` (set inside `_open_db_connection`) makes the SQLite
  driver retry automatically for up to 5 s when it encounters SQLITE_BUSY,
  so callers using `backend.db.connection()` do not need an explicit LOCK.
- The legacy `app.LOCK` + `app.DB` pair is preserved unchanged. All existing
  writers in `app.py` continue to acquire LOCK before writing. Phase 4 will
  migrate those call-sites to `backend.db.connection()` and retire the lock.
"""
import threading

import app as _app

# Module-level name so tests can patch it: patch.object(backend.db, 'DB_PATH', ...)
DB_PATH = _app.DB_PATH
_open_db_connection = _app._open_db_connection

_local = threading.local()


def connection():
    """Return this thread's SQLite connection, creating it on first call."""
    if not getattr(_local, 'conn', None):
        _local.conn = _open_db_connection(DB_PATH)
    return _local.conn
