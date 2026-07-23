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
