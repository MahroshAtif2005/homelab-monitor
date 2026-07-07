"""backend/auth.py — API key authentication helpers (Phase 3.4).

Extracted from app.py so both app.py (re-export) and backend/api/experiments.py
(decorator usage) can import without circular dependency.
"""
import time
from functools import wraps
from flask import request, jsonify, g


def _key_lookup(presented):
    import app as _app
    if not presented:
        return None
    now = int(time.time())
    with _app.LOCK:
        row = _app.DB.execute("SELECT id, expires_at FROM api_keys WHERE key_hash=?",
                              (_app._hash_key(presented),)).fetchone()
        if not row:
            return None
        kid, exp = row
        if exp and exp < now:
            return None
        _app.DB.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, kid))
        _app.DB.commit()
    return kid


def _presented_key():
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        kid = _key_lookup(_presented_key())
        if not kid:
            return jsonify({"ok": False, "error": "missing, invalid, or expired API key"}), 401
        g.api_key_id = kid
        return fn(*a, **kw)
    return wrapper
