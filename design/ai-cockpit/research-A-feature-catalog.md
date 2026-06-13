# HomeLab Monitor → AI/DS Cockpit: Ranked Feature Catalog

**Goal:** Turn HomeLab Monitor into THE go-to dashboard for AI engineers / data scientists running home labs. Grow ~100 → ~1000 GitHub stars in a week with a "Towards Data Science"-worthy release.

**Hard constraints (respected throughout):** pure Python stdlib + Flask only (NO psutil/torch/prometheus_client/etc.), reads `/proc` + `nvidia-smi` + Docker socket + simple HTTP to local services, SQLite storage, Chart.js frontend, Linux hub + remote hosts over SSH.

**Author's stance:** Opinionated. The existing app is already excellent at *infrastructure* monitoring and *model-server presence*. What it lacks is the **AI-workload-native** layer: the things you actually stare at during a training run or while serving an LLM — throughput, throttling, stalls, efficiency, experiment context. That layer is where the screenshots that get shared live.

---

## Current state (what already exists — don't rebuild)

From `app.py`:
- GPU sample loop: `--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu` (line ~3290) → SQLite `samples`.
- Per-process VRAM: `--query-compute-apps=pid,used_memory` → attributed to Docker service via `/proc/<pid>/cgroup` (line ~3311, `service_for_pid`).
- Model-server probes: Ollama `/api/ps` + `/api/tags`, vLLM/llama.cpp/etc. via `/v1/models`, ComfyUI, A1111, Triton, Wyoming, TGI, etc. (lines 247-360). **Only model NAME + VRAM is captured — no performance metrics.**
- Caller attribution via `/proc/net/tcp` inode→pid (`sample_callers`).
- Models tab with VRAM timelines, OOM markers, caller attribution.
- Multi-host over SSH; host metrics from `/proc/{stat,meminfo,loadavg,uptime,net/dev}`.

**The gap in one sentence:** it knows *which* models exist and *how much VRAM* they hold, but nothing about *how hard they're working, how efficiently, whether they're healthy, or what experiment they belong to.*

---

## Research findings (data sources verified)

### 1. Ollama `/api/show` — rich model metadata (free, one call per model)
`POST /api/show {"model": "<name>"}` returns a `details` object: `format` (gguf), `family`, `parameter_size` (e.g. `"8.0B"`), `quantization_level` (e.g. `"Q4_K_M"`), plus a `model_info` block with architecture detail (context length via `<arch>.context_length`, embedding length, head counts) and a `capabilities` array (`completion`, `vision`, `embedding`, `tools`). This is a **passive metadata call** — no inference triggered.
**Per-request throughput:** Ollama does NOT expose a passive metrics endpoint. Tokens/sec (`eval_count` / `eval_duration`) only come back **in the response body of an actual generate/chat call** — you cannot scrape it without issuing inference. So token-rate for Ollama must be **inferred** (util + VRAM activity) or captured by an opt-in benchmark, not scraped live. Important constraint to design around.

### 2. vLLM `/metrics` — Prometheus text, cheaply scrapable (stdlib only)
Plain-text `GET /metrics` (no auth by default). Parse with a ~15-line regex parser — no `prometheus_client` dependency needed. Verified exact metric names (vLLM stable docs):
- `vllm:num_requests_running` (gauge) — active requests in batch.
- `vllm:num_requests_waiting` (gauge) — queue depth. **This is the headline gauge — queue building = you're saturated.**
- `vllm:kv_cache_usage_perc` (gauge, 0–1) — KV-cache fill. Near 1.0 = imminent preemption/OOM.
- `vllm:prompt_tokens` / `vllm:generation_tokens` (counters) — diff over time → **real tokens/sec, no inference needed.**
- `vllm:time_to_first_token_seconds` (histogram) — TTFT.
- `vllm:request_time_per_output_token_seconds` (histogram) — inter-token latency.
- `vllm:e2e_request_latency_seconds` (histogram).
Same `/metrics` shape applies to TGI (`text-generation-inference`), llama.cpp `--metrics`, and TEI. **This is the single biggest free win** — real serving telemetry from one cheap HTTP GET.

### 3. nvidia-smi — high-value fields the app does NOT yet query
Add to the existing `--query-gpu` CSV (all single-call, negligible cost):
- `utilization.memory` — **memory-controller** util (distinct from `utilization.gpu`). High mem-util + low SM-util = memory-bandwidth-bound → the classic "why is my GPU slow" tell.
- `clocks_throttle_reasons.active` (bitmask, hex) — **thermal/power throttling detection.** Decode `sw_power_cap`, `hw_slowdown` (thermal), `hw_thermal_slowdown`. This is gold: "your training is 18% slower because you're power-capped."
- `pstate` (P0–P12) — performance state; P0=full, high P-state under load = problem.
- `power.limit` — so you can show draw-vs-limit headroom (and detect power-capping).
- `clocks.current.sm` / `clocks.max.sm` — clock droop.
- `total_energy_consumption` (millijoules, monotonic since driver load) — **enables true energy/Joules accounting per run without integrating power samples.** Datacenter/newer cards only; fall back to integrating `power.draw` over time where absent.
- `encoder.stats.sessionCount` / `utilization.encoder` / `utilization.decoder` — NVENC/NVDEC (relevant for video/transcription labs).
- `fan.speed`, `temperature.memory` (HBM/GDDR6X temp — throttles before core on 3090/4090).
Per-process *compute* via `nvidia-smi pmon -c 1` (sm/mem/enc/dec per pid) and `dmon` (rolling) exist but are higher-overhead; `--query-compute-apps` (already used) is enough for VRAM.

### 4. Jupyter detection — port + `/proc` cmdline, REST API with token caveat
Detect by listening port (8888/8889/8890) + `/proc/<pid>/cmdline` containing `jupyter-lab`/`jupyter-notebook`/`jupyter-server`. The REST API (`GET /api/sessions`, `/api/kernels`, `/api/status`) lists running notebooks + kernel states (`idle`/`busy`) — **but requires the token** (`?token=` or `Authorization: token …`) when auth is on. The token is recoverable locally without the user pasting it: read `/proc/<pid>/cmdline` for `--IdentityProvider.token`/`--ServerApp.token`, or parse `~/.local/share/jupyter/runtime/*server*.json` (or `jpserver-*.json`) which contains url+token for each running server. With that, surface: # active kernels, which notebooks are busy, idle kernels squatting on VRAM (the #1 home-lab VRAM-leak culprit). Without token: still detect presence + the squatting-VRAM signal via PID→compute-apps.

### 5. Experiment trackers — passive presence + deep-link
- **TensorBoard**: default port **6006**, cmdline `tensorboard --logdir …`. Surface logdir + clickable link.
- **MLflow**: tracking server default port **5000**, cmdline `mlflow server`/`gunicorn … mlflow`. Its REST API (`/api/2.0/mlflow/experiments/search`, `/runs/search`) is unauthenticated by default → can show experiment + run counts and active runs.
- **Weights & Biases**: SaaS by default (no local port); self-hosted `wandb/local` on **8080**. The useful passive signal is the **`wandb-service`/`wandb` agent child process** in `/proc` cmdline next to a training PID, plus the `wandb` dir — i.e. "this run is being tracked in W&B," link out to it if `wandb/debug.log` yields the run URL.
- A passive monitor's job here: **detect + deep-link + run/experiment counts**, not reimplement the tracker.

### 6. Training-run detection — `/proc` cmdline classification
Recognize a training process by scanning `/proc/<pid>/cmdline` (the app already walks `/proc` for cgroups, so the machinery exists):
- Launchers: `torchrun`, `torch.distributed.run`, `accelerate launch`, `deepspeed`, `python -m torch.distributed.launch`, `mpirun … python`.
- Scripts: argv contains `train.py`/`finetune.py`/`sft.py`/`pretrain.py`/`train_*.py`, or framework tells `transformers`, `--deepspeed`, `--fsdp`, `trl`, `axolotl`, `llama-factory`, `unsloth`, `lit-gpt`.
- A PID that holds GPU compute (`--query-compute-apps`) **and** isn't a known inference server is, by elimination, a training/experiment job.
Track its lifetime (first-seen ts → exit), integrate power → **energy + cost**, watch GPU-util time-series → **stall detection** (util collapses from ~95% to <10% for N samples while the process is alive and still holds VRAM = stalled/deadlocked/data-loader-starved). ETA from a logfile is fragile (no standard); skip ETA-from-logs in v1, do "time elapsed + stall alert" instead.

### 7. Tokens-per-Joule / GPU efficiency — the writeup money metric
Combine #2 (tokens, from vLLM counters) or inferred tokens with #3 energy:
- **Tokens / Joule** = Δgeneration_tokens / Δenergy (or Δ(power·dt)). Headline efficiency number; great for "which model/quant is most efficient on my hardware" charts.
- **VRAM efficiency** = useful KV-cache vs allocated; **MFU-lite** proxy = sustained SM-util × clock-ratio.
- **Idle-but-warm waste** = energy burned while util≈0 but model loaded (the OLLAMA_KEEP_ALIVE tax). Quantifiable in kWh and €/$ — extremely shareable.

---

## RANKED FEATURE CATALOG (by impact-per-effort)

Effort: S ≈ <½ day, M ≈ 1–2 days, L ≈ 3+ days. Impact 1–5 toward the star goal.

| # | Feature | Effort | Impact | I/E |
|---|---------|--------|--------|-----|
| 1 | vLLM/TGI `/metrics` live serving telemetry | M | 5 | ★★★★★ |
| 2 | GPU throttle & efficiency fields in the sample loop | S | 5 | ★★★★★ |
| 3 | Training-run cards (auto-detect, lifetime, stall alert) | M | 5 | ★★★★★ |
| 4 | Tokens-per-Joule / energy & cost-per-run panel | M | 5 | ★★★★ |
| 5 | Ollama `/api/show` model metadata enrichment | S | 4 | ★★★★ |
| 6 | Jupyter kernel awareness + idle-VRAM-squatter alert | M | 4 | ★★★★ |
| 7 | Experiment-tracker auto-discovery + deep-links | S | 3 | ★★★★ |
| 8 | "Idle but warm" wasted-energy / keep-alive tax meter | S | 4 | ★★★★ |
| 9 | OOM / preemption early-warning (KV-cache + VRAM headroom) | S | 4 | ★★★ |
| 10 | Memory-bandwidth-bound detector (mem-util vs sm-util) | S | 3 | ★★★ |
| 11 | Per-model serving leaderboard (tok/s, TTFT, $/1M tok) | M | 4 | ★★★ |
| 12 | Multi-GPU per-card breakout + imbalance/NVLink hints | M | 3 | ★★ |
| 13 | Quant/param-size badges + "fits in VRAM?" planner | S | 3 | ★★★ |
| 14 | Run history / experiment journal (SQLite-backed timeline) | M | 3 | ★★ |
| 15 | Datasets & checkpoint disk-usage watcher | S | 2 | ★★ |

Detailed entries below, ordered by rank.

---

### #1 — vLLM / TGI `/metrics` live serving telemetry  `[M, impact 5]` 🏆 WOW
- **(a) Value prop:** Real-time tokens/sec, queue depth, KV-cache fill, and TTFT for your local LLM server — the numbers you currently only see by tailing logs.
- **(b) Source/collect:** `GET http://<ip>:<port>/metrics` (vLLM default 8000; TGI 80/3000/8080 — reuse the port lists already in `probe_*`). Stdlib `http.client`, parse Prometheus text with a small regex (`^(\w[\w:]*)(\{[^}]*\})?\s+([0-9.eE+-]+)$`). Gauges read directly; counters (`vllm:generation_tokens`) → tokens/sec by storing last value+ts and diffing each sample tick (you already have a per-tick loop and SQLite). Histograms: read `_sum`/`_count` and diff for a running-average TTFT.
- **(c) Effort:** M — one parser + 4–5 new SQLite columns + a Chart.js panel on the models tab.
- **(d) Impact:** 5. This is the headline "it actually understands my inference server" feature.
- **(e) Risk:** Metric names drift across vLLM versions (`gpu_cache_usage_perc` was renamed to `kv_cache_usage_perc`) — match on a prefix/alias set, degrade gracefully. `/metrics` may be disabled (`--disable-log-stats`); treat absence as "no telemetry," don't error.

### #2 — GPU throttle & efficiency fields in the sample loop  `[S, impact 5]` 🏆 WOW
- **(a) Value prop:** Instantly see *why* the GPU is slow — thermal/power throttling, memory-bandwidth-bound, clock droop — not just a util %.
- **(b) Source/collect:** Extend the existing CSV (line ~3290) to: `--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,power.limit,temperature.gpu,temperature.memory,clocks.current.sm,clocks.max.sm,clocks_throttle_reasons.active,pstate,total_energy_consumption,fan.speed`. Same single subprocess call already in place. Decode `clocks_throttle_reasons.active` hex bitmask → human reasons (`SW Power Cap`, `HW Thermal Slowdown`). Keep the existing `[N/A]`-tolerant `_gpu_num` parsing — many fields are `[Not Supported]` on consumer cards.
- **(c) Effort:** S — extend one query string + a few columns + a throttle badge.
- **(d) Impact:** 5. A red "THROTTLING: power-capped, −18% clocks" banner is exactly the screenshot people post.
- **(e) Risk:** `total_energy_consumption`, `temperature.memory`, `clocks.max.sm` unsupported on some consumer GPUs → fall back (integrate `power.draw` for energy; hide unsupported badges). Don't widen the CSV so far that one bad field breaks the row — keep the defensive per-field parse.

### #3 — Training-run cards: auto-detect + lifetime + stall alert  `[M, impact 5]` 🏆 WOW
- **(a) Value prop:** Every training/fine-tune job shows up automatically as a live card — elapsed time, GPU/VRAM/power it's using, and a loud alert when it stalls (util collapse) so you don't lose 6 hours to a dead data loader.
- **(b) Source/collect:** Scan `/proc/<pid>/cmdline` for launchers/scripts (list in finding #6). Cross-reference with `--query-compute-apps` PIDs (already collected) to confirm it's on the GPU and exclude known inference servers. Persist first-seen ts in SQLite; on each tick record util/power for the run's PID-set. Stall = util time-series drops below threshold for K consecutive samples while PID alive and VRAM still held. Energy via #2.
- **(c) Effort:** M — cmdline classifier + a `runs` table + a card UI + alert wiring (reuse OOM-marker plumbing).
- **(d) Impact:** 5. "It told me my training stalled at 2am" is a story people share. No free tool in this niche does passive stall detection.
- **(e) Risk:** False positives (a `train.py` that's a script name but not training; multi-PID DDP needs grouping by parent/`torchrun` session). Keep classification conservative + allow a config allow/deny list. Don't kill anything — observe only.

### #4 — Tokens-per-Joule / energy & cost-per-run panel  `[M, impact 5]` ⭐ WOW
- **(a) Value prop:** "This fine-tune cost €0.42 and 1.9 kWh" / "Qwen-Q4 gives you 3.1× more tokens-per-Joule than the FP16 build on your 4090." The numbers nobody else surfaces.
- **(b) Source/collect:** Energy from `total_energy_consumption` deltas (#2) or ∫power.draw·dt. Tokens from vLLM counters (#1) or, for Ollama, an opt-in one-shot benchmark. Cost = energy(kWh) × user-set €/kWh (one config field). Attribute energy to a run (#3) or a model-server (existing attribution).
- **(c) Effort:** M — mostly derived math + one config value + a panel.
- **(d) Impact:** 5 for shareability; the TDS-article hero chart.
- **(e) Risk:** `nvidia-smi` power is known to under-sample / lag on consumer cards (cite caveat) — label it "estimate." Ollama tok/s needs inference to measure; be explicit it's a benchmark, not passive.

### #5 — Ollama `/api/show` model metadata enrichment  `[S, impact 4]`
- **(a) Value prop:** Each Ollama model shows param size, quant level, context length, and capabilities (vision/tools) at a glance — no `ollama show` in a terminal.
- **(b) Source/collect:** For each name from `/api/ps`+`/api/tags`, `POST /api/show {"model":name}` → `details.parameter_size`, `details.quantization_level`, `model_info["<arch>.context_length"]`, `capabilities`. Passive (no inference). Cache by name (immutable per tag).
- **(c) Effort:** S — extend `probe_ollama`, add a couple of columns.
- **(d) Impact:** 4. Cheap polish that makes the models tab feel authoritative.
- **(e) Risk:** N+1 calls if many models loaded — cache aggressively, 2s timeout (already the pattern), only for *loaded* models on the hot path.

### #6 — Jupyter kernel awareness + idle-VRAM-squatter alert  `[M, impact 4]` ⭐ WOW
- **(a) Value prop:** See every running notebook, which kernels are busy, and get pinged when an **idle kernel is hogging VRAM** — the #1 reason "my GPU is full but nothing's running."
- **(b) Source/collect:** Detect by port (8888/8889) + `/proc` cmdline. Recover token from cmdline `--ServerApp.token` or `~/.local/share/jupyter/runtime/jpserver-*.json`. `GET /api/sessions` + `/api/kernels` → notebook paths + `execution_state`. Map kernel PID → `--query-compute-apps` VRAM. Idle-squatter = kernel `idle` for >T minutes while holding >X MB VRAM.
- **(c) Effort:** M — detection + token recovery + REST parse + alert. Degrades to presence-only without token.
- **(d) Impact:** 4. Very relatable pain for DS folks; the squatter alert is screenshot-worthy.
- **(e) Risk:** Token recovery only works when the monitor can read the user's home/runtime dir (fine on the hub host; harder over SSH). Never display the token in the UI/DB.

### #7 — Experiment-tracker auto-discovery + deep-links  `[S, impact 3]`
- **(a) Value prop:** TensorBoard / MLflow / W&B show up as clickable tiles with run counts — your whole experiment stack in one place.
- **(b) Source/collect:** Port + cmdline: TB 6006 (`tensorboard --logdir`), MLflow 5000 (`mlflow server`), W&B local 8080 / agent cmdline. MLflow run counts via unauth `GET /api/2.0/mlflow/experiments/search`. Render link to the host:port.
- **(c) Effort:** S — extend the port/cmdline service catalog you already have.
- **(d) Impact:** 3. Convenience + "it knows my tools" credibility.
- **(e) Risk:** Auth-on trackers → presence-only. Don't assume MLflow API is open in all setups.

### #8 — "Idle but warm" wasted-energy / keep-alive tax meter  `[S, impact 4]` ⭐ WOW
- **(a) Value prop:** "Your loaded-but-idle models burned 0.7 kWh (€0.21) today doing nothing." Quantifies the OLLAMA_KEEP_ALIVE / squatting-kernel tax.
- **(b) Source/collect:** Per tick, if a model is loaded (VRAM held) AND util≈0, accumulate power·dt into a "wasted" bucket per server/model in SQLite. Convert to kWh + cost. You already track loaded models and power.
- **(c) Effort:** S — derived accumulation on existing data.
- **(d) Impact:** 4. Provocative, sticky, sharply on-theme for home labbers minding the electricity bill.
- **(e) Risk:** "Idle" threshold needs care (brief util dips ≠ idle); use a short rolling window.

### #9 — OOM / preemption early-warning  `[S, impact 4]`
- **(a) Value prop:** A warning *before* the CUDA OOM — VRAM headroom and KV-cache nearing 100%.
- **(b) Source/collect:** VRAM headroom from existing `memory.used/total`; KV-cache from `vllm:kv_cache_usage_perc` (#1). Threshold banner + extends existing OOM markers from reactive to predictive.
- **(c) Effort:** S (given #1/#2). 
- **(d) Impact:** 4. "It warned me before the OOM" is great word-of-mouth.
- **(e) Risk:** False alarms near steady-state high usage; require sustained breach.

### #10 — Memory-bandwidth-bound detector  `[S, impact 3]`
- **(a) Value prop:** Tells you the truth behind "GPU at 100%": compute-bound vs memory-bandwidth-bound (typical for LLM decode).
- **(b) Source/collect:** `utilization.memory` vs `utilization.gpu` (from #2). High mem-util + lower sm-util → "memory-bandwidth-bound" badge.
- **(c) Effort:** S (rides on #2).
- **(d) Impact:** 3. Educational; the kind of insight TDS readers love.
- **(e) Risk:** `utilization.memory` is a coarse controller-busy %, not true bandwidth — label as heuristic.

### #11 — Per-model serving leaderboard  `[M, impact 4]`
- **(a) Value prop:** Sortable table: tok/s, TTFT, KV-cache, $/1M tokens per model on *your* hardware. Your personal benchmark board.
- **(b) Source/collect:** Aggregate #1 + #4 over time windows into a `model_perf` table.
- **(c) Effort:** M — aggregation + table UI.
- **(d) Impact:** 4. Highly shareable ("my 4090 numbers"), drives repeat visits.
- **(e) Risk:** Fair comparison needs comparable workloads; mark as observed-traffic, not controlled bench.

### #12 — Multi-GPU per-card breakout + imbalance hints  `[M, impact 3]`
- **(a) Value prop:** Per-card util/VRAM/temp/power with imbalance + (heuristic) NVLink/PCIe-bottleneck hints for multi-GPU rigs.
- **(b) Source/collect:** App already parses per-card rows but aggregates them (line ~3306). Stop flattening; store per-`index`. Imbalance = util variance across cards during a DDP run.
- **(c) Effort:** M — schema + UI for N cards.
- **(d) Impact:** 3 (high for the multi-GPU subset).
- **(e) Risk:** Schema migration from aggregated columns; keep back-compat.

### #13 — Quant/param-size badges + "fits in VRAM?" planner  `[S, impact 3]`
- **(a) Value prop:** "Will Llama-70B-Q4 fit on your 24GB card?" — quick VRAM-fit estimate from param size + quant + context.
- **(b) Source/collect:** From #5 metadata (param_size, quant) + a simple VRAM formula (params×bytes/param + KV-cache(ctx)). Compare to detected `memory.total`.
- **(c) Effort:** S.
- **(d) Impact:** 3. Useful planner, fun to demo.
- **(e) Risk:** Estimates are approximate; label clearly.

### #14 — Run history / experiment journal  `[M, impact 3]`
- **(a) Value prop:** A scrollable timeline of past runs (model, duration, energy, cost, peak VRAM, stalled?) — lab logbook you didn't have to keep.
- **(b) Source/collect:** Persist #3 run records to a `runs` table; render a timeline (Chart.js / table).
- **(c) Effort:** M.
- **(d) Impact:** 3. Retention feature; not a first-screenshot hook.
- **(e) Risk:** DB growth — add retention/rollup like existing sample pruning.

### #15 — Datasets & checkpoint disk-usage watcher  `[S, impact 2]`
- **(a) Value prop:** Watch the dirs that eat your disk (HF cache, `checkpoints/`, `datasets/`) and warn before they fill it.
- **(b) Source/collect:** `os.scandir`/`statvfs` on user-configured paths (`~/.cache/huggingface`, etc.). Pure stdlib.
- **(c) Effort:** S.
- **(d) Impact:** 2. Solid utility, not a hook.
- **(e) Risk:** Recursive sizing is slow on huge trees — cache + cap depth.

---

## TOP 5 "WOW / SCREENSHOT-WORTHY" FEATURES

These are the ones that get posted to r/LocalLLaMA, r/homelab, and HN. Build these first.

1. **#3 Training-run stall alert** — "My dashboard caught a stalled fine-tune at 2am and saved 6 GPU-hours." No passive tool does this. Story-shaped.
2. **#1 Live vLLM serving telemetry (tok/s, queue, KV-cache, TTFT)** — the real-time inference cockpit screenshot. Instantly says "this tool gets LLM serving."
3. **#2 GPU throttling banner** — a red "THROTTLING: power-capped −18%" / "HW thermal slowdown" badge. One-glance, universally relatable, drives "wait, mine does that?".
4. **#4 / #8 Tokens-per-Joule + the keep-alive "wasted-energy tax" meter** — "Qwen-Q4 is 3.1× more tokens/Joule" and "your idle models burned €0.21 today." The TDS hero chart + the provocative number.
5. **#6 Jupyter idle-VRAM-squatter alert** — "Found it: an idle notebook kernel hogging 9GB." Solves a pain every DS person has felt; the relatable-villain screenshot.

---

## 3 CANDIDATE RELEASE HEADLINES

1. **"I built a free, open-source cockpit for my AI home lab — it shows live tokens/sec, GPU throttling, and tells me when a training run stalls (pure Python, no agents)."**
   *(r/LocalLLaMA + r/homelab + HN "Show HN")*

2. **"Tokens per Joule: an open-source dashboard that measures how efficiently your home GPU actually serves LLMs — and how much your idle models cost you in electricity."**
   *(Towards Data Science long-form / HN)*

3. **"Your GPU says 100% util. My dashboard tells you the truth: throttled, memory-bandwidth-bound, or a Jupyter kernel squatting on 9GB of VRAM."**
   *(r/LocalLLaMA + r/MachineLearning — provocative, screenshot-led)*

---

## Suggested build order for tonight (impact-per-effort, dependency-aware)

1. **#2** GPU fields (S) — unlocks #4, #8, #9, #10. Do first.
2. **#1** vLLM `/metrics` (M) — unlocks #4, #9, #11. The headline.
3. **#3** training-run detection + stall (M) — the story feature.
4. **#8** wasted-energy meter + **#4** tokens/Joule (S/M on top of #1/#2) — the shareable numbers.
5. **#5** Ollama `/api/show` (S) — cheap polish.
6. **#6** Jupyter awareness (M) — relatable villain.
7. **#7** tracker deep-links (S) — credibility filler.

Everything above stays within stdlib+Flask+SQLite+Chart.js and the existing `/proc` / `nvidia-smi` / Docker-socket / HTTP-probe machinery. No new pip deps required.
