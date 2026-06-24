import os, pytest
os.environ.setdefault("DATABASE_URL", ":memory:")

@pytest.fixture
def client():
    import importlib, sys
    for mod in list(sys.modules):
        if "app" in mod:
            del sys.modules[mod]
    os.environ["PUBLIC_STATUS"] = "1"
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    del os.environ["PUBLIC_STATUS"]

@pytest.fixture
def client_off():
    import importlib, sys
    for mod in list(sys.modules):
        if "app" in mod:
            del sys.modules[mod]
    os.environ.pop("PUBLIC_STATUS", None)
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c

def test_off_by_default_api(client_off):
    assert client_off.get("/api/public-status").status_code == 404

def test_off_by_default_page(client_off):
    assert client_off.get("/public").status_code == 404

def test_enabled_api_returns_200(client):
    assert client.get("/api/public-status").status_code == 200

def test_enabled_page_returns_200(client):
    assert client.get("/public").status_code == 200

def test_no_sensitive_keys(client):
    import json
    data = json.loads(client.get("/api/public-status").data)
    blocked = {"containers", "services", "processes", "os_updates", "diagnostics",
               "discord_webhook_url", "telegram_token", "email_password",
               "slack_webhook_url", "webhook_url", "api_key"}
    assert not blocked & set(data.keys())

def test_overview_cards_safe(client):
    import json
    data = json.loads(client.get("/api/public-status").data)
    allowed = {"key", "label", "status", "metric", "detail"}
    for card in data.get("overview", []):
        assert set(card.keys()) <= allowed

def test_status_field_valid(client):
    import json
    data = json.loads(client.get("/api/public-status").data)
    assert data["status"] in ("ok", "warn", "crit")

def test_lab_branding(client):
    import json
    data = json.loads(client.get("/api/public-status").data)
    assert "lab_name" in data
    assert "lab_emoji" in data

def test_no_private_paths_in_body(client):
    body = client.get("/api/public-status").data.decode()
    assert "/var/lib" not in body
    assert "image" not in body.lower() or "lab_emoji" in body
