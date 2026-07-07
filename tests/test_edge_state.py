import sqlite3
import pytest
from backend.db.repos import edge_state as repo

SCHEMA = """
CREATE TABLE notified_keys (key TEXT PRIMARY KEY, armed_at INTEGER NOT NULL);
CREATE TABLE uptime_down_since (check_id TEXT PRIMARY KEY, since_ts INTEGER NOT NULL);
"""


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn


def test_arm_and_load(db):
    repo.arm_key("host.cpu", 1000, conn=db)
    rows = repo.load_notified_keys(conn=db)
    assert ("host.cpu",) in rows


def test_disarm(db):
    repo.arm_key("host.cpu", 1000, conn=db)
    repo.disarm_key("host.cpu", conn=db)
    assert repo.load_notified_keys(conn=db) == []


def test_arm_replace(db):
    repo.arm_key("k", 1, conn=db)
    repo.arm_key("k", 2, conn=db)
    rows = repo.load_notified_keys(conn=db)
    assert len(rows) == 1


def test_set_and_load_down_since(db):
    repo.set_down_since("check1", 500, conn=db)
    rows = repo.load_down_since(conn=db)
    assert ("check1", 500) in rows


def test_clear_down_since(db):
    repo.set_down_since("check1", 500, conn=db)
    repo.clear_down_since("check1", conn=db)
    assert repo.load_down_since(conn=db) == []


def test_restart_restore_notified(db):
    repo.arm_key("host.mem", 999, conn=db)
    _NOTIFIED = {}
    _NOTIFIED.update({row[0]: 1 for row in repo.load_notified_keys(conn=db)})
    assert _NOTIFIED.get("host.mem") == 1


def test_restart_restore_down_since(db):
    repo.set_down_since("chk", 1234, conn=db)
    _uptime_down_since = {}
    _uptime_down_since.update({row[0]: row[1] for row in repo.load_down_since(conn=db)})
    assert _uptime_down_since.get("chk") == 1234
