"""backend/db/repos/host_samples.py — per-host time-series (multi-host slice).

One raw row per successful host poll plus an hourly rollup keyed (ts, host),
mirroring the hub's own samples/samples_1h split (see samples.rollup_now).
The Costs integration reads only the 1h rollup, exactly like the hub path.
"""
from backend.db import connection

_COLS = ("cpu", "ram_used", "ram_total", "load1", "ctemp",
         "gpu_util", "gpu_mem_used", "gpu_mem_total",
         "gpu_power", "cpu_power", "dram_power", "gpu_temp")

_UPSERT_SET = ",\n".join(
    f"{c}=CASE WHEN excluded.{c} IS NOT NULL "
    f"THEN (COALESCE({c},0)*cnt+excluded.{c})/(cnt+1) ELSE {c} END"
    for c in _COLS
)


def record(conn, ts: int, host: str, **fields):
    """Insert one raw poll row and fold it into the hourly rollup. conn is
    required — the caller holds app.LOCK. Unknown fields are ignored; missing
    fields store NULL so absent sensors (no GPU, unreadable RAPL) never read
    as zero watts."""
    vals = tuple(fields.get(c) for c in _COLS)
    conn.execute(
        f"INSERT OR REPLACE INTO host_samples(ts,host,{','.join(_COLS)}) "
        f"VALUES(?,?{',?' * len(_COLS)})",
        (ts, host) + vals
    )
    h = (ts // 3600) * 3600
    conn.execute(
        f"INSERT INTO host_samples_1h(ts,host,{','.join(_COLS)},cnt) "
        f"VALUES(?,?{',?' * len(_COLS)},1) "
        f"ON CONFLICT(ts,host) DO UPDATE SET\n{_UPSERT_SET},\ncnt=cnt+1",
        (h, host) + vals
    )


def min_ts_1h(host: str, conn=None):
    """Earliest rollup ts for a host, or None."""
    c = conn or connection()
    return c.execute(
        "SELECT MIN(ts) FROM host_samples_1h WHERE host=?", (host,)
    ).fetchone()[0]


def min_ts(host: str, conn=None):
    """Earliest sample for a host across raw and rollup, or None.

    What `range=all` should actually span: raw rows are retention-purged, so
    asking `host_samples` alone would shrink a host's "all" window to the last
    couple of days the moment the purge runs.
    """
    c = conn or connection()
    raw = c.execute("SELECT MIN(ts) FROM host_samples WHERE host=?", (host,)).fetchone()[0]
    roll = min_ts_1h(host, conn=c)
    vals = [v for v in (raw, roll) if v is not None]
    return min(vals) if vals else None


def _use_rollup(host: str, since: int, conn) -> bool:
    """True when the raw table can't honestly answer for this window.

    Same rule as gpu_samples: if the oldest raw row is newer than the window
    start, raw would answer a 30d question with 2d of data and label it 30d.
    """
    oldest = conn.execute(
        "SELECT MIN(ts) FROM host_samples WHERE host=?", (host,)
    ).fetchone()[0]
    return oldest is None or oldest > since


def vitals_series(host: str, since: int, bucket: int, conn=None) -> list:
    """Bucketed CPU / RAM / load / temperature for one host since `since`.

    Returns (bucket_ts, avg_cpu, avg_ram_used, max_ram_total, avg_load1,
    avg_ctemp) ordered by time — the per-host counterpart of the hub's own
    `D.total` series, so the System tab's chart has one shape to draw whichever
    machine is selected.

    `ram_total` takes MAX rather than AVG: it is a capacity, not a rate, and
    averaging it across a bucket where one poll missed the value would drag the
    denominator of every RAM percentage down with it.
    """
    c = conn or connection()
    table = "host_samples_1h" if _use_rollup(host, since, c) else "host_samples"
    return c.execute(
        "SELECT (ts/?)*? b, AVG(cpu), AVG(ram_used), MAX(ram_total), AVG(load1), AVG(ctemp) "
        f"FROM {table} WHERE host=? AND ts>=? GROUP BY b ORDER BY b",
        (bucket, bucket, host, since)
    ).fetchall()


def comp_bucketed(host: str, ts: int, bk: int, conn=None) -> list:
    """(bucket, avg_gpu_power, avg_cpu_power, avg_dram_power) since ts."""
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, AVG(gpu_power), AVG(cpu_power), AVG(dram_power) "
        "FROM host_samples_1h WHERE host=? AND ts>=? GROUP BY b ORDER BY b",
        (bk, bk, host, ts)
    ).fetchall()


def full_since(host: str, ts: int, conn=None) -> list:
    """(ts, gpu_power, cpu_power, dram_power, cnt) since ts."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,gpu_power,cpu_power,dram_power,cnt "
        "FROM host_samples_1h WHERE host=? AND ts>=?",
        (host, ts)
    ).fetchall()


def total_w_since(host: str, ts: int, conn=None) -> list:
    """(ts, total_watts, cnt) since ts — GPU + CPU + DRAM pooled."""
    c = conn or connection()
    return c.execute(
        "SELECT ts, COALESCE(gpu_power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0) w, cnt "
        "FROM host_samples_1h WHERE host=? AND ts>=?",
        (host, ts)
    ).fetchall()


def heatmap(host: str, ts: int, conn=None) -> list:
    """(ts, total_w, cnt) since ts for the busy-hours heatmap, ordered."""
    c = conn or connection()
    return c.execute(
        "SELECT ts, COALESCE(gpu_power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0) w, cnt "
        "FROM host_samples_1h WHERE host=? AND ts>=? ORDER BY ts",
        (host, ts)
    ).fetchall()


def rename_host(old: str, new: str, conn=None):
    """Follow a host rename so its power history doesn't split."""
    c = conn or connection()
    c.execute("UPDATE host_samples SET host=? WHERE host=?", (new, old))
    c.execute("UPDATE host_samples_1h SET host=? WHERE host=?", (new, old))
