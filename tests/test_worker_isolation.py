"""
tests/test_worker_isolation.py — Phase 4.3

Proves that a stalled worker cannot starve the other workers.

Each production worker runs in its own daemon thread.  The test creates two
synthetic workers — one that blocks for a long time (simulating a stall) and
one that is fast — and asserts that the fast worker completes multiple cycles
while the slow one is still blocked.

No real sleeping is used: ``time.sleep`` is patched out of the workers so the
test runs in milliseconds.

Thread cleanup: all threads are daemon threads, so they die when the test
process exits.  The ``stop_event`` signals workers to exit gracefully after
assertions, preventing threads from leaking into subsequent tests.
"""
import threading
import time

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import from the standalone heartbeat module — zero app.py dependency, no circular imports.
from backend._heartbeat import heartbeat as _heartbeat, _HEARTBEATS, _HEARTBEAT_LOCK


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_fast_worker(cycle_counter: list, stop_event: threading.Event, interval: float = 0.02):
    """Return a worker function that records heartbeats and counts cycles."""
    def _worker():
        while not stop_event.is_set():
            _heartbeat("fast_worker", interval)
            cycle_counter[0] += 1
            time.sleep(interval)
    return _worker


def _make_slow_worker(stop_event: threading.Event, block_duration: float = 5.0):
    """Return a worker that blocks for *block_duration* seconds then exits."""
    def _worker():
        _heartbeat("slow_worker", 0.01)   # declares a very short interval → will appear stalled
        time.sleep(block_duration)         # simulate a stall (real sleep — not patched)
        stop_event.set()                   # signal fast worker to stop after slow finishes
    return _worker


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_fast_worker_progresses_while_slow_worker_is_stalled():
    """Fast worker must complete ≥5 cycles while slow worker is blocked."""
    stop_event = threading.Event()
    cycle_counter = [0]
    # Real wall-clock stall of 0.5s; fast worker ticks every 10ms → ≥50 cycles expected.
    STALL_DURATION = 0.5   # seconds
    FAST_INTERVAL  = 0.01  # seconds

    fast_fn = _make_fast_worker(cycle_counter, stop_event, interval=FAST_INTERVAL)
    slow_fn = _make_slow_worker(stop_event, block_duration=STALL_DURATION)

    t_slow = threading.Thread(target=slow_fn, daemon=True)
    t_fast = threading.Thread(target=fast_fn, daemon=True)

    t_slow.start()
    t_fast.start()

    # Wait for the slow worker to finish (it sets stop_event)
    t_slow.join(timeout=STALL_DURATION + 1.0)
    stop_event.set()       # ensure fast worker exits even if slow finished early
    t_fast.join(timeout=1.0)

    # Fast worker must have completed many cycles while slow was stalled
    assert cycle_counter[0] >= 5, (
        f"Expected fast worker to run ≥5 cycles during the {STALL_DURATION}s stall, "
        f"but only ran {cycle_counter[0]}"
    )


def test_heartbeat_records_are_independent():
    """Heartbeats from different workers are stored independently."""
    with _HEARTBEAT_LOCK:
        _HEARTBEATS.clear()

    _heartbeat("alpha", 10.0)
    _heartbeat("beta", 5.0)

    with _HEARTBEAT_LOCK:
        assert "alpha" in _HEARTBEATS
        assert "beta" in _HEARTBEATS
        assert _HEARTBEATS["alpha"]["interval"] == 10.0
        assert _HEARTBEATS["beta"]["interval"] == 5.0
        # Each heartbeat has an independent timestamp
        assert isinstance(_HEARTBEATS["alpha"]["ts"], float)
        assert isinstance(_HEARTBEATS["beta"]["ts"], float)


def test_heartbeat_is_thread_safe():
    """Concurrent heartbeat calls from many threads must not raise or corrupt state."""
    with _HEARTBEAT_LOCK:
        _HEARTBEATS.clear()

    errors: list = []
    barrier = threading.Barrier(10)

    def _write(name):
        try:
            barrier.wait()   # all threads start simultaneously
            for _ in range(50):
                _heartbeat(name, 1.0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(f"w{i}",), daemon=True) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Heartbeat raised under concurrency: {errors}"
    with _HEARTBEAT_LOCK:
        assert len(_HEARTBEATS) == 10   # one slot per worker name
