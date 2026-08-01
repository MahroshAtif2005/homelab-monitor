# Spec — GPU cockpit: the same GPU tab on every host, with dynamics and smart thermal alerts

**Status:** mockup for review · **Target branch:** `next` (feature branch `feat/gpu-cockpit`)
**Baseline:** `next` @ `83cf977` (post-v0.28.0) · before-state in [`journal.md`](journal.md)

---

## 1. The ask

> "This hub has graphs whereas other hosts don't — they have the current
> picture/snapshot. What I need in order to monitor my fleet is each box to have
> the same GPU tab, not just a snapshot but a chart with dynamics. On vader we
> have 3 GPUs — I want graphs for each of them plus one combined. Not only VRAM
> used and power but also fan speed and temperature, and ideally the split
> between services using the VRAM and/or power. A cockpit where I can instantly
> identify what is going on. Plus alerts on cards throttling or overheating — a
> smart way."

Five things, in dependency order:

1. **Parity** — one GPU tab, same code path, every host.
2. **Dynamics** — per-card history, not a snapshot.
3. **Wider telemetry** — fan speed + temperature alongside VRAM and power.
4. **Attribution** — which service is holding the VRAM / burning the watts, over time.
5. **Smart alerts** — throttling and overheating, sustained, per card, per host.

---

## 2. The cockpit — ASCII mockup

### 2.1 Alert strip (only rendered when something is actually wrong)

```
╔══════════════════════════════════════════════════════════════════════════════════════════════╗
║ ▲  GPU 1 has been thermally throttling for 12 min                    [ Mute 1h ] [ Rules ] ✕ ║
║    87 °C peak · HW thermal slowdown · core clock 1 395 → 1 005 MHz (−28 %)                    ║
║    Driver: llama-server (ollama) holds 21.6 GB on this card · fan already at 100 % (2 310 rpm)║
╚══════════════════════════════════════════════════════════════════════════════════════════════╝
```

Three sentences, in the order a human debugs: **what**, **how bad**, **who did it and
is there any headroom left**. One strip per condition, ranked critical → warning.

### 2.2 Pooled header — the whole box in one line of eye movement

```
┌ vader · 3 × RTX 3090 · 72 GB pooled ──────────────────── live · 4 s ago · range [ 6h ▾ ] ──┐
│                                                                                             │
│   UTIL             VRAM                 POWER               TEMP              FAN           │
│   67 %             63.9 / 72.0 GB       729 / 840 W         87 °C max         78 % avg      │
│   ▁▂▅█▇▇█▇▅▂▃▇█    ▁▁▃▇███████████      ▂▃▅███████████      ▃▄▅▆▇█████████    ▂▃▄▅▆▇███████ │
│   2 of 3 cards     89 % full            87 % of cap         1 card ≥ 84 °C    2 310 rpm max │
│                                                                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

Every KPI carries its own sparkline over the selected range — the snapshot and the
trend in the same glance. The sub-line is the *fleet-relevant* fact, not a repeat
of the number.

### 2.3 Per-card small multiples — default view

Three identical panels; the eye finds the odd one out with no reading required.

```
┌ Per card ───────────────────────  view: [ ●By card  ○By metric ]   [ ⛶ expand all ] ───────┐
│                                                                                             │
│ ┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌─────────────────────────┐ │
│ │ GPU 0  RTX 3090     ● OK    │ │ GPU 1  RTX 3090   ▲ THROTTLE│ │ GPU 2  RTX 3090  ○ IDLE │ │
│ │ 100 % · 267 W · 80 °C       │ │ 100 % · 229 W · 86 °C       │ │ 0 % · 233 W · 64 °C     │ │
│ ├─────────────────────────────┤ ├─────────────────────────────┤ ├─────────────────────────┤ │
│ │ util  ▁▃▇████████████  100 %│ │ util  ▁▂▇███████▇████  100 %│ │ util  ▁▁▁▁▁▁▁▁▁▁▁▁   0 %│ │
│ │ vram  ▃▃▇████████████   92 %│ │ vram  ▃▃▇████████████   90 %│ │ vram  ████████████   78 %│ │
│ │ temp  ▂▃▄▅▆▇▇████████   80 °│ │ temp  ▂▄▅▆▇█████████▓  86 °│ │ temp  ▃▃▃▃▃▂▂▂▂▂▂▂   64 °│ │
│ │ fan   ▂▃▄▅▆▇▇▇▇▇▇▇▇▇    72 %│ │ fan   ▃▄▅▆▇██████████  100 %│ │ fan   ▂▂▂▂▂▂▂▂▂▂▂▂   45 %│ │
│ │ power ▁▃▅████████████  267 W│ │ power ▁▃▅▇█████▇█████  229 W│ │ power ▅▅▅▅▅▅▅▅▅▅▅▅  233 W│ │
│ ├─────────────────────────────┤ ├─────────────────────────────┤ ├─────────────────────────┤ │
│ │ llama-server       22.1 GB  │ │ llama-server       21.6 GB  │ │ llama-server      18.4 GB│ │
│ │ ████████████████████░░      │ │ ███████████████████░░░      │ │ ████████████████░░░░    │ │
│ │ python · whisper    0.4 GB  │ │ —                           │ │ —                        │ │
│ └─────────────────────────────┘ └─────────────────────────────┘ └─────────────────────────┘ │
│                                                                                             │
│  ▓ shaded span on a sparkline = the card was throttling · click a card for its full chart ▸ │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

Details that carry the design:

- **Fixed row order** (util, vram, temp, fan, power) and **shared y-scale per metric
  across cards** — GPU 1's temp line sits visibly higher than GPU 0's *because it is*,
  not because it was auto-scaled separately.
- **Status pill** is computed, not raw: `OK / IDLE / BUSY / ▲ THROTTLE / ▲ HOT / ✕ GONE`.
- **Throttle spans are shaded on the sparkline itself**, so "when did it start" is
  answered without opening anything.
- The VRAM attribution strip is **per card** — which is the piece even the hub
  doesn't have today.

### 2.4 By-metric view — "which card is the problem?"

Same data, transposed. One chart, one line per card, for the metric you pick.

```
┌ Per card ─────────────────────  view: [ ○By card  ●By metric ]   range 6h ─────────────────┐
│                                                                                             │
│  Temperature °C                                    ─── GPU 0   ─── GPU 1   ─── GPU 2        │
│   90 ┤                                                                                      │
│   84 ┤╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌  alert threshold               │
│   80 ┤                        ╭────╮ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                       │
│   70 ┤              ╭─────────╯    ╰───────────────────────────────                         │
│   60 ┤ ═════════════════════════════════════════════════════════════  GPU 2 idle            │
│   50 ┤                                                                                      │
│      └──────────────────────────────────────────────────────────────────────                │
│       12:00       13:00       14:00       15:00       16:00       17:00                      │
│                                                                                             │
│  [ Util ] [ VRAM ] [ ●Temp ] [ Fan ] [ Power ] [ Clocks ]      ▓ = throttling  ▼ = OOM      │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.5 Combined chart — the box as one unit

```
┌ All cards combined ─────────────────────────────────────────────────── stacked ▾ ──────────┐
│  VRAM (stacked per card, GB)                                    72 GB ╌╌╌╌╌╌╌ capacity      │
│   72 ┤╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌                    │
│   54 ┤              ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒  GPU 2                   │
│   36 ┤        ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  GPU 1                   │
│   18 ┤ ███████████████████████████████████████████████████████████  GPU 0                   │
│    0 └──────────────────────────────────────────────────────────────                        │
│                                                                                             │
│  Pooled power (W) + hottest card (°C)                            840 W ╌╌╌╌╌ pooled cap     │
│  800 ┤                    ╭──────────────────────────────────────╮                          │
│  400 ┤        ╭───────────╯                                      ╰──                        │
│    0 └──────────────────────────────────────────────────────────────                        │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.6 Who is using the box — attribution over time

```
┌ Who is using the GPUs · last 6h ───────────────────────────────────────────────────────────┐
│                                                                                             │
│  VRAM by service (stacked)                                      72 GB ╌╌╌╌╌╌╌ capacity      │
│   60G ┤          ██████████████████████████████████████████████  ollama · llama-server      │
│   40G ┤          ██████████████████████████████████████████████                             │
│   20G ┤ ▒▒▒▒▒▒▒▒▒███████████████████████████████████████████████  whisper (3090-1)          │
│     0 └───────────────────────────────────────────────────────────                          │
│                                                                                             │
│  Power by service (stacked, W)                                  840 W ╌╌╌╌╌╌╌ pooled cap    │
│   800 ┤              ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                            │
│   400 ┤ ░░░░░░░░░░░░▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  ░ idle floor              │
│     0 └───────────────────────────────────────────────────────────                          │
│                                                                                             │
│  Service          Peak VRAM    Avg VRAM    % of time    Est. energy    Est. cost            │
│  ollama           63.4 GB      58.1 GB     97 %         2.94 kWh       €0.68                │
│  whisper           0.4 GB       0.3 GB     41 %         0.11 kWh       €0.03                │
│  idle floor           —            —       100 %        0.86 kWh       €0.20                │
│                                                                                             │
│  ⓘ Power per service is apportioned from each card's measured draw in proportion to the     │
│    VRAM that service holds on that card, minus the card's idle floor. It's an estimate —    │
│    GPUs don't meter power per process.                                                      │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

The ⓘ note is not decoration. Apportioning measured watts by VRAM share is a
defensible model but it *is* a model, and the tab says so rather than presenting
an estimate as a measurement.

### 2.7 Card health + alert rules

```
┌ Card health · last 6h ─────────────────────────────────────────────────────────────────────┐
│  Card    Throttled     ≥ 84 °C     Peak temp   Fan peak   Power-capped   Health             │
│  GPU 0   4 min  1.1 %  0 min       81 °C       74 %       12 % of time   ● good             │
│  GPU 1   12 min 3.3 %  38 min      87 °C       100 %      31 % of time   ▲ watch            │
│  GPU 2   0 min         0 min       65 °C       45 %       0 %            ● good             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘

┌ Alert rules · GPU ─────────────────────────────────────────────── applies to [ all hosts ▾ ]┐
│  ● on   Temperature ≥ [ 84 ] °C sustained [ 3 ] min                        → 🔴 critical    │
│  ● on   Thermal or power throttling sustained [ 2 ] min                     → 🟠 warning     │
│  ● on   Fan at 0 % while temp ≥ [ 50 ] °C  (fan stall / dead pump)          → 🔴 critical    │
│  ● on   VRAM ≥ [ 95 ] % sustained [ 5 ] min                                → 🟠 warning     │
│  ● on   A card that was present disappears from the bus                     → 🔴 critical    │
│  ○ off  Card idle at > [ 100 ] W for [ 30 ] min  (wasted watts)             → 🟠 warning     │
│                                                                                             │
│  Recovery message when the condition clears: [ ✔ ]     Quiet hours: [ 23:00 ]–[ 07:00 ]     │
│  Per-host override: [ vader: temp ≥ 86 °C ▾ ]  [ + add override ]                           │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.8 Degraded states — honest, not empty

The tab must render for a Windows laptop with an iGPU and for an AMD card with no
`fan1_input` just as well as for vader.

```
┌ GPU 0  Radeon RX 7900 XTX                                                        ● OK ─────┐
│ util  ▁▃▇████████  64 %      vram  ▃▃▇█████████  71 %      power  ▁▃▅███████  212 W        │
│ temp  ▂▃▄▅▆▇▇▇▇▇▇  68 °                                                                     │
│ fan   ── not reported by this driver ──                              ⓘ why?                 │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

A metric the card cannot report shows **"not reported by this driver"** with a
tooltip, never a flat zero line. (The codebase already holds this line — see the
`mem_util` comment at `app.py:5393`: "a coerced 0 would read as a confident
0 % mem-bandwidth". Same rule, applied to fan and to every new metric.)

For a host whose probe is older than this release:

```
┌ vader ─────────────────────────────────────────────────────────────────────────────────────┐
│  ⏳ Collecting per-card history                                                              │
│  vader's probe is reporting 3 cards. Charts fill in as samples land — first points          │
│  in ~10 s, a useful 6h view after about an hour.                                            │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.9 Narrow / mobile

Small multiples stack to one column; the pooled header wraps to a 2×3 KPI grid;
the by-metric chart keeps full width. No horizontal scroll on the page body.

---

## 3. What has to be built

### 3.1 Collect — fan speed and deep telemetry, everywhere

| Where | Change |
|---|---|
| `probe.py:_nvidia_cards()` | extend the query to `fan.speed`, and add the enrichment pass the hub already does — `utilization.memory`, `clocks.current.sm`, `clocks.current.memory`, `power.limit`, `temperature.memory`, `pstate` — plus `clocks_throttle_reasons.active` with the `clocks_event_reasons` fallback |
| `probe.py:_nvidia_procs()` | query `gpu_uuid` too and map uuid → index, so a process can be attributed **to a card**; keep the existing cross-card pooling as the aggregate |
| `probe.py` AMD path | `fan1_input` (rpm) and `pwm1` (0–255 → %) from hwmon; `power1_cap`; absent → field omitted, never 0 |
| `app.py:_enrich_gpus()` | add `fan` for the hub's own cards — currently the hub has no fan data either |
| `probe.ps1` | pass through whatever the NVIDIA/WMI path exposes; omit fan cleanly on cards that don't report it |

Shared decode logic (`_decode_throttle`, `_THROTTLE_BITS`) moves to one place so
hub and probe cannot drift.

### 3.2 Store — one table, hub and remotes alike

This is the change that makes parity structural rather than a copied renderer.

```sql
CREATE TABLE host_gpu_samples(
  ts INTEGER NOT NULL, host TEXT NOT NULL, idx INTEGER NOT NULL,
  util REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL,
  fan REAL, mem_util REAL, clk_sm REAL, clk_mem REAL,
  power_limit REAL, temp_mem REAL, throttle INTEGER,   -- bitmask, 0 = none
  PRIMARY KEY(ts, host, idx));

CREATE TABLE host_gpu_samples_1h(       -- avg for rates, MAX for temp/fan/throttle
  ts INTEGER NOT NULL, host TEXT NOT NULL, idx INTEGER NOT NULL,
  util REAL, mem_used REAL, mem_total REAL, power REAL,
  temp REAL, temp_max REAL, fan REAL, fan_max REAL,
  throttle_secs INTEGER, cnt INTEGER DEFAULT 1,
  PRIMARY KEY(ts, host, idx));

CREATE TABLE host_gpu_vram(             -- per service, per card, over time
  ts INTEGER NOT NULL, host TEXT NOT NULL, idx INTEGER NOT NULL,
  service TEXT NOT NULL, vram REAL,
  PRIMARY KEY(ts, host, idx, service));
```

**The hub writes itself as `host='local'`.** One table, one repo, one API, one
renderer — the local/remote fork disappears instead of being duplicated.

- `host_samples` gains `gpu_temp` (it drops GPU temperature today).
- The dead `gpu_samples` table stays untouched — no destructive migration; it is
  simply superseded and gets a comment saying so.
- Retention: raw purged on the existing schedule (default 48 h), `_1h` rollup kept
  like `samples_1h`. Volume at 10 s polling: ~26 k rows/host/day for 3 cards.

### 3.3 API — one endpoint for every host

```
GET /api/gpu/history?host=<name|local>&range=6h
  → { host, cards:[ {idx, name, vendor, mem_total, power_limit, supports:{fan,mem_util,…},
                     series:{util,vram,power,temp,fan,clk_sm}, throttle_spans:[[t0,t1,reason]]} ],
      combined:{ util, vram, power, temp_max, fan_avg },
      labels:[…], capacity_mb, events:[…], health:[ per-card 6h rollup ] }

GET /api/gpu/attribution?host=<name|local>&range=6h
  → { services:[ {name, kind, by_card:{0:…,1:…}, series:[…], peak, avg, pct_time,
                  est_energy_kwh, est_cost} ], idle_floor_w, capacity_mb, estimated:true }
```

`GET /api/gpu/history?host=all` feeds a future fleet-wide roll-up; out of scope here
but the shape leaves room for it.

MCP `get_gpu` gains an optional `host` argument so the fleet is reachable from
Claude the same way the dashboard sees it.

### 3.4 UI — delete the fork

- One `renderGpuTab()` for all hosts. `renderRemoteGpu()` and the `tabId === 'gpu'`
  special case in `renderLocalOnlyNotice()` are **removed**, not extended.
- Small-multiples panels reuse the existing `.gpucard` shell, `mc-panel` surface,
  `mc-pill` status pills, `.rb`/`.btn-mini` controls and `sic()` inline icons —
  no new component vocabulary, no raw-emoji icons.
- Sparklines: inline SVG polylines (≈40 points), not Chart.js instances — 15 of them
  on screen at 15 s refresh has to stay cheap.
- Full charts stay on Chart.js with the existing `mk()` helper and theme vars.
- Every new string goes through `I18N.t` and lands in **both** `locales/en.json`
  and `locales/zh-CN.json`.

### 3.5 Alerts — smart means sustained, per card, and self-clearing

New keys in `notify_scan()`, following the existing `_emit` / `_clear` edge-trigger
pattern so the rules engine, min-level, quiet hours and maintenance windows all apply
unchanged:

| Key | Fires when | Level |
|---|---|---|
| `gpu:temp:<host>:<idx>` | temp ≥ threshold for N consecutive polls | critical |
| `gpu:throttle:<host>:<idx>` | throttle bitmask non-zero (thermal/power bits only) for N polls | warning |
| `gpu:fanstall:<host>:<idx>` | fan = 0 % while temp ≥ 50 °C, card reports fan | critical |
| `gpu:vram:<host>:<idx>` | VRAM ≥ threshold sustained | warning |
| `gpu:missing:<host>:<idx>` | a card present in the last N polls vanishes | critical |
| `gpu:idlewatts:<host>:<idx>` | util ≈ 0 with power > threshold for 30 min (opt-in, default off) | warning |

What makes it *smart* rather than noisy:

- **Sustained, not instantaneous** — a 2-second spike to 85 °C is not an incident.
- **Hysteresis on clear** — clears at threshold − 3 °C, so a card hovering on the
  line doesn't flap.
- **Per card, per host key** — GPU 1 alerting doesn't suppress GPU 0.
- **The alert body carries the cause**: which service holds VRAM on that card, and
  whether the fan already had headroom. That is the difference between "GPU hot"
  and "GPU hot, fan already at 100 %, ollama holds 21.6 GB".
- **Recovery message** when it clears, opt-in, on by default.
- Thresholds live in settings with per-host overrides (vader's 3090s run hot by
  design at a 280 W cap — a global 84 °C would cry wolf there).

### 3.6 Tests

- probe parsing: fan present / `[N/A]` / missing field; throttle bitmask both field
  names; `gpu_uuid` → idx mapping.
- repo: rollup avg-vs-max semantics; retention purge; `host='local'` round-trip.
- API: shape for a 3-card host, a 1-card host, a no-GPU host, and a host with no
  history yet.
- alerts: fires only after N polls; clears with hysteresis; per-card independence;
  respects a maintenance window.
- a `supports:{}` regression test — a card that doesn't report fan must never
  serialise `fan: 0`.

---

## 4. Assumptions (stated, not asked)

1. Raw per-card retention follows the existing sample retention setting (48 h
   default); the 1 h rollup is kept indefinitely, like `samples_1h`.
2. Default temperature threshold 84 °C, throttle 2 min, VRAM 95 % — all editable.
3. Power-per-service is an apportionment, labelled as an estimate in the UI.
4. Windows/AMD hosts degrade per §2.8 rather than being excluded.
5. No probe redeploy is required for the tab to *work* — hosts on an older probe
   get the "collecting history" state and fill in whatever fields they do send.

## 5. Risks

- **Probe redeploy.** Fan, clocks and per-card process attribution need the new
  `probe.py` on each remote. The hub already has a "Run on remote" path; the tab
  must be honest about which fields a host isn't sending yet.
- **DB growth.** Mitigated by the raw/1 h split that the codebase already uses.
- **Chart count.** 15 sparklines + 2 charts per render at a 15 s tick — SVG
  sparklines and a single `requestAnimationFrame` batch keep it off the main thread.
- **Alert fatigue.** vader's cards sit at 80–86 °C under a deliberate 280 W cap;
  shipping a global 84 °C default would fire on a healthy box. Per-host overrides
  are part of the slice, not a follow-up.

## 6. Suggested delivery order

1. Collect + store (probe, `_enrich_gpus`, tables, repo, hub writes as `local`) — invisible, safe.
2. `/api/gpu/history` + the unified `renderGpuTab()` with small multiples + combined chart.
3. By-metric view and per-card VRAM attribution strip.
4. `/api/gpu/attribution` + the who-is-using-the-box card.
5. Alerts + settings + per-host overrides.
6. Tests, i18n, CHANGELOG, docs, release.

Each step is shippable on its own and leaves `next` green.
