"""Per-card GPU alerting — the rules that decide whether a human gets woken up.

The bar here is not "does it fire" but "does it fire only when it should". An
alert that cries wolf gets muted, and a muted alert protects nothing — so most
of these tests are about the things that must NOT fire.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _card(idx=0, **kw):
    g = {"idx": idx, "name": "NVIDIA GeForce RTX 3090", "vendor": "nvidia",
         "util": 100, "mem_used": 12000, "mem_total": 24576, "power": 250, "temp": 70}
    g.update(kw)
    return g


class _GpuAlertCase(unittest.TestCase):
    """Drives notify_gpu_cards with a fake fleet and captures what it emits."""

    def setUp(self):
        import app
        from backend import notify
        self.app, self.notify = app, notify
        notify._GPU_SINCE.clear()
        notify._GPU_ROSTER.clear()
        self.emitted = []
        self.cleared = []
        self.settings = {"gpu_temp_alert_c": "84", "gpu_temp_sustain_s": "180",
                         "gpu_throttle_sustain_s": "120", "gpu_vram_alert_pct": "95",
                         "gpu_vram_sustain_s": "300", "gpu_fanstall_alerts": "1",
                         "gpu_missing_alerts": "1"}

    def run_scan(self, fleet, at):
        """One notifier pass over `fleet` = [(host, cards, online)] at time `at`."""
        with mock.patch.object(self.app, "fleet_gpu_cards", return_value=fleet), \
             mock.patch.object(self.app, "_emit",
                               side_effect=lambda s, k, lvl, t, d, rules=None: self.emitted.append((k, lvl, t, d))), \
             mock.patch.object(self.app, "_clear", side_effect=lambda k: self.cleared.append(k)), \
             mock.patch.object(self.notify.time, "time", return_value=at):
            self.notify.notify_gpu_cards(self.settings, None)

    def keys(self):
        return [e[0] for e in self.emitted]

    def sustain(self, fleet, t0, seconds, step=20):
        """Hold a condition true across repeated scans, as the collector would."""
        for t in range(t0, t0 + seconds + 1, step):
            self.run_scan(fleet, t)


class TestSustainedNotInstant(_GpuAlertCase):
    def test_a_brief_temperature_spike_does_not_alert(self):
        # The whole difference between a useful GPU alert and a muted one.
        fleet = [("vader", [_card(0, temp=87)], True)]
        self.run_scan(fleet, 1000)
        self.run_scan(fleet, 1020)
        self.assertEqual(self.keys(), [])

    def test_sustained_heat_does_alert(self):
        fleet = [("vader", [_card(0, temp=87)], True)]
        self.sustain(fleet, 1000, 200)
        self.assertIn("gpu:temp:vader:0", self.keys())

    def test_the_clock_resets_when_the_condition_lapses(self):
        hot  = [("vader", [_card(0, temp=87)], True)]
        cool = [("vader", [_card(0, temp=60)], True)]
        self.sustain(hot, 1000, 100)      # not yet 180s
        self.run_scan(cool, 1120)         # lapse — clock resets
        self.sustain(hot, 1140, 100)      # another 100s, still short of 180
        self.assertEqual([k for k in self.keys() if k.startswith("gpu:temp")], [])


class TestPowerCapIsNotThrottling(_GpuAlertCase):
    def test_a_power_capped_card_never_raises_a_throttle_alert(self):
        # A box whose cards run at a deliberately lowered power limit sits at its
        # cap essentially always. Alerting on that would fire continuously on
        # healthy hardware.
        fleet = [("vader", [_card(0, temp=70, throttle_mask=0x04,
                                  throttle=["Power cap"], throttled=True)], True)]
        self.sustain(fleet, 1000, 600)
        self.assertEqual([k for k in self.keys() if "throttle" in k], [])

    def test_thermal_throttling_does_alert(self):
        fleet = [("vader", [_card(0, temp=88, throttle_mask=0x40,
                                  throttle=["HW thermal"], throttled=True)], True)]
        self.sustain(fleet, 1000, 200)
        self.assertIn("gpu:throttle:vader:0", self.keys())


class TestFanStall(_GpuAlertCase):
    def test_a_passively_cooled_card_never_trips_the_fan_alert(self):
        # A Tesla reports no fan at all. Treating absent as 0% would page the
        # user about hardware that has no fan to stall.
        fleet = [("vader", [_card(0, temp=75)], True)]        # no 'fan' key
        self.sustain(fleet, 1000, 300)
        self.assertEqual([k for k in self.keys() if "fanstall" in k], [])

    def test_a_genuinely_stopped_fan_on_a_hot_card_alerts(self):
        fleet = [("vader", [_card(0, temp=80, fan=0)], True)]
        self.sustain(fleet, 1000, 200)
        self.assertIn("gpu:fanstall:vader:0", self.keys())

    def test_a_stopped_fan_on_a_cold_card_is_normal(self):
        # Zero-RPM idle modes are a feature; a cold card with stopped fans is
        # working exactly as designed.
        fleet = [("vader", [_card(0, temp=35, fan=0, util=0)], True)]
        self.sustain(fleet, 1000, 300)
        self.assertEqual([k for k in self.keys() if "fanstall" in k], [])

    def test_zero_rpm_idle_in_the_fifties_is_not_a_stall(self):
        # Caught on the live fleet: a 3090 idling at 53 C with its fan fully
        # stopped, which is exactly what a zero-RPM cooler is supposed to do.
        # A flat 50 C bar would have fired a critical on healthy hardware.
        fleet = [("vader", [_card(0, temp=53, fan=0, util=0, power=40)], True)]
        self.sustain(fleet, 1000, 600)
        self.assertEqual([k for k in self.keys() if "fanstall" in k], [])

    def test_the_stall_bar_follows_the_configured_threshold(self):
        # With a raised threshold the stall bar moves with it, so a host that is
        # allowed to run hotter doesn't get a stall alert at a normal idle temp.
        self.settings["gpu_temp_alert_c"] = "95"
        fleet = [("vader", [_card(0, temp=80, fan=0)], True)]
        self.sustain(fleet, 1000, 300)
        self.assertEqual([k for k in self.keys() if "fanstall" in k], [])


class TestPerCardPerHostIsolation(_GpuAlertCase):
    def test_one_hot_card_does_not_suppress_or_implicate_the_others(self):
        fleet = [("vader", [_card(0, temp=60), _card(1, temp=88), _card(2, temp=64)], True)]
        self.sustain(fleet, 1000, 200)
        temp_keys = [k for k in self.keys() if k.startswith("gpu:temp")]
        self.assertEqual(set(temp_keys), {"gpu:temp:vader:1"})

    def test_two_hosts_are_two_incidents(self):
        fleet = [("vader", [_card(0, temp=88)], True),
                 ("local", [_card(0, temp=88)], True)]
        self.sustain(fleet, 1000, 200)
        self.assertEqual(set(k for k in self.keys() if k.startswith("gpu:temp")),
                         {"gpu:temp:vader:0", "gpu:temp:local:0"})


class TestPerHostOverride(_GpuAlertCase):
    def test_a_host_override_raises_the_bar_for_that_host_only(self):
        # A box whose cards run hot by design under a lowered power limit needs a
        # higher threshold, or it cries wolf; the rest of the fleet should not.
        self.settings["gpu_temp_overrides"] = '{"vader": 90}'
        fleet = [("vader", [_card(0, temp=86)], True),
                 ("local", [_card(0, temp=86)], True)]
        self.sustain(fleet, 1000, 200)
        temp_keys = set(k for k in self.keys() if k.startswith("gpu:temp"))
        self.assertEqual(temp_keys, {"gpu:temp:local:0"})

    def test_malformed_override_json_falls_back_to_the_global_threshold(self):
        self.settings["gpu_temp_overrides"] = "{not json"
        fleet = [("vader", [_card(0, temp=88)], True)]
        self.sustain(fleet, 1000, 200)
        self.assertIn("gpu:temp:vader:0", self.keys())


class TestHysteresis(_GpuAlertCase):
    def test_a_card_hovering_on_the_line_does_not_flap(self):
        # 83 C is below the 84 C trip but inside the hysteresis band, so the
        # alert must stay armed rather than clearing and re-firing every scan.
        self.sustain([("vader", [_card(0, temp=88)], True)], 1000, 200)
        self.cleared.clear()
        self.run_scan([("vader", [_card(0, temp=83)], True)], 1300)
        self.assertNotIn("gpu:temp:vader:0", self.cleared)

    def test_a_card_that_genuinely_cooled_down_clears(self):
        self.sustain([("vader", [_card(0, temp=88)], True)], 1000, 200)
        self.cleared.clear()
        self.run_scan([("vader", [_card(0, temp=70)], True)], 1300)
        self.assertIn("gpu:temp:vader:0", self.cleared)


class TestOfflineHosts(_GpuAlertCase):
    def test_a_stale_snapshot_from_an_offline_host_never_alerts(self):
        # The last reading before a machine went down says nothing about its
        # temperature now, and the machine may simply be powered off.
        fleet = [("vader", [_card(0, temp=95, throttle_mask=0x40)], False)]
        self.sustain(fleet, 1000, 600)
        self.assertEqual(self.keys(), [])


class TestMissingCard(_GpuAlertCase):
    def test_a_card_that_vanishes_alerts_after_it_persists(self):
        three = [("vader", [_card(0), _card(1), _card(2)], True)]
        two   = [("vader", [_card(0), _card(2)], True)]
        self.run_scan(three, 1000)
        self.sustain(two, 1020, 200)
        self.assertIn("gpu:missing:vader:1", self.keys())

    def test_one_missed_poll_is_not_a_missing_card(self):
        three = [("vader", [_card(0), _card(1), _card(2)], True)]
        two   = [("vader", [_card(0), _card(2)], True)]
        self.run_scan(three, 1000)
        self.run_scan(two, 1020)           # nvidia-smi timed out once
        self.run_scan(three, 1040)
        self.assertEqual([k for k in self.keys() if "missing" in k], [])

    def test_a_deliberately_removed_card_stops_alerting_after_an_hour(self):
        # Otherwise pulling a GPU leaves a critical armed forever — the same
        # false alarm the cockpit's "retired" state exists to avoid.
        two = [("vader", [_card(0), _card(1)], True)]
        one = [("vader", [_card(0)], True)]
        self.run_scan(two, 1000)
        self.sustain(one, 1020, 200)
        self.assertIn("gpu:missing:vader:1", self.keys())
        self.cleared.clear()
        self.run_scan(one, 1000 + 3700)
        self.assertIn("gpu:missing:vader:1", self.cleared)

    def test_a_card_that_comes_back_clears(self):
        two = [("vader", [_card(0), _card(1)], True)]
        one = [("vader", [_card(0)], True)]
        self.run_scan(two, 1000)
        self.sustain(one, 1020, 200)
        self.cleared.clear()
        self.run_scan(two, 1300)
        self.assertIn("gpu:missing:vader:1", self.cleared)

    def test_a_new_host_does_not_alert_on_its_first_sighting(self):
        self.run_scan([("vader", [_card(0), _card(1)], True)], 1000)
        self.assertEqual([k for k in self.keys() if "missing" in k], [])


class TestAlertBodyNamesTheCause(_GpuAlertCase):
    def test_the_body_says_who_is_driving_the_card_and_the_fan_headroom(self):
        # "GPU hot" is a fact. "GPU hot, fan already at 100%, ollama holds
        # 21.6 GB on this card" is something a human can act on.
        fleet = [("vader", [_card(0, temp=88, fan=100)], True)]
        with mock.patch.object(self.notify, "_gpu_card_driver", return_value=("ollama", 21600)):
            self.sustain(fleet, 1000, 200)
        body = [e[3] for e in self.emitted if e[0] == "gpu:temp:vader:0"][0]
        self.assertIn("ollama", body)
        self.assertIn("no cooling headroom", body)

    def test_fan_headroom_is_stated_when_there_is_some(self):
        fleet = [("vader", [_card(0, temp=88, fan=62)], True)]
        self.sustain(fleet, 1000, 200)
        body = [e[3] for e in self.emitted if e[0] == "gpu:temp:vader:0"][0]
        self.assertIn("62%", body)


class TestVramPressure(_GpuAlertCase):
    def test_per_card_vram_pressure_alerts_when_sustained(self):
        fleet = [("vader", [_card(0, mem_used=24000, mem_total=24576)], True)]
        self.sustain(fleet, 1000, 400)
        self.assertIn("gpu:vram:vader:0", self.keys())

    def test_a_card_with_no_reported_capacity_never_alerts(self):
        fleet = [("vader", [_card(0, mem_used=0, mem_total=0)], True)]
        self.sustain(fleet, 1000, 400)
        self.assertEqual([k for k in self.keys() if k.startswith("gpu:vram")], [])


class TestSettingsValidation(unittest.TestCase):
    """A malformed override must be REJECTED, not silently ignored — otherwise
    the user believes a host has a raised threshold when it doesn't, and finds
    out via a 3am page."""

    def setUp(self):
        import app
        self.v = app._validate_gpu_alert_settings

    def test_valid_override_passes(self):
        self.assertIsNone(self.v({"gpu_temp_overrides": '{"vader": 88}'}))

    def test_blank_override_passes(self):
        self.assertIsNone(self.v({"gpu_temp_overrides": "   "}))

    def test_malformed_json_is_rejected(self):
        self.assertIsNotNone(self.v({"gpu_temp_overrides": "{not json"}))

    def test_non_object_json_is_rejected(self):
        self.assertIsNotNone(self.v({"gpu_temp_overrides": '["vader"]'}))

    def test_absurd_temperature_is_rejected(self):
        self.assertIsNotNone(self.v({"gpu_temp_overrides": '{"vader": 5}'}))
        self.assertIsNotNone(self.v({"gpu_temp_overrides": '{"vader": 400}'}))

    def test_non_numeric_temperature_is_rejected(self):
        self.assertIsNotNone(self.v({"gpu_temp_overrides": '{"vader": "hot"}'}))

    def test_out_of_range_thresholds_are_rejected(self):
        self.assertIsNotNone(self.v({"gpu_temp_alert_c": "5"}))
        self.assertIsNotNone(self.v({"gpu_vram_alert_pct": "500"}))
        self.assertIsNotNone(self.v({"gpu_temp_sustain_s": "99999"}))

    def test_in_range_thresholds_pass(self):
        self.assertIsNone(self.v({"gpu_temp_alert_c": "84", "gpu_vram_alert_pct": "95",
                                  "gpu_temp_sustain_s": "180"}))


class TestIdleWattsOptIn(_GpuAlertCase):
    def test_idle_watts_is_off_unless_configured(self):
        fleet = [("vader", [_card(0, util=0, power=230)], True)]
        self.sustain(fleet, 1000, 2000, step=100)
        self.assertEqual([k for k in self.keys() if "idlewatts" in k], [])

    def test_idle_watts_alerts_once_enabled_and_sustained(self):
        self.settings["gpu_idle_watts"] = "100"
        self.settings["gpu_idle_sustain_s"] = "600"
        fleet = [("vader", [_card(0, util=0, power=230)], True)]
        self.sustain(fleet, 1000, 700, step=100)
        self.assertIn("gpu:idlewatts:vader:0", self.keys())


if __name__ == "__main__":
    unittest.main()
