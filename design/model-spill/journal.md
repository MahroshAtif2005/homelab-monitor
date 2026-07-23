# Model RAM-spill visibility + per-model caller attribution

**Started:** 2026-07-23 · branch `feat/model-spill-attribution` off `next` · target: dev instance ardi:9801

## Ask (Arsen, 3 messages)
1. AI Models tab: for the runs, when a model spilled into system RAM, show the
   total (GPU + RAM) and how many runs spilled vs ran fully on GPU.
2. Same visibility on the GPU tab.
3. AI Models tab doesn't show WHICH app is using a model (pipeline, open-webui, …).

## Before-state (captured from prod ardi:9800, 2026-07-23)
- `models` table stores only `(ts, service, model, vram)` — `probe_ollama` reads
  `size_vram` from `/api/ps` and throws away `size`, so spill is invisible.
- `model_summary` (AI Models tab rows): only `peak`/`avg` VRAM. No run counts, no RAM.
- Caller attribution exists per **server** only (`edges` table → "Driven by" row):
  live prod shows jarvis-server, open-webui, langfuse → ollama/whisperx, but nothing
  ties a caller to a *model* when the server hosts several.
- The Benchmark Lab (backend/bench.py) already models this exact concept:
  `size - size_vram = ram_offload_mb`, fit = vram/partial/cpu. Reuse the vocabulary.

## Design
- `probe_ollama` → 3-tuples `(name, vram_mb, ram_spill_mb)`; other probes stay
  2-wide, `probe_models` normalizes to 3-wide (ram=None = unknown).
- DB: `models` gains `ram REAL` column (migration) + `idx_models_ts`, `idx_edges_ts`.
- Runs = contiguous residency sessions in `models` rows (gap > max(3×INTERVAL, 90s)
  splits a run — matches ollama keep-alive unload). SQL window-function derivation.
- `model_summary` gains: `peak_ram`, `runs`, `runs_spilled`, `used_by` (top callers
  by time-overlap of caller↔server connection samples with the model's residency).
- UI: AI Models rows get spill split in Now cell + "Loaded · RAM spill" badge,
  Runs column ("N · M spilled"), Used-by pills. GPU tab gets a warning banner when
  a model is spilling now + range totals. Insight Feed entry while spilling.
- Prometheus: `homelab_model_ram_spill_mb{server,model}` gauge.

## Log
- Branch created; recon done (probe→collector→DB→/api/data→renderModels chain traced).
- Implemented end-to-end; full suite on ardi (python:3.12 container): 528 passed,
  6 failed — the same 6 fail on the origin/next baseline (maintenance-window +
  no-silent-swallow), pre-existing and unrelated.
- Deploy note: ardi:9801 was still held by the leftover build-off container
  `homelab-monitor-next` (arena judged 2026-07-15) → stopped it (docker start
  brings it back) so the dev instance could bind. Old local WIP in
  /home/ardi/homelab-monitor-dev stashed as "pre-spill-deploy WIP" (its
  os-upgrade half was already merged upstream as d1906a5).
- Live verification on 9801: forced a real spill (nomic-embed-text with
  num_gpu:0, 120s keep-alive) → now.models {vram:0, ram:359}, summary
  {runs:1, runs_spilled:1, peak_ram:359}, warning insight, Prometheus
  homelab_model_ram_spill_mb=359. Fully-CPU model now shows Loaded (was Idle).
- UI verified via Playwright, no JS errors: AI Models rows show "1 · 1 spilled"
  + Used-by pills (jarvis-server 50% on qwen3-coder:30b); GPU tab shows the
  amber spill banner with per-model spilled-run counts.
