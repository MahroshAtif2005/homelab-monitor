# Snapshot helper: write/compare JSON endpoint responses.
# Run with UPDATE_SNAPSHOTS=1 pytest to regenerate all snapshots.
import json
import os
import pathlib
import unittest
import datetime as _dt
from unittest.mock import patch

SNAP_DIR = pathlib.Path(__file__).parent / "snapshots"
SNAP_DIR.mkdir(exist_ok=True)

FROZEN_TS = 1735689600  # 2025-01-01 00:00:00 UTC
FROZEN_DT = _dt.datetime(2025, 1, 1, 0, 0, 0)


def assert_snapshot(test_case, name: str, data: dict):
    """Compare `data` against tests/snapshots/<name>.json. Create if missing."""
    path = SNAP_DIR / f"{name}.json"
    serialized = json.dumps(data, indent=2, sort_keys=True, default=str)
    if os.environ.get("UPDATE_SNAPSHOTS") or not path.exists():
        path.write_text(serialized)
        return
    expected = json.loads(path.read_text())
    test_case.assertEqual(
        serialized,
        json.dumps(expected, indent=2, sort_keys=True, default=str),
        f"Snapshot mismatch for {name}. Run UPDATE_SNAPSHOTS=1 pytest to rebaseline.",
    )


class _MultiPatch:
    """Context manager that applies multiple patches simultaneously."""
    def __init__(self, *patches):
        self._patches = patches
        self._mocks = []

    def __enter__(self):
        self._mocks = [p.start() for p in self._patches]
        return self._mocks[0] if len(self._mocks) == 1 else self._mocks

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.stop()


def frozen_time():
    """Context manager: freeze time.time() in app module and all blueprint modules."""
    targets = [
        "app.time.time",
        "backend.api.costs.time.time",
    ]
    return _MultiPatch(*[patch(t, return_value=FROZEN_TS) for t in targets])
