"""backend/db/repos/notify.py — CRUD for notification_rules and maintenance_windows (Phase 4.1)."""
import time
import uuid
from backend.db import connection


# ── notification_rules ────────────────────────────────────────────────────────

def list_rules(conn=None) -> list:
    """Return all notification rules as raw tuples."""
    c = conn or connection()
    return c.execute(
        "SELECT id, match_kind, match_pattern, channel, min_level, enabled "
        "FROM notification_rules ORDER BY id"
    ).fetchall()


def insert_rule(match_kind, match_pattern, channel, min_level, enabled, conn=None):
    """Insert a new notification rule."""
    c = conn or connection()
    c.execute(
        "INSERT INTO notification_rules (match_kind, match_pattern, channel, min_level, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        (match_kind, match_pattern, channel, min_level, enabled)
    )
    c.commit()


def update_rule(rule_id, match_kind, match_pattern, channel, min_level, enabled, conn=None):
    """Update an existing notification rule by id."""
    c = conn or connection()
    c.execute(
        "UPDATE notification_rules SET match_kind=?, match_pattern=?, channel=?, "
        "min_level=?, enabled=? WHERE id=?",
        (match_kind, match_pattern, channel, min_level, enabled, rule_id)
    )
    c.commit()


def delete_rule(rule_id, conn=None):
    """Delete a notification rule by id."""
    c = conn or connection()
    c.execute("DELETE FROM notification_rules WHERE id=?", (rule_id,))
    c.commit()


# ── maintenance_windows ───────────────────────────────────────────────────────

def list_windows(conn=None) -> list:
    """Return all maintenance windows as raw tuples."""
    c = conn or connection()
    return c.execute(
        "SELECT id, label, kind, pattern, start_ts, end_ts, recurrence, note, created_at "
        "FROM maintenance_windows ORDER BY start_ts"
    ).fetchall()


def get_active_windows(conn=None) -> list:
    """Return all window rows needed for _in_maintenance check."""
    c = conn or connection()
    return c.execute(
        "SELECT kind, pattern, start_ts, end_ts, recurrence FROM maintenance_windows"
    ).fetchall()


def insert_window(wid, label, kind, pattern, start_ts, end_ts, recurrence, note, created_at,
                  conn=None):
    """Insert a new maintenance window."""
    c = conn or connection()
    c.execute(
        "INSERT INTO maintenance_windows(id,label,kind,pattern,start_ts,end_ts,recurrence,note,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (wid, label, kind, pattern, start_ts, end_ts, recurrence, note, created_at)
    )
    c.commit()


def delete_window(wid, conn=None):
    """Delete a maintenance window by id."""
    c = conn or connection()
    c.execute("DELETE FROM maintenance_windows WHERE id=?", (wid,))
    c.commit()
