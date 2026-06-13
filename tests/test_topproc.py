"""Unit tests for the Top-processes /proc reader (issue #32)."""
import os
import re
import sys
import unittest
from unittest.mock import patch, mock_open

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _stat_blob(pid, comm, utime, stime=0):
    # /proc/<pid>/stat: pid (comm) state ppid ... then at field 14/15 utime/stime.
    # After the comm, fields start at 'state'; index 11 = utime, 12 = stime.
    return f"{pid} ({comm}) S 1 1 1 0 -1 0 0 0 0 0 {utime} {stime} 0 0 20 0 1 0 100\n"


class TestTopProcesses(unittest.TestCase):
    def setUp(self):
        app._PROC_PREV = {"total": None, "pids": {}}

    def _run(self, total, procs):
        """procs: {pid: (comm, jiffies, rss_pages)} -> collect_top_processes()."""
        def fake_open(path, *a, **k):
            if path == "/proc/stat":
                return mock_open(read_data=f"cpu {total} 0 0 0 0 0 0 0\n")()
            m = re.match(r"/proc/(\d+)/stat$", path)
            if m:
                comm, jiff, _ = procs[m.group(1)]
                return mock_open(read_data=_stat_blob(m.group(1), comm, jiff))()
            m = re.match(r"/proc/(\d+)/statm$", path)
            if m:
                pages = procs[m.group(1)][2]
                return mock_open(read_data=f"9999 {pages} 0 0 0 0 0\n")()
            raise FileNotFoundError(path)
        with patch("app.os.listdir", return_value=list(procs.keys())), \
             patch("app.os.cpu_count", return_value=4), \
             patch("builtins.open", side_effect=fake_open):
            return app.collect_top_processes()

    def test_aggregates_workers_by_command(self):
        procs = {"10": ("python", 100, 256), "11": ("python", 100, 256),
                 "20": ("ollama", 50, 1024)}
        r = self._run(1000, procs)
        self.assertIsNotNone(r)
        py = next(x for x in r["by_mem"] if x["name"] == "python")
        self.assertEqual(py["count"], 2)                       # 2 workers collapsed
        self.assertEqual(py["mem_mb"], round(2 * 256 * 4 / 1024))   # 2 MB
        # ollama (1024 pages = 4 MB) tops the RAM column over python (2 MB)
        self.assertEqual(r["by_mem"][0]["name"], "ollama")

    def test_first_scan_has_zero_cpu(self):
        r = self._run(1000, {"10": ("x", 500, 100)})
        self.assertEqual(r["by_cpu"][0]["cpu_pct"], 0.0)       # no prev delta yet

    def test_cpu_percent_from_jiffy_delta(self):
        self._run(1000, {"10": ("stress", 0, 100)})           # seed prev
        r = self._run(1400, {"10": ("stress", 100, 100)})     # +100 of +400 total
        st = next(x for x in r["by_cpu"] if x["name"] == "stress")
        self.assertAlmostEqual(st["cpu_pct"], 100.0, delta=0.1)  # *ncpu(4) per-core

    def test_returns_none_without_proc(self):
        with patch("app.os.listdir", side_effect=FileNotFoundError):
            self.assertIsNone(app.collect_top_processes())


if __name__ == "__main__":
    unittest.main()
