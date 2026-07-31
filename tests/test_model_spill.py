"""Model RAM-spill visibility + per-model caller attribution.

Covers the chain the feature rides on:
  • probe_ollama parses /api/ps `size` vs `size_vram` into (name, vram, ram_spill)
  • probe_models normalizes 2-wide probe rows (spill unknown) to 3-wide
  • query_model_summary / query_model_runs / query_model_callers derive the
    spill split, load sessions ("runs") and time-overlap caller attribution
  • the models-table ALTER migration adds `ram` to a pre-spill database
"""
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import probes
from backend.db.repos import schema as schema_repo
from backend.db.repos import system as system_repo

MB = 1048576


def _models_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE models(ts INTEGER, service TEXT, model TEXT, vram REAL, ram REAL)")
    db.execute("CREATE TABLE edges(ts INTEGER, caller TEXT, server TEXT, conns INTEGER)")
    db.commit()
    return db


class TestProbeOllamaSpill(unittest.TestCase):
    def test_partial_spill_split(self):
        ps = {"models": [{"name": "big:30b", "size": 20000 * MB, "size_vram": 15000 * MB}]}
        with patch("backend.probes._http_json", return_value=ps):
            rows = probes.probe_ollama("1.2.3.4")
        self.assertEqual(rows, [("big:30b", 15000.0, 5000.0)])

    def test_fully_on_gpu_reports_zero_spill(self):
        ps = {"models": [{"name": "fits:8b", "size": 5000 * MB, "size_vram": 5000 * MB}]}
        with patch("backend.probes._http_json", return_value=ps):
            rows = probes.probe_ollama("1.2.3.4")
        self.assertEqual(rows, [("fits:8b", 5000.0, 0.0)])

    def test_fully_on_cpu_is_still_loaded(self):
        # size_vram=0 used to collapse to the idle fallback; a fully-CPU-resident
        # model is very much loaded — the worst spill there is.
        ps = {"models": [{"name": "cpu:70b", "size": 40000 * MB, "size_vram": 0}]}
        with patch("backend.probes._http_json", return_value=ps):
            rows = probes.probe_ollama("1.2.3.4")
        self.assertEqual(rows, [("cpu:70b", 0.0, 40000.0)])

    def test_idle_catalogue_fallback_unchanged(self):
        def fake(ip, port, path, timeout=2):
            return {"models": []} if path == "/api/ps" else {"models": [{"name": "a"}, {"name": "b"}]}
        with patch("backend.probes._http_json", side_effect=fake):
            rows = probes.probe_ollama("1.2.3.4")
        self.assertEqual(rows, [("a", None), ("b", None)])


class TestProbeModelsNormalize(unittest.TestCase):
    CT = {"name": "srv", "image": "x", "ip": "1.2.3.4"}

    def test_two_wide_rows_get_unknown_spill(self):
        with patch.object(probes, "_match_probe",
                          return_value=lambda ip: [("m1", 100), ("m2", None)]):
            rows = probes.probe_models(self.CT)
        self.assertEqual(rows, [("m1", 100, None), ("m2", None, None)])

    def test_three_wide_rows_pass_through(self):
        with patch.object(probes, "_match_probe",
                          return_value=lambda ip: [("m1", 100.0, 40.0)]):
            rows = probes.probe_models(self.CT)
        self.assertEqual(rows, [("m1", 100.0, 40.0)])

    def test_collapsed_catalogue_is_three_wide(self):
        idle = [(f"m{i}", None) for i in range(probes.CATALOG_MAX + 1)]
        with patch.object(probes, "_match_probe", return_value=lambda ip: idle):
            rows = probes.probe_models(self.CT)
        self.assertEqual(rows, [(f"{probes.CATALOG_MAX + 1} models available", None, None)])


class TestModelRunsAndSummary(unittest.TestCase):
    def setUp(self):
        self.db = _models_db()
        rows = []
        # Run 1: fully on GPU, ts 1000..1100 every 10 s, with one benign 30 s hiccup.
        for ts in (1000, 1010, 1020, 1050, 1060, 1070, 1080, 1090, 1100):
            rows.append((ts, "ollama", "A", 15000, 0))
        # Unloaded for 500 s (> gap) → new run. Run 2 spills at one sample.
        for ts in (1600, 1610, 1620):
            rows.append((ts, "ollama", "A", 14000, 2000 if ts == 1610 else 0))
        # Another model with unknown spill (NULL ram) — never counted as spilled.
        rows.append((1000, "vllm", "B", 8000, None))
        self.db.executemany("INSERT INTO models VALUES(?,?,?,?,?)", rows)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_runs_split_on_gap_and_flag_spill(self):
        got = {(s, m): (n, sp, pr)
               for s, m, n, sp, pr in system_repo.query_model_runs(0, 90, conn=self.db)}
        self.assertEqual(got[("ollama", "A")], (2, 1, 2000))   # 2 runs, 1 spilled
        self.assertEqual(got[("vllm", "B")], (1, 0, 0))        # NULL ram → no spill

    def test_summary_reports_peak_spill(self):
        got = {(s, m): (round(pk), round(pr))
               for s, m, pk, av, pr in system_repo.query_model_summary(0, conn=self.db)}
        self.assertEqual(got[("ollama", "A")], (15000, 2000))
        self.assertEqual(got[("vllm", "B")], (8000, 0))


class TestModelCallers(unittest.TestCase):
    def test_overlap_attribution(self):
        db = _models_db()
        db.executemany("INSERT INTO models VALUES(?,?,?,?,?)", [
            (1000, "ollama", "A", 15000, 0),
            (1010, "ollama", "A", 15000, 0),
        ])
        db.executemany("INSERT INTO edges VALUES(?,?,?,?)", [
            (1000, "open-webui", "ollama", 2),   # overlaps A at both ticks
            (1010, "open-webui", "ollama", 1),
            (1010, "pipeline", "ollama", 1),     # overlaps A once
            (2000, "open-webui", "ollama", 1),   # model unloaded — no credit
            (1000, "someapp", "vllm", 1),        # other server — no credit
        ])
        db.commit()
        got = {(s, m, c): n for s, m, c, n in system_repo.query_model_callers(0, conn=db)}
        self.assertEqual(got, {("ollama", "A", "open-webui"): 2,
                               ("ollama", "A", "pipeline"): 1})
        db.close()


class TestModelsRamMigration(unittest.TestCase):
    def test_alter_adds_ram_to_old_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE models(ts INTEGER, service TEXT, model TEXT, vram REAL)")
        conn.execute("INSERT INTO models VALUES(1, 's', 'm', 100)")
        schema_repo.apply_schema_migrations(
            conn, "CREATE TABLE IF NOT EXISTS models(ts INTEGER, service TEXT, model TEXT, vram REAL, ram REAL);",
            (), (), (), (), (), models_migrations=("ram REAL",))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(models)")]
        self.assertIn("ram", cols)
        # Existing rows read back with NULL spill (= unknown), not 0.
        self.assertEqual(conn.execute("SELECT ram FROM models").fetchone()[0], None)
        conn.close()


if __name__ == "__main__":
    unittest.main()
