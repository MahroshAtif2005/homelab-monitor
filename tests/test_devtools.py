"""Unit tests for DS/AI tool discovery (Jupyter, TensorBoard, MLflow, W&B, …)."""
import os
import re
import sys
import unittest
from io import BytesIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestExtractPort(unittest.TestCase):
    def test_equals_and_spaced(self):
        self.assertEqual(app._extract_port(["jupyter-lab", "--port=8890"]), 8890)
        self.assertEqual(app._extract_port(["tensorboard", "--port", "6007"]), 6007)
        self.assertIsNone(app._extract_port(["jupyter-lab", "--no-browser"]))


class TestCollectDevtools(unittest.TestCase):
    def _run(self, procs, gpu_pids):
        def fake_open(path, *a, **k):
            m = re.match(r"/proc/(\d+)/cmdline$", path)
            if m and m.group(1) in procs:
                return BytesIO(procs[m.group(1)])
            raise FileNotFoundError(path)
        with patch("app.os.listdir", return_value=list(procs.keys())), \
             patch("builtins.open", side_effect=fake_open):
            return app.collect_devtools(gpu_pids)

    def test_discovers_tools_and_ports(self):
        procs = {
            "10": b"/usr/bin/python\x00/opt/conda/bin/jupyter-lab\x00--port=8890\x00--no-browser\x00",
            "20": b"python\x00-m\x00tensorboard\x00--logdir\x00runs\x00--port\x006007\x00",
            "30": b"mlflow\x00server\x00--host\x000.0.0.0\x00",
            "40": b"python\x00serve.py\x00",                  # not a tool
        }
        out = self._run(procs, {10: 9000})
        by = {d["kind"]: d for d in out}
        self.assertEqual(by["jupyter"]["port"], 8890)
        self.assertEqual(by["tensorboard"]["port"], 6007)
        self.assertEqual(by["mlflow"]["port"], 5000)         # default when no --port
        self.assertNotIn("serve", by)

    def test_idle_vram_flag(self):
        procs = {"10": b"jupyter-lab\x00"}
        out = self._run(procs, {10: 9216})
        self.assertTrue(out[0]["idle_vram"])                 # holding VRAM -> squatter
        self.assertEqual(out[0]["vram"], 9216)
        out2 = self._run({"11": b"jupyter-lab\x00"}, {})     # no VRAM
        self.assertFalse(out2[0]["idle_vram"])

    def test_dedup_same_kind_and_port(self):
        procs = {"10": b"jupyter-lab\x00--port=8888\x00",
                 "11": b"jupyter-lab\x00--port=8888\x00"}     # worker fork, same port
        out = self._run(procs, {})
        self.assertEqual(len([d for d in out if d["kind"] == "jupyter"]), 1)


if __name__ == "__main__":
    unittest.main()
