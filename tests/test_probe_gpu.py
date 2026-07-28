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


class TestNvidiaCards(unittest.TestCase):
    def test_parses_every_card(self):
        out = ("0, NVIDIA GeForce RTX 3090, 12, 20990, 24576, 118.53, 45\n"
               "1, NVIDIA GeForce RTX 3090, 0, 3, 24576, 21.06, 38\n"
               "2, NVIDIA GeForce RTX 3090, 97, 24001, 24576, 279.80, 71\n")
        with mock.patch("probe.subprocess.run", return_value=_smi_result(out)):
            cards = probe._nvidia_cards()
        self.assertEqual(len(cards), 3)
        self.assertEqual(cards[0], {"idx": 0, "name": "NVIDIA GeForce RTX 3090",
                                    "util": 12, "mem_used": 20990, "mem_total": 24576,
                                    "power": 118, "temp": 45, "vendor": "nvidia"})
        self.assertEqual(cards[2]["idx"], 2)
        self.assertEqual(cards[2]["power"], 279)

    def test_na_fields_degrade_to_zero(self):
        out = "0, Quadro P2000, [N/A], 512, 5120, [Not Supported], [N/A]\n"
        with mock.patch("probe.subprocess.run", return_value=_smi_result(out)):
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
        with mock.patch("probe.subprocess.run", return_value=_smi_result(out)):
            procs = probe._nvidia_procs()
        self.assertEqual([p["pid"] for p in procs], [77, 1234])
        self.assertEqual(procs[1], {"pid": 1234, "name": "ollama", "mem": 20480})

    def test_comma_in_process_path_survives(self):
        # process_name is a free path — a comma inside it must not shift fields.
        out = "50, /srv/my,dir/llama-server, 4096\n"
        with mock.patch("probe.subprocess.run", return_value=_smi_result(out)):
            procs = probe._nvidia_procs()
        self.assertEqual(procs[0]["name"], "llama-server")
        self.assertEqual(procs[0]["mem"], 4096)

    def test_windows_backslash_path(self):
        out = "88, C:\\Program Files\\Ollama\\ollama.exe, 8192\n"
        with mock.patch("probe.subprocess.run", return_value=_smi_result(out)):
            procs = probe._nvidia_procs()
        self.assertEqual(procs[0]["name"], "ollama.exe")

    def test_unavailable_returns_empty(self):
        with mock.patch("probe.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(probe._nvidia_procs(), [])


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
