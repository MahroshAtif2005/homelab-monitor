"""backend/db/repos/gpu_samples.py — per-card GPU history for the whole fleet.

One raw row per card per poll plus an hourly rollup, keyed (ts, host, idx). The
hub stores its own cards as host='local', so a remote and the hub are the same
shape and the GPU cockpit reads both through one function. That is the point of
this module: the dashboard used to fork into a local renderer with charts and a
remote renderer with a snapshot, and the fork existed only because the storage
did.

Rollup rule: rates (util, power, VRAM) are averaged, but temperature and fan
also keep a MAX, and throttling keeps a duration. An hour that averaged 71 °C
while peaking at 87 °C is an hour with a thermal problem; averaging alone would
smooth away precisely the event worth alerting on.
"""
from backend.db import connection

# Raw per-sample columns, in the order record() writes them, paired with the key
# to read from a card dict. The pairing is explicit because one of them differs:
# the DB column `throttle` stores the numeric bitmask, which the card carries as
# `throttle_mask` — its `throttle` key is the list of human-readable reasons.
_COLS = ("util", "mem_used", "mem_total", "power", "temp", "fan",
         "mem_util", "clk_sm", "clk_mem", "power_limit", "temp_mem", "throttle")
_SRC_KEY = {"throttle": "throttle_mask"}

# Which throttle bits mean "something is wrong" as opposed to "doing what it was
# configured to do". SW/HW thermal and the generic HW slowdown are in; SW_POWER_CAP
# (0x04) is deliberately OUT.
#
# That exclusion is the difference between a useful signal and a permanent red
# light: a box whose cards run at a deliberately lowered power limit sits at its
# cap essentially all the time by design, so counting power-cap as throttling
# would report it as continuously throttled and train the user to ignore the
# indicator. Power-capping is still surfaced — health() derives it from power vs
# power_limit and the cockpit shows it as its own calm state.
# Kept in sync with app._THERMAL_BITS.
_THERMAL_BITS = 0x0000000000000068


def record(conn, ts: int, host: str, cards, interval: int = 10):
    """Store one poll's worth of cards for `host` and fold them into the rollup.

    `cards` is the probe/collector per-card list. conn is required — the caller
    holds app.LOCK. A metric the card didn't report stays NULL rather than 0, so
    "no fan sensor" and "fan stopped" remain distinguishable all the way down to
    storage; AVG() then skips the gap instead of charting a fake zero.

    `interval` is the poll period in seconds, used to turn "this sample was
    throttling" into seconds-per-hour in the rollup.
    """
    if not cards:
        return
    raw, roll = [], []
    for g in cards:
        idx = g.get("idx")
        if idx is None:
            continue
        vals = tuple(g.get(_SRC_KEY.get(c, c)) for c in _COLS)
        raw.append((ts, host, idx) + vals)
        thr = g.get("throttle_mask") or 0
        # Only thermal/power-brake bits count as throttled time. Idle and
        # app-clock bits are normal operation and would otherwise report a
        # perfectly healthy idle card as throttling all night.
        secs = interval if (thr & _THERMAL_BITS) else 0
        roll.append(((ts // 3600) * 3600, host, idx,
                     g.get("util"), g.get("mem_used"), g.get("mem_total"), g.get("power"),
                     g.get("temp"), g.get("temp"), g.get("fan"), g.get("fan"), secs))
    if not raw:
        return
    conn.executemany(
        f"INSERT INTO gpu_samples(ts,host,idx,{','.join(_COLS)}) "
        f"VALUES(?,?,?{',?' * len(_COLS)})", raw)
    # Averages are recomputed incrementally from the running count, the same way
    # host_samples_1h does it; MAX columns take the greater of stored and new.
    conn.executemany(
        "INSERT INTO gpu_samples_1h(ts,host,idx,util,mem_used,mem_total,power,"
        "temp,temp_max,fan,fan_max,throttle_secs,cnt) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1) "
        "ON CONFLICT(ts,host,idx) DO UPDATE SET "
        "  util=CASE WHEN excluded.util IS NOT NULL THEN (COALESCE(util,0)*cnt+excluded.util)/(cnt+1) ELSE util END,"
        "  mem_used=CASE WHEN excluded.mem_used IS NOT NULL THEN (COALESCE(mem_used,0)*cnt+excluded.mem_used)/(cnt+1) ELSE mem_used END,"
        "  mem_total=COALESCE(excluded.mem_total, mem_total),"
        "  power=CASE WHEN excluded.power IS NOT NULL THEN (COALESCE(power,0)*cnt+excluded.power)/(cnt+1) ELSE power END,"
        "  temp=CASE WHEN excluded.temp IS NOT NULL THEN (COALESCE(temp,0)*cnt+excluded.temp)/(cnt+1) ELSE temp END,"
        "  temp_max=MAX(COALESCE(temp_max,-273), COALESCE(excluded.temp_max,-273)),"
        "  fan=CASE WHEN excluded.fan IS NOT NULL THEN (COALESCE(fan,0)*cnt+excluded.fan)/(cnt+1) ELSE fan END,"
        "  fan_max=MAX(COALESCE(fan_max,-1), COALESCE(excluded.fan_max,-1)),"
        "  throttle_secs=throttle_secs+excluded.throttle_secs,"
        "  cnt=cnt+1",
        roll)


def cards_for(host: str, conn=None) -> list:
    """The card indexes this host has ever reported, ascending."""
    c = conn or connection()
    return [r[0] for r in c.execute(
        "SELECT DISTINCT idx FROM gpu_samples WHERE host=? ORDER BY idx", (host,)).fetchall()]


def series(host: str, since: int, bucket: int, conn=None) -> list:
    """Bucketed per-card series since `since`.

    Returns (bucket_ts, idx, util, mem_used, mem_total, power, temp, temp_max,
    fan, fan_max, mem_util, clk_sm, throttle_any) ordered by time then card.
    Reads the RAW table: buckets here are chart pixels, and the raw ring is what
    holds sub-hour resolution. MAX(temp) rides alongside AVG so a bucket that
    spans a spike shows both the trend and the peak.
    """
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, idx, AVG(util), AVG(mem_used), MAX(mem_total), AVG(power), "
        "       AVG(temp), MAX(temp), AVG(fan), MAX(fan), AVG(mem_util), AVG(clk_sm), "
        "       MAX(COALESCE(throttle,0)) "
        "FROM gpu_samples WHERE host=? AND ts>=? GROUP BY b, idx ORDER BY b, idx",
        (bucket, bucket, host, since)).fetchall()


def health(host: str, since: int, conn=None) -> list:
    """Per-card health rollup since `since`, for the card-health table.

    (idx, samples, avg_temp, peak_temp, peak_fan, throttled_samples,
     hot_samples_at_84, capped_samples). Counts rather than durations — the
     caller multiplies by the poll interval, which it knows and this doesn't.
    """
    c = conn or connection()
    return c.execute(
        "SELECT idx, COUNT(*), AVG(temp), MAX(temp), MAX(fan), "
        "       SUM(CASE WHEN COALESCE(throttle,0) & ? THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN temp >= 84 THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN power_limit > 0 AND power >= power_limit * 0.98 THEN 1 ELSE 0 END) "
        "FROM gpu_samples WHERE host=? AND ts>=? GROUP BY idx ORDER BY idx",
        (_THERMAL_BITS, host, since)).fetchall()


def throttle_spans(host: str, since: int, conn=None) -> list:
    """Raw (ts, idx) samples where the card reported a thermal/power throttle.

    The caller stitches consecutive samples into spans — it knows the poll
    interval and therefore what "consecutive" means; SQL here would have to
    guess. Bounded by the raw retention window like everything else.
    """
    c = conn or connection()
    return c.execute(
        "SELECT ts, idx, throttle FROM gpu_samples "
        "WHERE host=? AND ts>=? AND COALESCE(throttle,0) & ? ORDER BY ts",
        (host, since, _THERMAL_BITS)).fetchall()


def latest(host: str, conn=None) -> list:
    """The most recent stored sample per card — used when a host has history but
    is currently offline, so the cockpit can show its last known state rather
    than an empty tab."""
    c = conn or connection()
    return c.execute(
        "SELECT g.* FROM gpu_samples g JOIN "
        "  (SELECT idx, MAX(ts) mts FROM gpu_samples WHERE host=? GROUP BY idx) m "
        "  ON g.idx=m.idx AND g.ts=m.mts WHERE g.host=? ORDER BY g.idx",
        (host, host)).fetchall()


def last_seen(host: str, conn=None) -> dict:
    """{card idx: last sample ts} for a host.

    Distinguishes "this card stopped reporting a minute ago" (an incident) from
    "this card was removed from the machine last month" (history). Without it,
    every retired GPU would raise a permanent critical alert.
    """
    c = conn or connection()
    return {r[0]: r[1] for r in c.execute(
        "SELECT idx, MAX(ts) FROM gpu_samples WHERE host=? GROUP BY idx", (host,)).fetchall()}


def min_ts(host: str, conn=None):
    """Earliest raw sample for a host, or None when it has no history yet."""
    c = conn or connection()
    return c.execute("SELECT MIN(ts) FROM gpu_samples WHERE host=?", (host,)).fetchone()[0]


def vram_by_service(host: str, since: int, bucket: int, conn=None) -> list:
    """Bucketed per-service VRAM since `since`: (bucket_ts, service, avg_mem).

    Reads `proc`, which the hub has always written for itself and remotes now
    write alongside it under their own host name.
    """
    c = conn or connection()
    return c.execute(
        "SELECT (ts/?)*? b, service, AVG(mem) FROM proc "
        "WHERE host=? AND ts>=? GROUP BY b, service ORDER BY b",
        (bucket, bucket, host, since)).fetchall()


def service_totals(host: str, since: int, conn=None) -> list:
    """(service, peak_mem, avg_mem, samples_present) since `since`."""
    c = conn or connection()
    return c.execute(
        "SELECT service, MAX(mem), AVG(mem), COUNT(DISTINCT ts) FROM proc "
        "WHERE host=? AND ts>=? GROUP BY service ORDER BY MAX(mem) DESC",
        (host, since)).fetchall()


def distinct_sample_times(host: str, since: int, conn=None) -> int:
    """How many distinct polls `host` recorded since `since` — the denominator
    for "% of time" columns, so a service present in every sample reads 100%
    regardless of how many cards it spanned."""
    c = conn or connection()
    return c.execute("SELECT COUNT(DISTINCT ts) FROM proc WHERE host=? AND ts>=?",
                     (host, since)).fetchone()[0] or 0


def rename_host(old: str, new: str, conn=None):
    """Follow a host rename so its GPU history doesn't split in two."""
    c = conn or connection()
    for tbl in ("gpu_samples", "gpu_samples_1h", "proc"):
        c.execute(f"UPDATE {tbl} SET host=? WHERE host=?", (new, old))
