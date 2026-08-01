"""The GPU cockpit API — /api/gpu/history and /api/gpu/attribution.

The contract these pin down is "one endpoint serves every host identically".
A remote with three cards and the hub with one must come back in the same shape,
because the dashboard renders both through a single code path; the moment the
payloads differ, the old charts-here / snapshot-there fork grows back.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _wipe():
    import app
    for t in ("gpu_samples", "gpu_samples_1h", "proc"):
        app.DB.execute(f"DELETE FROM {t}")
    app.DB.commit()


def _card(idx=0, **kw):
    g = {"idx": idx, "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia",
         "util": 50, "mem_used": 12000, "mem_total": 24576, "power": 200, "temp": 70}
    g.update(kw)
    return g


def _seed(host, cards_at):
    """cards_at: [(ts, [card, ...]), ...] written straight through the repo."""
    import app
    from backend.db.repos import gpu_samples as repo
    for ts, cards in cards_at:
        repo.record(app.DB, ts, host, cards, interval=10)
    app.DB.commit()


class TestHistoryShape(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_three_card_remote_returns_one_entry_per_card(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp=80), _card(1, temp=86), _card(2, temp=64, util=0)])
                        for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["host"], "vader")
        self.assertEqual([c["idx"] for c in d["cards"]], [0, 1, 2])
        self.assertTrue(d["has_gpu"])
        self.assertTrue(d["has_history"])

    def test_single_card_hub_has_the_same_shape_as_a_remote(self):
        now = int(time.time())
        _seed("local", [(now - t, [_card(0)]) for t in range(300, 0, -10)])
        _seed("vader", [(now - t, [_card(0), _card(1)]) for t in range(300, 0, -10)])
        a = self.c.get("/api/gpu/history?host=local&range=1h").get_json()
        b = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(sorted(a.keys()), sorted(b.keys()))
        self.assertEqual(sorted(a["cards"][0].keys()), sorted(b["cards"][0].keys()))
        self.assertEqual(sorted(a["cards"][0]["series"].keys()),
                         sorted(b["cards"][0]["series"].keys()))

    def test_series_length_matches_labels_for_every_card(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0), _card(1)]) for t in range(600, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        n = len(d["labels"])
        for card in d["cards"]:
            for metric, vals in card["series"].items():
                self.assertEqual(len(vals), n, f"{metric} on card {card['idx']}")
        for metric, vals in d["combined"].items():
            self.assertEqual(len(vals), n, metric)

    def test_host_with_no_gpu_is_distinguishable_from_one_with_no_history_yet(self):
        d = self.c.get("/api/gpu/history?host=nosuchhost&range=1h").get_json()
        self.assertFalse(d["has_gpu"])
        self.assertFalse(d["has_history"])
        self.assertEqual(d["cards"], [])


class TestSupportsMap(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_unreported_fan_is_advertised_unsupported_not_zero(self):
        # The regression this guards: serialising fan as 0 for a passively cooled
        # card would draw a confident flat line at zero and, worse, look exactly
        # like a stalled fan.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0)]) for t in range(300, 0, -10)])   # no fan key
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        card = d["cards"][0]
        self.assertFalse(card["supports"]["fan"])
        self.assertTrue(all(v is None for v in card["series"]["fan"]))

    def test_reported_fan_is_supported(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, fan=62)]) for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertTrue(d["cards"][0]["supports"]["fan"])

    def test_a_stalled_fan_reports_zero_and_stays_supported(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, fan=0)]) for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        card = d["cards"][0]
        self.assertTrue(card["supports"]["fan"])
        self.assertEqual(card["series"]["fan"][-1], 0)


class TestCombined(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_combined_temperature_is_the_hottest_card_not_the_mean(self):
        # Averaging temperature across cards hides the one card that is cooking,
        # which is the entire question the combined panel answers.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp=60), _card(1, temp=86), _card(2, temp=64)])
                        for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(max(v for v in d["combined"]["temp_max"] if v is not None), 86)

    def test_combined_vram_and_power_are_sums(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, mem_used=10000, power=100),
                                   _card(1, mem_used=20000, power=200)])
                        for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["combined"]["vram"][-1], 30000)
        self.assertEqual(d["combined"]["power"][-1], 300)


class TestThrottleSpans(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_consecutive_throttle_samples_merge_into_one_span(self):
        now = (int(time.time()) // 10) * 10
        _seed("vader", [(now - t, [_card(0, temp=87, throttle_mask=0x40)])
                        for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        spans = d["cards"][0]["throttle_spans"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["reasons"], ["HW thermal"])
        self.assertGreaterEqual(spans[0]["end"] - spans[0]["start"], 280)

    def test_a_gap_starts_a_new_span(self):
        now = (int(time.time()) // 10) * 10
        pts = ([(now - t, [_card(0, throttle_mask=0x40)]) for t in (600, 590, 580)]
               + [(now - t, [_card(0, throttle_mask=0)]) for t in (570, 560, 550)]
               + [(now - t, [_card(0, throttle_mask=0x40)]) for t in (300, 290)])
        _seed("vader", pts)
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(len(d["cards"][0]["throttle_spans"]), 2)

    def test_power_cap_alone_is_not_a_throttle_span(self):
        # A deliberately power-limited box sits at its cap by design; showing it
        # as a permanent throttle band would make the indicator meaningless.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, throttle_mask=0x04)]) for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertEqual(d["cards"][0]["throttle_spans"], [])


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_health_reports_seconds_not_raw_sample_counts(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, temp=87, throttle_mask=0x40)])
                        for t in range(300, 0, -10)])   # 30 samples @ 10s
        d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        h = d["cards"][0]["health"]
        self.assertEqual(h["throttled_sec"], 300)
        self.assertEqual(h["hot_sec"], 300)
        self.assertEqual(h["peak_temp"], 87)


class TestAttribution(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def _seed_procs(self, host, rows):
        import app
        app.DB.executemany("INSERT INTO proc(ts,service,mem,host) VALUES(?,?,?,?)", rows)
        app.DB.commit()

    def test_power_split_is_flagged_as_an_estimate(self):
        # Load-bearing: GPUs never meter power per process. If this flag ever
        # goes away the UI silently starts presenting a model as a measurement.
        d = self.c.get("/api/gpu/attribution?host=vader&range=1h").get_json()
        self.assertIs(d["estimated"], True)

    def test_vram_is_split_per_service_and_scoped_to_the_host(self):
        now = int(time.time())
        self._seed_procs("vader", [(now - t, "ollama", 60000, "vader") for t in range(300, 0, -10)]
                         + [(now - t, "whisper", 400, "vader") for t in range(300, 0, -10)]
                         + [(now - t, "ollama", 8000, "local") for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/attribution?host=vader&range=1h").get_json()
        by_name = {s["service"]: s for s in d["services"]}
        self.assertEqual(sorted(by_name), ["ollama", "whisper"])
        self.assertEqual(by_name["ollama"]["peak_mb"], 60000)

    def test_idle_floor_is_reported_separately_from_services(self):
        # An idle 3090 burns ~100 W just being powered on. Charging that baseline
        # to whichever service holds VRAM would materially overstate its cost.
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, power=300, mem_used=20000)]) for t in range(300, 100, -10)]
              + [(now - t, [_card(0, power=100, mem_used=20000)]) for t in range(100, 0, -10)])
        self._seed_procs("vader", [(now - t, "ollama", 20000, "vader") for t in range(300, 0, -10)])
        d = self.c.get("/api/gpu/attribution?host=vader&range=1h").get_json()
        self.assertGreater(d["idle_floor_w"], 0)
        self.assertIn("idle", d)

    def test_no_gpu_processes_yields_empty_services_not_zeros(self):
        d = self.c.get("/api/gpu/attribution?host=quiet&range=1h").get_json()
        self.assertEqual(d["services"], [])


if __name__ == "__main__":
    unittest.main()
