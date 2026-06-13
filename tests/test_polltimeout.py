"""Unit tests for the adaptive per-host poll timeout (issue #99)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _add_host(name):
    with app.LOCK:
        app.DB.execute("DELETE FROM hosts WHERE name=?", (name,))
        app.DB.execute("INSERT INTO hosts(name, ssh_target, added_at) VALUES(?,?,0)", (name, "u@h"))
        app.DB.commit()


class TestLearnedTimeout(unittest.TestCase):
    def test_headroom_floor_and_ceiling(self):
        self.assertEqual(app._learned_timeout(20800), 34)        # 20.8s*1.5+3
        self.assertEqual(app._learned_timeout(500), app.HOST_POLL_TIMEOUT)   # floored at default
        self.assertEqual(app._learned_timeout(10**6), app.POLL_TIMEOUT_MAX)  # capped at ceiling


class TestPollAndAdapt(unittest.TestCase):
    def test_fast_host_stays_on_default(self):
        _add_host("fast")
        with patch("app.probe_host_metrics", return_value=({"ok": 1}, None, 400, False)):
            data, err = app._poll_and_adapt("fast", "u", "h", 22, "linux")
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(app._host_poll_state("fast"), (app.HOST_POLL_TIMEOUT, 0))

    def test_calibrates_after_tripwire(self):
        _add_host("slow")

        def fake(u, h, p, fam, timeout=15):
            if timeout >= 100:                      # the ceiling calibration probe
                return ({"ok": 1}, None, 20000, False)
            return (None, f"ssh timed out after {timeout}s", timeout * 1000, True)

        with patch("app.probe_host_metrics", side_effect=fake):
            d1, e1 = app._poll_and_adapt("slow", "u", "h", 22, "linux")   # 1st timeout
            self.assertIsNone(d1)
            self.assertEqual(app._host_poll_state("slow"), (app.HOST_POLL_TIMEOUT, 1))
            d2, e2 = app._poll_and_adapt("slow", "u", "h", 22, "linux")   # trips -> calibrates
        self.assertEqual(d2, {"ok": 1})
        t, fails = app._host_poll_state("slow")
        self.assertEqual(t, app._learned_timeout(20000))   # 33s
        self.assertEqual(fails, 0)

    def test_real_error_does_not_touch_tuning(self):
        _add_host("down")
        with patch("app.probe_host_metrics",
                   return_value=(None, "Permission denied (publickey)", 120, False)):
            data, err = app._poll_and_adapt("down", "u", "h", 22, "linux")
        self.assertIsNone(data)
        self.assertIn("Permission denied", err)
        self.assertEqual(app._host_poll_state("down"), (app.HOST_POLL_TIMEOUT, 0))  # untouched

    def test_recalibrates_when_learned_budget_also_times_out(self):
        _add_host("slower")
        with app.LOCK:   # pretend it was already calibrated to 33s
            app.DB.execute("UPDATE hosts SET poll_timeout=33, poll_fails=1 WHERE name=?", ("slower",))
            app.DB.commit()

        def fake(u, h, p, fam, timeout=15):
            if timeout >= 100:
                return ({"ok": 1}, None, 40000, False)   # now needs ~40s
            return (None, f"ssh timed out after {timeout}s", timeout * 1000, True)

        with patch("app.probe_host_metrics", side_effect=fake):
            data, err = app._poll_and_adapt("slower", "u", "h", 22, "linux")  # fails 1->2 -> recal
        self.assertEqual(data, {"ok": 1})
        t, _ = app._host_poll_state("slower")
        self.assertEqual(t, app._learned_timeout(40000))   # 63s, bigger than before


if __name__ == "__main__":
    unittest.main()
