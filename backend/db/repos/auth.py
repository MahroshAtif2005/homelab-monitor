"""backend/db/repos/auth.py — CRUD helpers for the api_keys table (Phase 4.1)."""
import time
from backend.db import connection


def get_by_hash(key_hash: str, conn=None):
    """Return (id, expires_at) for a key by hash, or None."""
    c = conn or connection()
    return c.execute(
        "SELECT id, expires_at FROM api_keys WHERE key_hash=?", (key_hash,)
    ).fetchone()


def update_last_used(kid: str, ts: int, conn=None):
    """Update last_used_at for a key."""
    c = conn or connection()
    c.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (ts, kid))
    c.commit()


def list_all(conn=None) -> list:
    """Return all api_keys rows ordered by created_at DESC."""
    c = conn or connection()
    return c.execute(
        "SELECT id,name,prefix,created_at,expires_at,last_used_at "
        "FROM api_keys ORDER BY created_at DESC"
    ).fetchall()


def count_runs_by_key(conn=None) -> list:
    """Return [(key_id, count)] for runs grouped by key_id."""
    c = conn or connection()
    return c.execute(
        "SELECT key_id, COUNT(*) FROM runs WHERE key_id IS NOT NULL GROUP BY key_id"
    ).fetchall()


def delete(kid: str, conn=None) -> int:
    """Delete an api_key by id. Returns rowcount."""
    c = conn or connection()
    cur = c.execute("DELETE FROM api_keys WHERE id=?", (kid,))
    c.commit()
    return cur.rowcount


def insert(id: str, name: str, key_hash: str, prefix: str,
           created_at: int, expires_at, last_used_at, conn=None):
    """Insert a new api_key row."""
    c = conn or connection()
    c.execute(
        "INSERT INTO api_keys(id,name,key_hash,prefix,created_at,expires_at,last_used_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (id, name, key_hash, prefix, created_at, expires_at, last_used_at)
    )
    c.commit()


def check_exists_by_hash(key_hash: str, conn=None) -> bool:
    """Return True if a key with this hash already exists."""
    c = conn or connection()
    return bool(c.execute(
        "SELECT 1 FROM api_keys WHERE key_hash=?", (key_hash,)
    ).fetchone())


def get_names(conn=None) -> list:
    """Return [(id, name)] for all api keys."""
    c = conn or connection()
    return c.execute("SELECT id, name FROM api_keys").fetchall()
