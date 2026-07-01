"""Unit tests for the Disk I/O throughput collector."""
import os
import sys
import unittest
from unittest.mock import patch, mock_open

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _diskstats_blob(read_sectors, write_sectors, dev="sda"):
    # /proc/diskstats format:
    # 0: major, 1: minor, 2: dev_name, 3: reads_completed, 4: reads_merged,
    # 5: sectors_read, 6: time_reading, 7: writes_completed, 8: writes_merged,
    # 9: sectors_written
    return f" 8 0 {dev} 1 0 {read_sectors} 0 1 0 {write_sectors} 0 0 0 0 0 0 0\n"


class TestDiskIo(unittest.TestCase):
    def setUp(self):
        app._disk_prev = {}

    def _run(self, time_val, read_sectors, write_sectors, dev="sda"):
        blob = _diskstats_blob(read_sectors, write_sectors, dev)
        with patch("app.os.path.exists", return_value=False), \
             patch("builtins.open", mock_open(read_data=blob)), \
             patch("app.time.time", return_value=time_val):
            return app.collect_disk_io()

    def test_first_poll_returns_empty_items(self):
        # First poll sets the baseline, shouldn't yield throughput yet
        r = self._run(100.0, 1000, 1000)
        self.assertTrue(r["available"])
        self.assertEqual(len(r["items"]), 0)
        self.assertEqual(r["summary"]["total_read_mb_s"], 0.0)

    def test_throughput_delta_calculation(self):
        # Seed baseline
        self._run(100.0, 1000, 2000)
        
        # 10 seconds later: 20480 sectors read (10MB), 40960 sectors written (20MB)
        # Rate: 1 MB/s read, 2 MB/s write
        r = self._run(110.0, 1000 + 20480, 2000 + 40960)
        self.assertEqual(len(r["items"]), 1)
        item = r["items"][0]
        self.assertEqual(item["device"], "sda")
        self.assertAlmostEqual(item["read_mb_s"], 1.0)
        self.assertAlmostEqual(item["write_mb_s"], 2.0)
        
        self.assertAlmostEqual(r["summary"]["total_read_mb_s"], 1.0)
        self.assertAlmostEqual(r["summary"]["total_write_mb_s"], 2.0)

    def test_filters_loop_ram_sr_devices(self):
        blob = (
            _diskstats_blob(1000, 1000, "sda") +
            _diskstats_blob(1000, 1000, "loop0") +
            _diskstats_blob(1000, 1000, "ram0") +
            _diskstats_blob(1000, 1000, "sr0")
        )
        with patch("app.os.path.exists", return_value=False), \
             patch("builtins.open", mock_open(read_data=blob)), \
             patch("app.time.time", return_value=100.0):
            r1 = app.collect_disk_io()
            
        with patch("app.os.path.exists", return_value=False), \
             patch("builtins.open", mock_open(read_data=blob)), \
             patch("app.time.time", return_value=110.0):
            r2 = app.collect_disk_io()
            
        # Only 'sda' should be tracked
        self.assertEqual(len(r2["items"]), 1)
        self.assertEqual(r2["items"][0]["device"], "sda")


if __name__ == "__main__":
    unittest.main()
