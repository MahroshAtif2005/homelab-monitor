"""backend/db/repos/edge_state.py — persist alert edge-state across restarts."""
from backend.db import connection


def load_notified_keys(conn=None) -> list:
    c = conn or connection()
    return c.execute("SELECT key FROM notified_keys").fetchall()


def arm_key(key: str, armed_at: int, conn=None) -> None:
    c = conn or connection()
    c.execute(
        "INSERT OR REPLACE INTO notified_keys(key, armed_at) VALUES(?,?)",
        (key, armed_at)
    )
    c.commit()


def disarm_key(key: str, conn=None) -> None:
    c = conn or connection()
    c.execute("DELETE FROM notified_keys WHERE key=?", (key,))
    c.commit()


def load_down_since(conn=None) -> list:
    c = conn or connection()
    return c.execute("SELECT check_id, since_ts FROM uptime_down_since").fetchall()


def set_down_since(check_id: str, since_ts: int, conn=None) -> None:
    c = conn or connection()
    c.execute(
        "INSERT OR REPLACE INTO uptime_down_since(check_id, since_ts) VALUES(?,?)",
        (check_id, since_ts)
    )
    c.commit()


def clear_down_since(check_id: str, conn=None) -> None:
    c = conn or connection()
    c.execute("DELETE FROM uptime_down_since WHERE check_id=?", (check_id,))
    c.commit()
