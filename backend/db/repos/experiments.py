"""backend/db/repos/experiments.py — CRUD helpers for runs and run_metrics (Phase 4.1)."""
from backend.db import connection


def insert_run(id, name, source, status, started_at, ended_at, host, params, tags, notes,
               heartbeat_at, ext_id, created_at, key_id, conn=None):
    """Insert or ignore a new run row."""
    c = conn or connection()
    c.execute(
        "INSERT INTO runs(id,name,source,status,started_at,ended_at,host,params,tags,notes,"
        "heartbeat_at,ext_id,created_at,key_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO NOTHING",
        (id, name, source, status, started_at, ended_at, host, params, tags, notes,
         heartbeat_at, ext_id, created_at, key_id)
    )
    c.commit()


def update_run(fields_sql: str, args: list, conn=None) -> int:
    """Run dynamic UPDATE runs SET <fields_sql> WHERE id=?. Returns rowcount."""
    c = conn or connection()
    cur = c.execute(f"UPDATE runs SET {fields_sql} WHERE id=?", args)
    c.commit()
    return cur.rowcount


def update_run_status(rid, status, ended_at, heartbeat_at, conn=None) -> int:
    """Update run status, ended_at, heartbeat_at. Returns rowcount."""
    c = conn or connection()
    cur = c.execute(
        "UPDATE runs SET status=?, ended_at=?, heartbeat_at=? WHERE id=?",
        (status, ended_at, heartbeat_at, rid)
    )
    c.commit()
    return cur.rowcount


def update_run_heartbeat(rid, ts, conn=None):
    """Update heartbeat_at for a run."""
    c = conn or connection()
    c.execute("UPDATE runs SET heartbeat_at=? WHERE id=?", (ts, rid))
    c.commit()


def exists_run(rid: str, conn=None) -> bool:
    """Return True if a run with this id exists."""
    c = conn or connection()
    return bool(c.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone())


def delete_run(rid: str, conn=None) -> int:
    """Delete a run by id. Returns rowcount."""
    c = conn or connection()
    cur = c.execute("DELETE FROM runs WHERE id=?", (rid,))
    c.commit()
    return cur.rowcount


def delete_run_metrics(run_id: str, conn=None):
    """Delete all metrics for a run."""
    c = conn or connection()
    c.execute("DELETE FROM run_metrics WHERE run_id=?", (run_id,))
    c.commit()


def list_runs(query: str, args: list, conn=None) -> list:
    """Execute a dynamic SELECT on runs with args. Returns fetchall()."""
    c = conn or connection()
    return c.execute(query, args).fetchall()


def get_run(rid: str, conn=None):
    """Return one run row or None."""
    c = conn or connection()
    return c.execute(
        "SELECT id,name,source,status,started_at,ended_at,host,params,tags,notes "
        "FROM runs WHERE id=?", (rid,)
    ).fetchone()


def get_run_metrics(rid: str, conn=None) -> list:
    """Return all metrics for a run ordered by key, ts, step."""
    c = conn or connection()
    return c.execute(
        "SELECT key,ts,step,value FROM run_metrics WHERE run_id=? ORDER BY key,ts,step",
        (rid,)
    ).fetchall()


def get_run_metrics_latest(rid: str, conn=None) -> list:
    """Return the latest value per key for a run."""
    c = conn or connection()
    return c.execute(
        "SELECT key, value FROM run_metrics WHERE run_id=? AND rowid IN "
        "(SELECT MAX(rowid) FROM run_metrics WHERE run_id=? GROUP BY key)",
        (rid, rid)
    ).fetchall()


def insert_metrics(rows: list, conn=None):
    """Bulk-insert run_metrics rows [(run_id, ts, step, key, value)]."""
    c = conn or connection()
    c.executemany(
        "INSERT INTO run_metrics(run_id,ts,step,key,value) VALUES(?,?,?,?,?)", rows
    )
    c.commit()


def get_run_power_buckets(started: int, end: int, bk: int, conn=None) -> list:
    """Return (bucket, avg_power, avg_util) for samples within [started, end]."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, AVG(power), AVG(util) FROM samples "
        "WHERE ts>=? AND ts<=? GROUP BY b ORDER BY b",
        (bk, bk, started, end)
    ).fetchall()


def get_run_cost_samples(started: int, end: int, conn=None):
    """Iterate (ts, util, power) samples for cost computation over [started, end]."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,util,power FROM samples WHERE ts>=? AND ts<=? AND power IS NOT NULL",
        (started, end)
    ).fetchall()


def upsert_mlflow_run(rid, name, status, started, ended, ext, params_json, tags_json, now,
                      conn=None):
    """Insert or update an mlflow-sourced run."""
    c = conn or connection()
    c.execute(
        "INSERT INTO runs(id,name,source,status,started_at,ended_at,host,params,tags,"
        "notes,heartbeat_at,ext_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source,ext_id) DO UPDATE SET status=excluded.status, "
        "ended_at=excluded.ended_at, name=excluded.name, params=excluded.params, "
        "tags=excluded.tags, heartbeat_at=excluded.heartbeat_at",
        (rid, name, "mlflow", status, started, ended, "mlflow",
         params_json, tags_json, "", now, ext, now)
    )
    c.commit()


def get_mlflow_run_id(ext_id: str, conn=None):
    """Return the run id for an mlflow ext_id, or None."""
    c = conn or connection()
    row = c.execute("SELECT id FROM runs WHERE source='mlflow' AND ext_id=?", (ext_id,)).fetchone()
    return row[0] if row else None


def insert_metrics_many(rows: list, conn=None):
    """Bulk-insert run_metrics without committing (for use within a LOCK block)."""
    c = conn or connection()
    c.executemany("INSERT INTO run_metrics(run_id,ts,step,key,value) VALUES(?,?,?,?,?)", rows)
    # No commit — caller commits
