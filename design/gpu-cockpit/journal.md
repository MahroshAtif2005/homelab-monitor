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
| `gpu_samples(ts, idx, …)` | hub only | ✓ | ✓ | ✗ | **nothing — dead table** |
| `host_samples` / `host_samples_1h` | per host | ✗ pooled | ✗ **no gpu temp** | ✗ | `_record_host_sample()` (app.py:3629) |

So: the hub's *own* per-card history is never stored either (`gpu_samples` has an
index and a schema and zero writers), and the per-host table drops GPU temperature
on the floor. Fan speed is **not collected anywhere in the codebase** — neither
`_nvidia_cards()` in `probe.py:374` nor `_enrich_gpus()` in `app.py:5379` query
`fan.speed`.

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
  `SPEC.md` with the ASCII cockpit mockup. Awaiting go-ahead before building.
