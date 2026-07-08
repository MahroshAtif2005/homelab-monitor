"""Helpers for the settings table."""
from backend.db import connection


def get(key: str, default=None, conn=None):
    """Return the value for key, or default if not set."""
    c = conn or connection()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set(key: str, value: str, conn=None):
    """Upsert a setting."""
    c = conn or connection()
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    c.commit()


def get_all(conn=None) -> list:
    """Return all (key, value) pairs from settings."""
    c = conn or connection()
    return c.execute("SELECT key, value FROM settings").fetchall()
