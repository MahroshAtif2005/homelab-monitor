"""CRUD helpers for uptime_checks and uptime_results tables."""
from backend.db import connection


def list_checks(conn=None) -> list:
    """Return all uptime checks as tuples, ordered by created_at."""
    c = conn or connection()
    return c.execute(
        "SELECT * FROM uptime_checks ORDER BY created_at"
    ).fetchall()


def get_check(cid: str, conn=None):
    """Return one uptime_check row by id, or None."""
    c = conn or connection()
    return c.execute(
        "SELECT * FROM uptime_checks WHERE id=?", (cid,)
    ).fetchone()


def insert_check(id, label, type, target, interval_sec, timeout_sec, conn=None):
    """Insert a new uptime check."""
    c = conn or connection()
    c.execute(
        "INSERT INTO uptime_checks(id,label,type,target,interval_sec,timeout_sec) VALUES(?,?,?,?,?,?)",
        (id, label, type, target, interval_sec, timeout_sec)
    )
    c.commit()


def delete_check(cid: str, conn=None) -> int:
    """Delete a check by id. Returns rowcount."""
    c = conn or connection()
    cur = c.execute("DELETE FROM uptime_checks WHERE id=?", (cid,))
    c.commit()
    return cur.rowcount


def insert_result(check_id, ts, ok, latency_ms, conn=None):
    """Insert one uptime result row."""
    c = conn or connection()
    c.execute(
        "INSERT INTO uptime_results(check_id,ts,ok,latency_ms) VALUES(?,?,?,?)",
        (check_id, ts, ok, latency_ms)
    )
    c.commit()


def results_since(check_id: str, ts: int, conn=None) -> list:
    """Return uptime_results for a check since ts, ordered ascending."""
    c = conn or connection()
    return c.execute(
        "SELECT * FROM uptime_results WHERE check_id=? AND ts>=? ORDER BY ts",
        (check_id, ts)
    ).fetchall()
