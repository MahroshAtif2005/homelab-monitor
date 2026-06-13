"""Unit tests for the defence-in-depth hardening follow-ups (issue #96):
du `--` argv guard, ssh-target dash guard, and webhook URL validation."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestSshTargetDashGuard(unittest.TestCase):
    def test_normal_targets_parse(self):
        self.assertEqual(app._parse_ssh_target("anakin@cloudy"), ("anakin", "cloudy", 22))
        self.assertEqual(app._parse_ssh_target("ardi@192.168.1.12:2222"),
                         ("ardi", "192.168.1.12", 2222))

    def test_user_starting_with_dash_rejected(self):
        self.assertIsNone(app._parse_ssh_target("-oProxyCommand@host"))

    def test_host_starting_with_dash_rejected(self):
        self.assertIsNone(app._parse_ssh_target("user@-evil"))

    def test_ssh_argv_passes_destination_after_double_dash(self):
        fake = MagicMock(returncode=0, stdout=b"ok", stderr=b"")
        with patch("app.subprocess.run", return_value=fake) as run:
            app._ssh("user", "host", 22, "echo ok")
        argv = run.call_args[0][0]
        self.assertIn("--", argv)
        # "--" must immediately precede the user@host destination.
        self.assertEqual(argv[argv.index("--") + 1], "user@host")


class TestDuArgvGuard(unittest.TestCase):
    def test_du_path_passed_after_double_dash(self):
        fake = MagicMock(stdout="", returncode=0)
        # statvfs() in the worker is already wrapped in try/except, so a missing
        # free-space figure is harmless and we don't need to stub it here.
        with patch("app.subprocess.run", return_value=fake) as run:
            app._disk_scan_worker("/", "/rootfs/some/dir")
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "du")
        self.assertIn("--", argv)
        # The scanned path must come straight after "--" so it can't be a flag.
        self.assertEqual(argv[argv.index("--") + 1], "/rootfs/some/dir")


class TestWebhookUrlValidation(unittest.TestCase):
    def test_empty_value_allowed(self):
        self.assertIsNone(app._validate_url_settings({"discord_webhook_url": ""}))
        self.assertIsNone(app._validate_url_settings({"ntfy_server": "   "}))

    def test_missing_key_allowed(self):
        self.assertIsNone(app._validate_url_settings({"alerts_enabled": "1"}))

    def test_valid_http_and_https_accepted(self):
        self.assertIsNone(app._validate_url_settings(
            {"discord_webhook_url": "https://discord.com/api/webhooks/1/abc"}))
        self.assertIsNone(app._validate_url_settings(
            {"ntfy_server": "http://ntfy.lan:8080"}))

    def test_non_http_scheme_rejected(self):
        err = app._validate_url_settings({"discord_webhook_url": "file:///etc/passwd"})
        self.assertIsNotNone(err)
        self.assertIn("discord_webhook_url", err)

    def test_missing_host_rejected(self):
        self.assertIsNotNone(app._validate_url_settings({"ntfy_server": "https://"}))

    def test_garbage_rejected(self):
        self.assertIsNotNone(app._validate_url_settings({"ntfy_server": "not a url"}))


if __name__ == "__main__":
    unittest.main()
