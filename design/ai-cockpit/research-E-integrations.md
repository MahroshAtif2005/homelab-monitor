# Integrations / Experiment-tracking API — buildable design (E)

Research + paste-ready design for pivoting **Experiments** in HomeLab Monitor from `/proc`
auto-detection to an **API-key-authenticated ingest + query API**, a tiny **Python client SDK**
(`homelab_run.py`) for Jupyter/Colab/Kaggle, and **native MLflow push/pull** — all reflected with
resource/energy/cost graphs by bridging a pushed session to the GPU `samples` we already collect.

Stack constraints honored: **pure Python stdlib + Flask, no new pip deps server-side; Chart.js
frontend.** Existing GPU-activity-sessions (`/api/sessions`, `_gpu_sessions`) stay as supporting
context; they answer "what ran on the GPU" — the new Runs API answers "what *I* ran, what it cost".

Target files in the live app:
- `app.py` — `_DB_SCHEMA` (~L92), `_apply_schema_migrations` (~L135), `SETTING_DEFAULTS` (~L3247),
  `save_settings`/`get_settings` (~L3267), `_public_settings` (~L4440), `/api/cost` cost logic to
  reuse (~L3982), `_make_is_night`/`_hhmm_to_min` helpers, `RANGES` (~L3895), `INTERVAL`/`MAX_POINTS`,
  `LOCK`/`DB`/`LATEST`, the collector thread (~L4775) for the MLflow sync hook.
- `static/dashboard.html` — Experiments tab (`<section data-tab="experiments">` ~L717),
  `renderExperiments()` (~L2641), tab registry (~L1118), Settings/Alerts tab (~L761).
- New client artifact (shipped, not imported by the server): `homelab_run.py`.

Why this design (maintainer's verbatim intent): *"integrations not detection… a wrapper or API
(with key) that I can run and push some data about the session… written to the database… see what
resources used and how much money spent… reflected with graphs… also pullable… same with Colab,
Kaggle… native integration with MLflow — push/pull… otherwise probably no one will use it."* So:
**push from the notebook → store → attach real GPU cost → pull/graph back**, plus **MLflow mirror**.

---

## 1. Auth model — one Bearer key for the instance

A self-hosted instance is single-tenant on a trusted LAN. We add **one API key**, generated on
demand, stored in `settings`, shown (masked, with regenerate) in the Settings UI. Clients send it as
`Authorization: Bearer <key>` **or** `X-API-Key: <key>`.

Decision: **gate writes always; gate reads optionally (recommended ON).** The dashboard's own browser
calls (`/api/data`, `/api/cost`, `/api/sessions`, `/api/runs?...` rendered by the page) must stay
unauthenticated, so we scope the decorator to the **new `/api/runs*` routes only** and let the
browser reach the read routes via a same-origin exemption: the dashboard reads runs through the same
open `GET /api/runs` but we keep that open on LAN (matches the existing "dashboard API is
intentionally unauthenticated on a trusted LAN" stance, app.py ~L3281). **Writes** (`POST`/`PATCH`)
are always key-gated. This keeps the browser zero-config while making *ingest* forge-resistant.

### 1.1 New setting key

Add to `SETTING_DEFAULTS` (~L3247):

```python
    # ── Integrations / Experiment-tracking API (research-E) ───────────────────
    "api_key":            "",        # Bearer/X-API-Key for run ingest; empty => not yet generated
    "mlflow_uri":         "",        # MLflow tracking server base, e.g. http://mlflow.lan:5000 (blank=off)
    "mlflow_token":       "",        # optional bearer for a secured MLflow (Databricks/proxy); blank=open LAN
    "mlflow_push":        "0",       # "1" => also mirror our native runs INTO MLflow
```

Add `api_key`, `mlflow_token` to `SETTING_SECRETS` (~L3265) so `_public_settings()` returns
`api_key_set` / `mlflow_token_set` booleans, never the raw secret in the bulk settings payload. The
key is revealed only through the dedicated endpoint below (so it can be copied once after regenerate).

### 1.2 Key generation, storage, constant-time compare

```python
import secrets, hmac

def _get_api_key():
    """Current key, or '' if never generated."""
    return get_settings().get("api_key") or ""

def _gen_api_key():
    """Generate, persist, return a new key. URL-safe, ~256 bits."""
    key = "hlm_" + secrets.token_urlsafe(32)
    save_settings({"api_key": key})
    return key

def _key_ok(presented):
    """Constant-time compare against the stored key. False if no key set yet
    (fail-closed: ingest is disabled until the operator generates a key)."""
    real = _get_api_key()
    if not real or not presented:
        return False
    return hmac.compare_digest(presented, real)
```

`secrets`/`hmac` are stdlib. `hmac.compare_digest` is the constant-time compare. Fail-closed: with no
key generated, **all ingest is rejected** (so an instance isn't writable by default).

### 1.3 The decorator (gates ONLY ingest/write routes)

```python
from functools import wraps

def _presented_key():
    """Pull the key from Authorization: Bearer <k> or X-API-Key: <k>."""
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()

def require_api_key(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _key_ok(_presented_key()):
            return jsonify({"ok": False, "error": "missing or invalid API key"}), 401
        return fn(*a, **kw)
    return wrapper
```

Applied to `POST /api/runs`, `PATCH /api/runs/<id>`, `POST /api/runs/<id>/metrics`,
`POST /api/runs/<id>/finish` — **not** to the `GET` read routes (LAN-open, dashboard-friendly). If
the operator wants reads gated too, wrap the GETs as well and have the dashboard JS attach the key
from a `window.__HLM_KEY` it fetches once from a same-origin `/api/runs/_browserkey` (out of scope;
default is open reads).

### 1.4 Key endpoints (browser-only, same-origin; not key-gated — they manage the key)

```python
@app.route("/api/integration/key", methods=["GET", "POST"])
def api_integration_key():
    """GET -> {has_key, key_masked}. POST {regenerate:true} -> {key} (revealed once)."""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if body.get("regenerate"):
            key = _gen_api_key()
            return jsonify({"ok": True, "key": key})        # full key, shown once in UI
        return jsonify({"ok": False, "error": "nothing to do"}), 400
    k = _get_api_key()
    masked = (k[:8] + "…" + k[-4:]) if k else ""
    return jsonify({"has_key": bool(k), "key_masked": masked})
```

This is the only place the full key is returned, and only right after an explicit regenerate. These
routes are same-origin browser actions (like the existing `/api/settings` POST), consistent with the
trusted-LAN model.

---

## 2. Schema — `runs` + `run_metrics` (migration-safe)

Two tables appended to `_DB_SCHEMA` (the `executescript` at ~L92, all `CREATE TABLE IF NOT EXISTS`,
so it is idempotent and safe on every boot — same pattern as `samples`, `hosts`, etc.):

```sql
CREATE TABLE IF NOT EXISTS runs(
  id         TEXT PRIMARY KEY,         -- uuid4 hex (client- or server-minted)
  name       TEXT NOT NULL,
  source     TEXT NOT NULL,            -- jupyter|colab|kaggle|mlflow|api|cli
  status     TEXT NOT NULL,            -- running|finished|failed|killed
  started_at INTEGER NOT NULL,         -- unix epoch seconds (matches samples.ts)
  ended_at   INTEGER,                  -- NULL while running
  host       TEXT,                     -- client-reported hostname
  params     TEXT,                     -- JSON object text
  tags       TEXT,                     -- JSON array/object text
  notes      TEXT,
  heartbeat_at INTEGER,                -- last PATCH/heartbeat; used to mark stale runs killed
  ext_id     TEXT,                     -- source-native id (e.g. mlflow run_uuid) for idempotent sync
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run_metrics(
  run_id TEXT NOT NULL,
  ts     INTEGER NOT NULL,             -- unix epoch seconds
  step   INTEGER DEFAULT 0,            -- optimizer step / epoch index
  key    TEXT NOT NULL,                -- 'loss', 'lr', 'val_acc', ...
  value  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started   ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status     ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runmetrics_rid  ON run_metrics(run_id, key, ts);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_runs_ext ON runs(source, ext_id);  -- mlflow idempotency
```

Notes:
- All timestamps are **unix epoch seconds** to line up 1:1 with `samples.ts` so cost integration is a
  trivial `WHERE ts BETWEEN started_at AND ended_at`. (MLflow's ms epochs are divided by 1000 on
  import — see §6.)
- `uniq_runs_ext(source, ext_id)` makes MLflow sync idempotent: re-pulling the same MLflow run
  upserts instead of duplicating. Native runs leave `ext_id` NULL (NULLs are distinct in SQLite
  unique indexes, so many native runs coexist).
- No `ALTER` needed for a fresh install. If these tables ship in a later release on top of an existing
  DB, `executescript` adds them automatically; to add a *column* later, follow the existing
  `_SAMPLE_MIGRATIONS` try/except `ALTER` pattern (app.py ~L137). For forward-safety, add a
  `_RUNS_MIGRATIONS = ()` tuple now and loop it in `_apply_schema_migrations` like the others.

---

## 3. Ingest API (key-gated) — push from notebooks/CLI

All four are wrapped with `@require_api_key`. Payload sizes are validated/limited to keep a notebook
client from filling the homelab disk. Limits (constants near the routes):

```python
MAX_RUN_FIELD   = 4096       # bytes per text field (name/host/notes)
MAX_RUN_JSON    = 64 * 1024  # bytes for params/tags JSON
MAX_METRICS_REQ = 1000       # metric points per POST (mirrors MLflow log-batch cap)
RUN_SOURCES = {"jupyter", "colab", "kaggle", "mlflow", "api", "cli"}
RUN_STATUS  = {"running", "finished", "failed", "killed"}
```

### 3.1 `POST /api/runs` — create, returns id

Request JSON: `{name, source?, host?, params?, tags?, notes?, id?, started_at?}`.
If `id` is omitted the server mints a uuid4; if present (client-minted) it is accepted so the SDK can
log metrics before the create round-trips. `started_at` defaults to now.

```python
import uuid

def _clip(v, n):
    s = "" if v is None else str(v)
    return s[:n]

def _json_field(v, n):
    """Serialize to compact JSON text, size-capped. Accepts dict/list/str/None."""
    if v is None:
        return None
    txt = v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))
    if len(txt.encode("utf-8")) > n:
        raise ValueError("payload too large")
    return txt

@app.route("/api/runs", methods=["POST"])
@require_api_key
def api_runs_create():
    body = request.get_json(silent=True) or {}
    name = _clip(body.get("name") or "run", MAX_RUN_FIELD)
    source = (body.get("source") or "api").lower()
    if source not in RUN_SOURCES:
        source = "api"
    now = int(time.time())
    started = int(body.get("started_at") or now)
    rid = (body.get("id") or uuid.uuid4().hex)[:64]
    try:
        params = _json_field(body.get("params"), MAX_RUN_JSON)
        tags   = _json_field(body.get("tags"),   MAX_RUN_JSON)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 413
    with LOCK:
        DB.execute(
            "INSERT INTO runs(id,name,source,status,started_at,ended_at,host,params,tags,notes,"
            "heartbeat_at,ext_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (rid, name, source, "running", started, None, _clip(body.get("host"), 256),
             params, tags, _clip(body.get("notes"), MAX_RUN_FIELD), now, None, now))
        DB.commit()
    return jsonify({"ok": True, "id": rid})
```

### 3.2 `PATCH /api/runs/<id>` — heartbeat / update status / params

Updates any of `status`, `ended_at`, `params`, `tags`, `notes`, `name`; always refreshes
`heartbeat_at`. The SDK calls this on its heartbeat thread so a crashed run can be marked `killed` by
a janitor (see §3.5).

```python
@app.route("/api/runs/<rid>", methods=["PATCH"])
@require_api_key
def api_runs_update(rid):
    body = request.get_json(silent=True) or {}
    sets, args = ["heartbeat_at=?"], [int(time.time())]
    if "status" in body and body["status"] in RUN_STATUS:
        sets.append("status=?"); args.append(body["status"])
    if "ended_at" in body and body["ended_at"]:
        sets.append("ended_at=?"); args.append(int(body["ended_at"]))
    for f, n in (("name", MAX_RUN_FIELD), ("notes", MAX_RUN_FIELD)):
        if f in body:
            sets.append(f"{f}=?"); args.append(_clip(body[f], n))
    try:
        for f in ("params", "tags"):
            if f in body:
                sets.append(f"{f}=?"); args.append(_json_field(body[f], MAX_RUN_JSON))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 413
    args.append(rid)
    with LOCK:
        cur = DB.execute(f"UPDATE runs SET {','.join(sets)} WHERE id=?", args)
        DB.commit()
    if cur.rowcount == 0:
        return jsonify({"ok": False, "error": "unknown run"}), 404
    return jsonify({"ok": True})
```

### 3.3 `POST /api/runs/<id>/metrics` — log metric points

Accepts a batch: `{metrics:[{key,value,step?,ts?}, ...]}` (or a single `{key,value,...}`). Capped at
`MAX_METRICS_REQ` points per request.

```python
@app.route("/api/runs/<rid>/metrics", methods=["POST"])
@require_api_key
def api_runs_metrics(rid):
    body = request.get_json(silent=True) or {}
    pts = body.get("metrics")
    if pts is None and "key" in body:        # single-point convenience form
        pts = [body]
    pts = pts or []
    if len(pts) > MAX_METRICS_REQ:
        return jsonify({"ok": False, "error": f"max {MAX_METRICS_REQ} points/request"}), 413
    now = int(time.time())
    rows = []
    for p in pts:
        try:
            k = _clip(p["key"], 128); v = float(p["value"])
        except (KeyError, TypeError, ValueError):
            continue                          # skip malformed points, don't fail the batch
        rows.append((rid, int(p.get("ts") or now), int(p.get("step") or 0), k, v))
    with LOCK:
        ex = DB.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone()
        if not ex:
            return jsonify({"ok": False, "error": "unknown run"}), 404
        if rows:
            DB.executemany("INSERT INTO run_metrics(run_id,ts,step,key,value) VALUES(?,?,?,?,?)", rows)
            DB.execute("UPDATE runs SET heartbeat_at=? WHERE id=?", (now, rid))
            DB.commit()
    return jsonify({"ok": True, "logged": len(rows)})
```

### 3.4 `POST /api/runs/<id>/finish` — terminal status + ended_at

```python
@app.route("/api/runs/<rid>/finish", methods=["POST"])
@require_api_key
def api_runs_finish(rid):
    body = request.get_json(silent=True) or {}
    status = body.get("status") if body.get("status") in RUN_STATUS else "finished"
    if status == "running":
        status = "finished"
    ended = int(body.get("ended_at") or time.time())
    with LOCK:
        cur = DB.execute("UPDATE runs SET status=?, ended_at=?, heartbeat_at=? WHERE id=?",
                         (status, ended, ended, rid))
        DB.commit()
    if cur.rowcount == 0:
        return jsonify({"ok": False, "error": "unknown run"}), 404
    return jsonify({"ok": True, "id": rid, "status": status})
```

### 3.5 Stale-run janitor (optional, in the collector loop)

In the existing collector thread (app.py ~L3854, runs every `INTERVAL`s), once a minute mark runs
whose `heartbeat_at` is older than e.g. 3 minutes and still `running` as `killed` and set `ended_at`:

```python
if ts % 60 < INTERVAL:
    with LOCK:
        DB.execute("UPDATE runs SET status='killed', ended_at=COALESCE(ended_at,heartbeat_at) "
                   "WHERE status='running' AND heartbeat_at < ?", (ts - 180,))
        DB.commit()
```

This keeps a Colab session that lost connectivity from showing "running" forever, and gives it a
correct end time for cost integration.

---

## 4. Query/pull API — read back + attach resource/energy/cost

These are LAN-open (no decorator) so the dashboard and notebooks read freely.

### 4.1 `GET /api/runs?range=&status=` — list

Returns runs overlapping the range, newest-first, each with duration, key metrics (latest value per
key), and **energy + cost** integrated over its window. The cost reuse is the heart of the feature.

```python
def _run_cost_window(cur, started, ended, day_price, night_price, is_night, mode):
    """Integrate samples.power over [started, ended] -> (energy_kwh, cost, avg_w, peak_util).
    Reuses the exact cost math from /api/cost. ended=None -> now (run still live)."""
    end = ended or int(time.time())
    kwh_per = INTERVAL / 3_600_000.0
    e_kwh = cost = 0.0; sum_p = n = 0; peak_u = 0.0
    for ts, util, power in cur.execute(
            "SELECT ts,util,power FROM samples WHERE ts>=? AND ts<=? AND power IS NOT NULL",
            (started, end)):
        p = power or 0.0
        sum_p += p; n += 1; peak_u = max(peak_u, util or 0)
        e_kwh += p * kwh_per
        price = (night_price if (mode == "dual" and is_night(ts)) else day_price)
        cost += p * kwh_per * (price or 0.0)
    return round(e_kwh, 4), round(cost, 4), (round(sum_p / n) if n else 0), round(peak_u)

@app.route("/api/runs")
def api_runs_list():
    s = get_settings()
    day_price, night_price, is_night, mode, currency = _cost_ctx(s)   # small helper, below
    rng = request.args.get("range", "7d"); span = RANGES.get(rng, 604800)
    status = request.args.get("status")
    now = int(time.time()); since = 0 if span is None else now - span
    q = ("SELECT id,name,source,status,started_at,ended_at,host,params,tags,notes "
         "FROM runs WHERE (ended_at IS NULL OR ended_at>=?) AND started_at<=? ")
    args = [since, now]
    if status in RUN_STATUS:
        q += "AND status=? "; args.append(status)
    q += "ORDER BY started_at DESC LIMIT 500"
    out = []
    with LOCK:
        cur = DB.cursor()
        runs = cur.execute(q, args).fetchall()
        for (rid, name, source, st, started, ended, host, params, tags, notes) in runs:
            e_kwh, cost, avg_w, peak_u = _run_cost_window(
                cur, started, ended, day_price, night_price, is_night, mode)
            # latest value per metric key (compact KPI set for the table)
            kv = {}
            for k, v in cur.execute(
                    "SELECT key, value FROM run_metrics WHERE run_id=? "
                    "AND ts=(SELECT MAX(ts) FROM run_metrics m2 WHERE m2.run_id=run_metrics.run_id "
                    "AND m2.key=run_metrics.key) GROUP BY key", (rid,)):
                kv[k] = v
            out.append({
                "id": rid, "name": name, "source": source, "status": st,
                "started_at": started, "ended_at": ended,
                "duration": (ended or now) - started, "host": host,
                "params": _safe_json(params), "tags": _safe_json(tags), "notes": notes,
                "metrics_latest": kv,
                "energy_kwh": e_kwh, "cost": cost, "avg_w": avg_w, "peak_util": peak_u})
    return jsonify({"range": rng, "currency": currency, "tariff_mode": mode, "runs": out})
```

Helpers (factored from `/api/cost` so the math is identical and DRY):

```python
def _safe_json(txt):
    try:    return json.loads(txt) if txt else None
    except Exception: return None

def _cost_ctx(s):
    """Shared cost context: (day_price, night_price, is_night, mode, currency).
    Mirrors the top of /api/cost so runs are priced exactly like the cost card."""
    def fnum(key):
        v = (s.get(key) or "").strip()
        try:    return float(v) if v else None
        except ValueError: return None
    day = fnum("kwh_price") or 0.0
    night = fnum("kwh_price_night")
    mode = "dual" if (s.get("tariff_mode") == "dual" and night is not None and day > 0) else "single"
    return day, night, _make_is_night(s.get("night_start", "22:00"),
                                      s.get("night_end", "06:00")), mode, (s.get("currency") or "$")
```

### 4.2 `GET /api/runs/<id>` — full run + metrics + power/util series

Returns the run, all logged metric series (downsampled like `/api/data`), and the **GPU
power/util/cost time-series** integrated over the run window so the dashboard can graph exactly what
the session cost.

```python
@app.route("/api/runs/<rid>")
def api_runs_get(rid):
    s = get_settings()
    day_price, night_price, is_night, mode, currency = _cost_ctx(s)
    now = int(time.time())
    with LOCK:
        cur = DB.cursor()
        r = cur.execute(
            "SELECT id,name,source,status,started_at,ended_at,host,params,tags,notes "
            "FROM runs WHERE id=?", (rid,)).fetchone()
        if not r:
            return jsonify({"error": "unknown run"}), 404
        (rid, name, source, st, started, ended, host, params, tags, notes) = r
        end = ended or now

        # logged metrics -> {key: {steps:[], ts:[], values:[]}}
        metrics = {}
        for k, ts, step, v in cur.execute(
                "SELECT key,ts,step,value FROM run_metrics WHERE run_id=? ORDER BY key,ts,step",
                (rid,)):
            d = metrics.setdefault(k, {"steps": [], "ts": [], "values": []})
            d["steps"].append(step); d["ts"].append(ts); d["values"].append(v)

        # GPU power/util series over the run window (bucketed, MAX_POINTS like api_data)
        bk = max(INTERVAL, round(max(1, end - started) / MAX_POINTS))
        labels, power_w, util_pct = [], [], []
        e_kwh = cost = 0.0; kwh_per = INTERVAL / 3_600_000.0
        for b, ap, au, sp in cur.execute(
                "SELECT (ts/?)*? b, AVG(power), AVG(util), SUM(power) FROM samples "
                "WHERE ts>=? AND ts<=? GROUP BY b ORDER BY b", (bk, bk, started, end)):
            labels.append(int(b)); power_w.append(round(ap or 0)); util_pct.append(round(au or 0))
        e_kwh, cost, avg_w, peak_u = _run_cost_window(
            cur, started, end, day_price, night_price, is_night, mode)
    return jsonify({
        "id": rid, "name": name, "source": source, "status": st,
        "started_at": started, "ended_at": ended, "duration": end - started,
        "host": host, "params": _safe_json(params), "tags": _safe_json(tags), "notes": notes,
        "metrics": metrics,
        "resource": {"labels": labels, "power_w": power_w, "util_pct": util_pct, "bucket_sec": bk},
        "energy_kwh": e_kwh, "cost": cost, "avg_w": avg_w, "peak_util": peak_u,
        "currency": currency, "tariff_mode": mode})
```

This is the bridge the maintainer asked for: a pushed session (just a name + a `[start,end]` window)
comes back with **what GPU resources it used and how much money it spent**, ready to graph — using
the same power-integration and dual-tariff cost logic as the cost card. No per-process attribution is
needed; the run's wall-clock window over the box's GPU power is the honest, simple answer (documented
as "GPU energy during your run window", which on a single-GPU homelab is what the run cost).

---

## 5. Python client SDK — `homelab_run.py` (single file, stdlib-only)

Copy-paste into a Jupyter/Colab/Kaggle cell or `pip`-free drop-in. Uses `requests` if importable,
else stdlib `urllib`. Context manager + manual API + background heartbeat + pull/list.

```python
"""homelab_run.py — tiny client for HomeLab Monitor's run-tracking API.

Push training/session metadata + metrics to your self-hosted HomeLab Monitor and
pull it back, with real GPU energy/cost attached by the hub. Stdlib-only (urllib);
uses `requests` automatically if it's installed. Copy this one file anywhere.

Quickstart (Jupyter / Colab / Kaggle):

    import homelab_run as homelab
    homelab.configure(url="http://homelab.lan:9800", key="hlm_xxx")  # or env vars

    with homelab.run("sft-llama3-lora", params={"lr": 2e-4, "bs": 8}) as r:
        for step, loss in enumerate(train()):
            r.log_metric("loss", loss, step=step)
        r.log_params({"final_eval": 0.81})

    # pull it back (e.g. from another notebook)
    print(homelab.pull(r.id)["cost"], homelab.list_runs(range="7d"))

Colab/Kaggle note: those run in the cloud, so the hub URL must be reachable from
the internet — expose it via Tailscale (recommended) or an ngrok/Cloudflare tunnel
and pass that https URL. On your LAN, the plain http://host:9800 works.
"""
import os, sys, json, time, uuid, socket, threading, urllib.request, urllib.error

try:
    import requests as _rq           # optional; nicer, but never required
except Exception:
    _rq = None

_CFG = {"url": os.environ.get("HOMELAB_URL", "http://localhost:9800"),
        "key": os.environ.get("HOMELAB_KEY", ""),
        "timeout": 10}

def configure(url=None, key=None, timeout=None):
    if url is not None:     _CFG["url"] = url.rstrip("/")
    if key is not None:     _CFG["key"] = key
    if timeout is not None: _CFG["timeout"] = timeout

def _headers():
    h = {"Content-Type": "application/json"}
    if _CFG["key"]:
        h["Authorization"] = "Bearer " + _CFG["key"]
        h["X-API-Key"] = _CFG["key"]
    return h

def _request(method, path, payload=None):
    url = _CFG["url"].rstrip("/") + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    if _rq is not None:
        resp = _rq.request(method, url, data=data, headers=_headers(),
                           timeout=_CFG["timeout"])
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=_CFG["timeout"]) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:200].decode('utf-8','replace')}")

def _detect_source():
    if "google.colab" in sys.modules:                    return "colab"
    if os.environ.get("KAGGLE_KERNEL_RUN_TYPE"):         return "kaggle"
    try:
        import IPython
        if IPython.get_ipython() is not None:            return "jupyter"
    except Exception:
        pass
    return "cli"

class Run:
    """One tracked run. Use via `homelab.run(...)` (context manager) or manually:
        r = homelab.run("name").start(); r.log_metric(...); r.finish()
    """
    def __init__(self, name, params=None, tags=None, notes=None, source=None,
                 heartbeat=30, buffer=True):
        self.id = uuid.uuid4().hex
        self.name = name
        self.params = dict(params or {})
        self.tags = tags or []
        self.notes = notes or ""
        self.source = source or _detect_source()
        self.host = socket.gethostname()
        self._hb_interval = heartbeat
        self._buffer = buffer
        self._buf = []                       # pending metric points (flushed on size/finish)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._hb_thread = None
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        _request("POST", "/api/runs", {
            "id": self.id, "name": self.name, "source": self.source, "host": self.host,
            "params": self.params, "tags": self.tags, "notes": self.notes,
            "started_at": int(time.time())})
        self._started = True
        if self._hb_interval:
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._hb_thread.start()
        return self

    def _heartbeat_loop(self):
        while not self._stop.wait(self._hb_interval):
            try:
                self.flush()
                _request("PATCH", f"/api/runs/{self.id}", {"status": "running"})
            except Exception:
                pass                          # never let telemetry crash training

    # ── logging ──────────────────────────────────────────────────────────────
    def log_metric(self, key, value, step=None, ts=None):
        pt = {"key": key, "value": float(value),
              "step": int(step) if step is not None else 0,
              "ts": int(ts) if ts is not None else int(time.time())}
        if self._buffer:
            with self._lock:
                self._buf.append(pt)
                flush = len(self._buf) >= 100
            if flush: self.flush()
        else:
            _request("POST", f"/api/runs/{self.id}/metrics", {"metrics": [pt]})
        return self

    def log_metrics(self, d, step=None, ts=None):
        for k, v in d.items():
            self.log_metric(k, v, step=step, ts=ts)
        return self

    def flush(self):
        with self._lock:
            pts, self._buf = self._buf, []
        if pts:
            _request("POST", f"/api/runs/{self.id}/metrics", {"metrics": pts})
        return self

    def log_params(self, d):
        self.params.update(d or {})
        _request("PATCH", f"/api/runs/{self.id}", {"params": self.params})
        return self

    def set_notes(self, text):
        self.notes = text
        _request("PATCH", f"/api/runs/{self.id}", {"notes": text})
        return self

    # ── termination ──────────────────────────────────────────────────────────
    def _terminate(self, status):
        self._stop.set()
        try: self.flush()
        except Exception: pass
        _request("POST", f"/api/runs/{self.id}/finish",
                 {"status": status, "ended_at": int(time.time())})

    def finish(self): self._terminate("finished"); return self
    def fail(self):   self._terminate("failed");   return self

    # ── pull this run back (with cost attached by the hub) ─────────────────────
    def pull(self): return pull(self.id)

    # ── context manager ────────────────────────────────────────────────────────
    def __enter__(self):
        if not self._started: self.start()
        return self
    def __exit__(self, exc_type, exc, tb):
        self._terminate("failed" if exc_type else "finished")
        return False                          # don't suppress exceptions

# ── module-level conveniences ──────────────────────────────────────────────────
def run(name, **kw):
    """Create (and on __enter__, start) a Run. `with homelab.run('x') as r: ...`"""
    return Run(name, **kw)

def pull(run_id):
    """Full run + metrics + GPU power/util/cost series (dict)."""
    return _request("GET", f"/api/runs/{run_id}")

def list_runs(range="7d", status=None):
    path = f"/api/runs?range={range}" + (f"&status={status}" if status else "")
    return _request("GET", path)["runs"]
```

Design notes:
- **Buffered metrics** (default 100 points or heartbeat flush) keep training loops from blocking on
  HTTP each step; `flush()` is called on heartbeat and at finish. `buffer=False` for immediate posts.
- **Heartbeat thread** (daemon) PATCHes every 30s so the hub can mark a crashed/disconnected run
  `killed` (§3.5) and give it a correct end time for cost.
- **Source auto-detect** picks `colab`/`kaggle`/`jupyter`/`cli` so the run's badge is right with zero
  config.
- **Never crashes the host program**: heartbeat swallows errors; `__exit__` reports `failed` on
  exception but re-raises it (`return False`).
- **Colab/Kaggle reachability**: documented in the docstring — those run in Google/Kaggle cloud, so
  point `url` at a Tailscale MagicDNS name or an ngrok/Cloudflare tunnel that exposes the hub; on the
  LAN use `http://host:9800` directly.
- Optional `requests` is used if present (Colab/Kaggle ship it), else stdlib `urllib` — zero installs.

---

## 6. MLflow integration (push/pull) — pure REST, no `mlflow` dep

MLflow exposes a stable REST API under the base prefix **`/api/2.0/mlflow/`**. We talk to it with
stdlib `urllib` (same as our alert webhooks). All timestamps in MLflow are **unix epoch
milliseconds** → divide by 1000 to match our `samples.ts` seconds.

Auth: open MLflow on a LAN needs none. A secured server (behind a proxy, or Databricks-hosted) takes
a bearer token — we send `Authorization: Bearer <mlflow_token>` when `mlflow_token` is set.

### 6.1 Endpoints we use (verified against MLflow REST API docs)

| Purpose | Method | Path |
|---|---|---|
| List experiments | POST | `/api/2.0/mlflow/experiments/search` |
| List runs in experiments | POST | `/api/2.0/mlflow/runs/search` |
| Get one run (info+data) | GET | `/api/2.0/mlflow/runs/get?run_id=...` |
| Full metric history for a key | GET | `/api/2.0/mlflow/metrics/get-history?run_id=...&metric_key=...` |
| (push) Create run | POST | `/api/2.0/mlflow/runs/create` |
| (push) Log a metric | POST | `/api/2.0/mlflow/runs/log-metric` |
| (push) Log many at once | POST | `/api/2.0/mlflow/runs/log-batch` |
| (push) Set status/end | POST | `/api/2.0/mlflow/runs/update` |

Response shapes we rely on:
- `experiments/search` → `{experiments:[{experiment_id, name, ...}], next_page_token?}`.
- `runs/search` → `{runs:[{info:{run_id|run_uuid, run_name, status, start_time, end_time,
  experiment_id, ...}, data:{metrics:[{key,value,timestamp,step}], params:[{key,value}],
  tags:[{key,value}]}}], next_page_token?}`. `data.metrics` carries only the **latest** value per
  key; full series come from `metrics/get-history`.
- `metrics/get-history` → `{metrics:[{key,value,timestamp,step}, ...]}` (full series).

MLflow `status` values map to ours: `RUNNING→running`, `FINISHED→finished`, `FAILED→failed`,
`KILLED→killed`, `SCHEDULED→running`.

### 6.2 Settings (already added in §1.1): `mlflow_uri`, `mlflow_token`, `mlflow_push`.

### 6.3 PULL — mirror MLflow runs into our `runs`/`run_metrics` as `source=mlflow`

A `sync_mlflow()` function, called periodically from the collector loop (e.g. every 5 min). It pages
through experiments → runs, upserts each run keyed by `ext_id=run_id` (the `uniq_runs_ext(source,
ext_id)` index makes this idempotent), and pulls each metric's full history. Because the run's
`[start,end]` lands on our timeline in seconds, the **same `/api/runs/<id>` resource+cost
integration** then attaches GPU energy/cost to mirrored MLflow runs automatically — they show up in
the Experiments page priced exactly like native runs.

```python
def _mlf(method, path, payload=None, params=None):
    base = (get_settings().get("mlflow_uri") or "").rstrip("/")
    if not base:
        return None
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdr = {"Content-Type": "application/json"}
    tok = get_settings().get("mlflow_token")
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
        return json.loads(body) if body else {}

def _ms_to_s(v):
    return int(v) // 1000 if v else None

_MLF_STATUS = {"RUNNING": "running", "FINISHED": "finished", "FAILED": "failed",
               "KILLED": "killed", "SCHEDULED": "running"}

def sync_mlflow():
    """Pull experiments/runs/metrics from the configured MLflow server and mirror
    them into runs/run_metrics as source='mlflow'. Idempotent via uniq(source,ext_id).
    Stdlib REST only — no mlflow pip dep."""
    if not (get_settings().get("mlflow_uri") or "").strip():
        return
    # 1) experiments
    exp_ids, tok = [], None
    while True:
        body = _mlf("POST", "/api/2.0/mlflow/experiments/search",
                    {"max_results": 1000, **({"page_token": tok} if tok else {})}) or {}
        exp_ids += [e["experiment_id"] for e in body.get("experiments", [])]
        tok = body.get("next_page_token")
        if not tok:
            break
    if not exp_ids:
        return
    now = int(time.time())
    # 2) runs (paged), upserted
    tok = None
    while True:
        body = _mlf("POST", "/api/2.0/mlflow/runs/search",
                    {"experiment_ids": exp_ids, "max_results": 1000,
                     **({"page_token": tok} if tok else {})}) or {}
        for run in body.get("runs", []):
            info = run.get("info", {}); data = run.get("data", {})
            ext = info.get("run_id") or info.get("run_uuid")
            if not ext:
                continue
            name = info.get("run_name") or (
                {t["key"]: t["value"] for t in data.get("tags", [])}
                .get("mlflow.runName")) or ext[:8]
            started = _ms_to_s(info.get("start_time")) or now
            ended = _ms_to_s(info.get("end_time"))
            status = _MLF_STATUS.get(info.get("status"), "running")
            params = {p["key"]: p["value"] for p in data.get("params", [])}
            tags = {t["key"]: t["value"] for t in data.get("tags", [])
                    if not t["key"].startswith("mlflow.")}
            with LOCK:
                cur = DB.execute("SELECT id FROM runs WHERE source='mlflow' AND ext_id=?", (ext,))
                row = cur.fetchone()
                rid = row[0] if row else uuid.uuid4().hex
                DB.execute(
                    "INSERT INTO runs(id,name,source,status,started_at,ended_at,host,params,tags,"
                    "notes,heartbeat_at,ext_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source,ext_id) DO UPDATE SET status=excluded.status, "
                    "ended_at=excluded.ended_at, name=excluded.name, params=excluded.params, "
                    "tags=excluded.tags, heartbeat_at=excluded.heartbeat_at",
                    (rid, name, "mlflow", status, started, ended, "mlflow",
                     json.dumps(params, separators=(",", ":")),
                     json.dumps(tags, separators=(",", ":")), "", now, ext, now))
                DB.commit()
            # 3) metric histories (full series) — replace this run's metrics
            with LOCK:
                DB.execute("DELETE FROM run_metrics WHERE run_id=?", (rid,))
                DB.commit()
            for m in data.get("metrics", []):
                hist = _mlf("GET", "/api/2.0/mlflow/metrics/get-history",
                            params={"run_id": ext, "metric_key": m["key"]}) or {}
                rows = [(rid, _ms_to_s(h.get("timestamp")) or started,
                         int(h.get("step") or 0), m["key"], float(h["value"]))
                        for h in hist.get("metrics", [])]
                if rows:
                    with LOCK:
                        DB.executemany(
                            "INSERT INTO run_metrics(run_id,ts,step,key,value) VALUES(?,?,?,?,?)",
                            rows)
                        DB.commit()
        tok = body.get("next_page_token")
        if not tok:
            break
```

Wire-up in the collector loop (app.py ~L3801 cadence pattern):

```python
if get_settings().get("mlflow_uri") and ts % 300 < INTERVAL:    # every ~5 min
    try: sync_mlflow()
    except Exception as e: print("mlflow sync error:", e, flush=True)
```

Plus a manual `POST /api/integration/mlflow/sync` (browser, same-origin) for an on-demand "Sync now"
button, and a `GET` that does a one-shot reachability probe (`experiments/search` with
`max_results:1`) to show a green/red status in Settings.

### 6.4 PUSH (optional, `mlflow_push="1"`) — mirror native runs INTO MLflow

When enabled, on `finish` (or in the sync tick) mirror our native (`source != 'mlflow'`) runs to the
MLflow server so an MLflow-centric user sees them there too:

1. `POST /runs/create` `{experiment_id, run_name, start_time: started_at*1000, tags:[{key:"hlm.source",value:source}]}` → get `run_id`. Store it back on our row (e.g. in `notes` or a `push_ext_id` column if added) to avoid re-creating.
2. `POST /runs/log-batch` `{run_id, metrics:[{key,value,timestamp: ts*1000, step}, ...]}` in chunks of ≤1000 (MLflow's batch cap, mirrored by our `MAX_METRICS_REQ`).
3. `POST /runs/update` `{run_id, status: "FINISHED"|"FAILED", end_time: ended_at*1000}`.

Use a fixed/default experiment (resolve or create "HomeLab Monitor" once via
`experiments/search`→`experiments/create`). Push is best-effort and never blocks ingest. Recommend
shipping **pull first** (the high-value direction: MLflow runs gain real cost graphs); push is a
nice-to-have toggle.

---

## 7. UI — the new Experiments / Runs page

Reuse the existing `<section data-tab="experiments">` (dashboard.html ~L717) and
`renderExperiments()` (~L2641). Restructure into two stacked blocks; the existing GPU-activity
sessions move to a collapsible "supporting context" block at the bottom.

### 7.1 Runs table (top)

`GET /api/runs?range=&status=` → one row per run:

| Name | Source | Status | Started | Duration | Key metrics | Energy | Cost |
|---|---|---|---|---|---|---|---|
| sft-llama3-lora | 🟣 jupyter | ✅ finished | 14:02 | 1h 47m | loss 0.21 · lr 2e-4 | 0.83 kWh | €0.21 |
| nightly-eval | 🟠 mlflow | ▶ running | 02:00 | 3h 12m | acc 0.79 | 1.9 kWh | €0.38 |

- **Source badge** colors: jupyter, colab, kaggle, mlflow, api, cli (small pill, like the existing
  serving/training pills, dashboard.html ~L169).
- **Status** dot: running ▶, finished ✅, failed ⚠, killed ⛔.
- **Key metrics** = `metrics_latest` (latest value per key, capped to ~3).
- **Energy + Cost** straight from `/api/runs` (already integrated + dual-tariff priced). A range
  picker (reuse the page's `range` selector) and a `status` filter dropdown.

### 7.2 Run detail (click a row) → `GET /api/runs/<id>`

- Header: name, source badge, status, host, duration, params (pretty JSON), tags, notes.
- **Metric charts**: one Chart.js line per metric key (loss / lr / val_acc …), x = step (or ts),
  from `metrics`. Multiple keys → small-multiples or a key selector.
- **Resource + cost chart**: a dual-axis Chart.js from `resource` — GPU **power (W)** + **util (%)**
  over the run window — captioned with **energy kWh + cost** (`energy_kwh`, `cost`, `currency`), using
  the same green/blue palette as the cost card. This is the "what it cost" graph the maintainer asked
  for, on the same time base as the loss curve so you can see *which phase* burned the money.
- A "Pull snippet" helper shows the one-liner `homelab.pull("<id>")` so the user can read it back from
  a notebook.

### 7.3 Settings — Integrations block (Alerts/settings tab, ~L761)

- **API key**: shows `key_masked` from `GET /api/integration/key`; a **Regenerate** button calls
  `POST {regenerate:true}` and reveals the full key **once** with a copy button and the ready-to-paste
  snippet:
  ```python
  import homelab_run as homelab
  homelab.configure(url="http://<this-host>:9800", key="hlm_…")
  ```
  Plus a download link for `homelab_run.py` (serve it as a static asset).
- **MLflow**: `mlflow_uri` text field + optional `mlflow_token` (secret, shows `mlflow_token_set`),
  a **Test** button (one-shot reachability probe), a **Sync now** button
  (`POST /api/integration/mlflow/sync`), and a `mlflow_push` toggle. Caption notes Colab/Kaggle need
  the hub reachable via Tailscale/ngrok.

### 7.4 Existing GPU-activity sessions — kept as supporting context

`/api/sessions` and the current sessions table stay, relabeled "GPU activity (auto-detected)" under
the Runs table. They answer "what used the GPU even if nobody pushed a run" and remain useful when no
SDK/MLflow is wired up; the new Runs are the primary, user-driven view.

---

## 8. Build checklist (order)

1. `app.py`: `_DB_SCHEMA` += `runs` + `run_metrics` + indexes (§2); add `_RUNS_MIGRATIONS=()` loop.
2. `app.py`: `SETTING_DEFAULTS` += `api_key, mlflow_uri, mlflow_token, mlflow_push`;
   `SETTING_SECRETS` += `api_key, mlflow_token` (§1.1).
3. `app.py`: `secrets`/`hmac` key helpers + `require_api_key` decorator + `/api/integration/key` (§1).
4. `app.py`: ingest routes `POST /api/runs`, `PATCH /api/runs/<id>`,
   `POST /api/runs/<id>/metrics`, `POST /api/runs/<id>/finish` + size limits (§3); stale-run janitor.
5. `app.py`: `_cost_ctx`/`_run_cost_window`/`_safe_json` helpers + read routes
   `GET /api/runs`, `GET /api/runs/<id>` (§4).
6. `app.py`: `_mlf` REST helper + `sync_mlflow()` + collector hook + `/api/integration/mlflow/*` (§6).
7. Ship `homelab_run.py` (repo root + served at `/static/homelab_run.py`) (§5).
8. `static/dashboard.html`: Runs table + detail + charts in `renderExperiments()`; Settings
   integration block; source-badge CSS (§7).
9. Smoke test: generate key → SDK `with run(): log_metric` → row appears with non-zero
   energy/cost over its window → `pull()` returns metrics + resource series → set `mlflow_uri` →
   `sync_mlflow()` mirrors an MLflow run that then shows cost. Verify a `running` run with stale
   heartbeat flips to `killed`. Verify writes 401 without the key; reads work without it.

### Risks / notes
- **Cost attribution is whole-GPU over the run window**, not per-process — honest and simple on a
  single-GPU homelab; document it ("GPU energy during your run window"). Overlapping concurrent runs
  each get the full window's energy (they share the box) — acceptable, and called out in the UI.
- **Metric volume**: cap points/request (1000), buffer client-side, and index `run_metrics(run_id,
  key,ts)`. Retention can piggyback on the existing `RETENTION` sweep if desired (delete metrics for
  runs whose `ended_at < now-RETENTION`).
- **Fail-closed key**: ingest is disabled until a key is generated — an instance isn't writable by
  default even on a hostile LAN.
- **MLflow pull is idempotent** via `uniq(source,ext_id)` + metric-history replace; safe to run every
  5 min. Large MLflow servers: bound `experiment_ids` or add a `since` filter on `runs/search` if it
  gets heavy.

---

## Sources
- MLflow REST API reference (endpoints, ms-epoch timestamps, log-batch 1000 cap):
  https://mlflow.org/docs/latest/api_reference/rest-api.html
- MLflow Search Runs (RunInfo/RunData shape, latest-value-per-key in `data.metrics`, pagination via
  `next_page_token`): https://mlflow.org/docs/latest/ml/search/search-runs/
- Existing in-repo cost/tariff design reused for run cost integration:
  `design/ai-cockpit/research-C-cost-tariffs.md`.
