"""backend/auth.py — API key authentication helpers (Phase 3.4).

Extracted from app.py so both app.py (re-export) and backend/api/experiments.py
(decorator usage) can import without circular dependency.
"""
import time
from functools import wraps
from flask import request, jsonify, g

from backend.db.repos import auth as auth_repo


def _key_lookup(presented):
    import app as _app
    if not presented:
        return None
    now = int(time.time())
    with _app.LOCK:
        row = auth_repo.get_by_hash(_app._hash_key(presented), conn=_app.DB)
        if not row:
            return None
        kid, exp = row
        if exp and exp < now:
            return None
        auth_repo.update_last_used(kid, now, conn=_app.DB)
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
