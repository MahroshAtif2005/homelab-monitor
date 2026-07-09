"""Thread-local SQLite connection factory + versioned migration runner.

Each calling thread gets its own connection (WAL mode, busy_timeout=5000).
`connection()` is the public API for new code.
`run_migrations()` applies registered migrations in version order (idempotent).
`app.py` re-exports `DB` and `LOCK` for backward compatibility with tests.

Concurrency model (conn-per-thread vs. the legacy global DB + LOCK):
- WAL mode allows concurrent readers alongside one writer.
- `busy_timeout=5000` retries automatically on SQLITE_BUSY (up to 5 s).
- The legacy `app.LOCK` + `app.DB` pair is preserved until Phase 4.
"""
import os
import threading
import time as _time

from backend.db.repos.schema import open_db_connection as _open_db_connection

# Module-level name so tests can patch it: patch.object(backend.db, 'DB_PATH', ...)
# Mirrors app.py's own DB_PATH default — read directly from the env instead of
# `import app`, which deadlocks: when app.py runs as __main__ (the container's
# actual entrypoint, `python /app/app.py`), 'app' isn't yet in sys.modules under
# that name, so `import app` here re-executes app.py from scratch mid-import,
# looping back into this same module before it has finished defining `bp`.
DB_PATH = os.environ.get("DB_PATH", "/data/gpu.db")

_local = threading.local()


def connection():
    """Return this thread's SQLite connection, creating it on first call."""
    if not getattr(_local, 'conn', None):
        _local.conn = _open_db_connection(DB_PATH)
    return _local.conn


# ── Migration runner ──────────────────────────────────────────────────────────

_MIGRATIONS: dict[str, str] = {}  # version -> SQL string


def register_migration(version: str, sql: str) -> None:
    """Register a migration by version string (e.g. '0002')."""
    _MIGRATIONS[version] = sql


def run_migrations(conn) -> None:
    """Apply any pending migrations in version order. Idempotent."""
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version TEXT PRIMARY KEY,
        applied_at INTEGER NOT NULL
    )""")
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version in sorted(_MIGRATIONS):
        if version in applied:
            continue
        conn.executescript(_MIGRATIONS[version])
        conn.execute(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (version, int(_time.time()))
        )
        conn.commit()
