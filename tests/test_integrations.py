"""Unit tests for the experiment-tracking integration API: key auth, run ingest/
query, GPU-cost attachment over the run window, and MLflow sync."""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestRunsApi(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        with app.LOCK:
            app.DB.execute("DELETE FROM runs")
            app.DB.execute("DELETE FROM run_metrics")
            app.DB.execute("DELETE FROM api_keys")
            app.DB.commit()
        app.save_settings({"api_key": "", "kwh_price": "0.30", "currency": "$", "tariff_mode": "single"})

    def tearDown(self):
        app.save_settings({"api_key": "", "mlflow_uri": ""})

    def test_ingest_fail_closed_without_key(self):
        r = self.c.post("/api/runs", json={"name": "x"})
        self.assertEqual(r.status_code, 401)              # no key generated => writes rejected

    def test_bad_key_rejected(self):
        app._gen_api_key()
        r = self.c.post("/api/runs", json={"name": "x"}, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_full_push_pull_flow(self):
        key = app._gen_api_key()
        h = {"Authorization": "Bearer " + key}
        now = int(time.time())
        r = self.c.post("/api/runs", json={"id": "run1", "name": "sft", "source": "jupyter",
                                           "started_at": now - 50, "params": {"lr": 2e-4}}, headers=h)
        self.assertEqual(r.get_json()["id"], "run1")
        self.c.post("/api/runs/run1/metrics",
                    json={"metrics": [{"key": "loss", "value": 0.5, "step": 0},
                                      {"key": "loss", "value": 0.3, "step": 1}]}, headers=h)
        self.c.post("/api/runs/run1/finish", json={"status": "finished", "ended_at": now}, headers=h)
        # list (open read, no key)
        j = self.c.get("/api/runs?range=7d").get_json()
        run = next(x for x in j["runs"] if x["id"] == "run1")
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["source"], "jupyter")
        self.assertEqual(run["metrics_latest"]["loss"], 0.3)        # latest value per key
        # detail
        d = self.c.get("/api/runs/run1").get_json()
        self.assertEqual(len(d["metrics"]["loss"]["values"]), 2)
        self.assertEqual(d["params"], {"lr": 2e-4})

    def test_metrics_unknown_run_404(self):
        key = app._gen_api_key()
        r = self.c.post("/api/runs/nope/metrics",
                        json={"key": "loss", "value": 1.0}, headers={"X-API-Key": key})
        self.assertEqual(r.status_code, 404)

    def test_cost_attached_over_window(self):
        key = app._gen_api_key()
        h = {"Authorization": "Bearer " + key}
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            for i in range(10):                                    # 100s of 200W GPU samples
                app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                               "VALUES(?,?,?,?,?,?)", (now - 100 + i * 10, 80, 8000, 24000, 200, 60))
            app.DB.commit()
        self.c.post("/api/runs", json={"id": "r2", "name": "train", "started_at": now - 100}, headers=h)
        self.c.post("/api/runs/r2/finish", json={"ended_at": now}, headers=h)
        d = self.c.get("/api/runs/r2").get_json()
        self.assertGreater(d["energy_kwh"], 0)                     # GPU energy during the run window
        self.assertGreater(d["cost"], 0)
        self.assertEqual(d["peak_util"], 80)
        self.assertGreater(len(d["resource"]["power_w"]), 0)


class TestApiKeys(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        with app.LOCK:
            app.DB.execute("DELETE FROM api_keys")
            app.DB.execute("DELETE FROM runs")
            app.DB.commit()

    def test_create_list_revoke(self):
        r = self.c.post("/api/integration/keys", json={"name": "laptop"}).get_json()
        kid, key = r["id"], r["key"]
        self.assertTrue(key.startswith("hlm_"))
        j = self.c.get("/api/integration/keys").get_json()
        self.assertEqual(len(j["keys"]), 1)
        self.assertEqual(j["keys"][0]["name"], "laptop")
        self.assertNotIn("key", j["keys"][0])          # plaintext never listed
        self.assertNotIn("key_hash", j["keys"][0])      # hash never exposed
        h = {"Authorization": "Bearer " + key}
        self.assertEqual(self.c.post("/api/runs", json={"name": "x"}, headers=h).status_code, 200)
        self.assertEqual(self.c.delete("/api/integration/keys/" + kid).status_code, 200)
        self.assertEqual(self.c.post("/api/runs", json={"name": "y"}, headers=h).status_code, 401)  # revoked

    def test_expired_key_rejected(self):
        r = self.c.post("/api/integration/keys", json={"name": "old", "expires_in_days": 30}).get_json()
        with app.LOCK:                                  # backdate it past expiry
            app.DB.execute("UPDATE api_keys SET expires_at=? WHERE id=?", (int(time.time()) - 10, r["id"]))
            app.DB.commit()
        resp = self.c.post("/api/runs", json={"name": "x"}, headers={"Authorization": "Bearer " + r["key"]})
        self.assertEqual(resp.status_code, 401)
        j = self.c.get("/api/integration/keys").get_json()
        self.assertTrue(j["keys"][0]["expired"])

    def test_per_key_attribution_and_filter(self):
        ka = self.c.post("/api/integration/keys", json={"name": "A"}).get_json()
        self.c.post("/api/integration/keys", json={"name": "B"}).get_json()
        self.c.post("/api/runs", json={"id": "ra", "name": "r"}, headers={"Authorization": "Bearer " + ka["key"]})
        byname = {k["name"]: k for k in self.c.get("/api/integration/keys").get_json()["keys"]}
        self.assertEqual(byname["A"]["runs"], 1)
        self.assertEqual(byname["B"]["runs"], 0)
        self.assertIsNotNone(byname["A"]["last_used_at"])
        runs = self.c.get("/api/runs?range=7d&key=" + ka["id"]).get_json()["runs"]
        self.assertEqual(runs[0]["key_name"], "A")
        self.assertEqual(runs[0]["key_id"], ka["id"])

    def test_legacy_single_key_migrated(self):
        app.save_settings({"api_key": "hlm_legacytestkey123"})
        app._apply_schema_migrations(app.DB)             # migrates the setting into api_keys
        resp = self.c.post("/api/runs", json={"name": "x"},
                           headers={"Authorization": "Bearer hlm_legacytestkey123"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(app.get_settings().get("api_key", ""), "")   # setting cleared


class TestMlflowSync(unittest.TestCase):
    def tearDown(self):
        app.save_settings({"mlflow_uri": ""})
        with app.LOCK:
            app.DB.execute("DELETE FROM runs WHERE source='mlflow'")
            app.DB.commit()

    def test_pull_mirrors_runs_idempotently(self):
        app.save_settings({"mlflow_uri": "http://mlflow.test"})

        def fake_mlf(method, path, payload=None, params=None, timeout=15):
            if "experiments/search" in path:
                return {"experiments": [{"experiment_id": "1"}]}
            if "runs/search" in path:
                return {"runs": [{"info": {"run_id": "abc", "run_name": "nightly-eval",
                                           "status": "FINISHED", "start_time": 1000000, "end_time": 2000000},
                                  "data": {"metrics": [{"key": "acc"}],
                                           "params": [{"key": "lr", "value": "0.1"}], "tags": []}}]}
            if "metrics/get-history" in path:
                return {"metrics": [{"timestamp": 1500000, "step": 1, "value": 0.81}]}
            return {}

        with patch("app._mlf", side_effect=fake_mlf):
            n1 = app.sync_mlflow()
            n2 = app.sync_mlflow()                                 # second sync: idempotent upsert
        self.assertEqual(n1, 1)
        with app.LOCK:
            rows = app.DB.execute("SELECT name,status,started_at FROM runs WHERE source='mlflow' AND ext_id='abc'").fetchall()
            mcount = app.DB.execute("SELECT COUNT(*) FROM run_metrics WHERE key='acc'").fetchone()[0]
        self.assertEqual(len(rows), 1)                            # not duplicated on re-sync
        self.assertEqual(rows[0][0], "nightly-eval")
        self.assertEqual(rows[0][1], "finished")
        self.assertEqual(rows[0][2], 1000)                        # 1000000 ms -> 1000 s
        self.assertEqual(mcount, 1)                               # history replaced, not appended


if __name__ == "__main__":
    unittest.main()
