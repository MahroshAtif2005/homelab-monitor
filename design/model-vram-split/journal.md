# AI tab: VRAM split (weights vs context) + faster live refresh

**Started:** 2026-07-25 · builds on `feat/model-spill-attribution` · target: dev instance ardi:9801

## Ask (Arsen)
1. AI Models tab: faster refresh if possible (nice to have).
2. Most importantly: divide VRAM usage between **model weights** and **context
   size** — to understand better why and when a model spills into system RAM.

## Before-state (to capture)
- Refresh today: backend collector samples every `SAMPLE_INTERVAL` (10s default),
  frontend polls `/api/data` every 15s → worst-case ~25s staleness on the AI tab.
- VRAM today: ollama `/api/ps` gives `size` (total resident) and `size_vram`;
  we store vram + ram-spill split, but nothing says how much of the residency is
  weights vs KV-cache/context — so "why did it spill" is guesswork.

## Design (draft)
- **Weights**: on-disk GGUF size from ollama `/api/tags` (mmap'd ≈ in-memory
  weights). Cached per model alongside `_OLLAMA_META` (/api/show cache).
- **Context/overhead** = (vram + ram) − weights, clamped ≥0 — KV cache + compute
  buffers. Honest label: "context/KV + buffers".
- **Runtime ctx**: newer ollama `/api/ps` exposes `context_length` (the actual
  num_ctx of the load — the lever that grows the KV). Carry it through the probe
  → LATEST → UI chip "@ N ctx" next to the model-max ctx chip.
- **Faster refresh**: new light endpoint `/api/ai/now` (no DB, throttled
  on-demand ollama /api/ps re-probe, ~3s TTL) + AI-tab-only 5s poll in the
  dashboard while the tab is visible and auto-refresh is on.
- No DB migration — the historical "when it spilled" question is already served
  by runs/runs_spilled/peak_ram from the spill branch.

## Before-state (captured 2026-07-25, prod ollama + dev 9801)
- ardi ollama is **0.31.1** — `/api/ps` DOES include `context_length` (runtime
  num_ctx): nomic-embed-text `size` 323MB, `size_vram` 323MB, `context_length`
  2048. `/api/tags` has per-model on-disk `size` (weights bytes).
- 9801 `/api/data` (v0.25.0): `now.models` rows carry only vram/ram; model_meta
  has param/quant/ctx(max)/caps — **no weights, no runtime ctx**.
- The killer real example already in history: `gemma4:26b` peak 16,579MB VRAM +
  15,347MB RAM ≈ 32GB resident vs a 17.9GB weights file → ~14GB was context/KV —
  invisible before this feature.
- PR #243 (spill branch) still open → this work stacked on it as
  `feat/vram-weights-ctx-split`.

## Log
- Recon: probe_ollama → probe_models (3-wide rows) → collectors models tuples →
  DB models table + LATEST.models dicts; meta via /api/show cache; UI
  renderModels() joins now+summary; frontend 15s global poll + 10s sampler.
- Implemented:
  - probes: 4-wide rows `(name, vram, ram, ctx_now)`; ctx from /api/ps
    `context_length`.
  - collectors: 5-tuples through to LATEST (`ctx_now` per loaded model); DB
    insert unchanged (no migration); `_app.AI_SERVERS` (name/ip/provider) kept
    OUTSIDE LATEST so container IPs never reach the browser payload.
  - app.py: `_ollama_weights_mb` — per-IP /api/tags size cache (TTL 600s,
    60s refetch floor on unknown model, failure keeps old sizes);
    collect_model_meta attaches `weights_mb` to loaded models, self-heals into
    _OLLAMA_META on later samples; `ai_models_now()` — throttled (3s TTL)
    on-demand ollama re-probe merged over LATEST, empty/failed probe keeps the
    sampler view.
  - /api/ai/now (backend/api/gpu.py): no-DB/no-LOCK light payload
    {ts, probed_at, models, model_meta, serving, callers}.
  - Spill insight now explains WHY: "~W MB weights + ~K MB context/KV & buffers
    (running at N ctx) — smaller context window may fit VRAM".
  - UI: Now cell gains a weights-vs-ctx `.vsplit` stacked bar + caption
    ("weights 17.3 GB · ctx 3.4 GB"); meta chips show `@ 32K ctx` (runtime,
    with model-max in the tooltip) falling back to max-ctx chip; AI-tab-only
    5s fast poll of /api/ai/now (local host, auto-refresh on, busy-guarded).
- Tests: spill probe tests updated to 4-wide + new ctx cases; weights-cache
  tests (per-IP caching, refetch floor, failure keeps sizes, heal-later);
  new test_ai_now.py (replace-only-ollama, TTL cache, failure/empty keep view,
  idle 2-wide normalize, endpoint shape).
- Local Windows python is 3.8 → suite must run on ardi in python:3.12
  container (same as spill work).
- Full suite on ardi (python:3.12): **541 passed, 6 failed** — the same 6
  pre-existing baseline failures (maintenance-window + no-silent-swallow),
  unrelated. All new tests green.
- Deployed to ardi:9801 (compose dev build). Live verification:
  - `/api/ai/now` served the idle catalogue instantly; after an embed call the
    loaded model appeared within ~2s with `ctx_now: 2048` (vs up to ~25s before).
  - Next sampler pass attached `weights_mb: 262` for nomic-embed-text
    (308MB resident → weights 262 + ctx/KV 46).
  - Forced spill (num_gpu:0): insight now reads "~262 MB weights + ~97 MB
    context/KV & buffers (running at 2,048 ctx) — a smaller context window
    would shrink the KV cache and may fit VRAM."
  - Playwright on the tab: **no JS errors**; split bar + caption
    "weights 262 MB · ctx 97 MB" + "@ 2.0K ctx" chip render (after-ai-card.png,
    after-ai-tab.png).
- CHANGELOG Unreleased entries added. PR stacked on #243's branch.
