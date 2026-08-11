"""Unit tests for probe.read_cpu_and_procs() — the remote Top-processes block.

The point of the feature is that a remote's numbers mean the SAME thing as the
hub's, so most of what is asserted here is agreement with
app.collect_top_processes(): aggregation by command, CPU as a percentage of one
core, RAM summed across the group.

/proc is faked, so these run identically on any OS and in CI.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe


def _stat(comm, jiffies, starttime="777"):
    """A /proc/<pid>/stat line with utime+stime totalling `jiffies`.

    Fields after the comm: state(3) .. so utime is field 14 -> index 11 of the
    post-')' split, stime index 12, starttime index 19.
    """
    rest = ["S"] + ["0"] * 21
    rest[11] = str(jiffies // 2)
    rest[12] = str(jiffies - jiffies // 2)
    rest[19] = starttime
    return "1 (%s) %s" % (comm, " ".join(rest))


class FakeProc:
    """Two successive /proc states, served to _proc_sample() in order."""

    def __init__(self, first, second, rss_pages=None):
        self.states = [first, second]
        self.calls = 0
        self.rss_pages = rss_pages or {}

    def listdir(self, path):
        assert path == "/proc"
        return list(self.states[min(self.calls, 1)].keys()) + ["cpuinfo", "self"]

    def open(self, path, *a, **kw):
        import io
        st = self.states[min(self.calls, 1)]
        parts = path.split("/")
        pid, leaf = parts[2], parts[3]
        if pid not in st:
            raise OSError("gone")
        if leaf == "stat":
            return io.StringIO(st[pid])
        if leaf == "statm":
            pages = self.rss_pages.get(pid, 0)
            return io.StringIO("0 %d 0 0 0 0 0" % pages)
        raise OSError("nope")


class TestRemoteTopProcs(unittest.TestCase):
    def _run(self, fake, cpu_snaps, ncpu=4):
        """Drive read_cpu_and_procs() over a faked /proc and /proc/stat."""
        def _sample(with_rss=False):
            out = {}
            st = fake.states[min(fake.calls, 1)]
            for pid, line in st.items():
                rp = line.rfind(")")
                comm = line[line.find("(") + 1:rp]
                rest = line[rp + 2:].split()
                rss = fake.rss_pages.get(pid, 0) * probe._PAGE_KB if with_rss else 0
                out[pid] = (comm, int(rest[11]) + int(rest[12]), rest[19], rss)
            fake.calls += 1
            return out

        with patch.object(probe, "_proc_sample", _sample), \
             patch.object(probe, "_cpu_snapshot", side_effect=cpu_snaps), \
             patch.object(probe.time, "sleep", lambda s: None), \
             patch.object(probe.os, "cpu_count", lambda: ncpu):
            return probe.read_cpu_and_procs()

    def test_cpu_is_percent_of_one_core(self):
        """A command burning one whole core reads ~100%, not 100/ncpu."""
        # 400 total jiffies elapsed across 4 cores == 100 jiffies of one core.
        fake = FakeProc({"1": _stat("burner", 0)}, {"1": _stat("burner", 100)})
        out = self._run(fake, [(1000, 500), (1400, 800)], ncpu=4)
        row = out["processes"]["by_cpu"][0]
        self.assertEqual(row["name"], "burner")
        self.assertAlmostEqual(row["cpu_pct"], 100.0, places=1)

    def test_grouped_by_command(self):
        """Several pids of one command collapse to a row with a count."""
        first = {"1": _stat("nginx", 0), "2": _stat("nginx", 0), "3": _stat("redis", 0)}
        second = {"1": _stat("nginx", 20), "2": _stat("nginx", 20), "3": _stat("redis", 10)}
        fake = FakeProc(first, second, rss_pages={"1": 100, "2": 100, "3": 50})
        out = self._run(fake, [(0, 0), (400, 0)], ncpu=4)
        by_cpu = {r["name"]: r for r in out["processes"]["by_cpu"]}
        self.assertEqual(by_cpu["nginx"]["count"], 2)
        self.assertEqual(by_cpu["redis"]["count"], 1)
        # nginx burned twice what redis did, so it must sort above it.
        self.assertEqual(out["processes"]["by_cpu"][0]["name"], "nginx")
        self.assertGreater(by_cpu["nginx"]["cpu_pct"], by_cpu["redis"]["cpu_pct"])

    def test_ram_sums_across_the_group(self):
        by_mem = self._run(
            FakeProc({"1": _stat("a", 0), "2": _stat("a", 0)},
                     {"1": _stat("a", 0), "2": _stat("a", 0)},
                     rss_pages={"1": 1024, "2": 1024}),
            [(0, 0), (400, 0)])["processes"]["by_mem"]
        # 2048 pages summed, converted to MB via the page size.
        self.assertEqual(by_mem[0]["mem_mb"], round(2048 * probe._PAGE_KB / 1024.0))

    def test_process_started_during_the_dwell_is_not_charged_its_lifetime(self):
        """No baseline means no delta — otherwise a fresh pid tops the list."""
        fake = FakeProc({"1": _stat("old", 0)},
                        {"1": _stat("old", 10), "2": _stat("newcomer", 9999)})
        out = self._run(fake, [(0, 0), (400, 0)])
        rows = {r["name"]: r for r in out["processes"]["by_cpu"]}
        self.assertEqual(rows["newcomer"]["cpu_pct"], 0.0)
        self.assertEqual(out["processes"]["by_cpu"][0]["name"], "old")

    def test_reused_pid_is_not_charged_the_difference(self):
        """Same pid, different start time == a different process."""
        fake = FakeProc({"1": _stat("gone", 5000, starttime="100")},
                        {"1": _stat("fresh", 7000, starttime="900")})
        out = self._run(fake, [(0, 0), (400, 0)])
        self.assertEqual(out["processes"]["by_cpu"][0]["cpu_pct"], 0.0)

    def test_aggregate_cpu_still_reported(self):
        """The block this replaced still has to produce cpu/cores."""
        out = self._run(FakeProc({"1": _stat("x", 0)}, {"1": _stat("x", 0)}),
                        [(1000, 900), (2000, 1400)], ncpu=8)
        self.assertEqual(out["cores"], 8)
        self.assertAlmostEqual(out["cpu"], 50.0, places=1)   # 500 of 1000 busy

    def test_io_declared_unavailable_rather_than_faked(self):
        out = self._run(FakeProc({"1": _stat("x", 0)}, {"1": _stat("x", 0)}),
                        [(0, 0), (400, 0)])
        self.assertEqual(out["processes"]["io"], {"available": False})

    def test_no_processes_still_returns_cpu(self):
        """An unreadable /proc must degrade to the old payload, not raise."""
        with patch.object(probe, "_proc_sample", lambda with_rss=False: {}), \
             patch.object(probe, "_cpu_snapshot", side_effect=[(0, 0), (1000, 500)]), \
             patch.object(probe.time, "sleep", lambda s: None):
            out = probe.read_cpu_and_procs()
        self.assertIn("cpu", out)
        self.assertNotIn("processes", out)


class TestIdleTieBreak(unittest.TestCase):
    """On a short window nearly everything is 0.0% CPU, and a tie that falls
    back to /proc order puts kernel threads (lowest pids) at the top. Regression
    coverage for exactly that: an idle box reporting kthreadd and a column of
    kworkers as its top consumers."""

    def test_zero_cpu_tie_is_broken_by_ram_not_pid_order(self):
        # Kernel threads occupy the low pids and hold no RSS; the real service
        # is a high pid holding a lot. All of them idle at 0 jiffies.
        first, second, rss = {}, {}, {}
        for pid, comm in ((2, "kthreadd"), (3, "kworker/R-rcu_gp"), (4, "kworker/R-sync_wq")):
            first[str(pid)] = _stat(comm, 0)
            second[str(pid)] = _stat(comm, 0)
            rss[str(pid)] = 0
        first["9001"] = _stat("llama-server", 0)
        second["9001"] = _stat("llama-server", 0)
        rss["9001"] = 4096

        fake = FakeProc(first, second, rss_pages=rss)
        out = TestRemoteTopProcs()._run(fake, [(0, 0), (400, 0)])
        self.assertEqual(out["processes"]["by_cpu"][0]["name"], "llama-server")

    def test_a_busy_kernel_thread_still_outranks_an_idle_service(self):
        """The tiebreak must not become a filter — a genuinely busy kswapd is a
        real signal and has to keep its place."""
        fake = FakeProc(
            {"2": _stat("kswapd0", 0), "9001": _stat("llama-server", 0)},
            {"2": _stat("kswapd0", 80), "9001": _stat("llama-server", 0)},
            rss_pages={"2": 0, "9001": 4096})
        out = TestRemoteTopProcs()._run(fake, [(0, 0), (400, 0)])
        self.assertEqual(out["processes"]["by_cpu"][0]["name"], "kswapd0")

    def test_by_mem_ties_break_on_cpu(self):
        fake = FakeProc(
            {"2": _stat("idler", 0), "3": _stat("worker", 0)},
            {"2": _stat("idler", 0), "3": _stat("worker", 40)},
            rss_pages={"2": 1000, "3": 1000})
        out = TestRemoteTopProcs()._run(fake, [(0, 0), (400, 0)])
        self.assertEqual(out["processes"]["by_mem"][0]["name"], "worker")


class TestHubParity(unittest.TestCase):
    """The hub and the probe must sort the same way, or the same box reads
    differently depending on which side of the SSH connection you view it from."""

    def test_hub_uses_the_same_tie_break(self):
        import inspect
        import app
        src = inspect.getsource(app.collect_top_processes)
        self.assertIn('(-r["cpu_pct"], -r["mem_mb"])', src)
        self.assertIn('(-r["mem_mb"], -r["cpu_pct"])', src)


class TestProcSampleParsing(unittest.TestCase):
    """_proc_sample() against real /proc/<pid>/stat strings.

    Separate from the tests above, which stub it out: the field offsets and the
    comm-with-parens case are exactly where a /proc parser goes wrong, so they
    get exercised for real rather than mocked past.
    """

    def _sample(self, files, listing=None, with_rss=False):
        import io
        import builtins
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if isinstance(path, str) and path.startswith("/proc/"):
                if path not in files:
                    raise OSError("gone")
                return io.StringIO(files[path])
            return real_open(path, *a, **kw)

        names = listing if listing is not None else sorted(
            {p.split("/")[2] for p in files})
        with patch.object(probe.os, "listdir", lambda p: names), \
             patch.object(builtins, "open", fake_open):
            return probe._proc_sample(with_rss=with_rss)

    def test_comm_containing_spaces_and_parens(self):
        """Firefox's `Web Content` and friends must not shift every field."""
        rest = ["S"] + ["0"] * 21
        rest[11], rest[12], rest[19] = "7", "3", "4242"
        line = "11 (Web Content (tab)) " + " ".join(rest)
        got = self._sample({"/proc/11/stat": line})
        self.assertEqual(got["11"][0], "Web Content (tab)")
        self.assertEqual(got["11"][1], 10)        # utime 7 + stime 3
        self.assertEqual(got["11"][2], "4242")    # start time

    def test_utime_stime_and_starttime_offsets(self):
        got = self._sample({"/proc/10/stat": _stat("bash", 30, starttime="99")})
        self.assertEqual(got["10"][1], 30)
        self.assertEqual(got["10"][2], "99")

    def test_process_exiting_mid_walk_is_skipped_not_fatal(self):
        """/proc is a live directory; a pid can vanish between listdir and read."""
        got = self._sample({"/proc/10/stat": _stat("alive", 5)},
                           listing=["10", "11"])   # 11 has no files
        self.assertEqual(list(got), ["10"])

    def test_non_pid_entries_are_ignored(self):
        got = self._sample({"/proc/10/stat": _stat("alive", 5)},
                           listing=["10", "cpuinfo", "self", "meminfo"])
        self.assertEqual(list(got), ["10"])

    def test_rss_read_only_when_asked(self):
        files = {"/proc/10/stat": _stat("x", 0), "/proc/10/statm": "0 512 0 0 0 0 0"}
        self.assertEqual(self._sample(files)["10"][3], 0)
        self.assertEqual(self._sample(files, with_rss=True)["10"][3],
                         512 * probe._PAGE_KB)


if __name__ == "__main__":
    unittest.main()
