# Research D — Costs / Power page (per-machine → per-component → per-process)

Status: design, buildable. Stack rule honoured: **pure Python stdlib + Flask, no new pip deps; Chart.js frontend.**

Grounding (confirmed in `app.py` / `static/dashboard.html`):
- `samples(ts, util, mem_used, mem_total, power, temp, cpu, ram_used, ram_total, load1, ctemp)` — `power` is **GPU watts only** (sum of `nvidia-smi power.draw` across cards, `app.py:3709`). `cpu` is host CPU % (`_cpu_pct`). `INTERVAL=10s`, `RETENTION` default 180 d.
- `proc(ts, service, mem)` — per-service **GPU VRAM (MB)** per tick, written at `app.py:3786` from `procs` (built via `service_for_pid`).
- `collect_top_processes()` (`app.py:2128`) computes per-command CPU% and RAM **live only**, surfaced in `/api/health` (`HEALTH["processes"]`), **not persisted**.
- `/api/cost` (`app.py:3982`) integrates GPU `power` → kWh → money, day/night dual tariff with per-window split. Settings in `SETTING_DEFAULTS` (`app.py:3247`): `kwh_price`, `kwh_price_night`, `tariff_mode`, `currency`, `night_start`, `night_end`.
- Container runs `pid: host`, `network_mode: host` → `/proc`, `/sys`, `/proc/net` are the host's. Remote hosts are SSH-probed (`/api/fleet`, `host_data`); they may report their own GPU power.
- Frontend: `TABS` array (`dashboard.html:1114`), global range `R` (`:1129`) with `.rb` buttons re-rendering on click (`:3388`), helpers `mk()`/`fmtLabels()`/`cv()`/`areaBg`/`baseOpts`.

This design is **additive**: it adds RAPL CPU power to the sampler, one new persisted table, two new endpoints, and a new tab. `/api/cost` keeps working untouched (the new GPU-only behaviour there is unchanged); the Costs page consumes a new richer `/api/costs`.

---

## 1. CPU (and "other") power via Intel/AMD RAPL

### 1.1 What RAPL is and what's measured vs estimated

RAPL (Running Average Power Limit) exposes **monotonically increasing energy counters in microjoules** under the Linux *powercap* sysfs tree. Both Intel and AMD (Zen, via `amd_energy` / the in-kernel `intel-rapl` powercap registration) present at the **same path**: `/sys/class/powercap/intel-rapl/...`. There is no separate `amd-rapl` directory — Ryzen/EPYC register their RAPL domains under `intel-rapl:*` too. (Some kernels expose AMD via the `amd_energy` hwmon driver instead, which is *not* powercap; we treat that as "absent" and degrade — see §1.5.)

Tree shape:
```
/sys/class/powercap/intel-rapl/
  intel-rapl:0/                 package-0 (one per CPU socket)
    name                        -> "package-0"
    energy_uj                   -> cumulative µJ
    max_energy_range_uj         -> wraparound modulus
    intel-rapl:0:0/             sub-domain (core / dram / uncore)
      name  energy_uj  max_energy_range_uj
    intel-rapl:0:1/             another sub-domain
  intel-rapl:1/                 package-1 (second socket, if any)
  intel-rapl-mmio:0/            (sometimes a parallel mmio view — skip, dedup by name)
```
`psys` (platform/SoC-wide, "whole package incl. iGPU+DRAM+...") appears on some client Intel parts as a top-level domain named `psys`. When present it is the **best single proxy for the CPU+platform draw**, so we prefer it for the package total but still read sub-domains for the breakdown.

**Honesty about measured vs estimated** (surfaced verbatim in the UI captions):
- **GPU watts: MEASURED** — `nvidia-smi power.draw` (board-level, already collected).
- **CPU package watts: MEASURED** — RAPL `package-N` (or `psys`) energy deltas. This is the CPU package only: cores + uncore + (on most parts) the iGPU + the memory controller. It is *not* the wall draw.
- **DRAM watts: MEASURED where a `dram` sub-domain exists** (server parts, some desktops); otherwise folded into package.
- **"Other / rest of system": ESTIMATED.** Wall power needs a smart plug, which this project does not integrate yet → out of scope to *measure*. We do **not** invent a wall figure. Instead the component breakdown is explicitly **GPU (measured) + CPU package (measured) + DRAM (measured if present)**, and we offer an *optional, clearly-labelled* "estimated rest-of-system" baseline (mainboard/fans/PSU loss/disks) only if the user opts in by setting a `system_idle_watts` constant in Settings (default blank → not shown). When blank, "other" is omitted and we never claim a total wall figure. This keeps the page truthful: every watt shown is either measured or an operator-supplied constant.

### 1.2 Deriving watts from `energy_uj` deltas

Energy counter is cumulative µJ. Over a sample interval Δt seconds:

```
ΔE_uj = (e_now - e_prev) mod max_energy_range_uj      # handle wraparound
watts = ΔE_uj / 1e6 / Δt                               # µJ → J → J/s = W
```

The counter is a **uint that wraps** at `max_energy_range_uj` (typically ~262144 J ≈ 262 G·µJ for a 32-bit-ish field; the exact modulus is read from the file). With a 10 s interval at, say, 65 W, ΔE ≈ 650 J — a wrap only happens every few hours of accumulation, but it *will* happen, so we always reduce modulo the range. We detect a wrap as `e_now < e_prev` and add one modulus:

```
de = e_now - e_prev
if de < 0:
    de += max_range          # single wrap (interval << wrap period, so at most one)
```

First tick after boot/restart has no `e_prev` → emit nothing (None), seed state, move on. A counter that doesn't advance (permission, frozen) yields 0 W which we treat as "unavailable" for that domain, not a real zero.

### 1.3 Permissions, container caveat, degrade-gracefully

- `energy_uj` is historically root-readable only (CVE-2020-8694 "PLATYPUS" side-channel mitigation tightened it to `0400 root`). The hub container runs as **root** with `pid: host`. `/sys/class/powercap` is part of the host's `/sys`, which is mounted in the container by default (it is **not** namespaced like `/proc`). So with the current compose (`pid: host`, root) RAPL is **usually readable**. If a hardened host has `0400 root` and the container somehow isn't root, or `/sys` isn't mounted, every read raises `PermissionError`/`FileNotFoundError` → we degrade: `cpu_power=None`, the CPU component simply doesn't appear, GPU-only costs continue exactly as today. **No crash, no fake zero.**
- We never `chmod`. If absent, the UI shows a one-line "CPU power unavailable (RAPL not readable)" note with a doc link, mirroring the existing degraded-setup banner pattern.
- AMD on a kernel exposing only `amd_energy` hwmon (not powercap): treated as absent (degrade). Documented as a known gap.

### 1.4 Paste-ready sampler code (add near the host-metrics helpers, ~`app.py:606`)

```python
# ── CPU package power via RAPL (Intel/AMD powercap) ───────────────────────────
# Reads cumulative energy counters under /sys/class/powercap/intel-rapl and turns
# the per-interval delta into watts. AMD (Zen/EPYC) registers under the SAME
# intel-rapl path, so this covers both. Everything is best-effort: a missing tree,
# a permission-denied energy_uj, or a frozen counter degrades that domain to
# "unavailable" (None) rather than raising or inventing a zero. MEASURED, package
# only (cores+uncore+iGPU+memctl) — NOT wall power.
RAPL_ROOT = os.environ.get("RAPL_ROOT", "/sys/class/powercap")
_RAPL_PREV = {}   # domain-path -> (energy_uj, monotonic_ts)
_RAPL_AVAIL = None  # tri-state cache: None=unknown, False=absent, True=present

def _rapl_read_uj(path):
    """Return (energy_uj:int, max_range_uj:int) for a powercap domain dir, or None."""
    try:
        with open(os.path.join(path, "energy_uj")) as f:
            e = int(f.read().strip())
        with open(os.path.join(path, "max_energy_range_uj")) as f:
            m = int(f.read().strip())
        return e, m
    except (OSError, ValueError):
        return None

def _rapl_domains():
    """Discover powercap domains -> {path: name}. 'package-*'/'psys' are top-level;
    'core'/'dram'/'uncore' are nested intel-rapl:*:* dirs. Deduped by name."""
    out = {}
    try:
        for top in sorted(glob.glob(os.path.join(RAPL_ROOT, "intel-rapl:*"))):
            # skip the parallel mmio mirror to avoid double-counting a package
            if os.path.basename(top).startswith("intel-rapl-mmio"):
                continue
            nm = _rt(os.path.join(top, "name"))
            if nm:
                out[top] = nm.strip()
            for sub in sorted(glob.glob(os.path.join(top, "intel-rapl:*:*"))):
                snm = _rt(os.path.join(sub, "name"))
                if snm:
                    out[sub] = snm.strip()
        # psys / platform domains sometimes appear as their own top-level dir
        for p in sorted(glob.glob(os.path.join(RAPL_ROOT, "intel-rapl:*"))):
            nm = (_rt(os.path.join(p, "name")) or "").strip()
            if nm == "psys":
                out[p] = "psys"
    except Exception:
        pass
    return out

def read_rapl_power():
    """Per-interval RAPL watts. Returns a dict or {} when RAPL is unavailable:
      {"cpu_w": float|None,        # best CPU package figure (psys if present, else
                                   #   sum of package-* domains)
       "dram_w": float|None,       # sum of dram sub-domains if any
       "domains": {name: watts}}   # every measured domain, for transparency
    First call after boot/restart seeds state and returns None watts (no prior)."""
    global _RAPL_AVAIL
    domains = _rapl_domains()
    if not domains:
        _RAPL_AVAIL = False
        return {}
    now = time.monotonic()
    per = {}
    for path, name in domains.items():
        rd = _rapl_read_uj(path)
        if rd is None:                       # permission denied / frozen -> skip domain
            continue
        e, mrange = rd
        prev = _RAPL_PREV.get(path)
        _RAPL_PREV[path] = (e, now)
        if not prev:
            continue                         # first sample for this domain: no delta yet
        e0, t0 = prev
        dt = now - t0
        if dt <= 0:
            continue
        de = e - e0
        if de < 0:                           # uint wraparound: add one modulus
            de += mrange
        per[name] = max(0.0, de / 1e6 / dt)  # µJ -> J -> W
    _RAPL_AVAIL = bool(per) or _RAPL_AVAIL
    if not per:
        return {}
    # Prefer psys (whole-platform) as the CPU figure; else sum every package-* domain.
    psys = per.get("psys")
    pkgs = [w for n, w in per.items() if n.startswith("package")]
    cpu_w = psys if psys is not None else (round(sum(pkgs), 1) if pkgs else None)
    drams = [w for n, w in per.items() if n == "dram" or n.endswith(":dram")]
    dram_w = round(sum(drams), 1) if drams else None
    return {"cpu_w": (round(cpu_w, 1) if cpu_w is not None else None),
            "dram_w": dram_w, "domains": {n: round(w, 1) for n, w in per.items()}}
```

Notes:
- `_rt` already exists (`app.py:813`) for tolerant file reads.
- `time.monotonic()` (not `time.time()`) for Δt so an NTP step can't produce a spurious huge/negative watt figure.
- The first interval after process start returns `{}` for watts but seeds `_RAPL_PREV`; the very next tick is real. This matches how `_cpu_pct` and net counters already behave.

### 1.5 Wiring into `sample_once` and `samples`

Add two columns to `samples` via the existing migration mechanism (`_SAMPLE_MIGRATIONS`, `app.py:115`) — non-destructive `ALTER TABLE ... ADD COLUMN`:

```python
_SAMPLE_MIGRATIONS = ("cpu REAL", "ram_used REAL", "ram_total REAL", "load1 REAL",
                      "ctemp REAL", "cpu_power REAL", "dram_power REAL")   # +2 new
```

In `sample_once` (after `host = read_host()`, ~`app.py:3772`):
```python
    rapl = {}
    try:
        rapl = read_rapl_power()
    except Exception:
        rapl = {}
    cpu_power  = rapl.get("cpu_w")     # None when RAPL unavailable -> NULL in DB
    dram_power = rapl.get("dram_w")
```
And extend the `samples` INSERT (`app.py:3781`) to carry `cpu_power, dram_power`. Store `NULL` when None so history charts skip gaps (same convention as the GPU columns). Surface on `LATEST` too (`LATEST.update(..., cpu_power=cpu_power, dram_power=dram_power, rapl=rapl.get("domains"))`) so the live KPI can read it without a query.

`/api/data` and `/api/cost` are untouched and keep working — they only read `power` (GPU). The new columns are consumed only by `/api/costs`.

### 1.6 Multi-host note (hub-first)

The hub measures its own CPU via RAPL above. For **SSH-probed remotes**, the probe (`probe.py`) can read the same `/sys/class/powercap/.../energy_uj` and return a `cpu_power` field in `host_data`; until that lands, remotes are **GPU-only** for power (they already report GPU `power`). The endpoint/table design below is keyed by `machine` so a remote's GPU-only numbers slot in with `cpu`/`other` simply absent — the per-machine card degrades cleanly. No hub work blocks on the remote side.

---

## 2. Per-process / per-service energy attribution

### 2.1 Attribution model

Energy is measured per **resource** (GPU board, CPU package) per interval. We split each resource's interval-energy across the entities using that resource, by **share of that resource**:

- **GPU energy → per GPU-using service**, weighted by **VRAM share** from the `proc` table (already collected per tick). This is the lever the maintainer asked for and what we already have cheaply. (VRAM share is a proxy for GPU-energy share — a model resident but idle still draws some power; util-weighting would be better but per-process GPU util isn't available without DCGM/MIG. We label this honestly as "by VRAM share".) If `nvidia-smi` ever exposes per-PID `sm` utilisation we can switch the weight; the schema doesn't change.
  ```
  gpu_watts_service = gpu_power_total * (vram_service / vram_all_services_on_gpu)
  ```
  VRAM held by no recognised service ("host/other") gets the remainder so the split is conservation-exact.

- **CPU package energy → per process/command**, weighted by **CPU-time (jiffies) share** over the interval. `collect_top_processes` already computes per-command Δjiffies (`a["dcpu"]`) and the interval's total busy jiffies (`span`). The CPU-busy fraction of a command is `dcpu/span`; multiply by the measured package watts:
  ```
  cpu_watts_cmd = cpu_power_total * (dcpu_cmd / span_busy_jiffies)
  ```
  Idle jiffies are excluded from `span` already (it's `total - prev_total` minus nothing — actually `total` includes idle; see note). **Correction:** `span` in `collect_top_processes` is `total - prev_total` where `total` is *all* jiffies incl. idle, and `cpu_pct` is `100*dcpu/span*ncpu`. So `dcpu/span` is the fraction of **wall CPU capacity** (all cores) the command used. That is exactly the right weight for splitting package energy: a host that is 100% idle attributes ~0 to processes and the package's idle draw becomes the unattributed remainder ("CPU host/idle"). Good — it's honest.

This gives, per tick, a set of `(entity, watts)` rows whose GPU parts sum to the measured GPU watts and whose CPU parts sum to ≤ the measured CPU watts (the gap = idle/kernel, labelled "host/idle").

### 2.2 What to persist — new table `power_proc`

Per-tick top-process CPU is **not** stored today. Storing every command every 10 s is too much; storing nothing loses drill-down. Lean compromise: **persist the top-N CPU consumers and all GPU services, per tick, as watts** — already attributed, so the query path is a pure `SUM`.

```sql
CREATE TABLE IF NOT EXISTS power_proc(
  ts    INTEGER NOT NULL,     -- sample tick (epoch s), == samples.ts
  kind  TEXT    NOT NULL,     -- 'gpu' | 'cpu'
  name  TEXT    NOT NULL,     -- service/container/model name, or command (cpu)
  watts REAL    NOT NULL      -- attributed watts for THIS tick (see §2.1)
);
CREATE INDEX IF NOT EXISTS idx_powerproc_ts   ON power_proc(ts);
CREATE INDEX IF NOT EXISTS idx_powerproc_name ON power_proc(name, ts);
```

Add to `_DB_SCHEMA` (`app.py:91`); the index lines join the existing block. No migration needed (new table is created by `executescript`).

**Why watts (not energy) per row:** each row stands for `INTERVAL` seconds, exactly like the `samples.power` integration already in `/api/cost`. Energy over any range = `SUM(watts) * INTERVAL / 3_600_000` kWh. Reusing the same kWh-per-sample math means the per-entity numbers are guaranteed consistent with the machine total.

**Storage budget:** top-N CPU (N=8) + ~GPU services (typically 1–5) ≈ 12 rows/tick. At INTERVAL=10 s that's ~1.04 M rows/day. Each row ~40 B in SQLite ≈ ~42 MB/day raw, less with WAL/vacuum — but at 180 d retention that's multi-GB. **Mitigation (built into the write path):** only persist a CPU row when `watts >= POWER_PROC_MIN_W` (default 0.5 W) and cap at top-8 by watts; idle commands are dropped (their energy stays in the machine total as "host/idle", never lost). Realistically 4–10 rows/tick. We also reuse the existing retention sweep (`app.py:3801`) by adding `power_proc` to the table list. For long retention, an optional nightly rollup (below) keeps drill-down cheap.

**Optional rollup (recommended for 30 d+ ranges)** — hourly per-entity energy, written by a once-an-hour branch in `collector()`:
```sql
CREATE TABLE IF NOT EXISTS power_proc_hourly(
  hour  INTEGER NOT NULL,     -- epoch s floored to the hour
  kind  TEXT    NOT NULL,
  name  TEXT    NOT NULL,
  wh    REAL    NOT NULL,     -- watt-hours that entity used that hour
  PRIMARY KEY(hour, kind, name)
);
```
Built by `INSERT OR REPLACE ... SELECT (ts/3600)*3600, kind, name, SUM(watts)*INTERVAL/3600 ...` from `power_proc` for the just-completed hour. The endpoint reads `power_proc` for ranges ≤ 24 h (full resolution) and `power_proc_hourly` for longer ranges (cheap). This is optional; v1 can ship reading `power_proc` directly and add the rollup if storage bites.

### 2.3 Write path (in `sample_once`, inside the `with LOCK:` block)

Add after the existing `proc` writes (`app.py:3786`). We need the live CPU breakdown at sample time — call `collect_top_processes()` once here (it's cheap, reads `/proc`) and keep the attribution local so it stays consistent with the tick. (Today `collect_top_processes` runs in `health_scan` every 15 s; for the costs write we call it on the 10 s sampler. Either share the result via a module global or call it here — calling here is simplest and keeps `_PROC_PREV` advancing on the sampler cadence; move the `health_scan` call to read the cached result to avoid double `_PROC_PREV` stepping. See §2.4.)

```python
POWER_PROC_TOPN   = 8
POWER_PROC_MIN_W  = 0.5

def _attribute_power_rows(ts, gpu_power, procs_vram, cpu_power, top_cpu):
    """Build (ts, kind, name, watts) rows for power_proc.
       gpu_power : measured GPU watts this tick (float|0)
       procs_vram: {service: vram_mb} this tick (the `procs` dict)
       cpu_power : measured CPU package watts (float|None)
       top_cpu   : collect_top_processes() result (or None)."""
    rows = []
    # GPU split by VRAM share
    vtot = sum(procs_vram.values())
    if gpu_power and vtot > 0:
        for svc, mb in procs_vram.items():
            w = gpu_power * (mb / vtot)
            if w >= POWER_PROC_MIN_W:
                rows.append((ts, "gpu", svc, round(w, 2)))
    # CPU split by jiff-share, weighted by measured package watts
    if cpu_power and top_cpu:
        ncpu = top_cpu.get("ncpu") or 1
        # reconstruct each command's capacity fraction from its reported cpu_pct:
        # cpu_pct = 100 * frac_of_all_cores  ->  frac = cpu_pct / (100 * ncpu)
        ranked = sorted(top_cpu.get("by_cpu", []), key=lambda r: -r["cpu_pct"])[:POWER_PROC_TOPN]
        for r in ranked:
            frac = (r["cpu_pct"] / 100.0) / ncpu
            w = cpu_power * frac
            if w >= POWER_PROC_MIN_W:
                rows.append((ts, "cpu", r["name"], round(w, 2)))
    return rows
```
```python
        # inside `with LOCK:` in sample_once, after the proc writes:
        pp_rows = _attribute_power_rows(ts, power, procs, cpu_power, top_cpu)
        if pp_rows:
            DB.executemany("INSERT INTO power_proc(ts,kind,name,watts) VALUES(?,?,?,?)", pp_rows)
        # extend the retention sweep table list:
        # for t in ("samples","proc","models","edges","events","gpu_samples",
        #           "net_samples","power_proc"):
```
`top_cpu` is `collect_top_processes()` captured earlier in `sample_once` (outside the lock). `power` is the GPU watts already computed; `procs` is the existing `{service: vram_mb}` dict.

Using `cpu_pct` to back out the fraction avoids changing `collect_top_processes`'s return shape. (If preferred, expose raw `dcpu`/`span` instead — but `cpu_pct/(100*ncpu)` is exact given how `cpu_pct` is defined.)

### 2.4 Avoiding double-stepping `_PROC_PREV`

`collect_top_processes` mutates the module-global `_PROC_PREV` each call (it's a delta computation). It currently runs in `health_scan` (every ~15 s). If we also call it in `sample_once` (every 10 s) we'd interleave two cadences and corrupt both deltas. Fix: **call it once per sampler tick in `sample_once`, store the result on a module global** (e.g. `HEALTH["processes"]`), and make `health_scan` reuse that cached value instead of calling again:
```python
def health_scan():
    HEALTH["docker"]  = collect_docker()
    HEALTH["systemd"] = collect_systemd()
    HEALTH["update"]  = collect_update()
    # processes are now refreshed by sample_once (10s); reuse the cached value
    collect_os_releases()
    HEALTH["at"] = int(time.time())
```
and in `sample_once` (outside the lock, near the other collects):
```python
    try:
        top_cpu = collect_top_processes()
    except Exception:
        top_cpu = None
    HEALTH["processes"] = top_cpu     # keep the Top-processes card fed
```
This makes the Top-processes card refresh on the 10 s cadence (slightly snappier) and gives the costs write a consistent CPU breakdown. `tests/test_topproc.py` resets `_PROC_PREV` between cases and is unaffected.

### 2.5 Query path

Per-entity energy/cost over a range, reusing the established kWh-per-sample factor:
```python
KWH_PER_SAMPLE = INTERVAL / 3_600_000.0
# ranked breakdown:
rows = cur.execute(
    "SELECT kind, name, SUM(watts) FROM power_proc WHERE ts>=? GROUP BY kind,name",
    (since,)).fetchall()
# energy_kwh = sw * KWH_PER_SAMPLE ; cost = energy_kwh * price (tariff-aware below)
# per-entity time-series (drilldown), bucketed like /api/data:
series = cur.execute(
    "SELECT (ts/?)*? b, SUM(watts) FROM power_proc WHERE name=? AND ts>=? GROUP BY b ORDER BY b",
    (bk, bk, name, since)).fetchall()
```
For tariff-aware per-entity cost, fold each row's `ts` through the existing `_make_is_night(ts)` predicate (same one-pass pattern as `/api/cost`'s `split_kwh`). For ranges > 24 h, swap `power_proc` → `power_proc_hourly` (`wh` already in watt-hours: `energy_kwh = SUM(wh)/1000`).

---

## 3. Endpoints

Keep `/api/cost` exactly as-is (the existing cost card and `tests/test_cost.py` keep passing). Add a richer, page-dedicated `/api/costs` plus a drilldown.

### 3.1 `GET /api/costs?range=<1h|6h|24h|7d|30d|all>&machine=<name|local>`

Per-machine totals + component breakdown + ranked entity breakdown. Tariff-aware (reuses `get_settings`, `_make_is_night`, the day/night split). Shape:

```jsonc
{
  "enabled": true,                    // day price > 0 (same gate as /api/cost)
  "range": "7d", "bucket_sec": 600,
  "currency": "$",
  "tariff": { "mode": "dual", "price_day": 0.21, "price_night": 0.10,
              "night_start": "22:00", "night_end": "06:00" },
  "machines": [
    {
      "name": "local",
      "now_w":   { "gpu": 180, "cpu": 65, "dram": 8, "total": 253 },  // live (LATEST)
      "avg_w":   { "gpu": 120, "cpu": 40, "total": 160 },             // over range
      "energy_kwh": { "gpu": 20.1, "cpu": 6.7, "dram": 1.3, "total": 28.1 },
      "cost":       { "today": 1.84, "d7": 6.42, "d30": 24.9 },       // total, tariff-aware
      "cost_range": 5.90,                                             // cost over selected range
      "measured": ["gpu", "cpu", "dram"],   // which components are MEASURED
      "estimated": []                       // e.g. ["other"] only if system_idle_watts set
    }
    // remote machines slot in here GPU-only until probe.py reports cpu_power
  ],
  "components": {                     // stacked-area series for the active machine
    "labels": [ ... epoch ... ],
    "gpu":  [ ... watts per bucket ... ],
    "cpu":  [ ... ],
    "dram": [ ... ],
    "other":[ ... ]                  // present only if system_idle_watts is set
  },
  "breakdown": [                     // ranked entities over the range (for the table)
    { "kind": "gpu", "name": "ollama",       "energy_kwh": 12.4, "cost": 2.60, "avg_w": 74 },
    { "kind": "cpu", "name": "python",       "energy_kwh": 3.1,  "cost": 0.65, "avg_w": 18 },
    { "kind": "cpu", "name": "host/idle",    "energy_kwh": 2.0,  "cost": 0.42, "avg_w": 12 }
    // sorted by energy desc
  ],
  "unattributed": { "cpu_kwh": 2.0, "note": "CPU idle / kernel time not tied to a process" }
}
```

Implementation notes:
- `machines`: v1 = just `local` from `samples`/`power_proc`; remotes appended from `host_data` GPU power when available. Component series come from `samples` (`power`=gpu, `cpu_power`=cpu, `dram_power`=dram) bucketed exactly like `/api/data` (`(ts/?)*? b, AVG(...)`).
- `cost.today/d7/d30` reuses `/api/cost`'s window logic but **over the machine total watts** = `power + cpu_power + dram_power` (NULL-coalesced). Provide a small `_total_w_expr = "COALESCE(power,0)+COALESCE(cpu_power,0)+COALESCE(dram_power,0)"`.
- `breakdown` + `unattributed` from `power_proc` (or `_hourly` for long ranges) per §2.5.
- Gate `enabled` on `day_price > 0` exactly like `/api/cost`; when disabled the page shows the "set a price" prompt (reuse the cost-card disabled state).

### 3.2 `GET /api/costs/entity?name=<...>&range=<...>&kind=<gpu|cpu>`

Per-entity drill-down time-series (power + cumulative cost) for the clicked row.

```jsonc
{
  "name": "ollama", "kind": "gpu", "range": "7d", "bucket_sec": 600,
  "currency": "$",
  "energy_kwh": 12.4, "cost": 2.60, "avg_w": 74, "peak_w": 190,
  "series": { "labels": [...], "watts": [...], "cost_cum": [...] },
  "resources": {                    // what it used, for the "resources" line
    "gpu_vram_peak_mb": 8200,       // from proc table MAX(mem) where service=name
    "cpu_avg_pct": null             // from power_proc-derived avg, optional
  }
}
```
`series.watts` from the bucketed `power_proc` query; `cost_cum` integrates per-bucket watts × tariff price (same per-bucket classify loop as `/api/cost`'s dual path). `resources.gpu_vram_peak_mb` joins the existing `proc` table (`MAX(mem) WHERE service=name AND ts>=since`).

### 3.3 New settings key (optional "other" baseline)

Add to `SETTING_DEFAULTS` (`app.py:3247`), blank by default so nothing changes unless the user opts in:
```python
    "system_idle_watts": "",   # operator-supplied baseline for mainboard/fans/PSU/disks;
                               # blank => "other" component omitted (we never guess wall power)
```
When set, `/api/costs` adds a flat `other` series at that wattage and includes it in totals, clearly tagged `estimated`. Round-trips for free via the existing `save_settings` allowlist.

---

## 4. UI — dedicated **Costs** tab

### 4.1 Nav + scaffolding
Add to `TABS` (`dashboard.html:1114`), after `experiments`:
```js
  {id:'costs', label:'Costs', charts:true},
```
`charts:true` so `buildCharts()`/range changes refresh it. The global range bar already shows on every tab and `R` already re-renders on `.rb` click (`:3388`) — extend that handler to call `renderCosts()` when `TAB==='costs'`, exactly like the existing `if(TAB==='experiments')` branch:
```js
  if(TAB==='costs') renderCosts();
```
Costs is `local`-capable now; for remotes it shows the machine's GPU-only numbers (it's not in `LOCAL_ONLY_TABS`). Section markup (`<section data-tab="costs" hidden>`) mirrors the existing cost-card structure (`#cost-disabled` prompt + `#cost-body`).

### 4.2 Layout (customer-centric, clean)
```
┌ Costs ──────────────────────────────────────── [range bar: 1h 6h 24h 7d 30d All] ┐
│  Disabled state (no price set): "Set your electricity price in Settings → Alerts"  │
│  ── KPI strip (tariff-aware) ─────────────────────────────────────────────────    │
│   [Total draw now 253 W]  [Today $1.84]  [7d $6.42]  [30d $24.90]                  │
│      ☀ day · 🌙 night sub-line in dual mode (reuse existing split2())              │
│  ── Power by component (stacked area) ────────────────────────────────────────    │
│   GPU (measured) · CPU (measured) · DRAM (measured) · [Other (estimated)]          │
│   caption: "GPU & CPU package are MEASURED; 'Other' is your configured baseline."  │
│  ── Breakdown by process / container / model (sortable table) ────────────────     │
│   Name            Kind   Avg W   Energy (kWh)   Cost     [click row -> drilldown]   │
│   ollama          GPU      74       12.4        $2.60                               │
│   python          CPU      18        3.1        $0.65                               │
│   host/idle       CPU      12        2.0        $0.42                               │
│  ── Drilldown (revealed on row click) ────────────────────────────────────────     │
│   "ollama — last 7d"   power line + cumulative-cost line; resources: VRAM peak 8 GB │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Renderer sketch (mirrors `renderCost`, reuses `mk`/`fmtLabels`/`cv`/`areaBg`)
```js
async function renderCosts(){
  const card = document.getElementById('costs-card'); if(!card) return;
  let j; try{ j = await (await fetch('/api/costs?range='+encodeURIComponent(R)
            +'&machine='+encodeURIComponent(CURRENT_HOST))).json(); }
  catch(e){ card.hidden = true; return; }
  card.hidden = false;
  const dis=document.getElementById('costs-disabled'), body=document.getElementById('costs-body');
  if(!j.enabled){ dis.hidden=false; body.hidden=true;
    document.getElementById('costs-settings-link').onclick=e=>{e.preventDefault();showTab('alerts');};
    return; }
  dis.hidden=true; body.hidden=false;
  const cur=j.currency||'$', money=v=>cur+(+v||0).toFixed(2);
  const m=(j.machines||[]).find(x=>x.name===CURRENT_HOST)||j.machines[0]||{};
  // KPI strip
  document.getElementById('costs-kpis').innerHTML=[
    {v:(m.now_w&&m.now_w.total||0)+' W', l:'Total draw now'},
    {v:money(m.cost&&m.cost.today), l:'Cost today'},
    {v:money(m.cost&&m.cost.d7),    l:'Last 7 days'},
    {v:money(m.cost&&m.cost.d30),   l:'Last 30 days'},
  ].map(t=>`<div class="kpi"><div class="v">${t.v}</div><div class="l">${t.l}</div></div>`).join('');
  // stacked component area
  const C=j.components, labels=fmtLabels(C.labels);
  const ds=[{label:'GPU (measured)',data:C.gpu,backgroundColor:'#3fb95066',borderColor:'#3fb950',fill:true,stack:'p',pointRadius:0},
            {label:'CPU (measured)',data:C.cpu,backgroundColor:'#4dabf766',borderColor:'#4dabf7',fill:true,stack:'p',pointRadius:0}];
  if(C.dram) ds.push({label:'DRAM (measured)',data:C.dram,backgroundColor:'#e3b34166',borderColor:'#e3b341',fill:true,stack:'p',pointRadius:0});
  if(C.other)ds.push({label:'Other (estimated)',data:C.other,backgroundColor:'#6b728066',borderColor:'#6b7280',fill:true,stack:'p',pointRadius:0,borderDash:[4,3]});
  mk('costscomp',{type:'line',data:{labels,datasets:ds},options:{responsive:true,maintainAspectRatio:false,
     interaction:{mode:'index',intersect:false},scales:{x:{ticks:{color:cv('--mut'),maxTicksLimit:10}},
     y:{stacked:true,min:0,ticks:{color:cv('--mut'),callback:v=>v+' W'}}},
     plugins:{legend:{labels:{color:cv('--tx'),boxWidth:12}}}},plugins:[areaBg]});
  // sortable breakdown table
  renderCostsTable(j.breakdown, cur);   // builds rows; each <tr> onclick -> drillEntity(name,kind)
}
async function drillEntity(name, kind){
  const j=await (await fetch('/api/costs/entity?name='+encodeURIComponent(name)
        +'&kind='+kind+'&range='+encodeURIComponent(R))).json();
  const cur=j.currency||'$', labels=fmtLabels(j.series.labels);
  document.getElementById('costs-drill-title').textContent=
    `${name} — ${R}: ${(j.energy_kwh||0).toFixed(2)} kWh · ${cur}${(j.cost||0).toFixed(2)}`;
  mk('costsdrill',{type:'line',data:{labels,datasets:[
     {label:'Power (W)',data:j.series.watts,borderColor:'#a371f7',backgroundColor:'#a371f733',fill:true,pointRadius:0,yAxisID:'y'},
     {label:'Cumulative '+cur,data:j.series.cost_cum,borderColor:'#3fb950',pointRadius:0,yAxisID:'y1'}]},
    options:{responsive:true,maintainAspectRatio:false,scales:{
      y:{position:'left',ticks:{color:cv('--mut'),callback:v=>v+' W'}},
      y1:{position:'right',grid:{display:false},ticks:{color:cv('--mut'),callback:v=>cur+(+v).toFixed(2)}}}}});
  document.getElementById('costs-drill').hidden=false;
}
```
- The breakdown table is sortable by clicking column headers (plain JS sort of the `breakdown` array, re-render). Row click → `drillEntity`.
- Timeframe selector = the **global range bar** (no new control), satisfying "selectable timeframe / reuse the global range."
- Captions state measured-vs-estimated explicitly, e.g. *"GPU and CPU-package power are measured (nvidia-smi + RAPL). 'Other' is the baseline you set in Settings; we never guess wall power."*

---

## 5. Build checklist (additive, no new deps)

1. `app.py:115` — extend `_SAMPLE_MIGRATIONS` with `"cpu_power REAL", "dram_power REAL"`.
2. `app.py:91` — add `power_proc` (+ optional `power_proc_hourly`) tables + indexes to `_DB_SCHEMA`.
3. `app.py:~606` — add `read_rapl_power()` + helpers (§1.4).
4. `app.py:~2128` — add `_attribute_power_rows()` (§2.3); leave `collect_top_processes` shape unchanged.
5. `app.py:sample_once` — call `collect_top_processes()` once, cache to `HEALTH["processes"]`; call `read_rapl_power()`; extend `samples` INSERT with `cpu_power,dram_power`; write `power_proc` rows; add `power_proc` to the retention sweep; surface `cpu_power`/`dram_power`/`rapl` on `LATEST`.
6. `app.py:health_scan` — stop calling `collect_top_processes` (reuse cached) to avoid double-stepping `_PROC_PREV`.
7. `app.py:SETTING_DEFAULTS` — add `system_idle_watts` (blank).
8. `app.py` — add `@app.route("/api/costs")` and `@app.route("/api/costs/entity")` (§3); leave `/api/cost` untouched.
9. `static/dashboard.html` — add `costs` tab to `TABS`, a `<section data-tab="costs">`, `renderCosts()`/`drillEntity()`/sortable table, and a `if(TAB==='costs') renderCosts()` branch in the `.rb` click handler and in `showTab`.
10. Tests: extend `tests/test_cost.py` pattern with a `test_costs.py` (insert synthetic `samples` incl. `cpu_power` + `power_proc` rows, assert per-component energy and per-entity drilldown math). Add a unit test for `read_rapl_power` wraparound (feed it a fake `RAPL_ROOT` via tmpdir with `energy_uj` files) and graceful-absent (empty dir → `{}`).

---

## 6. Honesty summary (what the page claims)

| Component | Source | Status |
|---|---|---|
| GPU watts | `nvidia-smi power.draw` (board) | **Measured** |
| CPU package watts | RAPL `package-*` / `psys` energy deltas | **Measured** (package, not wall) |
| DRAM watts | RAPL `dram` sub-domain (when present) | **Measured where available** |
| Per-GPU-service split | VRAM share (proxy for energy share) | **Measured base, proportional attribution** |
| Per-CPU-process split | CPU-time (jiffies) share × measured package W | **Measured base, proportional attribution** |
| "Other / rest of system" | operator-set `system_idle_watts` constant | **Estimated, opt-in, labelled** |
| Wall power | (needs a smart plug — not integrated) | **Not claimed** |

Every watt on the page is measured, an explicit operator constant, or a clearly-labelled proportional split of a measured quantity. We never fabricate a wall-power total.
