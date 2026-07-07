"""
backend/collectors/_heartbeat — Phase 4.3

Lightweight, zero-dependency heartbeat registry.

Deliberately isolated from app.py and the DB layer so tests can import it
without triggering the circular-import chain that running the full application
causes (backend.collectors → backend.db.repos → backend.db → app → ...).

Public API
----------
heartbeat(name, interval)   — record a cycle start
get_heartbeats()            — snapshot the current state (for tests / watchdog)
"""
import threading
import time

_HEARTBEAT_LOCK = threading.Lock()
# Maps worker_name → {"ts": float (monotonic), "interval": float (seconds)}
_HEARTBEATS: dict = {}


def heartbeat(name: str, interval: float) -> None:
    """Record that *name* just started a new cycle with the given sleep interval.

    Thread-safe.  Called from worker threads at the top of each loop iteration.
    The lock is never held while calling into app code, so there is no ordering
    hazard with app.py's LOCK or _NOTIFIER_LOCK.
    """
    with _HEARTBEAT_LOCK:
        _HEARTBEATS[name] = {"ts": time.monotonic(), "interval": interval}


def get_heartbeats() -> dict:
    """Return a snapshot of the current heartbeat state (for tests / watchdog)."""
    with _HEARTBEAT_LOCK:
        return dict(_HEARTBEATS)
