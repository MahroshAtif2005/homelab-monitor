"""Unit tests for the network I/O endpoint (issue #30)."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestNetwork(unittest.TestCase):
    def test_rates_and_top_talkers(self):
        now = int(time.time())
        t0, t1 = now - 40, now - 30           # 10s apart, inside a 1h range
        with app.LOCK:
            app.DB.execute("DELETE FROM net_samples")
            app.DB.executemany(
                "INSERT INTO net_samples(ts,iface,bytes_in,bytes_out) VALUES(?,?,?,?)",
                [(t0, "eth0", 0, 0), (t1, "eth0", 1000, 500),          # 100/50 B/s
                 (t0, "@ollama", 0, 0), (t1, "@ollama", 5000, 1000)])  # 6000 B total
            app.DB.commit()
        j = app.app.test_client().get("/api/network?range=1h").get_json()

        eth = [x for x in j["ifaces"] if x["iface"] == "eth0"]
        self.assertTrue(eth, "eth0 should appear as a host NIC")
        self.assertEqual(max(eth[0]["in"]), 100)    # 1000 bytes / 10 s
        self.assertEqual(max(eth[0]["out"]), 50)

        # '@'-tagged rows are container talkers, never host NICs
        self.assertFalse(any(x["iface"].startswith("@") for x in j["ifaces"]))
        oll = next(t for t in j["talkers"] if t["name"] == "ollama")
        self.assertEqual(oll["bytes_in"], 5000)
        self.assertEqual(oll["total"], 6000)

    def test_counter_reset_is_ignored(self):
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM net_samples")
            app.DB.executemany(
                "INSERT INTO net_samples(ts,iface,bytes_in,bytes_out) VALUES(?,?,?,?)",
                [(now - 30, "eth0", 9000, 9000), (now - 20, "eth0", 10, 10)])  # reboot
            app.DB.commit()
        j = app.app.test_client().get("/api/network?range=1h").get_json()
        # the negative delta from the reset must not invent a huge spike
        for nic in j["ifaces"]:
            self.assertTrue(all(v >= 0 for v in nic["in"] + nic["out"]))


if __name__ == "__main__":
    unittest.main()
