"""Unit tests for host rename (hosts are keyed by name; renaming must move the
row, the in-memory poll cache and per-host history in one step)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _add_host(name, ssh_target="u@h", tags="lab"):
    with app.LOCK:
        app.DB.execute("DELETE FROM hosts WHERE name IN (?,?)", (name, name))
        app.DB.execute("INSERT INTO hosts(name, ssh_target, tags, added_at) VALUES(?,?,?,0)",
                       (name, ssh_target, tags))
        app.DB.commit()


def _cleanup(*names):
    with app.LOCK:
        for n in names:
            app.DB.execute("DELETE FROM hosts WHERE name=?", (n,))
            app.DB.execute("DELETE FROM runs WHERE host=?", (n,))
            app.DB.execute("DELETE FROM bench_runs WHERE host=?", (n,))
        app.DB.commit()
    with app.HOST_DATA_LOCK:
        for n in names:
            app.HOST_DATA.pop(n, None)


class TestRenameHost(unittest.TestCase):
    def tearDown(self):
        _cleanup("vadr", "vader", "other")

    def test_rename_moves_row_with_target_and_tags(self):
        _add_host("vadr", ssh_target="anakin@1.2.3.4", tags="gpu")
        host, err = app.rename_host("vadr", "vader")
        self.assertIsNone(err)
        self.assertEqual(host, {"name": "vader", "ssh_target": "anakin@1.2.3.4", "tags": "gpu"})
        names = [h["name"] for h in app.list_hosts()]
        self.assertIn("vader", names)
        self.assertNotIn("vadr", names)

    def test_rename_rekeys_poll_cache(self):
        _add_host("vadr")
        with app.HOST_DATA_LOCK:
            app.HOST_DATA["vadr"] = {"data": {"host": {"cpu": 1}}, "at": 123}
        app.rename_host("vadr", "vader")
        with app.HOST_DATA_LOCK:
            self.assertNotIn("vadr", app.HOST_DATA)
            self.assertEqual(app.HOST_DATA["vader"]["at"], 123)

    def test_rename_moves_history(self):
        _add_host("vadr")
        with app.LOCK:
            app.DB.execute("INSERT INTO runs(id, name, source, status, started_at, host, created_at) "
                           "VALUES('r1','x','api','done',0,'vadr',0)")
            app.DB.execute("INSERT INTO bench_runs(id, host, model, status, created_at) "
                           "VALUES('b1','vadr','m','done',0)")
            app.DB.commit()
        app.rename_host("vadr", "vader")
        with app.LOCK:
            self.assertEqual(app.DB.execute("SELECT host FROM runs WHERE id='r1'").fetchone()[0], "vader")
            self.assertEqual(app.DB.execute("SELECT host FROM bench_runs WHERE id='b1'").fetchone()[0], "vader")

    def test_rename_collision_rejected(self):
        _add_host("vadr")
        _add_host("other")
        host, err = app.rename_host("vadr", "other")
        self.assertIsNone(host)
        self.assertIn("already exists", err)

    def test_rename_validates_new_name(self):
        _add_host("vadr")
        for bad in ("", "has space", "-leading", "x" * 40):
            host, err = app.rename_host("vadr", bad)
            self.assertIsNone(host, bad)
        host, err = app.rename_host("vadr", "local")
        self.assertIsNone(host)
        self.assertIn("reserved", err)

    def test_rename_unknown_host_404s(self):
        host, err = app.rename_host("ghost", "vader")
        self.assertIsNone(host)
        self.assertIn("No host named", err)


class TestRenamePatchApi(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def tearDown(self):
        _cleanup("vadr", "vader")

    def test_patch_name_renames(self):
        _add_host("vadr", ssh_target="anakin@1.2.3.4")
        r = self.client.patch("/api/hosts/vadr", json={"name": "vader"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["host"]["name"], "vader")

    def test_patch_name_and_target_together(self):
        _add_host("vadr")
        r = self.client.patch("/api/hosts/vadr", json={"name": "vader", "ssh_target": "anakin@5.6.7.8"})
        self.assertEqual(r.status_code, 200)
        h = r.get_json()["host"]
        self.assertEqual((h["name"], h["ssh_target"]), ("vader", "anakin@5.6.7.8"))

    def test_patch_unknown_host_is_404(self):
        r = self.client.patch("/api/hosts/ghost", json={"name": "vader"})
        self.assertEqual(r.status_code, 404)

    def test_patch_empty_body_is_400(self):
        _add_host("vadr")
        r = self.client.patch("/api/hosts/vadr", json={})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
