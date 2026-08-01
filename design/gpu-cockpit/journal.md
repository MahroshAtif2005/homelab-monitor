# Journal — GPU cockpit (per-host GPU parity + dynamics + smart thermal alerts)

**Started:** 2026-08-01 · **Target branch:** `next` (feature branch `feat/gpu-cockpit`)
**Baseline:** `next` @ `83cf977` (post-v0.28.0)

---

## Before-state (verified 2026-08-01 against the live hub and the code on `next`)

### What the hub (`local`) GPU tab has
`static/dashboard.html` `section[data-tab="gpu"]` (line 1692):

- `#gpu-throttle` banner — live throttle reasons, **current tick only**, no duration
- `#gpu-spill` banner — model spilling into system RAM
- "GPU right now" KPIs + `#gpu-detail` chips (mem-BW, clocks, power vs cap, pstate, mem temp)
- `#pergpu-card` — per-card **snapshot** bars (util / VRAM / mem-BW), hidden when < 2 cards
- `#vram` chart — VRAM by service over time (stacked) ← **history**
- `#gpu2` chart — pooled GPU util, power & temperature ← **history**

### What every other host gets
`renderLocalOnlyNotice('gpu')` (line 7719) **hides every local card** and paints
`renderRemoteGpu()` (line 7545) instead: two KPIs, one bar per card, a process table.
Its own caption admits the gap:

> "Service attribution and history charts need per-host storage — a later slice."

### Root cause — there is no per-card, per-host storage anywhere

| Table | Scope | Per card? | Has temp? | Has fan? | Written by |
|---|---|---|---|---|---|
| `samples` / `samples_1m` / `samples_1h` | hub only | ✗ pooled | ✓ | ✗ | hub collector |
| `gpu_samples(ts, idx, …)` | hub only | ✓ | ✓ | ✗ | collector, **multi-GPU hubs only** |
| `host_samples` / `host_samples_1h` | per host | ✗ pooled | ✗ **no gpu temp** | ✗ | `_record_host_sample()` (app.py:3629) |

So: the per-host table drops GPU temperature on the floor, and fan speed is **not
collected anywhere in the codebase** — neither `_nvidia_cards()` in `probe.py:374`
nor `_enrich_gpus()` in `app.py:5379` query `fan.speed`.

> **Correction (found while implementing).** My first pass called `gpu_samples` a
> dead table with zero writers. Wrong: `backend/collectors/__init__.py:370` writes
> it every poll — but only when the hub has more than one card, and **nothing
> reads it**. No API, no repo, no UI. A multi-GPU hub has therefore been quietly
> accumulating per-card history for months and never showing a pixel of it. That
> makes extending this table the right move rather than adding a parallel one:
> `ardi`'s existing 39 046 rows become chartable the moment the reader exists.

### Remote probe coverage gap
`probe.py:_nvidia_cards()` queries 7 fields
(`index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu`).
The hub's `_enrich_gpus()` additionally gets `utilization.memory`, `clocks.current.sm`,
`clocks.current.memory`, `power.limit`, `temperature.memory`, `pstate` and the
throttle bitmask — **remotes get none of it**. `_nvidia_procs()` pools VRAM per pid
*across* cards, so there is no "which card is this process on".

### Live fleet at capture time (grounds the mockup)

`vader` — 3 × RTX 3090, 72 GB pooled, 280 W/card cap (deliberate, see memory):

| card | util | VRAM | power | temp |
|---|---|---|---|---|
| GPU 0 | 100 % | 22 497 / 24 576 MB | 267 W | 80 °C |
| GPU 1 | 100 % | 22 163 / 24 576 MB | 229 W | **86 °C** |
| GPU 2 | 0 % | 19 240 / 24 576 MB | 233 W | 64 °C |

`gpu_procs`: `llama-server` 63 446 MB (pid 460123), `python` 374 MB.
Containers already carry `vram_mb` (ollama 63 446, whisper-whisperx-3090-1 374) —
service attribution exists per host, just not per card and not over time.

GPU 1 sitting at 86 °C with no alert of any kind is exactly the hole this slice fills.

---

## Log

- **2026-08-01** — surveyed the code, captured the before-state above, wrote
  `SPEC.md` with the ASCII cockpit mockup. Approved: full scope, one branch.
- **2026-08-01** — branch `feat/gpu-cockpit` off `next` @ `83cf977`.
- **2026-08-01** — slice 1 `04a8178`, collection. Fan speed on both the hub and
  probe paths (NVIDIA `fan.speed`, AMD `pwm1`/`fan1_input`), the probe gains the
  deep telemetry pass the hub already had, and both sides map `gpu_uuid` → card
  index so VRAM is attributable to a card rather than only to the pool.
- **2026-08-01** — slice 2 `2deaf10`, storage. `gpu_samples` and `proc` gain
  `host`; new `gpu_samples_1h` keeps MAX alongside AVG for temp/fan.

### Corrections and gotchas found while building

1. **The probe is not installed on remotes.** `_ssh_with_stdin(user, host, port,
   "python3 -", _PROBE_SCRIPT)` — `probe.py` is streamed from the hub's own image
   on *every poll*. Shipping the new probe in the image is the entire rollout;
   there is nothing on vader or Work to update. (This retired the "deploy probes
   to each host" task.)
2. **nvidia-smi rejects the whole query on one unknown field name.** Appending
   `fan.speed` or `gpu_uuid` would make every card vanish on an older driver, so
   each new field is asked for first and falls back to the exact previous query.
3. **The compute-apps parser can't infer its shape from success.** It verifies
   the leading column actually looks like a UUID (`GPU-…`/`MIG-…`), because a
   wrong guess silently shifts every column by one.
4. **The test fixture handed one stdout to every nvidia-smi query**, which let the
   enrichment pass parse the card CSV as clock data. It now answers per query.
5. **Migration indexes can't live in the schema script.** `executescript` runs
   before the `ALTER`s, so an index over a newly added column fails on any
   existing install. They run in their own post-migration pass.
6. **Power-cap is not throttling.** `_THERMAL_BITS` deliberately excludes
   `SW_POWER_CAP`: vader's cards sit at their deliberate 280 W cap essentially
   always, and counting that as throttling would show a healthy box as
   permanently throttled and train the user to ignore the indicator.

### Baseline test state (so later failures are attributable)

On `next` @ `83cf977`, before any of my changes, **6 tests already fail**:
5 in `test_public_status.py` (maintenance-window flags) and
`test_no_silent_swallow.py` (2 pre-existing broad-except blocks in
`backend/api/benchmarks.py` and `backend/collectors/__init__.py`). My branch
holds at those same 6, with 677 passing.

Local Python is 3.8 and can't even import the app (PEP 585 annotations), so the
suite runs on ardi's 3.13 via a sync-and-run helper.
