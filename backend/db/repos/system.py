"""backend/db/repos/system.py — helpers for events, disk_io_samples, and misc queries (Phase 4.1)."""
from backend.db import connection

_TOTAL_W_EXPR = "COALESCE(power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0)"


# ── events table ─────────────────────────────────────────────────────────────

def insert_event(ts: int, service: str, kind: str, detail: str, conn=None):
    """Insert or ignore an event row."""
    c = conn or connection()
    c.execute(
        "INSERT OR IGNORE INTO events VALUES(?,?,?,?)",
        (ts, service, kind, detail)
    )
    c.commit()


def insert_events_batch(event_tuples: list, conn=None):
    """Insert multiple (ts, service, kind, detail) event rows in one transaction."""
    c = conn or connection()
    c.executemany("INSERT OR IGNORE INTO events VALUES(?,?,?,?)", event_tuples)
    c.commit()


def query_events_since(since: int, order_desc: bool = False, limit: int = None, conn=None) -> list:
    """Return events since `since`. Optional desc ordering and limit."""
    c = conn or connection()
    order = "DESC" if order_desc else "ASC"
    if limit is not None:
        return c.execute(
            f"SELECT ts, service, kind, detail FROM events WHERE ts>=? ORDER BY ts {order} LIMIT ?",
            (since, limit)
        ).fetchall()
    return c.execute(
        f"SELECT ts, service, kind, detail FROM events WHERE ts>=? ORDER BY ts {order}",
        (since,)
    ).fetchall()


def query_oom_events_since(since: int, conn=None) -> list:
    """Return OOM events since `since` ordered by ts."""
    c = conn or connection()
    return c.execute(
        "SELECT ts, service, detail FROM events WHERE kind='oom' AND ts>=? ORDER BY ts",
        (since,)
    ).fetchall()


# ── disk_io_samples ───────────────────────────────────────────────────────────

def query_disk_io_for_anomaly(since: int, conn=None):
    """Return (device, read_mb_s, write_mb_s) rows for anomaly detection."""
    c = conn or connection()
    return c.execute(
        "SELECT device, read_mb_s, write_mb_s FROM disk_io_samples "
        "WHERE ts>=? ORDER BY ts", (since,)
    ).fetchall()


# ── samples helpers ───────────────────────────────────────────────────────────

def min_ts_samples(conn=None):
    """Return the earliest ts in samples, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM samples").fetchone()[0]


def min_ts_net_samples(conn=None):
    """Return the earliest ts in net_samples, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM net_samples").fetchone()[0]


def count_samples_since(since: int, conn=None) -> int:
    """Return count of samples rows >= since."""
    c = conn or connection()
    return c.execute("SELECT COUNT(*) FROM samples WHERE ts>=?", (since,)).fetchone()[0] or 1


def query_samples_bucketed(bk: int, since: int, conn=None) -> list:
    """Return bucketed aggregate rows from samples since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b,AVG(util),AVG(mem_used),MAX(mem_used),AVG(power),AVG(temp),"
        "AVG(cpu),AVG(ram_used),AVG(ram_total),AVG(load1),AVG(ctemp) "
        "FROM samples WHERE ts>=? GROUP BY b ORDER BY b",
        (bk, bk, since)
    ).fetchall()


def query_proc_bucketed(bk: int, since: int, conn=None) -> list:
    """Return (bucket, service, avg_mem) from proc since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b,service,AVG(mem) FROM proc WHERE ts>=? GROUP BY b,service",
        (bk, bk, since)
    ).fetchall()


def query_disk_io_bucketed(bk: int, since: int, conn=None) -> list:
    """Return bucketed disk_io_samples averages since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b,device,AVG(read_mb_s),AVG(write_mb_s),AVG(util_pct) "
        "FROM disk_io_samples WHERE ts>=? GROUP BY b,device",
        (bk, bk, since)
    ).fetchall()


def query_proc_summary(since: int, conn=None) -> list:
    """Return (service, max_mem, avg_mem, count_distinct_ts) from proc since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT service,MAX(mem),AVG(mem),COUNT(DISTINCT ts) FROM proc WHERE ts>=? GROUP BY service",
        (since,)
    ).fetchall()


def query_model_summary(since: int, conn=None) -> list:
    """Return (service, model, max_vram, avg_vram) from models since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT service,model,MAX(vram),AVG(vram) FROM models WHERE ts>=? AND vram IS NOT NULL "
        "GROUP BY service,model",
        (since,)
    ).fetchall()


def query_edges_summary(since: int, conn=None) -> list:
    """Return (caller, server, sum_conns, count_ts) from edges since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT caller,server,SUM(conns),COUNT(DISTINCT ts) FROM edges WHERE ts>=? "
        "GROUP BY caller,server",
        (since,)
    ).fetchall()


def query_events_range(since: int, conn=None) -> list:
    """Return (ts, service, kind, detail) events since `since` ordered by ts."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,service,kind,detail FROM events WHERE ts>=? ORDER BY ts", (since,)
    ).fetchall()


def query_proc_at_time(ts: int, exclude_service: str, conn=None):
    """Return (service, mem) from proc at/before ts, excluding one service."""
    c = conn or connection()
    return c.execute(
        "SELECT service,mem FROM proc WHERE ts<=? AND service!=? ORDER BY ts DESC,mem DESC LIMIT 1",
        (ts, exclude_service)
    ).fetchone()


def query_net_samples(since: int, conn=None) -> list:
    """Return (ts, iface, bytes_in, bytes_out) from net_samples since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,iface,bytes_in,bytes_out FROM net_samples WHERE ts>=? ORDER BY iface,ts",
        (since,)
    ).fetchall()


def min_ts_samples_1h(conn=None):
    """Return the earliest ts in samples_1h, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM samples_1h").fetchone()[0]


def query_samples_for_cost(ts_from: int, ts_to: int, conn=None) -> list:
    """Return (ts, total_w) from samples in [ts_from, ts_to)."""
    c = conn or connection()
    return c.execute(
        f"SELECT ts, {_TOTAL_W_EXPR} w FROM samples WHERE ts>=? AND ts<?",
        (ts_from, ts_to)
    ).fetchall()


def min_ts_power_proc(conn=None):
    """Return the earliest ts in power_proc, or None."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM power_proc").fetchone()[0]
