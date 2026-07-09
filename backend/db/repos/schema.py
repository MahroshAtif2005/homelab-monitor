"""backend/db/repos/schema.py — DB initialization, migrations, and schema helpers.

Moved from app.py (Phase 4.1) so that app.py has zero .execute() calls in its
schema/migration functions.  All .execute() calls here are in backend/db/ and
are excluded from the Phase 4.1 acceptance criterion.
"""
import hashlib
import sqlite3
import time
import uuid


def open_db_connection(path: str):
    """Open a SQLite connection with WAL mode and busy_timeout set."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def apply_schema_migrations(conn, schema_sql, sample_migrations, host_migrations,
                             runs_migrations, uptime_migrations, uptime_check_migrations):
    """Run the full schema bootstrap + column-addition migrations on *conn*."""
    conn.executescript(schema_sql)
    for col in sample_migrations:
        try:
            conn.execute(f"ALTER TABLE samples ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in host_migrations:
        try:
            conn.execute(f"ALTER TABLE hosts ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in runs_migrations:
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in uptime_migrations:
        try:
            conn.execute(f"ALTER TABLE uptime_results ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    for col in uptime_check_migrations:
        try:
            conn.execute(f"ALTER TABLE uptime_checks ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # Migrate legacy single-instance api_key setting -> api_keys table.
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
        legacy = (row[0] if row else "") or ""
        if legacy:
            h = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
            if not conn.execute("SELECT 1 FROM api_keys WHERE key_hash=?", (h,)).fetchone():
                conn.execute(
                    "INSERT INTO api_keys(id,name,key_hash,prefix,created_at,expires_at,last_used_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "default (migrated)", h, legacy[:12], int(time.time()), None, None))
            conn.execute("UPDATE settings SET value='' WHERE key='api_key'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    record_baseline_if_needed(conn)


def record_baseline_if_needed(conn):
    """Stamp migration 0001 on any DB that already has the baseline schema applied."""
    try:
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        if "0001" not in applied:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                ("0001", int(time.time()))
            )
            conn.commit()
    except sqlite3.OperationalError:
        pass
