"""Unit tests for the AMD GPU back-end (issue #1) — the amdgpu sysfs readers used
by the hub's local collector (app.amd_gpus) and the remote Linux probe
(probe._amd_gpu_sysfs). We build a fake /sys/class/drm tree so the parsing is
verified without an AMD GPU present."""
import errno
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app
import probe


def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(value))


def _amd_card(drm, idx, *, total, used, busy, temp_mc=None, power_uw=None,
              name=None, vendor="0x1002", gtt_total=None, gtt_used=None):
    """Lay down a single fake card<idx>/device/ node under <drm>. `total`/`used` are
    the dedicated-VRAM nodes; pass `gtt_total`/`gtt_used` to also emit the GTT nodes
    an APU exposes (unified-memory pool mapped from system RAM)."""
    dev = os.path.join(drm, "card%d" % idx, "device")
    _write(os.path.join(dev, "vendor"), vendor + "\n")
    _write(os.path.join(dev, "mem_info_vram_total"), total)
    _write(os.path.join(dev, "mem_info_vram_used"), used)
    if gtt_total is not None:
        _write(os.path.join(dev, "mem_info_gtt_total"), gtt_total)
    if gtt_used is not None:
        _write(os.path.join(dev, "mem_info_gtt_used"), gtt_used)
    _write(os.path.join(dev, "gpu_busy_percent"), busy)
    if temp_mc is not None:
        _write(os.path.join(dev, "hwmon", "hwmon3", "temp1_input"), temp_mc)
    if power_uw is not None:
        _write(os.path.join(dev, "hwmon", "hwmon3", "power1_average"), power_uw)
    if name is not None:
        _write(os.path.join(dev, "product_name"), name)
    return dev


class TestAmdSysfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.drm = os.path.join(self.tmp, "drm")
        os.makedirs(self.drm)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_app_collector_reads_card(self):
        # 8 GiB total, 1 GiB used, 37% busy, 54.3°C, 42 W.
        _amd_card(self.drm, 0, total=8 * 1024**3, used=1 * 1024**3, busy=37,
                  temp_mc=54300, power_uw=42_000_000, name="AMD Radeon RX 7900 XTX")
        gpus = app.amd_gpus(drm_root=self.drm)
        self.assertEqual(len(gpus), 1)
        g = gpus[0]
        self.assertEqual(g["idx"], 0)
        self.assertEqual(g["name"], "AMD Radeon RX 7900 XTX")
        self.assertEqual(g["util"], 37.0)
        self.assertEqual(g["mem_total"], 8192)
        self.assertEqual(g["mem_used"], 1024)
        self.assertEqual(g["temp"], 54.3)
        self.assertEqual(g["power"], 42.0)

    def test_probe_card_matches(self):
        _amd_card(self.drm, 0, total=16 * 1024**3, used=2 * 1024**3, busy=10,
                  temp_mc=40000, name="AMD Instinct MI210")
        cards = probe._amd_gpu_sysfs(drm_root=self.drm)
        self.assertEqual(len(cards), 1)
        g = cards[0]
        self.assertEqual(g["name"], "AMD Instinct MI210")
        self.assertEqual(g["mem_total"], 16384)
        self.assertEqual(g["mem_used"], 2048)
        self.assertEqual(g["util"], 10)
        self.assertEqual(g["temp"], 40)
        self.assertEqual(g["vendor"], "amd")

    def test_probe_read_gpu_aggregates_all_cards(self):
        # Two cards: the legacy `gpu` aggregate must pool them (VRAM summed, util
        # averaged, temp = hottest) instead of reporting only card 0 — a 3×3090
        # rig must not read as a single 24 GB card. `gpus` carries the per-card
        # list with hub-collector field names.
        _amd_card(self.drm, 0, total=16 * 1024**3, used=2 * 1024**3, busy=10,
                  temp_mc=40000, name="AMD Instinct MI210")
        _amd_card(self.drm, 1, total=16 * 1024**3, used=6 * 1024**3, busy=30,
                  temp_mc=60000, name="AMD Instinct MI210")
        real = probe._amd_gpu_sysfs(drm_root=self.drm)
        with mock.patch("probe._nvidia_cards", return_value=[]), \
             mock.patch("probe._amd_gpu_sysfs", return_value=real):
            out = probe.read_gpu()
        g = out["gpu"]
        self.assertEqual(g["count"], 2)
        self.assertEqual(g["mem_total"], 32768)
        self.assertEqual(g["mem_used"], 8192)
        self.assertEqual(g["util"], 20)
        self.assertEqual(g["temp"], 60)
        self.assertEqual([c["idx"] for c in out["gpus"]], [0, 1])

    def test_apu_reports_gtt_not_vram_carveout(self):
        # Ryzen AI Max / Strix Halo: 512 MiB dedicated VRAM carve-out, 124 GiB GTT pool
        # (18 MiB in use on an idle box). The reader must report the GTT pool, not the
        # tiny carve-out — otherwise the tile reads "29% full" and pressure math flags a
        # permanent low-VRAM insight. Both the hub collector and the probe must agree.
        _amd_card(self.drm, 0, total=512 * 1024**2, used=148 * 1024**2, busy=0,
                  gtt_total=124 * 1024**3, gtt_used=18 * 1024**2, name="AMD Radeon 8060S")
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["mem_total"], 124 * 1024)   # 124 GiB, not 512 MiB
        self.assertEqual(g["mem_used"], 18)            # GTT usage, not the 148 MiB carve-out
        out = probe._amd_gpu_sysfs(drm_root=self.drm)
        self.assertEqual(out[0]["mem_total"], 124 * 1024)
        self.assertEqual(out[0]["mem_used"], 18)

    def test_apu_missing_gtt_used_degrades_to_zero(self):
        # amdgpu exposes gtt_total but (rarely) not gtt_used → used must fall back to 0,
        # never crash the round()/None math, while total still reflects the GTT pool.
        _amd_card(self.drm, 0, total=512 * 1024**2, used=148 * 1024**2, busy=0,
                  gtt_total=124 * 1024**3, name="AMD Radeon 8060S")
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["mem_total"], 124 * 1024)
        self.assertEqual(g["mem_used"], 0)
        out = probe._amd_gpu_sysfs(drm_root=self.drm)
        self.assertEqual(out[0]["mem_total"], 124 * 1024)
        self.assertEqual(out[0]["mem_used"], 0)

    def test_discrete_card_with_gtt_still_reports_vram(self):
        # Discrete cards also expose a GTT pool, but with a large dedicated VRAM they must
        # keep reporting VRAM exactly as before — no behaviour change for the common case.
        _amd_card(self.drm, 0, total=24 * 1024**3, used=8 * 1024**3, busy=55,
                  gtt_total=64 * 1024**3, gtt_used=1 * 1024**3, name="AMD Radeon RX 7900 XTX")
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["mem_total"], 24 * 1024)    # VRAM, not GTT
        self.assertEqual(g["mem_used"], 8 * 1024)
        out = probe._amd_gpu_sysfs(drm_root=self.drm)
        self.assertEqual(out[0]["mem_total"], 24 * 1024)
        self.assertEqual(out[0]["mem_used"], 8 * 1024)

    def test_non_amd_vendor_is_skipped(self):
        # An NVIDIA card (0x10de) in the same tree must be ignored by the AMD reader.
        _amd_card(self.drm, 0, total=8 * 1024**3, used=0, busy=0, vendor="0x10de")
        self.assertEqual(app.amd_gpus(drm_root=self.drm), [])
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=self.drm), [])

    def test_missing_optional_fields_degrade_to_zero(self):
        # No hwmon (temp/power) and no product_name → still a valid card, zeros + fallback name.
        _amd_card(self.drm, 1, total=4 * 1024**3, used=512 * 1024**2, busy=0)
        gpus = app.amd_gpus(drm_root=self.drm)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "AMD GPU 1")
        self.assertEqual(gpus[0]["temp"], 0)
        self.assertEqual(gpus[0]["power"], 0.0)
        self.assertEqual(gpus[0]["mem_used"], 512)

    def test_no_gpu_returns_empty(self):
        self.assertEqual(app.amd_gpus(drm_root=self.drm), [])
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=self.drm), [])

    def test_unreadable_root_is_safe(self):
        missing = os.path.join(self.tmp, "does-not-exist")
        self.assertEqual(app.amd_gpus(drm_root=missing), [])
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=missing), [])


class TestAmdEnrich(unittest.TestCase):
    """amd_gpus must attach the _enrich_gpus-equivalent fields (clk_sm/clk_mem/
    mem_util/power_limit/pstate/temp_mem) from amdgpu sysfs, so the same UI chips
    that light up on NVIDIA light up on AMD. Every node is optional: a card that
    lacks one simply doesn't get the field."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.drm = os.path.join(self.tmp, "drm")
        os.makedirs(self.drm)

    def _card(self, **kw):
        return _amd_card(self.drm, 0, total=24 * 1024**3, used=0, busy=0, **kw)

    def test_clocks_come_from_the_starred_dpm_rows(self):
        dev = self._card()
        _write(os.path.join(dev, "pp_dpm_sclk"), "0: 600Mhz *\n1: 1100Mhz \n2: 2900Mhz \n")
        _write(os.path.join(dev, "pp_dpm_mclk"), "0: 97MHz\n3: 1000MHz *\n")   # case varies
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["clk_sm"], 600)
        self.assertEqual(g["clk_mem"], 1000)

    def test_sclk_falls_back_to_hwmon_freq(self):
        # Unlabelled freq1 (very old kernels): trusted as sclk.
        dev = self._card()
        _write(os.path.join(dev, "hwmon", "hwmon3", "freq1_input"), 2_900_000_000)
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["clk_sm"], 2900)

    def test_labelled_freq_channels_are_matched_not_assumed(self):
        # freq1 is usually sclk, but only the label says so: with freq1 labelled
        # mclk, the sclk value must come from the channel actually labelled sclk,
        # wherever it sits.
        dev = self._card()
        _write(os.path.join(dev, "hwmon", "hwmon3", "freq1_label"), "mclk\n")
        _write(os.path.join(dev, "hwmon", "hwmon3", "freq1_input"), 1_000_000_000)
        _write(os.path.join(dev, "hwmon", "hwmon3", "freq2_label"), "sclk\n")
        _write(os.path.join(dev, "hwmon", "hwmon3", "freq2_input"), 600_000_000)
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["clk_sm"], 600)

    def test_labelled_non_sclk_freq_is_not_misread(self):
        # A labelled channel that is NOT sclk, and no sclk channel at all: better
        # no clock than the wrong one.
        dev = self._card()
        _write(os.path.join(dev, "hwmon", "hwmon3", "freq1_label"), "mclk\n")
        _write(os.path.join(dev, "hwmon", "hwmon3", "freq1_input"), 1_000_000_000)
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertNotIn("clk_sm", g)

    def test_labelled_channel_with_unreadable_input_keeps_scanning(self):
        # hwmon3 has the right label but no readable input; hwmon4 carries the
        # same label with a value. The scan must reach it, not give up.
        dev = self._card()
        _write(os.path.join(dev, "hwmon", "hwmon3", "temp3_label"), "mem\n")
        _write(os.path.join(dev, "hwmon", "hwmon4", "temp5_label"), "mem\n")
        _write(os.path.join(dev, "hwmon", "hwmon4", "temp5_input"), 78_000)
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["temp_mem"], 78.0)

    def test_no_starred_row_leaves_the_field_unset(self):
        dev = self._card()
        _write(os.path.join(dev, "pp_dpm_sclk"), "0: 600Mhz \n1: 1100Mhz \n")
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertNotIn("clk_sm", g)
        self.assertNotIn("clk_mem", g)
        self.assertNotIn("pstate", g)
        self.assertNotIn("power_limit", g)

    def test_discrete_extras_cap_membusy_memtemp(self):
        dev = self._card()
        _write(os.path.join(dev, "mem_busy_percent"), 42)
        _write(os.path.join(dev, "hwmon", "hwmon3", "power1_cap"), 291_000_000)
        _write(os.path.join(dev, "hwmon", "hwmon3", "temp3_label"), "mem\n")
        _write(os.path.join(dev, "hwmon", "hwmon3", "temp3_input"), 78_000)
        _write(os.path.join(dev, "hwmon", "hwmon3", "temp1_label"), "edge\n")
        _write(os.path.join(dev, "hwmon", "hwmon3", "temp1_input"), 55_000)
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["mem_util"], 42)
        self.assertEqual(g["power_limit"], 291.0)
        self.assertEqual(g["temp_mem"], 78.0)     # temp3 via its label, not its number

    def test_perf_level_becomes_pstate(self):
        dev = self._card()
        _write(os.path.join(dev, "power_dpm_force_performance_level"), "auto\n")
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["pstate"], "auto")

    def test_gpu_extra_aggregates_amd_fields(self):
        # The 'GPU right now' chips read the aggregate dict — it must work from
        # AMD-enriched cards exactly as from NVIDIA ones.
        dev = self._card()
        _write(os.path.join(dev, "pp_dpm_sclk"), "2: 2900Mhz *\n")
        _write(os.path.join(dev, "power_dpm_force_performance_level"), "auto\n")
        x = app._gpu_extra(app.amd_gpus(drm_root=self.drm))
        self.assertEqual(x["clk_sm"], 2900)
        self.assertEqual(x["pstate"], "auto")
        self.assertFalse(x["throttled"])
        # No card measured mem-bandwidth utilisation (APUs have no
        # mem_busy_percent): the aggregate must omit the field entirely, not
        # report a fabricated 0% — the UI hides the chip only when it's absent.
        self.assertNotIn("mem_util", x)

    def test_gpu_extra_keeps_measured_zero_mem_util(self):
        # A measured 0% is a real claim and must survive the presence filter.
        dev = self._card()
        _write(os.path.join(dev, "mem_busy_percent"), 0)
        x = app._gpu_extra(app.amd_gpus(drm_root=self.drm))
        self.assertEqual(x["mem_util"], 0)


class TestAmdPciName(unittest.TestCase):
    """Kernels without product_name (every APU) fall back to the host's pci.ids,
    read through HOST_ROOT. The bracket in an AMD entry lists retail names; it is
    used only when unambiguous, the codename otherwise."""

    IDS = (
        "# comment\n"
        "1001  Vendor before\n"
        "\t0001  Not an AMD device\n"
        "1002  Advanced Micro Devices, Inc. [AMD/ATI]\n"
        "\t1586  Strix Halo [Radeon Graphics / Radeon 8050S Graphics / Radeon 8060S Graphics]\n"
        "\t744c  Navi 31 [Radeon RX 7900 XT/7900 XTX/7900M]\n"
        "\t\t1002 0e3b  Subsystem line to be skipped\n"
        "\t73ff  Navi 23 [Radeon RX 6600]\n"
        "\t15bf  Phoenix1\n"
        "10de  NVIDIA Corporation\n"
        "\t2684  AD102 [GeForce RTX 4090]\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        ids = os.path.join(self.tmp, "pci.ids")
        _write(ids, self.IDS)
        self.addCleanup(setattr, app, "_AMD_PCI_NAMES", None)
        mock.patch.object(app, "_PCI_IDS_PATHS", (ids,)).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(app, "_hp", lambda p: p).start()
        app._AMD_PCI_NAMES = None      # drop the cache between tests

    def _dev(self, device_id):
        dev = os.path.join(self.tmp, "card0", "device")
        _write(os.path.join(dev, "device"), device_id + "\n")
        return dev

    def test_multi_variant_bracket_keeps_the_codename(self):
        # Strix Halo silicon ships as 8050S or 8060S — we can't tell which, so
        # claiming either would be wrong.
        self.assertEqual(app._amd_pci_name(self._dev("0x1586")), "AMD Strix Halo")
        self.assertEqual(app._amd_pci_name(self._dev("0x744c")), "AMD Navi 31")

    def test_single_retail_name_wins_over_codename(self):
        self.assertEqual(app._amd_pci_name(self._dev("0x73ff")), "AMD Radeon RX 6600")

    def test_entry_without_bracket_is_used_as_is(self):
        self.assertEqual(app._amd_pci_name(self._dev("0x15bf")), "AMD Phoenix1")

    def test_unknown_device_and_foreign_vendor_yield_none(self):
        self.assertIsNone(app._amd_pci_name(self._dev("0xdead")))
        self.assertIsNone(app._amd_pci_name(self._dev("0x2684")))   # NVIDIA row ignored

    def test_subsystem_rows_do_not_leak_into_the_table(self):
        # A regressed \t\t guard would file the subsystem row under its vendor id.
        self.assertNotIn("1002", app._amd_pci_names())

    def test_empty_result_is_not_cached(self):
        # pci.ids may appear after startup (bind mount added, package installed):
        # a miss must be retried, only a parsed table is cached for good.
        with mock.patch.object(app, "_PCI_IDS_PATHS", (os.path.join(self.tmp, "nope"),)):
            self.assertIsNone(app._amd_pci_name(self._dev("0x1586")))
        self.assertEqual(app._amd_pci_name(self._dev("0x1586")), "AMD Strix Halo")

    def test_missing_ids_file_yields_none(self):
        app._AMD_PCI_NAMES = None
        with mock.patch.object(app, "_PCI_IDS_PATHS", (os.path.join(self.tmp, "nope"),)):
            self.assertIsNone(app._amd_pci_name(self._dev("0x1586")))

    def test_amd_gpus_uses_the_lookup_when_product_name_is_absent(self):
        drm = os.path.join(self.tmp, "drm")
        dev = _amd_card(drm, 1, total=512 * 1024**2, used=0, busy=0,
                        gtt_total=124 * 1024**3)
        _write(os.path.join(dev, "device"), "0x1586\n")
        g = app.amd_gpus(drm_root=drm)[0]
        self.assertEqual(g["name"], "AMD Strix Halo")

    def test_product_name_still_wins(self):
        drm = os.path.join(self.tmp, "drm")
        dev = _amd_card(drm, 0, total=24 * 1024**3, used=0, busy=0,
                        name="AMD Radeon RX 7900 XTX")
        _write(os.path.join(dev, "device"), "0x744c\n")
        g = app.amd_gpus(drm_root=drm)[0]
        self.assertEqual(g["name"], "AMD Radeon RX 7900 XTX")


class TestVendorAwareDiagnostics(unittest.TestCase):
    """The local diagnostics GPU row must speak the right vendor: an AMD host must
    NOT be told to install the NVIDIA runtime (issue #1 follow-up)."""

    def _gpu_check(self):
        for c in app.local_diagnostics()["checks"]:
            if c["id"] == "nvidia":
                return c
        self.fail("no GPU diagnostic row produced")

    def setUp(self):
        self._saved = dict(app.LATEST)

    def tearDown(self):
        app.LATEST.clear()
        app.LATEST.update(self._saved)

    def test_amd_present_is_labelled_amd(self):
        app.LATEST.update(gpu_avail=True, gpu_vendor="amd", mem_total=16384)
        c = self._gpu_check()
        self.assertEqual(c["label"], "GPU (AMD)")
        self.assertEqual(c["status"], "ok")
        self.assertIn("amdgpu", c["detail"])

    def test_nvidia_present_is_labelled_nvidia(self):
        app.LATEST.update(gpu_avail=True, gpu_vendor="nvidia", mem_total=24576)
        c = self._gpu_check()
        self.assertEqual(c["label"], "GPU (NVIDIA)")
        self.assertIn("nvidia-smi", c["detail"])

    def test_no_gpu_remedy_mentions_amd_not_only_nvidia(self):
        app.LATEST.update(gpu_avail=False, gpu_vendor=None)
        c = self._gpu_check()
        self.assertEqual(c["label"], "GPU")
        self.assertEqual(c["status"], "info")
        # The remedy must guide AMD users, not just NVIDIA ones.
        blob = (c.get("remedy") or {}).get("where", "") + (c.get("remedy") or {}).get("cmd", "")
        self.assertIn("amdgpu", blob)
        self.assertIn("mem_info_vram_total", blob)

    def test_hybrid_is_labelled_both_vendors(self):
        app.LATEST.update(gpu_avail=True, gpu_vendor="hybrid", mem_total=32768)
        c = self._gpu_check()
        self.assertEqual(c["label"], "GPU (NVIDIA + AMD)")
        self.assertIn("nvidia-smi + amdgpu sysfs", c["detail"])


class TestEbusyRetry(unittest.TestCase):
    """amdgpu's gpu_busy_percent intermittently returns EBUSY ('Device or resource
    busy'); the reader must retry once rather than silently dropping utilisation."""

    def test_read_int_retries_once_on_ebusy(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        p = os.path.join(d, "gpu_busy_percent")
        with open(p, "w") as f:
            f.write("37")
        real_open, state = open, {"busy": True}
        def flaky(path, *a, **k):
            if isinstance(path, str) and path.endswith("gpu_busy_percent") and state["busy"]:
                state["busy"] = False
                raise OSError(errno.EBUSY, "Device or resource busy")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", side_effect=flaky):
            self.assertEqual(app._amd_read_int(p), 37)   # retry succeeded

    def test_read_int_gives_up_on_persistent_ebusy(self):
        def always_busy(path, *a, **k):
            raise OSError(errno.EBUSY, "busy")
        with mock.patch("builtins.open", side_effect=always_busy):
            self.assertIsNone(app._amd_read_int("/sys/x/gpu_busy_percent"))

    def test_other_oserror_is_not_retried(self):
        # A genuinely absent node (ENOENT) → None, no retry loop.
        self.assertIsNone(app._amd_read_int("/definitely/not/here"))


def _amdgpu_fdinfo(client, vram=None, gtt=None, driver="amdgpu", pdev="0000:0b:00.0",
                   vram_key="drm-memory-vram", gtt_key="drm-memory-gtt"):
    """A realistic /proc/<pid>/fdinfo/<fd> body for a DRM fd. `vram`/`gtt` are the
    literal value strings after the key (e.g. '524288 KiB') so unit-parsing tests
    can pass MiB/garbage; None omits the key (pre-5.19 kernels)."""
    lines = ["pos:\t0", "flags:\t02100002", "mnt_id:\t24", "ino:\t209",
             "drm-driver:\t%s" % driver, "drm-client-id:\t%s" % client,
             "drm-pdev:\t%s" % pdev]
    if vram is not None:
        lines.append("%s:\t%s" % (vram_key, vram))
    if gtt is not None:
        lines.append("%s:\t%s" % (gtt_key, gtt))
    return "\n".join(lines) + "\n"


class TestAmdFdinfoProcs(unittest.TestCase):
    """Per-process AMD VRAM attribution from DRM fdinfo — the amdgpu counterpart of
    `nvidia-smi --query-compute-apps` (app.amd_fdinfo_procs). Built on a fake /proc
    tree; the fd "symlinks" are regular files holding their target path and
    os.readlink is patched to read them, so the fixture also builds on hosts where
    creating real symlinks needs privilege (Windows dev boxes)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        p = mock.patch("os.readlink", side_effect=self._readlink)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _readlink(path, *a, **k):
        with open(path) as f:
            return f.read()

    def _fd(self, pid, fd, target, fdinfo=None):
        _write(os.path.join(self.proc, str(pid), "fd", str(fd)), target)
        if fdinfo is not None:
            _write(os.path.join(self.proc, str(pid), "fdinfo", str(fd)), fdinfo)

    def test_attributes_vram_and_gtt_per_pid(self):
        # One llama.cpp-style process holding 512 MiB VRAM + 2 MiB GTT.
        self._fd(100, 3, "/dev/dri/renderD128",
                 _amdgpu_fdinfo(42, vram="524288 KiB", gtt="2048 KiB"))
        _write(os.path.join(self.proc, "self", "status"), "")   # non-numeric → ignored
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc),
                         {100: {"0000:0b:00.0": {"vram": 512.0, "gtt": 2.0}}})

    def test_dup_fds_share_one_drm_client(self):
        # fds 3 and 4 are dup()s of the same DRM client (same pdev+client-id): its
        # 1 GiB must be counted once. fd 5 is a second, distinct client (+256 MiB).
        self._fd(200, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(7, vram="1048576 KiB"))
        self._fd(200, 4, "/dev/dri/renderD128", _amdgpu_fdinfo(7, vram="1048576 KiB"))
        self._fd(200, 5, "/dev/dri/renderD128", _amdgpu_fdinfo(8, vram="262144 KiB"))
        self.assertEqual(
            app.amd_fdinfo_procs(proc_root=self.proc)[200]["0000:0b:00.0"]["vram"],
            1280.0)

    def test_splits_by_pci_device(self):
        # One process touching two AMD cards (e.g. APU + discrete): the split must
        # survive per-pdev so the GTT policy can differ per card.
        self._fd(150, 3, "/dev/dri/renderD128",
                 _amdgpu_fdinfo(1, gtt="1048576 KiB", pdev="0000:c5:00.0"))
        self._fd(150, 4, "/dev/dri/renderD129",
                 _amdgpu_fdinfo(2, vram="262144 KiB", gtt="4096 KiB", pdev="0000:03:00.0"))
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc), {150: {
            "0000:c5:00.0": {"vram": 0.0, "gtt": 1024.0},
            "0000:03:00.0": {"vram": 256.0, "gtt": 4.0}}})

    def test_ignores_non_dri_fds_and_other_drm_drivers(self):
        self._fd(300, 3, "/var/log/syslog")                       # not a DRM fd
        self._fd(300, 4, "/dev/dri/card0",
                 _amdgpu_fdinfo(9, vram="8192 KiB", driver="i915"))  # Intel iGPU
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc), {})

    def test_pre_519_kernel_without_memory_keys_yields_nothing(self):
        # Older kernels emit drm-driver/client-id but no drm-memory-* — the pid must
        # be absent entirely, not reported as a zero-MB ghost.
        self._fd(400, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(11))
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc), {})

    def test_newer_total_keys_and_mib_units(self):
        # Kernels that print drm-total-vram (and a MiB unit) parse identically.
        self._fd(500, 3, "/dev/dri/renderD129",
                 _amdgpu_fdinfo(12, vram="512 MiB", vram_key="drm-total-vram"))
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc),
                         {500: {"0000:0b:00.0": {"vram": 512.0, "gtt": 0.0}}})

    def test_unreadable_fd_table_is_skipped(self):
        # A pid dir with no readable fd/ (vanished process / permission) is skipped
        # without aborting the scan of the remaining pids.
        os.makedirs(os.path.join(self.proc, "600"))
        self._fd(601, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(13, vram="1024 KiB"))
        self.assertEqual(list(app.amd_fdinfo_procs(proc_root=self.proc)), [601])

    def test_fdinfo_kib_unit_parsing(self):
        self.assertEqual(app._fdinfo_kib("1024 KiB"), 1024.0)
        self.assertEqual(app._fdinfo_kib("4 MiB"), 4096.0)
        self.assertEqual(app._fdinfo_kib("2 GiB"), 2 * 1048576.0)
        self.assertEqual(app._fdinfo_kib("1024"), 1024.0)   # unit-less → KiB
        self.assertEqual(app._fdinfo_kib("garbage"), 0.0)
        self.assertEqual(app._fdinfo_kib(""), 0.0)


class TestAmdUnifiedFlag(unittest.TestCase):
    """amd_gpus() must mark APU/GTT-mode cards with unified=True so the collector
    knows to count GTT in per-process attribution — and discrete cards False so a
    dGPU's staging buffers in GTT are not misread as VRAM."""

    def test_unified_flag_marks_apu_only(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        drm = os.path.join(tmp, "drm")
        # card0: discrete 24 GiB card that also exposes a GTT pool.
        _amd_card(drm, 0, total=24 * 1024**3, used=1 * 1024**3, busy=0,
                  gtt_total=64 * 1024**3, gtt_used=0)
        # card1: Strix-Halo-style APU — 512 MiB carve-out, 124 GiB GTT.
        _amd_card(drm, 1, total=512 * 1024**2, used=148 * 1024**2, busy=0,
                  gtt_total=124 * 1024**3, gtt_used=18 * 1024**2)
        g0, g1 = app.amd_gpus(drm_root=drm)
        self.assertFalse(g0["unified"])
        self.assertTrue(g1["unified"])


class TestAmdAttribMb(unittest.TestCase):
    """_amd_attrib_mb — the per-card GTT policy applied to a pid's fdinfo split:
    GTT counts only on unified (APU) devices, matched by PCI address, so a hybrid
    APU + discrete-AMD host doesn't misread the dGPU's staging buffers as VRAM."""

    APU  = {"pdev": "0000:c5:00.0", "unified": True}
    DGPU = {"pdev": "0000:03:00.0", "unified": False}

    def test_discrete_only_counts_vram_not_gtt(self):
        devs = {"0000:03:00.0": {"vram": 8192.0, "gtt": 512.0}}
        self.assertEqual(app._amd_attrib_mb(devs, [self.DGPU]), 8192.0)

    def test_apu_counts_gtt_too(self):
        devs = {"0000:c5:00.0": {"vram": 16.0, "gtt": 90000.0}}
        self.assertEqual(app._amd_attrib_mb(devs, [self.APU]), 90016.0)

    def test_hybrid_applies_policy_per_card(self):
        # The review-flagged edge: one pid on both cards — the dGPU's GTT staging
        # buffers must NOT be added just because an APU exists in the same box.
        devs = {"0000:03:00.0": {"vram": 8192.0, "gtt": 512.0},
                "0000:c5:00.0": {"vram": 16.0, "gtt": 90000.0}}
        self.assertEqual(app._amd_attrib_mb(devs, [self.DGPU, self.APU]),
                         8192.0 + 16.0 + 90000.0)

    def test_unknown_pdev_falls_back_to_any_unified(self):
        # Kernel omits drm-pdev (or sysfs gave no BDF): fall back to the host-wide
        # heuristic — GTT counts iff any card is unified.
        devs = {None: {"vram": 0.0, "gtt": 4096.0}}
        self.assertEqual(app._amd_attrib_mb(devs, [self.APU]), 4096.0)
        self.assertEqual(app._amd_attrib_mb(devs, [self.DGPU]), 0.0)
        self.assertEqual(app._amd_attrib_mb(devs, [{"pdev": None, "unified": True}]),
                         4096.0)


if __name__ == "__main__":
    unittest.main()
