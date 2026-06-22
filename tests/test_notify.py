"""Unit tests for alert dispatch and Telegram notifier (issue #27)."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestTelegramSettings(unittest.TestCase):
    def test_public_settings_masks_telegram_token(self):
        with patch.object(app, "get_settings", return_value={
            **app.SETTING_DEFAULTS,
            "telegram_token": "123456:SECRET",
            "telegram_chat_id": "-10099",
        }):
            pub = app._public_settings()
        self.assertNotIn("telegram_token", pub)
        self.assertTrue(pub["telegram_token_set"])
        self.assertEqual(pub["telegram_chat_id"], "-10099")

    def test_telegram_defaults_present(self):
        self.assertIn("telegram_token", app.SETTING_DEFAULTS)
        self.assertIn("telegram_chat_id", app.SETTING_DEFAULTS)
        self.assertIn("telegram_token", app.SETTING_SECRETS)


class TestTelegramNotifier(unittest.TestCase):
    @patch("app._post_json")
    def test_post_to_telegram_uses_markdown(self, mock_post):
        mock_post.return_value = (200, b"{}")
        app._post_to_telegram("tok", "12345", "warning", "Disk full", "Root at 95%")

        url, payload = mock_post.call_args[0]
        self.assertEqual(url, "https://api.telegram.org/bottok/sendMessage")
        self.assertEqual(payload["chat_id"], "12345")
        self.assertEqual(payload["parse_mode"], "Markdown")
        self.assertIn("Disk full", payload["text"])
        self.assertIn("Root at 95%", payload["text"])
        self.assertIn("warning", payload["text"])

    @patch("app._post_json")
    def test_tg_escape_special_chars(self, mock_post):
        mock_post.return_value = (200, b"{}")
        app._post_to_telegram("tok", "1", "info", "a*b_c", "d`e[f")

        _, payload = mock_post.call_args[0]
        self.assertIn(r"a\*b\_c", payload["text"])
        self.assertIn(r"d\`e\[f", payload["text"])

    @patch("app._post_json")
    def test_dispatch_includes_telegram_when_configured(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {
            **app.SETTING_DEFAULTS,
            "telegram_token": "tok",
            "telegram_chat_id": "99",
        }
        results = app.dispatch_alert(s, "info", "Title", "Body")
        channels = [c for c, ok, _ in results]
        self.assertIn("telegram", channels)
        self.assertTrue(all(ok for _, ok, _ in results))

    @patch("app._post_json")
    def test_dispatch_skips_telegram_without_chat_id(self, mock_post):
        s = {**app.SETTING_DEFAULTS, "telegram_token": "tok", "telegram_chat_id": ""}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        channels = [c for c, _, _ in results]
        self.assertNotIn("telegram", channels)
        mock_post.assert_not_called()

    @patch("app._post_json")
    def test_dispatch_reports_telegram_errors(self, mock_post):
        mock_post.side_effect = RuntimeError("network down")
        s = {
            **app.SETTING_DEFAULTS,
            "telegram_token": "tok",
            "telegram_chat_id": "99",
        }
        results = app.dispatch_alert(s, "info", "Title", "Body")
        tg = [r for r in results if r[0] == "telegram"]
        self.assertEqual(len(tg), 1)
        self.assertFalse(tg[0][1])
        self.assertIn("network down", tg[0][2])


class TestAlertHostLabel(unittest.TestCase):
    """Every alert must name the machine it's about (many-hosts UX)."""

    def test_label_prefers_probe_hostname(self):
        with patch.object(app, "LATEST", {"host": {"hostname": "ardi"}}):
            self.assertEqual(app._alert_host_label(), "ardi")

    def test_label_falls_back_to_socket(self):
        with patch.object(app, "LATEST", {}), \
             patch("socket.gethostname", return_value="hubbox"):
            self.assertEqual(app._alert_host_label(), "hubbox")

    @patch("app._post_json")
    def test_dispatch_prefixes_title_with_host(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {**app.SETTING_DEFAULTS, "telegram_token": "tok", "telegram_chat_id": "99"}
        with patch.object(app, "_alert_host_label", return_value="ardi"):
            app.dispatch_alert(s, "warning", "Container immich unhealthy", "down")
        _, payload = mock_post.call_args[0]
        self.assertIn("[ardi]", payload["text"])
        self.assertIn("Container immich unhealthy", payload["text"])

    @patch("app._post_json")
    def test_dispatch_explicit_host_overrides(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {**app.SETTING_DEFAULTS, "telegram_token": "tok", "telegram_chat_id": "99"}
        app.dispatch_alert(s, "info", "Title", "Body", host="webserver")
        _, payload = mock_post.call_args[0]
        self.assertIn("[webserver]", payload["text"])

    @patch("app._post_json")
    def test_dispatch_empty_host_opts_out(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {**app.SETTING_DEFAULTS, "telegram_token": "tok", "telegram_chat_id": "99"}
        app.dispatch_alert(s, "info", "Title", "Body", host="")
        _, payload = mock_post.call_args[0]
        self.assertNotIn("[", payload["text"].split("\n")[0])


class TestOutboundUserAgent(unittest.TestCase):
    """Discord sits behind Cloudflare, which 403s the default Python-urllib
    agent (error 1010). Every outbound notifier POST must carry a real
    User-Agent header. Regression guard for the webhook-test 403."""

    def _capture(self, fn):
        captured = {}

        class _Resp:
            status = 204
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _fake_urlopen(req, timeout=None):
            captured["req"] = req
            return _Resp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            fn()
        return captured["req"]

    def test_post_json_sets_user_agent(self):
        req = self._capture(lambda: app._post_json("https://discord.com/api/webhooks/1/x", {"a": 1}))
        self.assertEqual(req.get_header("User-agent"), app.NOTIFY_USER_AGENT)

    def test_post_text_sets_user_agent(self):
        req = self._capture(lambda: app._post_text("https://ntfy.sh/topic", "hi"))
        self.assertEqual(req.get_header("User-agent"), app.NOTIFY_USER_AGENT)

    def test_post_text_preserves_caller_headers_and_adds_ua(self):
        req = self._capture(lambda: app._post_text(
            "https://ntfy.sh/topic", "hi", headers={"Title": "T", "Priority": "5"}))
        self.assertEqual(req.get_header("Title"), "T")
        self.assertEqual(req.get_header("User-agent"), app.NOTIFY_USER_AGENT)

    def test_user_agent_is_non_default(self):
        self.assertIn("homelab-monitor", app.NOTIFY_USER_AGENT)
        self.assertNotIn("Python-urllib", app.NOTIFY_USER_AGENT)


# ── Email ──────────────────────────────────────────────────────────────────────

class TestEmailSettings(unittest.TestCase):
    def test_email_defaults_present(self):
        for k in ("email_host", "email_port", "email_use_tls", "email_username",
                  "email_password", "email_from", "email_to"):
            self.assertIn(k, app.SETTING_DEFAULTS)
        self.assertIn("email_password", app.SETTING_SECRETS)

    def test_public_settings_masks_email_password(self):
        with patch.object(app, "get_settings", return_value={
            **app.SETTING_DEFAULTS,
            "email_password": "s3cret",
            "email_host": "smtp.example.com",
        }):
            pub = app._public_settings()
        self.assertNotIn("email_password", pub)
        self.assertTrue(pub["email_password_set"])


class TestEmailNotifier(unittest.TestCase):
    @patch("app.smtplib.SMTP")
    def test_send_email_basic(self, mock_smtp):
        mock_ctx = MagicMock()
        mock_smtp.return_value = mock_ctx
        app._send_email("mail.example.com", "587", True, "", "",
                        "alerts@lab", "me@example.com",
                        "warning", "Disk full", "95% used")
        mock_smtp.assert_called_once_with("mail.example.com", 587, timeout=10)
        mock_ctx.starttls.assert_called_once()
        mock_ctx.login.assert_not_called()
        mock_ctx.send_message.assert_called_once()
        msg = mock_ctx.send_message.call_args[0][0]
        self.assertEqual(msg["From"], "alerts@lab")
        self.assertEqual(msg["To"], "me@example.com")
        self.assertEqual(msg["Subject"], "Disk full")
        self.assertIn("95% used", msg.get_content())

    @patch("app.smtplib.SMTP_SSL")
    @patch("app.smtplib.SMTP")
    def test_send_email_with_auth(self, mock_smtp, mock_ssl):
        mock_ctx = MagicMock()
        mock_ssl.return_value = mock_ctx
        app._send_email("mail.example.com", "465", True, "user", "pass",
                        "a@b", "c@d", "info", "Test", "Body")
        mock_ssl.assert_called_once_with("mail.example.com", 465, timeout=10)
        mock_smtp.assert_not_called()
        mock_ctx.starttls.assert_not_called()
        mock_ctx.login.assert_called_once_with("user", "pass")
        mock_ctx.send_message.assert_called_once()

    @patch("app.smtplib.SMTP")
    def test_send_email_without_tls(self, mock_smtp):
        mock_ctx = MagicMock()
        mock_smtp.return_value = mock_ctx
        app._send_email("mail.example.com", "25", False, "", "",
                        "a@b", "c@d", "info", "Test", "Body")
        mock_ctx.starttls.assert_not_called()
        mock_ctx.send_message.assert_called_once()


class TestEmailDispatch(unittest.TestCase):
    @patch("app.smtplib.SMTP")
    def test_dispatch_includes_email_when_configured(self, mock_smtp):
        mock_ctx = MagicMock()
        mock_smtp.return_value = mock_ctx
        s = {**app.SETTING_DEFAULTS,
             "email_host": "smtp.example.com", "email_from": "a@b", "email_to": "c@d"}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        chans = [c for c, ok, _ in results]
        self.assertIn("email", chans)
        self.assertTrue(all(ok for _, ok, _ in results))

    @patch("app.smtplib.SMTP")
    def test_dispatch_skips_email_without_host(self, mock_smtp):
        s = {**app.SETTING_DEFAULTS, "email_host": "", "email_from": "a@b", "email_to": "c@d"}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        chans = [c for c, _, _ in results]
        self.assertNotIn("email", chans)
        mock_smtp.assert_not_called()

    @patch("app.smtplib.SMTP")
    def test_dispatch_reports_email_errors(self, mock_smtp):
        mock_smtp.side_effect = RuntimeError("SMTP timeout")
        s = {**app.SETTING_DEFAULTS,
             "email_host": "smtp.example.com", "email_from": "a@b", "email_to": "c@d"}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        em = [r for r in results if r[0] == "email"]
        self.assertEqual(len(em), 1)
        self.assertFalse(em[0][1])
        self.assertIn("SMTP timeout", em[0][2])


class TestEmailHostLabel(unittest.TestCase):
    @patch("app.smtplib.SMTP")
    def test_email_prefixes_title_with_host(self, mock_smtp):
        mock_ctx = MagicMock()
        mock_smtp.return_value = mock_ctx
        s = {**app.SETTING_DEFAULTS,
             "email_host": "smtp.example.com", "email_from": "a@b", "email_to": "c@d"}
        with patch.object(app, "_alert_host_label", return_value="ardi"):
            app.dispatch_alert(s, "warning", "Container immich unhealthy", "down")
        msg = mock_ctx.send_message.call_args[0][0]
        self.assertIn("[ardi]", msg["Subject"])
        self.assertIn("Container immich unhealthy", msg["Subject"])


class TestEmailValidation(unittest.TestCase):
    @patch.object(app, "get_settings", return_value=app.SETTING_DEFAULTS)
    def test_email_validation_accepts_complete_config(self, _):
        err = app._validate_email_settings({
            "email_host": "smtp.example.com",
            "email_from": "a@b",
            "email_to": "c@d",
            "email_port": "2525",
        })
        self.assertIsNone(err)

    @patch.object(app, "get_settings", return_value=app.SETTING_DEFAULTS)
    def test_email_validation_requires_core_fields(self, _):
        err = app._validate_email_settings({
            "email_from": "a@b",
            "email_to": "c@d",
        })
        self.assertIsNotNone(err)

    @patch.object(app, "get_settings", return_value=app.SETTING_DEFAULTS)
    def test_email_validation_requires_numeric_port(self, _):
        err = app._validate_email_settings({
            "email_host": "smtp.example.com",
            "email_from": "a@b",
            "email_to": "c@d",
            "email_port": "abc",
        })
        self.assertIn("number", err)


class TestNotifyScan(unittest.TestCase):
    @patch.object(app, "get_settings", return_value={**app.SETTING_DEFAULTS, "alerts_enabled": "1", "discord_webhook_url": "https://discord.example/webhook"})
    def test_notify_scan_handles_docker_block(self, _):
        with patch.dict(app.__dict__, {"HEALTH": {"docker": {"available": True, "containers": [{"name": "immich", "status": "ok", "status_text": ""}]}}}, clear=False):
            app._NOTIFIED.clear()
            app.notify_scan()
            self.assertNotIn("container:immich", app._NOTIFIED)


# ── Slack ──────────────────────────────────────────────────────────────────────

class TestSlackSettings(unittest.TestCase):
    def test_slack_defaults_present(self):
        self.assertIn("slack_webhook_url", app.SETTING_DEFAULTS)
        self.assertIn("slack_webhook_url", app.SETTING_SECRETS)

    def test_public_settings_masks_slack_webhook(self):
        with patch.object(app, "get_settings", return_value={
            **app.SETTING_DEFAULTS, "slack_webhook_url": "https://hooks.slack.com/xxx",
        }):
            pub = app._public_settings()
        self.assertNotIn("slack_webhook_url", pub)
        self.assertTrue(pub["slack_webhook_url_set"])


class TestSlackNotifier(unittest.TestCase):
    @patch("app._post_json")
    def test_send_slack_payload(self, mock_post):
        mock_post.return_value = (200, b"{}")
        app.send_slack("https://hooks.slack.com/xxx", "warning", "Disk full", "95% used")
        url, payload = mock_post.call_args[0]
        self.assertEqual(url, "https://hooks.slack.com/xxx")
        self.assertIn("Disk full", payload["text"])
        self.assertIn("95% used", payload["text"])
        self.assertIn("warning", payload["text"])

    @patch("app._post_json")
    def test_dispatch_includes_slack_when_configured(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {**app.SETTING_DEFAULTS, "slack_webhook_url": "https://hooks.slack.com/xxx"}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        chans = [c for c, ok, _ in results]
        self.assertIn("slack", chans)
        self.assertTrue(all(ok for _, ok, _ in results))

    @patch("app._post_json")
    def test_dispatch_skips_slack_without_url(self, mock_post):
        s = {**app.SETTING_DEFAULTS, "slack_webhook_url": ""}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        chans = [c for c, _, _ in results]
        self.assertNotIn("slack", chans)
        mock_post.assert_not_called()

    @patch("app._post_json")
    def test_dispatch_reports_slack_errors(self, mock_post):
        mock_post.side_effect = RuntimeError("webhook rejected")
        s = {**app.SETTING_DEFAULTS, "slack_webhook_url": "https://hooks.slack.com/xxx"}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        sl = [r for r in results if r[0] == "slack"]
        self.assertEqual(len(sl), 1)
        self.assertFalse(sl[0][1])
        self.assertIn("webhook rejected", sl[0][2])


# ── Generic webhook ────────────────────────────────────────────────────────────

class TestWebhookSettings(unittest.TestCase):
    def test_webhook_defaults_present(self):
        self.assertIn("webhook_url", app.SETTING_DEFAULTS)
        self.assertIn("webhook_url", app.SETTING_SECRETS)

    def test_public_settings_masks_webhook_url(self):
        with patch.object(app, "get_settings", return_value={
            **app.SETTING_DEFAULTS, "webhook_url": "https://hooks.example.com/alerts",
        }):
            pub = app._public_settings()
        self.assertNotIn("webhook_url", pub)
        self.assertTrue(pub["webhook_url_set"])


class TestWebhookNotifier(unittest.TestCase):
    @patch("app._post_json")
    def test_send_webhook_payload(self, mock_post):
        mock_post.return_value = (200, b"{}")
        app.send_webhook("https://hooks.example.com/alerts", "critical",
                         "Disk full", "95% used", "ardi")
        url, payload = mock_post.call_args[0]
        self.assertEqual(url, "https://hooks.example.com/alerts")
        self.assertEqual(payload["level"], "critical")
        self.assertEqual(payload["title"], "Disk full")
        self.assertEqual(payload["detail"], "95% used")
        self.assertEqual(payload["host"], "ardi")

    @patch("app._post_json")
    def test_dispatch_includes_webhook_when_configured(self, mock_post):
        mock_post.return_value = (200, b"{}")
        s = {**app.SETTING_DEFAULTS, "webhook_url": "https://hooks.example.com/alerts"}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        chans = [c for c, ok, _ in results]
        self.assertIn("webhook", chans)
        self.assertTrue(all(ok for _, ok, _ in results))

    @patch("app._post_json")
    def test_dispatch_skips_webhook_without_url(self, mock_post):
        s = {**app.SETTING_DEFAULTS, "webhook_url": ""}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        chans = [c for c, _, _ in results]
        self.assertNotIn("webhook", chans)
        mock_post.assert_not_called()

    @patch("app._post_json")
    def test_dispatch_reports_webhook_errors(self, mock_post):
        mock_post.side_effect = RuntimeError("timeout")
        s = {**app.SETTING_DEFAULTS, "webhook_url": "https://hooks.example.com/alerts"}
        results = app.dispatch_alert(s, "info", "Title", "Body")
        wh = [r for r in results if r[0] == "webhook"]
        self.assertEqual(len(wh), 1)
        self.assertFalse(wh[0][1])
        self.assertIn("timeout", wh[0][2])


# ── URL validation ─────────────────────────────────────────────────────────────

class TestURLValidation(unittest.TestCase):
    def test_slack_webhook_validated(self):
        self.assertIn("slack_webhook_url", app._URL_SETTING_KEYS)

    def test_webhook_url_validated(self):
        self.assertIn("webhook_url", app._URL_SETTING_KEYS)

    def test_validate_rejects_bad_slack_url(self):
        err = app._validate_url_settings({"slack_webhook_url": "ftp://bad"})
        self.assertIsNotNone(err)

    def test_validate_accepts_http_webhook(self):
        err = app._validate_url_settings({"webhook_url": "http://example.com/hook"})
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
