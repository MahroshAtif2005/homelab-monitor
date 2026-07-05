"""Read/write helpers for the samples and samples_1h tables."""
from backend.db import connection


def latest_n(n: int, conn=None) -> list:
    """Return the last n rows from samples (ts DESC)."""
    c = conn or connection()
    return c.execute(
        "SELECT ts,util,mem_used,mem_total,power,temp FROM samples ORDER BY ts DESC LIMIT ?",
        (n,)
    ).fetchall()


def since(ts: int, table: str = "samples", conn=None) -> list:
    """Return all rows from `table` where ts >= ts, ordered ascending."""
    c = conn or connection()
    return c.execute(
        f"SELECT * FROM {table} WHERE ts>=? ORDER BY ts", (ts,)
    ).fetchall()


def insert(ts, util, mem_used, mem_total, power, temp, conn=None):
    """Insert one sample row."""
    c = conn or connection()
    c.execute(
        "INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) VALUES(?,?,?,?,?,?)",
        (ts, util, mem_used, mem_total, power, temp)
    )
    c.commit()
