"""backend/api/integrations.py — integrations routes (Phase 3.4)."""
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory, after_this_request, g, abort

from backend.db.repos import notify as notify_repo

bp = Blueprint('integrations', __name__)


@bp.route("/api/containers/<name>/logs")
def api_container_logs(name):
    import app as _app
    """Last `tail` log lines for a container; with follow=1, streams new lines as
    SSE. Read-only — `docker logs` needs no extra socket permissions."""
    if not _app._CT_NAME_RE.match(name or ""):
        return jsonify({"error": "invalid container name"}), 400
    try:
        tail = max(1, min(2000, int(request.args.get("tail", 200))))
    except (TypeError, ValueError):
        tail = 200
    follow = request.args.get("follow") == "1"
    return Response(_app._docker_log_stream(name, tail, follow),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@bp.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    import app as _app
    """Send a one-shot test alert using the currently saved settings."""
    s = _app.get_settings()
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")
            or (s.get("telegram_token") and s.get("telegram_chat_id"))
            or (s.get("email_host") and s.get("email_from") and s.get("email_to"))
            or s.get("slack_webhook_url")
            or s.get("webhook_url")):
        return jsonify({"ok": False, "results": [],
                        "reason": "No Discord webhook, ntfy topic, Telegram bot, email, Slack webhook, or generic webhook configured."}), 400
    results = dispatch_alert(s, "info",
                             "✅ HomeLab Monitor — test alert",
                             "If you see this, alerts are wired up correctly.")
    return jsonify({"ok": all(ok for _, ok, _ in results),
                    "results": [{"channel": c, "ok": ok, "error": err} for c, ok, err in results]})


@bp.route("/api/notify/rules", methods=["GET", "POST"])
def api_notify_rules():
    import app as _app
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        action = body.get("action", "add")
        if action == "add":
            with _app.LOCK:
                notify_repo.insert_rule(
                    body.get("match_kind", "container"), body.get("match_pattern", "*"),
                    body.get("channel", "all"), body.get("min_level", "warning"),
                    1 if body.get("enabled", True) else 0, conn=_app.DB)
        elif action == "update":
            rule_id = body.get("id")
            if not rule_id:
                return jsonify({"ok": False, "error": "id required"}), 400
            with _app.LOCK:
                notify_repo.update_rule(
                    rule_id, body.get("match_kind"), body.get("match_pattern"),
                    body.get("channel"), body.get("min_level"),
                    1 if body.get("enabled", True) else 0, conn=_app.DB)
        elif action == "delete":
            rule_id = body.get("id")
            if not rule_id:
                return jsonify({"ok": False, "error": "id required"}), 400
            with _app.LOCK:
                notify_repo.delete_rule(rule_id, conn=_app.DB)
        else:
            return jsonify({"ok": False, "error": f"unknown action: {action}"}), 400
        return jsonify({"ok": True, "rules": _app.get_notification_rules()})
    return jsonify({"rules": _app.get_notification_rules()})


@bp.route("/api/notify/rules/test", methods=["POST"])
def api_notify_rules_test():
    import app as _app
    """Test a notification rule by sending a sample alert that would match it."""
    body = request.get_json(silent=True) or {}
    s = _app.get_settings()
    if not (s.get("discord_webhook_url") or s.get("ntfy_topic")
            or (s.get("telegram_token") and s.get("telegram_chat_id"))):
        return jsonify({"ok": False, "error": "No notification channels configured."}), 400
    test_rule = {
        "match_kind": body.get("match_kind", "container"),
        "match_pattern": body.get("match_pattern", "*"),
        "channel": body.get("channel", "all"),
        "min_level": body.get("min_level", "warning"),
        "enabled": True,
    }
    test_key = f"{test_rule['match_kind']}:test-rule"
    channels = _app._apply_rules(test_key, body.get("level", "warning"), [test_rule])
    if channels is None:
        return jsonify({"ok": False, "error": "Rule would not match — check kind and pattern."}), 400
    _dispatch_to_channels(s, body.get("level", "warning"),
                          "🔔 HomeLab Monitor — rule test",
                          f"Test of rule: {test_rule['match_kind']} / {test_rule['match_pattern']} → {test_rule['channel']} @ {test_rule['min_level']}",
                          channels)
    return jsonify({"ok": True, "channels": list(channels)})


