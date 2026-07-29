"""Unit tests for the remote probe's docker inventory (per-host Containers).
docker CLI is mocked — these verify parsing, the stats merge, the problem
count, and that a docker-less host degrades to {} rather than an error."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe


def _res(stdout, rc=0):
    r = mock.Mock()
    r.returncode = rc
    r.stdout = stdout.encode()
    return r


def _ps_line(name, image, state, status, ports=""):
    return json.dumps({"Names": name, "Image": image, "State": state,
                       "Status": status, "Ports": ports})


class TestStatsMemBytes(unittest.TestCase):
    def test_units(self):
        self.assertEqual(probe._stats_mem_bytes("1.5GiB / 62GiB"), int(1.5 * 1024**3))
        self.assertEqual(probe._stats_mem_bytes("512MiB / 8GiB"), 512 * 1024**2)
        self.assertEqual(probe._stats_mem_bytes("900kB / 1GB"), 900 * 1000)
        self.assertIsNone(probe._stats_mem_bytes("garbage"))
        self.assertIsNone(probe._stats_mem_bytes(None))


class TestReadDocker(unittest.TestCase):
    def test_inventory_with_stats(self):
        ps = "\n".join([
            _ps_line("ollama", "ollama/ollama:latest", "running", "Up 2 days (healthy)", "0.0.0.0:11434->11434/tcp"),
            _ps_line("dead", "old:1", "exited", "Exited (137) 3 hours ago"),
            _ps_line("flappy", "img:2", "restarting", "Restarting (1) 5 seconds ago"),
            _ps_line("clean-exit", "img:3", "exited", "Exited (0) 2 weeks ago"),
        ])
        stats = json.dumps({"Name": "ollama", "MemUsage": "2GiB / 62GiB", "CPUPerc": "12.5%"})
        def run(args, **kw):
            return _res(ps) if args[1] == "ps" else _res(stats)
        with mock.patch("probe.subprocess.run", side_effect=run):
            out = probe.read_docker()
        dk = out["docker"]
        self.assertTrue(dk["available"])
        self.assertEqual(dk["summary"], {"total": 4, "running": 1, "problems": 2})
        byname = {c["name"]: c for c in dk["containers"]}
        self.assertEqual(byname["ollama"]["mem_bytes"], 2 * 1024**3)
        self.assertEqual(byname["ollama"]["cpu_pct"], 12.5)
        self.assertNotIn("mem_bytes", byname["dead"])

    def test_stats_failure_degrades_to_inventory_only(self):
        ps = _ps_line("web", "nginx", "running", "Up 1 hour")
        def run(args, **kw):
            if args[1] == "ps":
                return _res(ps)
            raise probe.subprocess.TimeoutExpired(args, 8)
        with mock.patch("probe.subprocess.run", side_effect=run):
            out = probe.read_docker()
        c = out["docker"]["containers"][0]
        self.assertEqual(c["name"], "web")
        self.assertNotIn("mem_bytes", c)

    def test_no_docker_returns_empty(self):
        with mock.patch("probe.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(probe.read_docker(), {})
        with mock.patch("probe.subprocess.run", return_value=_res("permission denied", rc=1)):
            self.assertEqual(probe.read_docker(), {})

    def test_no_containers_still_reports_available(self):
        with mock.patch("probe.subprocess.run", return_value=_res("")):
            out = probe.read_docker()
        self.assertTrue(out["docker"]["available"])
        self.assertEqual(out["docker"]["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
