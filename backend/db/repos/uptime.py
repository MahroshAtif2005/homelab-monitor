"""CRUD helpers for uptime_checks and uptime_results tables."""
from backend.db import connection


def list_checks(conn=None) -> list:
    """Return all uptime checks as tuples, ordered by created_at."""
    c = conn or connection()
    return c.execute(
        "SELECT * FROM uptime_checks ORDER BY created_at"
    ).fetchall()


def list_checks_full(conn=None) -> list:
    """Return all uptime checks with all columns, ordered by created_at."""
    c = conn or connection()
    return c.execute(
        "SELECT id,label,type,target,interval_sec,timeout_sec,expected_status,"
        "alerts_enabled,fail_threshold,latency_warn_ms,enabled,created_at,public "
        "FROM uptime_checks ORDER BY created_at"
    ).fetchall()


def get_check(cid: str, conn=None):
    """Return one uptime_check row by id, or None."""
    c = conn or connection()
    return c.execute(
        "SELECT * FROM uptime_checks WHERE id=?", (cid,)
    ).fetchone()


def check_exists(cid: str, conn=None) -> bool:
    """Return True if a check with this id exists."""
    c = conn or connection()
    return bool(c.execute("SELECT 1 FROM uptime_checks WHERE id=?", (cid,)).fetchone())


def insert_check(id, label, type, target, interval_sec, timeout_sec, conn=None):
    """Insert a new uptime check (legacy 6-column form)."""
    c = conn or connection()
    c.execute(
        "INSERT INTO uptime_checks(id,label,type,target,interval_sec,timeout_sec) VALUES(?,?,?,?,?,?)",
        (id, label, type, target, interval_sec, timeout_sec)
    )
    c.commit()


def insert_check_full(id, label, type, target, interval_sec, timeout_sec, expected_status,
                      alerts_enabled, fail_threshold, latency_warn_ms, enabled, created_at,
                      public, conn=None):
    """Insert a new uptime check with all columns."""
    c = conn or connection()
    c.execute(
        "INSERT INTO uptime_checks(id,label,type,target,interval_sec,timeout_sec,"
        "expected_status,alerts_enabled,fail_threshold,latency_warn_ms,enabled,created_at,public) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (id, label, type, target, interval_sec, timeout_sec, expected_status,
         alerts_enabled, fail_threshold, latency_warn_ms, enabled, created_at, public)
    )
    c.commit()


def update_check_full(cid, label, type, target, interval_sec, timeout_sec, expected_status,
                      alerts_enabled, fail_threshold, latency_warn_ms, enabled, public, conn=None):
    """Update all mutable fields of an uptime check."""
    c = conn or connection()
    c.execute(
        "UPDATE uptime_checks SET label=?,type=?,target=?,interval_sec=?,timeout_sec=?,"
        "expected_status=?,alerts_enabled=?,fail_threshold=?,latency_warn_ms=?,enabled=?,public=? WHERE id=?",
        (label, type, target, interval_sec, timeout_sec, expected_status,
         alerts_enabled, fail_threshold, latency_warn_ms, enabled, public, cid)
    )
    c.commit()


def update_check_fields(fields_sql: str, vals: list, cid: str, conn=None):
    """Dynamic partial update of an uptime check."""
    c = conn or connection()
    c.execute(f"UPDATE uptime_checks SET {fields_sql} WHERE id=?", (*vals, cid))
    c.commit()


def delete_check(cid: str, conn=None) -> int:
    """Delete a check by id. Returns rowcount."""
    c = conn or connection()
    cur = c.execute("DELETE FROM uptime_checks WHERE id=?", (cid,))
    c.commit()
    return cur.rowcount


def delete_check_and_results(cid: str, conn=None) -> int:
    """Delete a check and all its results. Returns rowcount of check delete."""
    c = conn or connection()
    cur = c.execute("DELETE FROM uptime_checks WHERE id=?", (cid,))
    c.execute("DELETE FROM uptime_results WHERE check_id=?", (cid,))
    c.commit()
    return cur.rowcount


def insert_result(check_id, ts, ok, latency_ms, conn=None):
    """Insert one uptime result row (legacy 4-column form)."""
    c = conn or connection()
    c.execute(
        "INSERT INTO uptime_results(check_id,ts,ok,latency_ms) VALUES(?,?,?,?)",
        (check_id, ts, ok, latency_ms)
    )
    c.commit()


def insert_result_full(check_id, ts, up, latency_ms, code, err, cert_days, cert_expires_at,
                       conn=None):
    """Insert a full uptime result row."""
    c = conn or connection()
    c.execute(
        "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err,cert_days_remaining,cert_expires_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (check_id, ts, up, latency_ms, code, err, cert_days, cert_expires_at)
    )
    c.commit()


def trim_results(check_id: str, cap: int, conn=None):
    """Delete old results keeping only the latest `cap` rows for a check."""
    c = conn or connection()
    c.execute(
        "DELETE FROM uptime_results WHERE check_id=? AND rowid NOT IN "
        "(SELECT rowid FROM uptime_results WHERE check_id=? ORDER BY rowid DESC LIMIT ?)",
        (check_id, check_id, cap)
    )
    c.commit()


def insert_result_and_trim(check_id: str, ts: int, up: int, latency_ms, cert_days, cap: int,
                            code=None, err=None, cert_expires_at=None, conn=None):
    """Insert a full uptime result and trim old rows in one transaction."""
    c = conn or connection()
    c.execute(
        "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err,cert_days_remaining,cert_expires_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (check_id, ts, up, latency_ms, code, err, cert_days, cert_expires_at)
    )
    c.execute(
        "DELETE FROM uptime_results WHERE check_id=? AND rowid NOT IN "
        "(SELECT rowid FROM uptime_results WHERE check_id=? ORDER BY rowid DESC LIMIT ?)",
        (check_id, check_id, cap)
    )
    c.commit()


def results_since(check_id: str, ts: int, conn=None) -> list:
    """Return uptime_results for a check since ts, ordered ascending."""
    c = conn or connection()
    return c.execute(
        "SELECT * FROM uptime_results WHERE check_id=? AND ts>=? ORDER BY ts",
        (check_id, ts)
    ).fetchall()


def results_since_full(check_id: str, since: int, conn=None) -> list:
    """Return full uptime_results columns for a check since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,up,latency_ms,code,err,cert_days_remaining,cert_expires_at "
        "FROM uptime_results WHERE check_id=? AND ts>=? ORDER BY ts",
        (check_id, since)
    ).fetchall()


def results_last_n(check_id: str, n: int, conn=None) -> list:
    """Return the most recent n results for a check (desc order)."""
    c = conn or connection()
    return c.execute(
        "SELECT up FROM uptime_results WHERE check_id=? ORDER BY ts DESC LIMIT ?",
        (check_id, n)
    ).fetchall()


def results_last_500(check_id: str, conn=None) -> list:
    """Return the most recent 500 (ts, up) results for a check (desc)."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,up FROM uptime_results WHERE check_id=? ORDER BY ts DESC LIMIT 500",
        (check_id,)
    ).fetchall()


def results_window_agg(check_id: str, since: int, conn=None):
    """Return (count, sum_up) for a check since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT COUNT(*), SUM(up) FROM uptime_results WHERE check_id=? AND ts>=?",
        (check_id, since)
    ).fetchone()


def results_last_one(check_id: str, conn=None):
    """Return the single most recent result row for a check, or None."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,up,latency_ms,code,err,cert_days_remaining,cert_expires_at "
        "FROM uptime_results WHERE check_id=? ORDER BY ts DESC LIMIT 1",
        (check_id,)
    ).fetchone()


