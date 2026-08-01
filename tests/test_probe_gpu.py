"""Unit tests for the remote probe's NVIDIA readers — the per-card list and the
compute-apps process list that feed the per-host GPU tab. nvidia-smi is mocked,
so these verify parsing (including '[N/A]' fields and comma-bearing process
paths) without a GPU present."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe


def _smi_result(stdout, rc=0):
    r = mock.Mock()
    r.returncode = rc
    r.stdout = stdout.encode()
    return r


def _fake_smi(**by_query):
    """A nvidia-smi stand-in that answers per QUERY, the way the real one does.

    Keys are matched as substrings of the --query-* argument, so a test declares
    only the queries it cares about; anything it doesn't declare comes back
    rc=2/empty — exactly how nvidia-smi rejects a field name it doesn't know.
    Without this, one fixed stdout is handed to every query and the enrichment
    pass happily parses the card CSV as clock/throttle data.

    Matching is LONGEST-KEY-FIRST, which matters for the card query: the
    fan-inclusive form ends in `temperature.gpu,fan.speed` and the older-driver
    fallback ends in `temperature.gpu`, so a plain iteration order can answer the
    fan query with the fallback's data and leave the fallback path untested.
    Use the `CARDS_FAN` / `CARDS_BASE` constants below rather than hand-written
    fragments.
    """
    keys = sorted(by_query, key=len, reverse=True)

    def run(args, **kw):
        q = next((a for a in args if a.startswith("--query")), "")
        for frag in keys:
            if frag in q:
                return _smi_result(by_query[frag])
        return _smi_result("", rc=2)
    return run


# The two card-query forms, spelled out so a test says which one it is answering.
CARDS_FAN = "temperature.gpu,fan.speed"      # modern driver
CARDS_BASE = "power.draw,temperature.gpu"    # matches both; use only as fallback


class TestNvidiaCards(unittest.TestCase):
    def test_parses_every_card(self):
        # No fan column: an older driver REJECTS fan.speed (rc != 0, no rows), so
        # only the base query answers. This exercises the real fallback path —
        # the fan query genuinely returns nothing here.
        out = ("0, NVIDIA GeForce RTX 3090, 12, 20990, 24576, 118.53, 45\n"
               "1, NVIDIA GeForce RTX 3090, 0, 3, 24576, 21.06, 38\n"
               "2, NVIDIA GeForce RTX 3090, 97, 24001, 24576, 279.80, 71\n")
        with mock.patch("probe.subprocess.run", _fake_smi(**{CARDS_FAN: "", CARDS_BASE: out})):
            cards = probe._nvidia_cards()
        self.assertEqual(len(cards), 3)
        self.assertEqual(cards[0], {"idx": 0, "name": "NVIDIA GeForce RTX 3090",
                                    "util": 12, "mem_used": 20990, "mem_total": 24576,
                                    "power": 118, "temp": 45, "vendor": "nvidia"})
        self.assertEqual(cards[2]["idx"], 2)
        self.assertEqual(cards[2]["power"], 279)
        self.assertNotIn("fan", cards[0])   # absent, never a fabricated 0

    def test_fan_speed_is_read_when_reported(self):
        out = ("0, NVIDIA GeForce RTX 3090, 12, 20990, 24576, 118.53, 45, 62\n"
               "1, NVIDIA GeForce RTX 3090, 97, 24001, 24576, 279.80, 86, 100\n")
        with mock.patch("probe.subprocess.run", _fake_smi(**{CARDS_FAN: out})):
            cards = probe._nvidia_cards()
        self.assertEqual(cards[0]["fan"], 62)
        self.assertEqual(cards[1]["fan"], 100)

    def test_fan_absent_on_passively_cooled_card(self):
        # A Tesla/A100 has no fan and answers [N/A]. Absent, not 0 — a 0 here
        # would read as a stalled fan and trip the fan-stall alert on hardware
        # that has no fan to stall.
        out = "0, Tesla V100-SXM2-16GB, 40, 8000, 16384, 210.00, 58, [N/A]\n"
        with mock.patch("probe.subprocess.run", _fake_smi(**{CARDS_FAN: out})):
            cards = probe._nvidia_cards()
        self.assertNotIn("fan", cards[0])
        self.assertEqual(cards[0]["temp"], 58)

    def test_na_fields_degrade_to_zero(self):
        out = "0, Quadro P2000, [N/A], 512, 5120, [Not Supported], [N/A]\n"
        with mock.patch("probe.subprocess.run", _fake_smi(**{CARDS_FAN: "", CARDS_BASE: out})):
            cards = probe._nvidia_cards()
        self.assertEqual(cards[0]["util"], 0)
        self.assertEqual(cards[0]["power"], 0)
        self.assertEqual(cards[0]["temp"], 0)
        self.assertEqual(cards[0]["mem_used"], 512)

    def test_no_driver_returns_empty(self):
        with mock.patch("probe.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(probe._nvidia_cards(), [])
        with mock.patch("probe.subprocess.run", return_value=_smi_result("", rc=9)):
            self.assertEqual(probe._nvidia_cards(), [])


class TestNvidiaProcs(unittest.TestCase):
    def test_parses_and_sorts_heaviest_first(self):
        out = ("1234, /usr/local/bin/ollama, 20480\n"
               "77, /opt/conda/bin/python3.12, 22100\n")
        with mock.patch("probe.subprocess.run", _fake_smi(**{"compute-apps=pid": out})):
            procs = probe._nvidia_procs()
        self.assertEqual([p["pid"] for p in procs], [77, 1234])
        self.assertEqual(procs[1], {"pid": 1234, "name": "ollama", "mem": 20480})

    def test_process_spanning_cards_is_pooled_once(self):
        # nvidia-smi emits one row per (process, GPU): a llama-server sharded
        # across 3 cards must show once with its VRAM summed, not three times.
        out = ("895276, /usr/bin/llama-server, 22672\n"
               "895276, /usr/bin/llama-server, 21640\n"
               "895276, /usr/bin/llama-server, 21568\n"
               "7421, /usr/bin/python, 588\n")
        with mock.patch("probe.subprocess.run", _fake_smi(**{"compute-apps=pid": out})):
            procs = probe._nvidia_procs()
        self.assertEqual(len(procs), 2)
        self.assertEqual(procs[0], {"pid": 895276, "name": "llama-server",
                                    "mem": 22672 + 21640 + 21568})

    def test_comma_in_process_path_survives(self):
        # process_name is a free path — a comma inside it must not shift fields.
        out = "50, /srv/my,dir/llama-server, 4096\n"
        with mock.patch("probe.subprocess.run", _fake_smi(**{"compute-apps=pid": out})):
            procs = probe._nvidia_procs()
        self.assertEqual(procs[0]["name"], "llama-server")
        self.assertEqual(procs[0]["mem"], 4096)

    def test_windows_backslash_path(self):
        out = "88, C:\\Program Files\\Ollama\\ollama.exe, 8192\n"
        with mock.patch("probe.subprocess.run", _fake_smi(**{"compute-apps=pid": out})):
            procs = probe._nvidia_procs()
        self.assertEqual(procs[0]["name"], "ollama.exe")

    def test_unavailable_returns_empty(self):
        with mock.patch("probe.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(probe._nvidia_procs(), [])

    def test_per_card_split_from_gpu_uuid(self):
        # The same llama-server sharded across 3 cards: `mem` stays the pooled
        # total (unchanged contract) and `by_card` says where it actually sits.
        uuids = ("0, GPU-aaaaaaaa-0000-0000-0000-000000000000\n"
                 "1, GPU-bbbbbbbb-0000-0000-0000-000000000000\n"
                 "2, GPU-cccccccc-0000-0000-0000-000000000000\n")
        apps = ("GPU-aaaaaaaa-0000-0000-0000-000000000000, 895276, /usr/bin/llama-server, 22672\n"
                "GPU-bbbbbbbb-0000-0000-0000-000000000000, 895276, /usr/bin/llama-server, 21640\n"
                "GPU-cccccccc-0000-0000-0000-000000000000, 895276, /usr/bin/llama-server, 21568\n"
                "GPU-aaaaaaaa-0000-0000-0000-000000000000, 7421, /usr/bin/python, 588\n")
        with mock.patch("probe.subprocess.run",
                        _fake_smi(**{"index,uuid": uuids, "compute-apps=gpu_uuid": apps})):
            procs = probe._nvidia_procs()
        top = procs[0]
        self.assertEqual(top["mem"], 22672 + 21640 + 21568)
        self.assertEqual(top["by_card"], {"0": 22672, "1": 21640, "2": 21568})
        self.assertEqual(procs[1]["by_card"], {"0": 588})

    def test_old_driver_without_gpu_uuid_still_parses(self):
        # nvidia-smi rejects an unknown field for the whole query; the fallback
        # must produce the pre-existing shape, with no by_card key invented.
        apps = "1234, /usr/local/bin/ollama, 20480\n"
        with mock.patch("probe.subprocess.run", _fake_smi(**{"compute-apps=pid": apps})):
            procs = probe._nvidia_procs()
        self.assertEqual(procs[0], {"pid": 1234, "name": "ollama", "mem": 20480})


class TestThrottleDecode(unittest.TestCase):
    def test_thermal_and_power_bits_decode(self):
        mask, reasons = probe._decode_throttle("0x0000000000000044")
        self.assertEqual(mask, 0x44)
        self.assertIn("HW thermal", reasons)
        self.assertIn("Power cap", reasons)

    def test_idle_bits_are_not_reported_as_throttling(self):
        # Bit 0x1 is "GPU idle" — normal, and must never read as throttling.
        _, reasons = probe._decode_throttle("0x0000000000000001")
        self.assertEqual(reasons, [])

    def test_unreadable_mask_is_inert(self):
        self.assertEqual(probe._decode_throttle("[N/A]"), (0, []))
        self.assertEqual(probe._decode_throttle(None), (0, []))

    def test_thermal_bits_match_the_hub(self):
        # probe.py is a standalone script piped to remotes, so it can't import
        # the hub's table — it carries its own copy. That copy must not drift:
        # if it did, `local` and a remote would disagree about what "throttling"
        # means and the same card would alert differently depending on which
        # side read it. Compared by source text so this runs anywhere, including
        # environments where importing app isn't possible.
        import re as _re
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app.py"), encoding="utf-8").read()
        block = _re.search(r"_THROTTLE_BITS = \[(.*?)\]", src, _re.S).group(1)
        hub_bits = _re.findall(r"\(([^,]+),\s*\"([^\"]+)\"\)", block)
        self.assertEqual([(int(b, 16), lbl) for b, lbl in hub_bits], probe._THROTTLE_BITS)
        hub_thermal = _re.search(r"_THERMAL_BITS = (0x[0-9A-Fa-f]+)", src).group(1)
        self.assertEqual(int(hub_thermal, 16), probe._THERMAL_BITS)


class TestNvidiaEnrich(unittest.TestCase):
    def test_deep_telemetry_attaches_by_index(self):
        cards = [{"idx": 0, "name": "RTX 3090"}, {"idx": 1, "name": "RTX 3090"}]
        deep = ("0, 71, 1695, 9751, 280.00, 84, P2\n"
                "1, 12, 1395, 9751, 280.00, 78, P0\n")
        thr = ("0, 0x0000000000000040\n"
               "1, 0x0000000000000000\n")
        with mock.patch("probe.subprocess.run",
                        _fake_smi(**{"utilization.memory": deep,
                                     "clocks_throttle_reasons.active": thr})):
            probe._nvidia_enrich(cards)
        self.assertEqual(cards[0]["mem_util"], 71)
        self.assertEqual(cards[0]["clk_sm"], 1695)
        self.assertEqual(cards[0]["power_limit"], 280)
        self.assertEqual(cards[0]["pstate"], "P2")
        self.assertTrue(cards[0]["throttled"])
        self.assertEqual(cards[0]["throttle"], ["HW thermal"])
        self.assertFalse(cards[1]["throttled"])

    def test_newer_driver_event_reasons_field_is_used(self):
        # The field was renamed clocks_event_reasons in newer drivers; the old
        # name errors out and the fallback must still find the throttle state.
        cards = [{"idx": 0, "name": "RTX 4090"}]
        with mock.patch("probe.subprocess.run",
                        _fake_smi(**{"clocks_event_reasons.active": "0, 0x0000000000000004\n"})):
            probe._nvidia_enrich(cards)
        self.assertEqual(cards[0]["throttle"], ["Power cap"])

    def test_unsupported_fields_stay_absent(self):
        cards = [{"idx": 0, "name": "GTX 1060"}]
        deep = "0, [N/A], 1500, [N/A], [Not Supported], [N/A], P0\n"
        with mock.patch("probe.subprocess.run", _fake_smi(**{"utilization.memory": deep})):
            probe._nvidia_enrich(cards)
        self.assertNotIn("mem_util", cards[0])
        self.assertNotIn("power_limit", cards[0])
        self.assertNotIn("temp_mem", cards[0])
        self.assertEqual(cards[0]["clk_sm"], 1500)


class TestReadGpuMerge(unittest.TestCase):
    def test_nvidia_aggregate_pools_cards(self):
        cards = [
            {"idx": 0, "name": "NVIDIA GeForce RTX 3090", "util": 12, "mem_used": 20990,
             "mem_total": 24576, "power": 118, "temp": 45, "vendor": "nvidia"},
            {"idx": 1, "name": "NVIDIA GeForce RTX 3090", "util": 0, "mem_used": 3,
             "mem_total": 24576, "power": 21, "temp": 38, "vendor": "nvidia"},
            {"idx": 2, "name": "NVIDIA GeForce RTX 3090", "util": 96, "mem_used": 24001,
             "mem_total": 24576, "power": 280, "temp": 71, "vendor": "nvidia"},
        ]
        with mock.patch("probe._nvidia_cards", return_value=cards), \
             mock.patch("probe._amd_gpu_sysfs", return_value=[]), \
             mock.patch("probe._nvidia_procs", return_value=[{"pid": 1, "name": "ollama", "mem": 20480}]):
            out = probe.read_gpu()
        g = out["gpu"]
        self.assertEqual(g["count"], 3)
        self.assertEqual(g["mem_total"], 3 * 24576)
        self.assertEqual(g["mem_used"], 20990 + 3 + 24001)
        self.assertEqual(g["util"], 36)          # (12+0+96)/3
        self.assertEqual(g["temp"], 71)          # hottest card
        self.assertEqual(g["power"], 118 + 21 + 280)
        self.assertEqual(g["vendor"], "nvidia")
        self.assertEqual(len(out["gpus"]), 3)
        self.assertEqual(out["gpu_procs"][0]["name"], "ollama")

    def test_hybrid_reindexes_amd_above_nvidia(self):
        nv = [{"idx": 0, "name": "RTX", "util": 0, "mem_used": 0, "mem_total": 24576,
               "power": 0, "temp": 0, "vendor": "nvidia"}]
        amd = [{"name": "AMD", "util": 0, "mem_used": 0, "mem_total": 16384,
                "power": 0, "temp": 0, "vendor": "amd"}]
        with mock.patch("probe._nvidia_cards", return_value=nv), \
             mock.patch("probe._amd_gpu_sysfs", return_value=amd), \
             mock.patch("probe._nvidia_procs", return_value=[]):
            out = probe.read_gpu()
        self.assertEqual([c["idx"] for c in out["gpus"]], [0, 1])
        self.assertEqual(out["gpu"]["vendor"], "hybrid")
        self.assertNotIn("gpu_procs", out)

    def test_no_gpu_returns_empty(self):
        with mock.patch("probe._nvidia_cards", return_value=[]), \
             mock.patch("probe._amd_gpu_sysfs", return_value=[]):
            self.assertEqual(probe.read_gpu(), {})


if __name__ == "__main__":
    unittest.main()
