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

### Bugs that only live verification found

The test suite, the JSON payloads and the JS syntax check were all green for
every one of these. Each was caught by pointing the thing at the real fleet and
looking at what came back.

1. **A retired GPU read as a hardware failure.** `ardi` still had history for a
   card physically removed weeks earlier, so it rendered `NOT PRESENT` with a
   critical. Any hardware change would have left a permanent false alarm. Split
   into `gone` (missing from a live list — an incident) vs `retired` (last seen
   long ago — history).
2. **A zero-RPM idle fan read as a stalled fan.** A 3090 sitting at 53 °C with
   its fan fully stopped is a zero-RPM cooler doing its job. The rule fired at a
   flat 50 °C. The bar now derives from the alert threshold (10 °C below it,
   floor 60 °C), where every zero-RPM design has long since spun up.
3. **Per-card VRAM attribution was silently lost for containers.** `by_card`
   came back `None` for every service. The pid is the only thing that knows both
   which GPU it sits on and which container it belongs to; nothing downstream can
   rebuild that from a container name, because the container is `ollama` while
   the process on the card is `llama-server`.
4. **Every card read `NOT PRESENT` for a minute after any restart.** `HOST_DATA`
   is empty until the first successful poll, so all three of vader's cards were
   "missing" from a live list that didn't exist yet. Added a third state,
   `stale` — we don't know, and that is not an incident.
5. **The dashboard and the notifier used different temperature thresholds.** The
   tab hardcoded 84 °C for its HOT pill, red line and health column while the
   alerts read a configurable setting with per-host overrides. Change the
   setting and the page would warn at one temperature while the notifier fired
   at another.
6. **The VRAM-by-service chart was a solid black block.** `colorFor()` returns
   `hsl(...)` and call sites append `'cc'` for alpha; `hsl(210 62% 56%)cc` is
   not a colour, so Chart.js fell back to black. Pre-existing, invisible until
   the cockpit gave that chart full width.
7. **Every sparkline was invisible.** Correct data, correct SVG, nothing on
   screen — the class name `.spark` was already taken by the Star-on-GitHub
   sparkle animation (`position:absolute; opacity:0`). The new rule overrode
   width/height but not those two, so each sparkline was pulled out of flow and
   painted at zero opacity. Renamed to `.gspark`. Found only by screenshotting a
   single card and seeing an empty column where five traces should be.

### Baseline test state (so later failures are attributable)

On `next` @ `83cf977`, before any of my changes, **6 tests already fail**:
5 in `test_public_status.py` (maintenance-window flags) and
`test_no_silent_swallow.py` (2 pre-existing broad-except blocks in
`backend/api/benchmarks.py` and `backend/collectors/__init__.py`). My branch
holds at those same 6, with 677 passing.

Local Python is 3.8 and can't even import the app (PEP 585 annotations), so the
suite runs on ardi's 3.13 via a sync-and-run helper.

Final state: **738 passing, the same 6 pre-existing failures.** 63 new tests.

> **Worth reporting separately:** `test_db_factory.py::test_different_threads_
> return_different_conns` is **flaky on `next`**, independent of this branch —
> it failed in roughly 1 of 3 full-suite runs and then passed 5 times in a row
> with no changes. Not investigated; out of scope for this slice.

### Verified live on `ardi:9801` against vader (3× RTX 3090)

- 3 cards, per-card history, 5 sparklines each, per-card VRAM attribution
- fan speed live (32% / 32% / 12%) — a metric this codebase never had
- clocks, mem-BW, power cap (280/230/280 W), perf state, all from the remote probe
- ollama's 63 GB correctly split 22.5 / 22.1 / 18.8 GB across the three cards
- card health showing GPU 1's real history: 8 min hot, 86 °C peak, fan 100%
- no page errors, no false alerts
- caught a live model unload mid-capture: the VRAM traces drop off a cliff and
  the combined chart shows all three cards releasing at once

### Shipped

**PR [#261](https://github.com/SikamikanikoBG/homelab-monitor/pull/261)** →
`next`, 12 commits. CI: `smoke` pass, `snapshot` pass, `review` pass.

> Note on the review gate: the `review` job reports green but posted no comment,
> and its log shows an empty `ANTHROPIC_API_KEY` plus "No buffered inline
> comments". It is passing because the action completed, not because a review
> was produced — worth checking the `CLAUDE_CODE_OAUTH_TOKEN` secret before
> relying on it as a gate.

Also: `locales/zh-CN.json` is generated — `_meta.untranslated`/`coverage` are
derived. Hand-adding keys there needs `python scripts/i18n-sync.py` afterwards
to refresh them (coverage 91.8% → 92.5% here).
