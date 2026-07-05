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


def frozen_time():
    """Context manager: freeze time.time() in the app module."""
    return patch("app.time.time", return_value=FROZEN_TS)
