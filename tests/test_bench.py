"""Unit tests for the LLM Benchmark Lab engine (backend/bench.py).

Pure helpers + the injected-I/O orchestration are exercised here without a GPU or
a live ollama — every external effect is a fake callable."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import bench


class TestPlanCtx(unittest.TestCase):
    def test_default_ladder_clamped_to_native(self):
        out = bench.plan_ctx_list(None, 8192)
        self.assertTrue(all(c <= 8192 for c in out))
        self.assertIn(8192, out)          # native always probed
        self.assertEqual(out, sorted(out))

    def test_explicit_list_filtered_and_deduped(self):
        out = bench.plan_ctx_list([4096, 4096, 100, 999999], 32768)
        self.assertIn(4096, out)
        self.assertNotIn(100, out)        # below floor
        self.assertNotIn(999999, out)     # above native

    def test_no_native_uses_requested(self):
        self.assertEqual(bench.plan_ctx_list([2048, 4096], None), [2048, 4096])

    def test_cap_length(self):
        many = list(range(1000, 40000, 500))
        self.assertLessEqual(len(bench.plan_ctx_list(many, None)), bench.MAX_CTX_PER_MODEL)

    def test_hard_ceiling_when_native_unknown(self):
        # A user-supplied absurd context is dropped even with no native ctx known.
        out = bench.plan_ctx_list([4096, 10_000_000], None)
        self.assertIn(4096, out)
        self.assertTrue(all(c <= bench.MAX_CTX for c in out))
        self.assertNotIn(10_000_000, out)

    def test_below_floor_dropped(self):
        self.assertNotIn(64, bench.plan_ctx_list([64, 4096], None))


class TestTiming(unittest.TestCase):
    def test_tps_and_load(self):
        resp = {"eval_count": 100, "eval_duration": 2_000_000_000,       # 2s -> 50 tok/s
                "prompt_eval_count": 500, "prompt_eval_duration": 1_000_000_000,  # 500 tok/s
                "load_duration": 3_000_000_000, "total_duration": 6_000_000_000}
        t = bench.parse_generate_timing(resp)
        self.assertEqual(t["gen_tps"], 50.0)
        self.assertEqual(t["prompt_tps"], 500.0)
        self.assertEqual(t["load_ms"], 3000.0)
        self.assertEqual(t["total_ms"], 6000.0)
        self.assertEqual(t["ttft_ms"], 4000.0)   # load 3000 + prompt 1000

    def test_missing_fields_degrade(self):
        t = bench.parse_generate_timing({})
        self.assertIsNone(t["gen_tps"])
        self.assertIsNone(t["load_ms"])

    def test_none_response(self):
        t = bench.parse_generate_timing(None)
        self.assertIsNone(t["gen_tps"])


class TestFit(unittest.TestCase):
    def test_full_vram(self):
        self.assertEqual(bench.classify_fit(1000, 1000), "vram")

    def test_partial(self):
        self.assertEqual(bench.classify_fit(1000, 600), "partial")

    def test_cpu(self):
        self.assertEqual(bench.classify_fit(1000, 0), "cpu")

    def test_resident_split(self):
        ps = {"models": [{"name": "qwen3:30b", "size": 20 * 1048576, "size_vram": 15 * 1048576}]}
        r = bench.resident_for(ps, "qwen3:30b")
        self.assertEqual(r["total_size_mb"], 20)
        self.assertEqual(r["vram_mb"], 15)
        self.assertEqual(r["ram_offload_mb"], 5)
        self.assertEqual(r["fit"], "partial")

    def test_resident_absent(self):
        self.assertIsNone(bench.resident_for({"models": []}, "x"))


class TestSmi(unittest.TestCase):
    def test_parse(self):
        text = ("0, NVIDIA GeForce RTX 3090, 695, 24576, 41.7\n"
                "1, Quadro P2000, 4000, 5120, 12.0\n"
                "bad line without commas\n")
        g = bench.parse_smi_gpus(text)
        self.assertEqual(len(g), 2)
        self.assertEqual(g[0]["name"], "NVIDIA GeForce RTX 3090")
        self.assertEqual(g[0]["mem_used"], 695.0)
        self.assertEqual(g[1]["idx"], 1)

    def test_na_fields(self):
        g = bench.parse_smi_gpus("0, GPU, [N/A], [N/A], [N/A]")
        self.assertEqual(g[0]["mem_used"], None)

    def test_attribute_gpu_picks_grown_card(self):
        before = [{"idx": 0, "name": "3090", "mem_used": 700},
                  {"idx": 1, "name": "P2000", "mem_used": 4000}]
        after = [{"idx": 0, "name": "3090", "mem_used": 18000},
                 {"idx": 1, "name": "P2000", "mem_used": 4010}]
        landed = bench.attribute_gpu(before, after)
        self.assertEqual(len(landed), 1)            # P2000 delta below min
        self.assertEqual(landed[0]["idx"], 0)
        self.assertGreater(landed[0]["delta_mb"], 17000)

    def test_gpu_advice_warns_on_small_card(self):
        gpus = [{"idx": 0, "name": "3090", "mem_total": 24576},
                {"idx": 1, "name": "P2000", "mem_total": 5120}]
        landed = [{"idx": 0, "name": "3090", "delta_mb": 12000},
                  {"idx": 1, "name": "P2000", "delta_mb": 3000}]
        self.assertIn("P2000", bench.gpu_advice(landed, gpus))
        # single card -> no advice
        self.assertIsNone(bench.gpu_advice(landed, gpus[:1]))


class TestSummarize(unittest.TestCase):
    def test_picks_best_and_recommended_ctx(self):
        points = [
            {"ctx": 2048, "gen_tps": 60, "prompt_tps": 400, "load_ms": 3000,
             "fit": "vram", "vram_mb": 16000, "ram_offload_mb": 0, "total_size_mb": 16000, "ok": True},
            {"ctx": 8192, "gen_tps": 55, "prompt_tps": 380, "load_ms": 3100,
             "fit": "vram", "vram_mb": 17000, "ram_offload_mb": 0, "total_size_mb": 16000, "ok": True},
            {"ctx": 32768, "gen_tps": 20, "prompt_tps": 300, "load_ms": 4000,
             "fit": "partial", "vram_mb": 20000, "ram_offload_mb": 4000, "total_size_mb": 16000, "ok": True},
        ]
        s = bench.summarize(points)
        self.assertEqual(s["best_gen_tps"], 60)
        self.assertEqual(s["best_ctx"], 2048)
        self.assertEqual(s["max_fit_ctx"], 8192)      # largest fully-in-VRAM
        self.assertEqual(s["recommended_ctx"], 8192)
        self.assertEqual(s["fit"], "vram")

    def test_all_spill_reports_least_offload(self):
        points = [
            {"ctx": 4096, "gen_tps": 10, "fit": "partial", "vram_mb": 20000,
             "ram_offload_mb": 6000, "total_size_mb": 24000, "ok": True},
            {"ctx": 2048, "gen_tps": 12, "fit": "partial", "vram_mb": 20000,
             "ram_offload_mb": 4000, "total_size_mb": 24000, "ok": True},
        ]
        s = bench.summarize(points)
        self.assertEqual(s["recommended_ctx"], 2048)   # least offloaded
        self.assertEqual(s["fit"], "partial")

    def test_no_ok_points(self):
        s = bench.summarize([{"ctx": 2048, "ok": False}])
        self.assertIsNone(s["best_gen_tps"])


class TestOrchestration(unittest.TestCase):
    def _fakes(self, size_mb=16000, vram_mb=16000):
        calls = {"gen": 0}

        def gen_fn(model, prompt, num_ctx, num_predict, num_gpu, keep_alive):
            calls["gen"] += 1
            # warm-up (num_predict small) carries the load time; measured carries tok/s
            if num_predict <= 8:
                return {"load_duration": 3_000_000_000, "eval_count": 8,
                        "eval_duration": 200_000_000}
            return {"eval_count": 128, "eval_duration": 2_000_000_000,
                    "prompt_eval_count": 512, "prompt_eval_duration": 1_000_000_000,
                    "load_duration": 0, "total_duration": 3_000_000_000}

        def ps_fn():
            return {"models": [{"name": "m", "size": size_mb * 1048576,
                                "size_vram": vram_mb * 1048576}]}

        def smi_fn():
            # grow card 0 after load
            grow = 0 if calls["gen"] == 0 else 17000
            return "0, RTX 3090, %d, 24576, 40\n1, P2000, 4000, 5120, 12" % (700 + grow)

        return gen_fn, ps_fn, smi_fn

    def test_end_to_end(self):
        gen_fn, ps_fn, smi_fn = self._fakes()
        cfg = {"ctx_list": [2048, 4096], "gen_tokens": 128}
        seen = []
        points, summary = bench.run_model_benchmark(
            "m", cfg, gen_fn, ps_fn, smi_fn, on_point=seen.append,
            sleep_fn=lambda s: None, meta={"ctx": 8192})
        self.assertEqual(len(points), 2)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(p["ok"] for p in points))
        self.assertEqual(points[0]["gen_tps"], 64.0)      # 128 / 2s
        self.assertEqual(points[0]["load_ms"], 3000.0)    # from warm-up
        self.assertEqual(points[0]["fit"], "vram")
        self.assertEqual(summary["best_gen_tps"], 64.0)

    def test_cancel_stops_early(self):
        gen_fn, ps_fn, smi_fn = self._fakes()
        cfg = {"ctx_list": [2048, 4096, 8192]}
        state = {"n": 0}

        def should_cancel():
            state["n"] += 1
            return state["n"] > 2      # cancel after a couple of checks

        points, _ = bench.run_model_benchmark(
            "m", cfg, gen_fn, ps_fn, smi_fn, should_cancel=should_cancel,
            sleep_fn=lambda s: None, meta={"ctx": 8192})
        self.assertLess(len(points), 3)

    def test_one_bad_ctx_does_not_sink_run(self):
        def gen_fn(model, prompt, num_ctx, num_predict, num_gpu, keep_alive):
            if num_ctx == 4096:
                raise RuntimeError("boom")
            if num_predict <= 8:
                return {"load_duration": 1_000_000_000}
            return {"eval_count": 100, "eval_duration": 1_000_000_000}
        points, summary = bench.run_model_benchmark(
            "m", {"ctx_list": [2048, 4096]}, gen_fn, lambda: {}, lambda: "",
            sleep_fn=lambda s: None, meta={"ctx": 8192})
        self.assertEqual(len(points), 2)
        oks = [p for p in points if p["ok"]]
        bad = [p for p in points if not p["ok"]]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(bad), 1)
        self.assertIn("boom", bad[0]["err"])


if __name__ == "__main__":
    unittest.main()
