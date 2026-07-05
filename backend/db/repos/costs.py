"""Read helpers for cost/rollup tables."""
from backend.db import connection


def power_since(ts: int, table: str = "samples_1h", conn=None) -> list:
    """Return (ts, power) rows from a rollup table since ts."""
    c = conn or connection()
    return c.execute(
        f"SELECT ts, power FROM {table} WHERE ts>=? AND power IS NOT NULL ORDER BY ts",
        (ts,)
    ).fetchall()


def heatmap_since(ts: int, total_w_expr: str, conn=None) -> list:
    """Return (ts, total_watts) rows from samples_1h for heatmap computation."""
    c = conn or connection()
    return c.execute(
        f"SELECT ts, {total_w_expr} FROM samples_1h WHERE ts>=? ORDER BY ts", (ts,)
    ).fetchall()


def bucketed_power(ts: int, bucket_sec: int, table: str = "samples", conn=None) -> list:
    """Return (bucket, sum_power) rows grouped into bucket_sec intervals since ts."""
    c = conn or connection()
    return c.execute(
        f"SELECT (ts/?)*? b, SUM(power) FROM {table} WHERE ts>=? AND power IS NOT NULL GROUP BY b ORDER BY b",
        (bucket_sec, bucket_sec, ts)
    ).fetchall()
