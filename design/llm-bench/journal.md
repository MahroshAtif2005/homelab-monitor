# LLM Benchmark Lab — build journal

**Goal (Arsen, 2026-07-19):** New feature for the homelab-monitor to benchmark LLMs across
the fleet (or a selected box). For each model measure: does it fit in VRAM or spill to RAM
and by how much; the optimal context size that still fits VRAM (and whether to cap it);
tokens/sec (generation + prompt eval); load time; power/energy. Choose GPU allocation
(all VRAM vs single card). Store benchmarks so they don't need re-running often, but allow
rerun. SOTA UI: detailed graphs, bars, model logos.

**Constraints / context:**
- Repo: SikamikanikoBG/homelab-monitor, local at R:\projects\homelab-monitor-dev
- Develop on `next` (feature branch off next), deploy dev to ardi:9801, never touch prod (9800).
- Monitor is read-only over the fleet for *telemetry*, but benchmarking is an opt-in ACTIVE op
  (drives ollama HTTP API). Keep it explicit & user-triggered.
- UI must reuse existing component classes (mc-panel, mc-sdot/mc-pill, .rb/.btn-mini, sic() icons).
- No competitor names in copy. Jarvis approves any user-facing comms.

## Before-state (captured at start)
- On branch `next`, clean, up to date with origin/next. Latest release v0.25.0.
- Fleet: ardi (local hub, RTX ~29.7GB VRAM), Work, JarvisVM online; cloudy/oldie offline.
- ollama on ardi reachable; models: qwen3-coder:30b (Q4_K_M, loaded), qwen3-coder-30b-64k,
  gemma4:26b, gemma4:latest(8b), gemma3:1b, nomic-embed-text. Whisper ASR loaded too.
- Existing infra to mirror: experiments/runs (priced by GPU energy), models registry,
  get_ai_models/get_installed_models, costs engine.

## Key discoveries
- Deploy loop: push `next` → CI `smoke` (test_snapshots.py) green → `publish-next` ships
  `sikamikaniko123/homelab-monitor:next` → Watchtower redeploys ardi:9801 container
  `homelab-monitor-next`. (The /home/ardi/homelab-monitor-dev dir is stale — ignore it.)
- **ardi has TWO GPUs**: Quadro P2000 (5 GB) + RTX 3090 (24 GB) → total 29.7 GB. This is
  exactly why Arsen wants "all VRAMs vs which one" — the P2000 drags throughput if ollama
  spreads onto it. GPU-allocation insight is real and valuable.
- ollama 0.31.1 local on ardi. Existing infra to reuse: `_http_post_json`, `_model_registry`,
  `_run_cost_window` (energy+cost from samples), `smi()`/`_gpu_num()`, `LATEST["gpus"]`
  per-device, `_DISK_SCAN` async-job pattern (module dict + lock + daemon thread + state).
- Python 3.8 locally — avoid `X|Y` / `list[...]` runtime annotations.

## Design — "Benchmark Lab"
Active, opt-in benchmark of ollama models (fleet or a chosen box) → stored, rerunnable.

**Metrics per (model × ctx × gpu-layers):** gen tok/s, prompt tok/s, TTFT, load ms, VRAM used,
RAM-offload MB (size − size_vram from /api/ps), gpu_fraction, fit verdict (vram/partial/cpu),
per-GPU landing (nvidia-smi mem.used delta → which card), power/energy/cost (via _run_cost_window).

**Sweep logic:** for each model, warm-load then time a fixed generation across a list of ctx sizes;
find max ctx that stays fully in VRAM → recommended ctx cap. num_gpu layer control = the real
"how much spills to RAM" knob (per-request). Physical device pinning noted as advice (server env).

**Storage:** `bench_runs` (parent: model/family/quant/status/config/summary/gpu/energy/cost/times)
+ `bench_points` (per ctx/config row). New run each execution → history; rerun compares over time.

**API (backend/api/benchmarks.py):** POST /api/bench (start job), GET /api/bench (list+live),
GET /api/bench/<id>, DELETE /api/bench/<id>, POST /api/bench/cancel, GET /api/bench/targets.
Single-flight worker (GPU is shared) — module job store like _DISK_SCAN.

**UI — new "Benchmarks" tab:** launcher (model multi-select w/ logos, ctx chips, gpu mode, gen tokens),
live progress, leaderboard tok/s bars, VRAM-fit stacked bars, ctx-sweep line chart, per-model cards
(best tok/s, load, max-fit ctx, recommended cap, fit pill, energy/cost, which-GPU note, rerun/delete),
stored-history sparkline. Reuse mc-panel/sic()/theme vars. No competitor names.

## Log
- (start) Located repo (R:\projects\homelab-monitor-dev), mapped tree, captured live GPU/model state.
- Mapped backend: experiments/runs pattern, schema, ollama poller, async job, cost+gpu helpers.
- Created branch feat/llm-benchmarks off next. Launched frontend-mapping Explore agent.
- Built backend: backend/bench.py (pure engine, injected I/O), backend/db/repos/bench.py,
  backend/api/benchmarks.py (single-flight async worker), 2 tables in _DB_SCHEMA, blueprint
  registered. Added MCP tools get_benchmarks/get_benchmark (+client). CHANGELOG entry.
- Built frontend: new "Benchmarks" tab (nav+section+CSS+JS), launcher (model multi-select w/
  lettermark logos, ctx chips, GPU-placement select), live progress, tok/s leaderboard bars,
  per-model cards (fit bars, recommended ctx, energy/cost, rerun/delete), context-sweep charts.
- 22 unit tests (tests/test_bench.py) — pure helpers + injected-I/O orchestration. JS syntax
  checked via node --check.
- VALIDATION on ardi (Python 3.13, real dual-GPU): full app imports, all routes OK. Real
  benchmark of gemma3:1b → 49.4 tok/s gen, 1314 tok/s prompt, load 5.2s, 825MB VRAM, 0 offload,
  fit=vram, recommended ctx 8192, 0.0003 kWh @ 51W. Full suite: 94 passed (snapshots+bench+swallow).
  Confirmed the only pre-existing failures (test_public_status maintenance) also fail on pristine
  `next` — time-dependent, unrelated, not the CI gate. Fixed backend/ silent-broad-except gate.
- Per-GPU "which card" attribution is best-effort (nvidia-smi delta) — clean only when the GPU is
  otherwise idle; on a busy box (qwen churning) it returns empty and the UI hides it gracefully.
  Physical device pinning is advice-only (needs ollama server env). Future: real brand SVG logos.

## v2 (2026-07-19, branch feat/bench-gpu-select) — GPU choice, setup display, compare
Arsen: want to CHOOSE which card(s) each test runs on, show the setup in results, and overlay
benchmarks on one chart to compare. Also discovered (via the Lab's first real use!) that ardi's
ollama was pinned to the P2000 — fixed to the 3090 (7→133 tok/s), see [[reference_ardi_ollama_gpu]].

- **Device selection mechanism:** ollama has no per-request GPU choice, so choosing card(s) spins up
  a THROWAWAY ollama container pinned to them (Docker API create/start, `DeviceRequests` = chosen
  GPU indices), mounting the same `vol_ollama` models, on a FREE port; run the sweep; force-remove.
  Main ollama untouched. Gated behind ENABLE_CONTROLS (it launches a container). Reused the proven
  `_docker_req` POST /containers/create pattern from the self-update flow.
- **Robustness fix:** first cut reused a fixed port → 2nd back-to-back job failed to bind. Now a free
  port per job + wait-until-old-container-gone. Validated: 3090 (132 tok/s) then P2000 (46 tok/s)
  back-to-back both done, clean teardown, no leftover.
- **Setup recorded/shown:** cfg stores `devices` + `device_label`; leaderboard rows + cards show
  "⚙ RTX 3090"/"Quadro P2000".
- **Compare view:** tick N stored runs → overlay tok/s-vs-ctx + VRAM-vs-ctx on one chart, legend
  "model @ setup", distinct palette per run. Pure frontend over /api/bench/<id>.
