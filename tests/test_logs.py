"""Unit tests for the container logs endpoint guard (issue #28)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestContainerNameGuard(unittest.TestCase):
    def test_accepts_normal_names(self):
        for n in ("ollama", "immich_server", "langfuse-stack-redis-1", "a.b_c-1"):
            self.assertTrue(app._CT_NAME_RE.match(n), n)

    def test_rejects_injection_and_paths(self):
        for n in ("../etc", "a/b", "a b", "", "-leading", "name;rm", "json?all=1"):
            self.assertIsNone(app._CT_NAME_RE.match(n), n)

    def test_endpoint_400_on_bad_name(self):
        # A leading-dash name reaches the handler but fails the guard before any
        # Docker socket access -> 400 (not 500).
        r = app.app.test_client().get("/api/containers/-bad/logs")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
