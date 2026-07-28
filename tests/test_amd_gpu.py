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

    def test_probe_representative_matches(self):
        _amd_card(self.drm, 0, total=16 * 1024**3, used=2 * 1024**3, busy=10,
                  temp_mc=40000, name="AMD Instinct MI210")
        out = probe._amd_gpu_sysfs(drm_root=self.drm)
        self.assertIn("gpu", out)
        self.assertEqual(out["gpu"]["count"], 1)
        self.assertEqual(out["gpu"]["name"], "AMD Instinct MI210")
        self.assertEqual(out["gpu"]["mem_total"], 16384)
        self.assertEqual(out["gpu"]["mem_used"], 2048)
        self.assertEqual(out["gpu"]["util"], 10)
        self.assertEqual(out["gpu"]["temp"], 40)

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
        self.assertEqual(out["gpu"]["mem_total"], 124 * 1024)
        self.assertEqual(out["gpu"]["mem_used"], 18)

    def test_apu_missing_gtt_used_degrades_to_zero(self):
        # amdgpu exposes gtt_total but (rarely) not gtt_used → used must fall back to 0,
        # never crash the round()/None math, while total still reflects the GTT pool.
        _amd_card(self.drm, 0, total=512 * 1024**2, used=148 * 1024**2, busy=0,
                  gtt_total=124 * 1024**3, name="AMD Radeon 8060S")
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["mem_total"], 124 * 1024)
        self.assertEqual(g["mem_used"], 0)
        out = probe._amd_gpu_sysfs(drm_root=self.drm)
        self.assertEqual(out["gpu"]["mem_total"], 124 * 1024)
        self.assertEqual(out["gpu"]["mem_used"], 0)

    def test_discrete_card_with_gtt_still_reports_vram(self):
        # Discrete cards also expose a GTT pool, but with a large dedicated VRAM they must
        # keep reporting VRAM exactly as before — no behaviour change for the common case.
        _amd_card(self.drm, 0, total=24 * 1024**3, used=8 * 1024**3, busy=55,
                  gtt_total=64 * 1024**3, gtt_used=1 * 1024**3, name="AMD Radeon RX 7900 XTX")
        g = app.amd_gpus(drm_root=self.drm)[0]
        self.assertEqual(g["mem_total"], 24 * 1024)    # VRAM, not GTT
        self.assertEqual(g["mem_used"], 8 * 1024)
        out = probe._amd_gpu_sysfs(drm_root=self.drm)
        self.assertEqual(out["gpu"]["mem_total"], 24 * 1024)
        self.assertEqual(out["gpu"]["mem_used"], 8 * 1024)

    def test_non_amd_vendor_is_skipped(self):
        # An NVIDIA card (0x10de) in the same tree must be ignored by the AMD reader.
        _amd_card(self.drm, 0, total=8 * 1024**3, used=0, busy=0, vendor="0x10de")
        self.assertEqual(app.amd_gpus(drm_root=self.drm), [])
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=self.drm), {})

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
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=self.drm), {})

    def test_unreadable_root_is_safe(self):
        missing = os.path.join(self.tmp, "does-not-exist")
        self.assertEqual(app.amd_gpus(drm_root=missing), [])
        self.assertEqual(probe._amd_gpu_sysfs(drm_root=missing), {})


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
                   vram_key="drm-memory-vram", gtt_key="drm-memory-gtt",
                   shared_vram=None, shared_gtt=None):
    """A realistic /proc/<pid>/fdinfo/<fd> body for a DRM fd. `vram`/`gtt` are the
    literal value strings after the key (e.g. '524288 KiB') so unit-parsing tests
    can pass MiB/garbage; None omits the key (pre-5.19 kernels). The default keys
    are the legacy drm-memory-* names the 6.1–6.14 kernels publish; pass
    vram_key="drm-resident-vram" for the standardised layout, shared_* to emit the
    drm-shared-* lines a dma-buf-sharing client shows, and pdev=None to drop the
    drm-pdev line the way kernels without a BDF do."""
    lines = ["pos:\t0", "flags:\t02100002", "mnt_id:\t24", "ino:\t209",
             "drm-driver:\t%s" % driver, "drm-client-id:\t%s" % client]
    if pdev is not None:
        lines.append("drm-pdev:\t%s" % pdev)
    if vram is not None:
        lines.append("%s:\t%s" % (vram_key, vram))
    if gtt is not None:
        lines.append("%s:\t%s" % (gtt_key, gtt))
    if shared_vram is not None:
        lines.append("drm-shared-vram:\t%s" % shared_vram)
    if shared_gtt is not None:
        lines.append("drm-shared-gtt:\t%s" % shared_gtt)
    return "\n".join(lines) + "\n"


class TestAmdFdinfoProcs(unittest.TestCase):
    """Per-process AMD VRAM attribution from DRM fdinfo — the amdgpu counterpart of
    `nvidia-smi --query-compute-apps` (app.amd_fdinfo_procs). Built on a fake /proc
    tree with real symlinks for the fd entries. The fd's identity comes from
    os.stat's device number: where the symlink target exists and is a DRM node
    (a dev box with a GPU) the stat says so outright, and where it doesn't (a
    GPU-less CI runner) the stat fails and the scanner falls back to reading
    fdinfo anyway — the same degradation path a rootless-podman container takes —
    so the suite exercises both roads without needing an AMD card."""

    @classmethod
    def setUpClass(cls):
        # The fixture needs real symlinks (fd identity goes through os.stat). On a
        # Windows dev box without Developer Mode os.symlink raises — skip the class
        # rather than fail it at fixture-build time.
        probe = tempfile.mkdtemp()
        try:
            os.symlink("target", os.path.join(probe, "ln"))
        except OSError:
            raise unittest.SkipTest("os.symlink unavailable on this host")
        finally:
            __import__("shutil").rmtree(probe, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)

    def _fd(self, pid, fd, target, fdinfo=None):
        link = os.path.join(self.proc, str(pid), "fd", str(fd))
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(target, link)
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
        # fd 3 resolves to a real regular file: st_rdev is 0, so the device-number
        # check rejects it even though its fdinfo looks perfectly amdgpu-shaped.
        lib = os.path.join(self.tmp, "libGLX.so")
        _write(lib, "not a device")
        self._fd(300, 3, lib, _amdgpu_fdinfo(9, vram="8192 KiB"))
        self._fd(300, 4, "/dev/dri/card0",
                 _amdgpu_fdinfo(9, vram="8192 KiB", driver="i915"))  # Intel iGPU
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc), {})

    def test_pre_519_kernel_without_memory_keys_yields_nothing(self):
        # Older kernels emit drm-driver/client-id but no residency keys — the pid
        # must be absent entirely, not reported as a zero-MB ghost.
        self._fd(400, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(11))
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc), {})

    def test_newer_total_keys_and_mib_units(self):
        # Kernels that print drm-total-vram (and a MiB unit) parse identically.
        self._fd(500, 3, "/dev/dri/renderD129",
                 _amdgpu_fdinfo(12, vram="512 MiB", vram_key="drm-total-vram"))
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc),
                         {500: {"0000:0b:00.0": {"vram": 512.0, "gtt": 0.0}}})

    def test_standardised_resident_keys_are_preferred(self):
        # 6.15+ prints drm-resident-* — the actual residency — next to the
        # drm-total-* alias. When both are present, resident must win.
        body = _amdgpu_fdinfo(21, vram="1048576 KiB", vram_key="drm-resident-vram")
        body += "drm-total-vram:\t9999999 KiB\n"
        self._fd(700, 3, "/dev/dri/renderD128", body)
        self.assertEqual(
            app.amd_fdinfo_procs(proc_root=self.proc)[700]["0000:0b:00.0"]["vram"],
            1024.0)

    def test_shared_buffers_are_not_credited_to_each_client(self):
        # A dma-buf shared between two clients appears in the residency of BOTH,
        # and their client-ids differ so dedup can't catch it. Counting it whole
        # in each would push per-service totals past the card's capacity.
        self._fd(800, 3, "/dev/dri/renderD128",
                 _amdgpu_fdinfo(1, gtt="10485760 KiB", shared_gtt="4194304 KiB"))
        self._fd(801, 3, "/dev/dri/renderD128",
                 _amdgpu_fdinfo(2, gtt="6291456 KiB", shared_gtt="4194304 KiB"))
        got = app.amd_fdinfo_procs(proc_root=self.proc)
        self.assertEqual(got[800]["0000:0b:00.0"]["gtt"], 6144.0)   # 10 GiB - 4 GiB
        self.assertEqual(got[801]["0000:0b:00.0"]["gtt"], 2048.0)   # 6 GiB - 4 GiB

    def test_bare_values_are_bytes_not_kib(self):
        # The kernel formatter scales up only while the value divides evenly by
        # 1024, so an unaligned figure prints raw in bytes — which is why a zero
        # always shows up as a plain '0' and never '0 KiB'. Reading a bare number
        # as KiB would overcount it 1024x.
        self._fd(850, 3, "/dev/dri/renderD128",     # 1 GiB + 1 byte, unaligned — only
                 _amdgpu_fdinfo(1, vram="1073741825",     # the 6.15+ resident/total
                                vram_key="drm-resident-vram"))   # formatter prints bare
        self.assertAlmostEqual(
            app.amd_fdinfo_procs(proc_root=self.proc)[850]["0000:0b:00.0"]["vram"],
            1024.0, places=3)

    def test_fdinfo_bytes_unit_parsing(self):
        self.assertEqual(app._fdinfo_bytes("326612 KiB"), 326612 * 1024)
        self.assertEqual(app._fdinfo_bytes("4 MiB"), 4 * 1024**2)
        self.assertEqual(app._fdinfo_bytes("2 GiB"), 2 * 1024**3)
        self.assertEqual(app._fdinfo_bytes("4095"), 4095)          # bare = bytes
        self.assertEqual(app._fdinfo_bytes("0"), 0)
        self.assertEqual(app._fdinfo_bytes(""), 0)
        self.assertEqual(app._fdinfo_bytes(None), 0)
        self.assertEqual(app._fdinfo_bytes("not-a-number KiB"), 0)
        # An unknown unit must not be guessed at — a wrong scale is worse than none.
        self.assertEqual(app._fdinfo_bytes("12 PiB"), 0)

    def test_legacy_memory_key_beats_total_alias(self):
        # The middle arm of the priority chain: when a kernel prints drm-memory-*
        # next to the drm-total-* alias, memory (the closer-to-resident figure)
        # must win.
        body = _amdgpu_fdinfo(32, vram="524288 KiB")           # drm-memory-vram
        body += "drm-total-vram:\t9999999 KiB\n"
        self._fd(890, 3, "/dev/dri/renderD128", body)
        self.assertEqual(
            app.amd_fdinfo_procs(proc_root=self.proc)[890]["0000:0b:00.0"]["vram"],
            512.0)

    def test_shared_exceeding_resident_clamps_to_zero(self):
        # drm-shared-* is defined against the client's total, not its residency, so
        # resident - shared can legitimately go negative. It must clamp to 0, not
        # subtract from the per-service sum.
        self._fd(895, 3, "/dev/dri/renderD128",
                 _amdgpu_fdinfo(1, gtt="1024 KiB", shared_gtt="4096 KiB"))
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc), {})

    def test_zero_resident_does_not_fall_through_to_total(self):
        # An evicted allocation legitimately reports drm-resident-* as 0 while
        # drm-total-* stays nonzero. A truthiness-based priority chain would fall
        # through and attribute non-resident memory as current residency.
        body = _amdgpu_fdinfo(31, vram="0", vram_key="drm-resident-vram")
        body += "drm-total-vram:\t9999999 KiB\n"
        self._fd(860, 3, "/dev/dri/renderD128", body)
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc), {})

    def test_device_major_is_the_identity(self):
        # Pin the accept/reject decision without needing a GPU on the test host:
        # major 226 passes whatever the path looks like, another char device is
        # rejected even with a perfectly amdgpu-shaped fdinfo behind it.
        self._fd(870, 3, "/definitely/not/dri", _amdgpu_fdinfo(1, vram="1048576 KiB"))
        self._fd(871, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(2, vram="524288 KiB"))
        real_stat = os.stat

        def rigged(path, *a, **k):
            if "/870/fd/" in str(path):
                return mock.Mock(st_rdev=os.makedev(226, 128))   # a remapped DRM node
            if "/871/fd/" in str(path):
                return mock.Mock(st_rdev=os.makedev(1, 3))       # /dev/null's major
            return real_stat(path, *a, **k)

        with mock.patch("os.stat", side_effect=rigged):
            got = app.amd_fdinfo_procs(proc_root=self.proc)
        self.assertEqual(list(got), [870])
        self.assertEqual(got[870]["0000:0b:00.0"]["vram"], 1024.0)

    def test_pdevless_clients_on_two_cards_do_not_collide(self):
        # Kernels that omit drm-pdev: client-ids are per-device counters, so two
        # cards can both have a client 7. Collapsing them to (None, 7) would drop
        # whichever card is seen second; the fd's device number keeps them apart.
        self._fd(880, 3, "/dev/dri/renderD128",
                 _amdgpu_fdinfo(7, vram="1048576 KiB", pdev=None))
        self._fd(880, 4, "/dev/dri/renderD129",
                 _amdgpu_fdinfo(7, vram="262144 KiB", pdev=None))
        real_stat = os.stat

        def per_node(path, *a, **k):
            if "/880/fd/3" in str(path):
                return mock.Mock(st_rdev=os.makedev(226, 128))
            if "/880/fd/4" in str(path):
                return mock.Mock(st_rdev=os.makedev(226, 129))
            return real_stat(path, *a, **k)

        with mock.patch("os.stat", side_effect=per_node):
            got = app.amd_fdinfo_procs(proc_root=self.proc)
        self.assertEqual(got[880][None]["vram"], 1024.0 + 256.0)

    def test_client_shared_across_pids_is_credited_deterministically(self):
        # A supervisor that forked the real worker keeps the inherited fd open, so
        # one client shows up under two PIDs with different cgroups. Readdir order
        # isn't stable, so without a rule the memory would flip between the two
        # services from sample to sample and the history would sawtooth.
        self._fd(4242, 7, "/dev/dri/renderD128", _amdgpu_fdinfo(99, gtt="10485760 KiB"))
        self._fd(1111, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(99, gtt="10485760 KiB"))
        for order in (["4242", "1111"], ["1111", "4242"]):
            with mock.patch("os.listdir", side_effect=lambda p, _o=order, _r=os.listdir:
                            _o if p == self.proc else _r(p)):
                got = app.amd_fdinfo_procs(proc_root=self.proc)
            self.assertEqual(list(got), [1111])                    # lowest PID wins
            self.assertEqual(got[1111]["0000:0b:00.0"]["gtt"], 10240.0)

    def test_two_cards_with_the_same_client_id_both_count(self):
        # The same client-id on two different cards are unrelated clients: dedup is
        # keyed on the pdev too, so both must survive.
        self._fd(950, 3, "/dev/dri/renderD128",
                 _amdgpu_fdinfo(5, gtt="9216000 KiB", pdev="0000:c5:00.0"))
        self._fd(950, 4, "/dev/dri/renderD129",
                 _amdgpu_fdinfo(5, vram="8192000 KiB", pdev="0000:03:00.0"))
        self.assertEqual(app.amd_fdinfo_procs(proc_root=self.proc), {950: {
            "0000:c5:00.0": {"vram": 0.0, "gtt": 9000.0},
            "0000:03:00.0": {"vram": 8000.0, "gtt": 0.0}}})

    def test_denied_stat_still_reads_fdinfo(self):
        # A process inside another user namespace (rootless podman) denies the stat
        # on its fd/* while leaving fdinfo readable — and it's often the very
        # container holding all the VRAM. A denied stat must not skip the fd.
        self._fd(25847, 7, "/dev/dri/renderD128", _amdgpu_fdinfo(3, gtt="22500000 KiB"))
        real_stat = os.stat

        def denied(path, *a, **k):
            if "/25847/fd/" in str(path):
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_stat(path, *a, **k)

        with mock.patch("os.stat", side_effect=denied):
            got = app.amd_fdinfo_procs(proc_root=self.proc)
        self.assertEqual(got[25847]["0000:0b:00.0"]["gtt"], 22500000 / 1024.0)

    def test_render_node_mapped_to_another_path_is_still_read(self):
        # A container can expose the render node anywhere (--device=...:/dev/gpu0).
        # The fd is identified by its device number, not its path, so that
        # container doesn't lose all of its attribution.
        if not os.path.exists("/dev/dri/renderD128"):
            self.skipTest("no DRM render node on this host")
        remapped = os.path.join(self.tmp, "gpu0")     # same device, different path
        os.symlink("/dev/dri/renderD128", remapped)
        self._fd(970, 3, remapped, _amdgpu_fdinfo(4, gtt="10485760 KiB"))
        self.assertEqual(
            app.amd_fdinfo_procs(proc_root=self.proc)[970]["0000:0b:00.0"]["gtt"],
            10240.0)

    def test_process_dying_mid_scan_is_skipped(self):
        # The fdinfo vanishes between listdir and the read — the common case on a
        # busy host. It must cost that one client, not the whole sample.
        self._fd(900, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(1, gtt="10485760 KiB"))
        self._fd(901, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(2, gtt="2097152 KiB"))
        real_open = open

        def gone(path, *a, **k):
            if isinstance(path, str) and "/900/fdinfo/" in path:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", side_effect=gone):
            got = app.amd_fdinfo_procs(proc_root=self.proc)
        self.assertEqual(list(got), [901])

    def test_unreadable_fd_table_is_skipped(self):
        # A pid dir with no readable fd/ (vanished process / permission) is skipped
        # without aborting the scan of the remaining pids.
        os.makedirs(os.path.join(self.proc, "600"))
        self._fd(601, 3, "/dev/dri/renderD128", _amdgpu_fdinfo(13, vram="1024 KiB"))
        self.assertEqual(list(app.amd_fdinfo_procs(proc_root=self.proc)), [601])


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
