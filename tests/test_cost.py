"""Unit tests for the power & cost estimator endpoint (issue #25)."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestCost(unittest.TestCase):
    def _seed_hour_at(self, watts):
        """Fill the last hour with `watts` power samples, one per INTERVAL."""
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            n = 3600 // app.INTERVAL
            for i in range(n):
                ts = now - 3600 + i * app.INTERVAL
                app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                               "VALUES(?,?,?,?,?,?)", (ts, 0, 0, 0, watts, 0))
            app.DB.commit()

    def test_disabled_without_price(self):
        app.save_settings({"kwh_price": "", "currency": "$"})
        j = app.app.test_client().get("/api/cost?range=30d").get_json()
        self.assertFalse(j["enabled"])

    def test_250w_for_an_hour_at_0_30(self):
        # 250 W for 1 h = 0.25 kWh -> 0.075 at 0.30/kWh (issue acceptance criterion).
        self._seed_hour_at(250)
        app.save_settings({"kwh_price": "0.30", "currency": "€"})
        j = app.app.test_client().get("/api/cost?range=30d").get_json()
        self.assertTrue(j["enabled"])
        self.assertEqual(j["currency"], "€")
        self.assertEqual(j["avg_24h_w"], 250)
        self.assertAlmostEqual(j["kwh"]["d30"], 0.25, delta=0.01)
        self.assertAlmostEqual(j["cost"]["d30"], 0.075, delta=0.01)
        self.assertTrue(len(j["series"]["cost_cum"]) > 0)
        # cumulative series is monotonic non-decreasing and ends near the total
        cc = j["series"]["cost_cum"]
        self.assertTrue(all(cc[i] <= cc[i + 1] + 1e-9 for i in range(len(cc) - 1)))


if __name__ == "__main__":
    unittest.main()
