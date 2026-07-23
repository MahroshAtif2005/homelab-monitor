"""Unit tests for model intelligence: Ollama /api/show metadata + vLLM/TGI
/metrics live serving telemetry."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestParseProm(unittest.TestCase):
    def test_parse_sums_labels_and_skips_junk(self):
        text = (
            "# HELP foo bar\n"
            "# TYPE foo gauge\n"
            'vllm:num_requests_running{model="x"} 3\n'
            'vllm:num_requests_running{model="y"} 2\n'
            "vllm:kv_cache_usage_perc 0.42\n"
            "not a metric line\n"
            "nan_metric NaN\n"
        )
        d = app._parse_prom(text)
        self.assertEqual(d["vllm:num_requests_running"], 5.0)     # summed across label sets
        self.assertAlmostEqual(d["vllm:kv_cache_usage_perc"], 0.42)
        self.assertNotIn("nan_metric", d)                         # NaN dropped


class TestServingExtract(unittest.TestCase):
    def test_vllm(self):
        metrics = {"vllm:num_requests_running": 2, "vllm:num_requests_waiting": 5,
                   "vllm:kv_cache_usage_perc": 0.73, "vllm:generation_tokens_total": 12345,
                   "vllm:time_to_first_token_seconds_sum": 4.0,
                   "vllm:time_to_first_token_seconds_count": 20}
        o = app._serving_extract(metrics)
        self.assertEqual(o["running"], 2)
        self.assertEqual(o["waiting"], 5)
        self.assertEqual(o["kv_cache_pct"], 73.0)                 # fraction -> percent
        self.assertEqual(o["gen_tokens_total"], 12345)
        self.assertAlmostEqual(o["ttft_avg_s"], 0.2)             # 4.0s / 20

    def test_tgi_aliases(self):
        o = app._serving_extract({"tgi_batch_current_size": 3, "tgi_queue_size": 1})
        self.assertEqual(o["running"], 3)
        self.assertEqual(o["waiting"], 1)


class TestCollectServing(unittest.TestCase):
    def test_tokens_per_sec_from_counter_delta(self):
        ct = {"name": "vllm", "image": "vllm/vllm-openai", "ip": "1.2.3.4", "ports": [8000]}
        app._SERVE_PREV.clear()
        seq = iter([1000.0, 1010.0])     # two ticks, 10 s apart
        with patch("app.time.time", side_effect=lambda: next(seq)):
            with patch("app._http_text", return_value="vllm:generation_tokens_total 100\nvllm:num_requests_running 1\n"):
                r1 = app.collect_serving([ct])
            with patch("app._http_text", return_value="vllm:generation_tokens_total 600\nvllm:num_requests_running 2\n"):
                r2 = app.collect_serving([ct])
        self.assertNotIn("tok_per_s", r1[0])                     # first tick: no previous counter
        self.assertAlmostEqual(r2[0]["tok_per_s"], 50.0, delta=0.1)   # (600-100)/10s
        self.assertEqual(r2[0]["service"], "vllm")

    def test_non_metrics_server_skipped(self):
        ct = {"name": "ollama", "image": "ollama/ollama", "ip": "1.2.3.4", "ports": [11434]}
        with patch("app._http_text", return_value="should not be called"):
            self.assertEqual(app.collect_serving([ct]), [])      # ollama isn't a /metrics hint


class TestOllamaMeta(unittest.TestCase):
    def test_parses_and_caches(self):
        app._OLLAMA_META.clear()
        resp = {"details": {"parameter_size": "8.0B", "quantization_level": "Q4_K_M"},
                "model_info": {"llama.context_length": 8192},
                "capabilities": ["completion", "tools", "vision"]}
        with patch("app._http_post_json", return_value=resp) as pj:
            m1 = app._ollama_meta("1.2.3.4", "llama3")
            m2 = app._ollama_meta("1.2.3.4", "llama3")
        self.assertEqual(m1["param_size"], "8.0B")
        self.assertEqual(m1["quant"], "Q4_K_M")
        self.assertEqual(m1["ctx"], 8192)
        self.assertEqual(set(m1["caps"]), {"tools", "vision"})   # "completion" dropped
        self.assertEqual(pj.call_count, 1)                       # second call served from cache
        self.assertEqual(m1, m2)

    def test_collect_only_fetches_loaded(self):
        app._OLLAMA_META.clear()
        ai = [{"name": "ollama", "image": "ollama/ollama", "ip": "1.2.3.4"}]
        models = [("ollama", "loaded-model", 1234, 0), ("ollama", "idle-model", None, None)]
        resp = {"details": {"parameter_size": "7B", "quantization_level": "Q8_0"},
                "model_info": {}, "capabilities": []}
        with patch("app._http_post_json", return_value=resp) as pj:
            out = app.collect_model_meta(ai, models)
        self.assertIn("loaded-model", out)
        self.assertNotIn("idle-model", out)                     # idle catalogue not paid for
        self.assertEqual(pj.call_count, 1)


if __name__ == "__main__":
    unittest.main()
