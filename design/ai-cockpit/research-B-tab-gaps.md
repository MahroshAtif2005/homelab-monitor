# Research B — Per-Tab Gap Analysis (AI/DS "daily cockpit" lens)

**Scope:** `static/dashboard.html` (~3460 lines) + `app.py` (~4200 lines).
**Persona:** a data scientist / AI engineer running training jobs + LLM inference on a home lab who wants this dashboard to be their first-thing-in-the-morning cockpit.
**Stack constraints:** pure Python stdlib + Flask, no new pip deps, Chart.js frontend. Tabs are trivially extensible (registry comment at `dashboard.html:1027` — add a `TABS` entry + a `<section data-tab>` + a render function).

**Key data-source facts established while reading (these drive every "feasible?" call below):**

- GPU sampler `sample_once()` (`app.py:3271`) queries nvidia-smi with only `index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu` (`app.py:3290`). nvidia-smi exposes *far* more for free in the same call: `clocks.sm,clocks.mem,clocks.gr`, `clocks_throttle_reasons.active`, `utilization.memory` (memory-controller / bandwidth proxy), `power.limit`, `pstate`, `fan.speed`, `pcie.link.gen.current/.width.current`, `enforced.power.limit`, `temperature.memory`. Adding fields here is **S**.
- Per-process VRAM is already collected (`--query-compute-apps=pid,used_memory`, `app.py:3311`) but immediately **aggregated by service** into `procs[svc]` — the per-PID granularity is thrown away. nvidia-smi compute-apps can also emit `process_name` and (with `pmon`) `sm/mem/enc/dec` per-process utilization. Recovering per-process detail is **S–M**.
- Ollama probe (`probe_ollama`, `app.py:247`) calls `/api/ps` and reads only `name` + `size_vram`. The same JSON row carries `details.parameter_size`, `details.quantization_level`, `details.family`, `size` (disk), `context_length`/`expires_at` (keep-alive). Surfacing these is **S** — the bytes are already on the wire.
- vLLM / OpenAI-compatible servers (`_openai_models`, `app.py:258`) only read `data[].id`. vLLM also exposes Prometheus text at `/metrics` (tokens/sec, running/waiting queue depth, KV-cache usage, TTFT/ITL histograms) — pure-stdlib HTTP GET + text parse, **M**.
- History DB has tables `samples, proc, models, edges, events, gpu_samples, net_samples` (`app.py:3375`). GPU samples store `util,mem_used,mem_total,power,temp` — **no clock/throttle/per-process columns yet**. `/api/cost` already integrates `power` → kWh → money (`app.py:3526`).
- `collect_top_processes()` (`app.py:1973`) reads `/proc/<pid>/stat` + `/statm` for CPU%/RAM per process — already feeds the System "Top processes" card. No per-process GPU join today.
- Remote hosts are SSH-probed (agentless); GPU/Models/Containers/Disks tabs are **local-hub-only** by design (e.g. `renderDisksTab` bails for non-local at `dashboard.html:2578`).

---

## Tab 1 — Overview (`<section data-tab="overview">` @ 625; `renderData()` @ 1220, `renderHealth()` @ 1451)

### Today
- `🩺 Setup & requirements` diagnostics table (hidden until issues).
- `AI agent (MCP)` card — connection instructions.
- `🛰️ All hosts` fleet table: Host / Status / OS / Updates / CPU / RAM / GPU / Load1 / Uptime / Temp / Disks (`renderFleet()` @ 2354).
- Note: the historical "at-a-glance" cards/insights were moved off Overview; `renderData()` guards their now-absent IDs (comment @ 1226). Overview is essentially **a fleet inventory table**.

### Gaps (AI/DS POV)
The Overview is a sysadmin fleet table, not an *AI cockpit*. A working ML engineer opening this wants a one-screen answer to "what is my lab doing right now and what is it costing me." Missing:

1. **AI workload summary band** — active model servers, # models loaded, total VRAM committed vs free, aggregate GPU util, tokens/sec in flight, today's GPU $ cost. All sources already exist: `LATEST.models`, `LATEST.procs`, `LATEST.util/power`, `/api/cost` today figure. **Effort M** (new card + reuse existing JSON).
2. **GPU efficiency at a glance** — "GPU 78% util but 12% memory-bw" or "idle but 18 GB VRAM pinned" tells you instantly whether a run is compute- or memory-bound, or whether a model is wastefully resident. Needs `utilization.memory` added to the sampler (**S**) + a derived band.
3. **Today's cost + 7-day trend sparkline** right on Overview (not buried on System tab). `/api/cost` already returns `cost.today/d7/d30` and a cumulative series. **Effort S**.
4. **"Is anything training right now?"** — a count/badge of long-running high-GPU-util processes (heuristic: a compute-app PID at >50% util for >N min that is *not* a recognized inference server). Source: per-PID compute-apps + util history. **Effort M**.
5. **OOM / VRAM-pressure ticker** — events already in `D.events`; surface the latest as a red chip on Overview instead of only inside Models sparklines. **Effort S**.

---

## Tab 2 — GPU (`<section data-tab="gpu">` @ 663; `renderData()` GPU block @ 1234, `renderPerGpu()` @ 1284)

### Today
- `🎮 GPU right now`: two KPIs — VRAM in use (/total, % full) and GPU util (+ W, +°C). VRAM allocation bar by service. `nowtbl` per-service VRAM.
- `📋 Services on the GPU (range)`: peak / avg / %-time table from `D.summary`.
- `🎮 Per-GPU` cards (only when ≥2 cards): util / VRAM / power / temp bars (`renderPerGpu`).
- `📊 VRAM by service over time` stacked chart with pressure bands + OOM markers.
- `⚡ GPU utilization, power & temperature` line chart.

### Gaps (AI/DS POV) — this is the highest-impact tab to deepen
1. **Memory bandwidth / `utilization.memory`** — arguably the single most useful missing GPU metric for ML. Distinguishes compute-bound vs memory-bound kernels. nvidia-smi gives it free in the existing query. **Effort S** (sampler field + KPI + chart series). Add a `mem_util` column to `samples`/`gpu_samples`.
2. **Clocks (SM / mem / graphics) + throttle reasons** — `clocks.sm`, `clocks.mem`, `clocks_throttle_reasons.active`. Thermal/power throttling silently tanks training throughput; surfacing "🔥 thermal throttling 14% of last hour" is gold. **Effort S–M** (sampler + a small "clocks & throttle" KPI strip; throttle is a bitmask to decode).
3. **Power vs power-limit (headroom + % cap)** — `power.draw` is shown but not against `power.limit`/`enforced.power.limit`. "Capped at 320/350 W" tells you if a power-limit is the bottleneck. **Effort S**.
4. **Per-process VRAM breakdown** — today VRAM is aggregated to *service*; a notebook/python PID and an ollama PID under the same container or bare metal collapse together. Show a per-PID table (pid, process_name, VRAM, %util via `pmon`). Data already partially fetched at `app.py:3311`; stop aggregating. **Effort M**.
5. **VRAM high-water-mark + "free floor"** — the chart shows mean buckets; a peak/HWM line and "lowest free VRAM in range" KPI matter for sizing the next model/batch. `samples.mempk` (MAX) already exists in `/api/data` (`total.mempk`) but isn't surfaced as a KPI. **Effort S**.
6. **GPU efficiency / utilization-class KPI** — derived "% of range GPU was >50% util" and "VRAM committed but util ~0" (idle-but-pinned waste). Pure frontend math over existing `total.util`/`total.mem`. **Effort S**.
7. **Fan speed + memory-junction temp** — `fan.speed`, `temperature.memory` (GDDR6X memory temp is the real throttle culprit on 3090/4090). Free from nvidia-smi. **Effort S**.
8. **PCIe link gen/width** — `pcie.link.gen.current` / `.width.current`; a card that dropped to Gen1 x4 explains mysterious data-loading stalls. **Effort S**.
9. **Multi-GPU NVLink / P2P + per-card power/thermal history** — `gpu_samples` already stores per-card history but the per-GPU card (`renderPerGpu`) only shows *live* values, no per-card timeline. **Effort M**.

---

## Tab 3 — AI Models (`<section data-tab="models">` @ 686; `renderModels()` @ 1385, `modelSpark()` @ 1436, `callersFor()` @ 1425)

### Today
- One card per model **server**. Per server: resident-% + server-peak pill, a VRAM-over-time SVG sparkline (shared by all its models) with OOM markers, "Driven by" caller chips (connection-seconds, `D.callers`), and a table: Model / Status (Loaded/Idle) / Now / Peak / Avg / Peak·Avg bars.
- Recognizes a huge list of servers (Ollama, vLLM, SGLang, llama.cpp, TGI/TEI, ComfyUI, faster-whisper, etc.).
- Backend: `LATEST.models` (live), `D.model_summary` (peak/avg per model), `D.summary` (per-service), `D.callers`.

### Gaps (AI/DS POV)
This tab knows *that* a model is loaded and *how much VRAM* it holds, but nothing about the model itself or its serving performance. This is the second-highest-impact tab.

1. **Model metadata: parameter size, quantization, family, context length, on-disk size** — for Ollama these are already in the `/api/ps`/`/api/tags` payload (`details.parameter_size`, `details.quantization_level`, `details.family`, `size`) and just discarded by `probe_ollama` (`app.py:247`). Surfacing them is **S** for Ollama, **M** for others (TGI `/info` carries `max_total_tokens`, dtype; vLLM `/v1/models` is thin but model name often encodes it). Huge value: "llama3:70b Q4_K_M, 8k ctx, 40 GB on disk, 38 GB VRAM."
2. **Tokens/sec (throughput) + TTFT/inter-token latency** — the metric an inference engineer lives by. vLLM/SGLang/TGI expose Prometheus `/metrics` (e.g. `vllm:avg_generation_throughput_toks_per_s`, `vllm:time_to_first_token_seconds`, `vllm:e2e_request_latency_seconds`). Stdlib HTTP GET + text parse, persist to a new `model_perf` table. **Effort M** (one parser per metrics dialect; start with vLLM + TGI).
3. **Request queue depth / running vs waiting / KV-cache utilization** — vLLM `vllm:num_requests_running` / `:num_requests_waiting` / `:gpu_cache_usage_perc`. Tells you if you're saturated and should batch differently. Same `/metrics` source. **Effort M**.
4. **Cost per model / per server** — apportion the already-computed GPU $ (`/api/cost`) across servers by VRAM-share or util-share over the range. Frontend can do the apportioning from `D.summary` present-% + `/api/cost`. **Effort M**.
5. **Keep-alive / idle countdown** — Ollama `/api/ps` returns `expires_at`; show "unloads in 3m12s" so you know why VRAM is about to free. **Effort S** (data already fetched).
6. **Requests/min + error rate per model** — from `/metrics` counters (`vllm:request_success_total`, `:request_failure_total`). **Effort M**.
7. **Loaded-models history / churn** — "model X was swapped out 4× in the last hour" (thrashing detector). `models` table already timestamps VRAM-bearing rows; idle catalogue lives only in LATEST. Persisting load/unload edges → **M**.

---

## Tab 4 — Containers (`<section data-tab="containers">` @ 692; `renderHealth()` containers block @ 1557)

### Today
- KPIs: total / running / need-attention / stopped.
- Table: Container / State / Image / Ports / Uptime / RAM (resident) / VRAM (GPU mem attributed via `/proc/<pid>/cgroup`) / Disk (writable layer + volumes) / Status. Footer totals.

### Gaps (AI/DS POV)
The container table is already strong (it even has per-container VRAM — unusual and great). Gaps are about *AI-workload focus* and *resource trends*:

1. **AI-workload filter / tag** — a chip filter or auto-label for ollama / vllm / jupyter / comfyui / sglang / tgi / sd-webui images so you can collapse the 30 infra containers and see just your AI stack. Image strings are already in the row (`c.image`); reuse the `PROBES`/`_match_probe` recognizer that Models already uses. **Effort S**.
2. **CPU% per container** — table shows RAM/VRAM/Disk but **not CPU%**. Docker stats API (`/containers/<id>/stats?stream=0`) gives `cpu_stats` deltas — same Docker socket already in use (`_docker`, `app.py`). Data-loader pegging CPU is a classic training bottleneck. **Effort M** (one stats call per container; cache).
3. **Per-container net I/O inline** — Network tab already has per-container talkers (`net_samples`); echo a compact in/out rate column here. **Effort S**.
4. **Restart count / health-check history / OOM-killed flag** — Docker inspect carries `RestartCount`, `State.OOMKilled`, health log. A container silently restarting mid-run is invisible today. **Effort M**.
5. **GPU-process count per container** — "3 python PIDs on GPU" vs one; helps spot leaked workers. From the compute-apps→cgroup map already built. **Effort S**.
6. **Sortable columns** — table is static; sort by VRAM/RAM/Disk would matter at 30+ containers. **Effort S** (frontend only).

---

## Tab 5 — Services (`<section data-tab="services">` @ 708; `renderServicesBlock()` @ 1606, `renderServicesTab()` @ 1651)

### Today
- systemd units list (active/failed), per active host. Largely a sysadmin view.

### Gaps (AI/DS POV)
Lower priority for the AI persona. Reasonable additions:
1. **Filter to AI-relevant units** (e.g. `jupyter`, `nvidia-persistenced`, `docker`, `ollama` when run as a unit). **Effort S**.
2. **nvidia-persistenced / nvidia-fabricmanager status surfaced prominently** — these failing breaks GPU jobs. Source: systemd unit state already collected. **Effort S**.
3. **Per-service resource use** (RAM/CPU from `systemctl status`/cgroup) — **M**, marginal value vs Containers tab.

---

## Tab 6 — System (`<section data-tab="host">` @ 893; `renderData()` host block @ 1256, `renderSysInfo()` @ 2716, `renderTopProcs()` @ 1309, `renderCost()` @ 1342, `renderRamTree()` @ 2523)

### Today
- KPIs: CPU% / RAM% / Load1 / Uptime / CPU temp. Disks bars. System+Hardware info grid (`renderSysInfo`).
- `🧠 Top processes` mini-htop (by CPU, by RAM) — `collect_top_processes()` @ `app.py:1973`.
- `💰 Power & cost` (GPU-power→money) — local only.
- `🧠 Memory map` treemap of containers & services.
- `🖥️ CPU, RAM & load` history chart.

### Gaps (AI/DS POV)
1. **RAM bandwidth / NUMA / swap pressure** — true RAM-bandwidth needs perf counters (not stdlib-friendly), but **swap-in/out rate** and **page-cache vs anon split** are in `/proc/meminfo` + `/proc/vmstat` (`pgmajfault`, `pswpin/out`) — already partially read at `app.py:798`. Heavy swapping = dataset doesn't fit RAM; very actionable. **Effort S–M**.
2. **Per-process GPU column in Top-processes** — the mini-htop shows CPU & RAM; joining the compute-apps PID→VRAM map (already computed) would add a "by GPU" column, turning it into a real ML htop. **Effort M**.
3. **Disk I/O throughput (read/write MB/s)** — training is often I/O-bound on the dataset disk. `/proc/diskstats` is stdlib-readable; no field today. **Effort M** (new sampler + table; mirror net_samples pattern).
4. **CPU per-core / iowait breakdown** — `/proc/stat` per-core + iowait already parsed (`app.py:749`); show an iowait KPI ("CPU 90% but 60% iowait" = storage-starved). **Effort S**.
5. **Cost card: include CPU/system power estimate, not just GPU** — currently `/api/cost` integrates only GPU `power`. A rough host-power model (or RAPL `/sys/class/powercap` if present) would make "cost today" whole. **Effort M**.

---

## Tab 7 — Disks (`<section data-tab="disks">` @ 921; `renderDisksTab()` @ 2576, `scanDisk()` @ 2605)

### Today
- WizTree-style treemap per mount, on-demand "Scan contents" drill-down (`/api/disk_scan`). Local-hub only.

### Gaps (AI/DS POV)
1. **Model-store / dataset-aware grouping** — auto-highlight `~/.ollama/models`, HF cache (`~/.cache/huggingface/hub`), `/var/lib/docker`, `comfyui/models`, dataset dirs. ML disks are dominated by these; a one-glance "model cache = 412 GB" is very useful. The scanner already returns per-folder bytes; just tag/sort known paths. **Effort S–M**.
2. **Disk fill-rate / time-to-full projection** — checkpoints fill disks fast mid-run. Disk %-used isn't time-seriesed today (only live `disks[]`). Persist a periodic free-bytes sample → "/data fills in ~6h at current rate." **Effort M** (new tiny table or reuse samples).
3. **Inode usage** — many small dataset files exhaust inodes before bytes. `statvfs` gives `f_files/f_ffree` (stdlib). **Effort S**.

---

## Tab 8 — Network (`<section data-tab="network">` @ 926; `renderNetIO()` @ 2763, `renderNetwork()` @ 2796)

### Today
- `📶 Throughput` host NIC chart + KPIs. `📊 Top talkers — containers` (range bytes, `net_samples`). `🌐 Network` general info (routes/ARP/listeners).

### Gaps (AI/DS POV)
Modest priority. Useful additions:
1. **Distributed-training / NCCL awareness** — for multi-node training, per-NIC throughput vs link speed and inter-host bandwidth matter. At minimum show NIC link speed (`/sys/class/net/<if>/speed`) and saturation %. **Effort S**.
2. **Inference-port traffic** — tie talker rows to model servers (11434, 8000, vLLM ports) so "who's hammering Ollama over the network" is visible. Overlaps with Models callers. **Effort S–M**.
3. **Per-container packet drops / errors** — `/proc/<pid>/net/dev` carries errs/drops already being read (`app.py:603`). **Effort S**.

---

## Tab 9 — Security (`<section data-tab="security">` @ 942; `renderSecurity()` @ 2847)

### Today
- Firewall, SSH config, SELinux/AppArmor, fail2ban, reboot-required, auto-updates, pending updates — read-only, agentless. Solid sysadmin posture view.

### Gaps (AI/DS POV)
Low priority for this persona. One relevant item:
1. **Exposed AI endpoints check** — flag model servers (Ollama 11434, vLLM, Jupyter) **listening on 0.0.0.0 without auth**. Listening-socket scan already exists (`app.py:933`); cross-reference with the model-server port map. High-value security nudge specific to AI labs. **Effort M**.

---

## Tab 10 — Hosts (`<section data-tab="hosts">` @ 831; `renderHosts()` @ 1958) — Settings

### Today
- 3-step agentless SSH onboarding (authorize key, add host, test). LAN scan. Per-host checklist.

### Gaps (AI/DS POV)
Functional; AI-specific gap is **remote GPU visibility**. Remote hosts ship CPU/RAM/disk via probe.py but GPU/Models tabs are local-only. For a multi-box GPU lab this is the biggest structural gap — a second GPU rig is invisible on the GPU/Models tabs. Extending probe.py to ship nvidia-smi basics is **L** (probe protocol + storage + per-host GPU history) but transformative. Flag, don't scope-creep.

---

## Tab 11 — Alerts (`<section data-tab="alerts">` @ 715) — Settings

### Today
- Discord / ntfy / Telegram config, min severity, disk threshold, kWh price + currency (powers cost card). Triggers: container unhealthy/exited, systemd failed, VRAM pressure, GPU OOM, disk threshold.

### Gaps (AI/DS POV)
1. **GPU-thermal / throttle alert** — "GPU sustained >83°C / thermal-throttling" (needs throttle field from GPU gap #2). **Effort S** once the data exists.
2. **Training-job-finished / GPU-went-idle alert** — "GPU dropped from 95%→0% util after 6h" = your run finished or crashed; ping me. Pure heuristic on util history. **Effort M**.
3. **Disk fill-rate alert** ("/data full in <1h") — pairs with Disks gap #2. **Effort M**.
4. **Cost-budget alert** — "today's GPU cost exceeded $X." `/api/cost` already has today's figure. **Effort S**.

---

## Tab 12 — Backup (`<section data-tab="backup">` @ 810) — Settings

### Today
- SQLite history export/restore. Adequate; **no AI-specific gaps** beyond ensuring any new `model_perf`/`gpu_extra` tables get included in the export (they will, since backup ships the whole DB file).

---

# Proposed New Tabs

### A. **Experiments / Training** (Monitoring) — *highest-value new tab*
A run-centric view rather than a hardware view. Detect a "run" as a sustained high-GPU-util compute-app PID (or a Jupyter/python container) and track it as a timeline: start time, duration, GPU util & VRAM profile, est. energy/$ consumed, peak temp/throttle %, OOM events during the run, finished/crashed (util→0).
- **Sources:** per-PID compute-apps (`app.py:3311`, stop aggregating), util/power history (`samples`), events table, container list. New `runs` table.
- **Effort:** **L** (needs run-detection heuristic + storage), but it's the single feature that turns this from "homelab monitor" into "ML cockpit." Could ship a **M** v1 = "GPU activity sessions" purely derived from existing util history (no new sampling): segment the util timeline into contiguous >threshold windows and show each as a session with duration/energy/peak-temp.

### B. **Notebooks / Endpoints** (Monitoring)
A focused view of Jupyter/JupyterLab/VS-Code-server/Ollama/vLLM endpoints: URL, port, auth-exposed?, active kernels (Jupyter `/api/kernels` & `/api/sessions` are stdlib-HTTP-pollable), last-activity, idle time. Helps reclaim VRAM from forgotten kernels.
- **Sources:** container list + port map, Jupyter REST API, model-server probes.
- **Effort:** **M**.

### C. **Inference Performance** (could be merged into Models)
Dedicated tokens/sec, queue-depth, latency-percentile, KV-cache charts from vLLM/TGI/SGLang `/metrics`. If Models tab gets crowded, split here.
- **Effort:** **M** (depends on the `/metrics` parser landing).

---

# Prioritized Top-15 Gaps (impact × feasibility)

| # | Tab | Gap | Source | Effort |
|---|-----|-----|--------|--------|
| 1 | GPU | Memory-bandwidth `utilization.memory` (compute- vs mem-bound) | nvidia-smi (existing query) | **S** |
| 2 | GPU | Clocks + throttle-reason decode ("thermal throttling X% of range") | nvidia-smi | **S–M** |
| 3 | Models | Param size / quant / context / on-disk size | Ollama `/api/ps` (already fetched), TGI `/info` | **S** (Ollama) |
| 4 | Models | Tokens/sec + queue depth + KV-cache + TTFT | vLLM/TGI/SGLang `/metrics` (stdlib HTTP+parse) | **M** |
| 5 | Overview | AI-workload summary band (loaded models, VRAM committed/free, util, today's $) | `LATEST.*` + `/api/cost` | **M** |
| 6 | GPU | Per-process VRAM breakdown (stop aggregating to service) | compute-apps PID list (`app.py:3311`) | **M** |
| 7 | New | **Experiments/Training** tab (v1 = GPU activity sessions from util history) | `samples` util/power + events | **M** (v1) / **L** (full) |
| 8 | GPU | Power vs power-limit headroom + VRAM high-water-mark KPI | nvidia-smi `power.limit`; `total.mempk` already in API | **S** |
| 9 | Containers | AI-workload filter/tag (ollama/jupyter/comfyui/vllm…) | `c.image` + existing `_match_probe` | **S** |
| 10 | Containers | Per-container CPU% (data-loader bottleneck) | Docker `/stats?stream=0` (existing socket) | **M** |
| 11 | Overview/System | Today's cost + 7-day sparkline promoted to Overview | `/api/cost` (existing) | **S** |
| 12 | System | Disk I/O throughput + iowait KPI | `/proc/diskstats`, `/proc/stat` (stdlib) | **M** |
| 13 | Models | Cost-per-model/server (apportion GPU $) + Ollama keep-alive countdown | `/api/cost` + `D.summary` + `expires_at` | **M** / **S** |
| 14 | Disks | Model-store/HF-cache/dataset-aware grouping + fill-rate projection | existing `/api/disk_scan` + new free-bytes samples | **S–M** |
| 15 | Alerts | GPU-thermal/throttle + training-finished + cost-budget alerts | throttle field (#2), util history, `/api/cost` | **S–M** |

**If only three things ship:** (1) richer GPU sampler — add `utilization.memory`, clocks, throttle, power-limit in the one existing nvidia-smi call (`app.py:3290`) — unlocks GPU gaps #1/#2/#8 and Alerts #15 at once; (3) surface the Ollama model metadata already sitting unused in `probe_ollama`; (5/7) an AI-workload summary on Overview plus a derived "GPU activity sessions" view. Together these convert the dashboard from a competent homelab monitor into an AI/DS daily cockpit with almost no new dependencies and mostly small backend deltas.
