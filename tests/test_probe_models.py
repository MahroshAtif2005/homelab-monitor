"""Unit tests for the probe's ollama model reader — the fleet registry's
remote slice. http.client is faked per-path, so these verify the /api/ps +
/api/tags merge (loaded flags + registry detail together, not ps-or-tags)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import probe

PS = {"models": [{"name": "glm-air:80k", "size_vram": 65495378161}]}
TAGS = {"models": [
    {"name": "glm-air:80k", "size": 65495378161, "modified_at": "2026-07-20T10:00:00Z",
     "details": {"family": "glm4moe", "parameter_size": "110.5B", "quantization_level": "Q3_K_M"}},
    {"name": "qwen3:8b", "size": 5000000000, "modified_at": "2026-06-01T10:00:00Z",
     "details": {"family": "qwen3", "parameter_size": "8.2B", "quantization_level": "Q4_K_M"}},
]}


def _fake_http(routes):
    """An http.client.HTTPConnection stand-in serving canned JSON per path."""
    class FakeResp:
        def __init__(self, body, status=200):
            self._b = json.dumps(body).encode() if body is not None else b""
            self.status = status if body is not None else 404
        def read(self):
            return self._b
    class FakeConn:
        def __init__(self, *a, **kw):
            self._path = None
        def request(self, method, path):
            self._path = path
        def getresponse(self):
            return FakeResp(routes.get(self._path))
        def close(self):
            pass
    return FakeConn


class TestReadOllamaModels(unittest.TestCase):
    def test_loaded_and_catalogue_merge(self):
        with mock.patch.object(probe.http.client, "HTTPConnection",
                               _fake_http({"/api/ps": PS, "/api/tags": TAGS})), \
             mock.patch.object(probe.socket, "gethostname", return_value="vader"):
            out = probe.read_ollama_models()
        byname = {m["model"]: m for m in out}
        self.assertEqual(len(out), 2)                       # full catalogue, not just loaded
        g = byname["glm-air:80k"]
        self.assertTrue(g["loaded"])
        self.assertEqual(g["vram_mb"], round(65495378161 / 1048576))
        self.assertEqual(g["param_size"], "110.5B")
        self.assertEqual(g["quant"], "Q3_K_M")
        self.assertEqual(g["size_bytes"], 65495378161)
        self.assertEqual(g["host"], "vader")
        q = byname["qwen3:8b"]
        self.assertFalse(q["loaded"])
        self.assertIsNone(q["vram_mb"])

    def test_resident_but_not_on_disk_still_listed(self):
        ps = {"models": [{"name": "ghost:1b", "size_vram": 1048576000}]}
        with mock.patch.object(probe.http.client, "HTTPConnection",
                               _fake_http({"/api/ps": ps, "/api/tags": {"models": []}})):
            out = probe.read_ollama_models()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["model"], "ghost:1b")
        self.assertTrue(out[0]["loaded"])

    def test_no_ollama_returns_empty(self):
        with mock.patch.object(probe.http.client, "HTTPConnection",
                               side_effect=ConnectionRefusedError):
            self.assertEqual(probe.read_ollama_models(), [])


class TestReadAiModels(unittest.TestCase):
    """The multi-provider slice: every recognised server on the host, not just
    ollama. Only LISTENING ports may be touched, and only a documented response
    shape counts."""

    OPENAI = {"data": [{"id": "mistral-7b-instruct"}, {"id": "qwen2.5-coder-7b"}]}

    def test_openai_server_on_a_listening_port_is_reported(self):
        listen = [{"proto": "tcp", "port": 8000}, {"proto": "tcp", "port": 22}]
        with mock.patch.object(probe.http.client, "HTTPConnection",
                               _fake_http({"/api/ps": None, "/api/tags": None,
                                           "/v1/models": self.OPENAI})), \
             mock.patch.object(probe.socket, "gethostname", return_value="vader"):
            out = probe.read_ai_models(listen)
        self.assertEqual([m["model"] for m in out],
                         ["mistral-7b-instruct", "qwen2.5-coder-7b"])
        self.assertEqual({m["provider"] for m in out}, {"vllm"})   # 8000 → vLLM
        self.assertEqual(out[0]["host"], "vader")
        # /v1/models lists what the server CAN serve, so entries are Idle — the
        # same semantics the hub applies to its own non-ollama servers.
        self.assertFalse(out[0]["loaded"])
        self.assertIsNone(out[0]["vram_mb"])

    def test_ports_that_are_not_listening_are_never_probed(self):
        seen = []

        class Spy:
            def __init__(self, ip, port, timeout=None):
                seen.append(port)
            def request(self, *a):
                raise ConnectionRefusedError
            def getresponse(self):
                raise ConnectionRefusedError
            def close(self):
                pass

        with mock.patch.object(probe.http.client, "HTTPConnection", Spy):
            probe.read_ai_models([{"proto": "tcp", "port": 8000}])
        self.assertEqual(set(seen), {11434, 8000})   # ollama + the one listener

    def test_non_model_server_on_a_known_port_is_ignored(self):
        # Something answers on :8080 with valid JSON that is NOT /v1/models.
        with mock.patch.object(probe.http.client, "HTTPConnection",
                               _fake_http({"/api/ps": None, "/api/tags": None,
                                           "/v1/models": {"message": "hello"}})):
            out = probe.read_ai_models([{"proto": "tcp", "port": 8080}])
        self.assertEqual(out, [])

    def test_udp_listeners_do_not_count(self):
        with mock.patch.object(probe.http.client, "HTTPConnection",
                               _fake_http({"/api/ps": None, "/api/tags": None,
                                           "/v1/models": self.OPENAI})):
            out = probe.read_ai_models([{"proto": "udp", "port": 8000}])
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
