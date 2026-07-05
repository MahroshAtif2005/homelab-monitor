"""
backend/collectors — background worker functions extracted from app.py (Phase 3.2).

Each function uses a lazy ``import app as _app`` so that module-level globals
(_app.LATEST, _app.HEALTH, DB, _app.LOCK, …) are resolved at call time, avoiding circular
imports. Thread-start lines remain in app.py and resolve via re-exports.
"""
import time
from concurrent.futures import ThreadPoolExecutor

# ── Rollup helpers (called by sample_once, take explicit conn param) ─────────

def _rollup_now(conn, ts, util, mem_used, mem_total, power, temp,
                cpu=None, ram_used=None, ram_total=None, load1=None, ctemp=None,
                cpu_power=None, dram_power=None):
    """Upsert the current minute and hour rollup buckets for samples."""
    m = (ts // 60) * 60
    h = (ts // 3600) * 3600
    for bucket, tbl in ((m, "samples_1m"), (h, "samples_1h")):
        conn.execute(f"""
            INSERT INTO {tbl}(ts,util,mem_used,mem_total,power,temp,cnt,
                cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power)
            VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?)
            ON CONFLICT(ts) DO UPDATE SET
              util=CASE WHEN excluded.util IS NOT NULL THEN (COALESCE(util,0)*cnt+excluded.util)/(cnt+1) ELSE util END,
              mem_used=CASE WHEN excluded.mem_used IS NOT NULL THEN (COALESCE(mem_used,0)*cnt+excluded.mem_used)/(cnt+1) ELSE mem_used END,
              mem_total=CASE WHEN excluded.mem_total IS NOT NULL THEN (COALESCE(mem_total,0)*cnt+excluded.mem_total)/(cnt+1) ELSE mem_total END,
              power=CASE WHEN excluded.power IS NOT NULL THEN (COALESCE(power,0)*cnt+excluded.power)/(cnt+1) ELSE power END,
              temp=CASE WHEN excluded.temp IS NOT NULL THEN (COALESCE(temp,0)*cnt+excluded.temp)/(cnt+1) ELSE temp END,
              cpu=CASE WHEN excluded.cpu IS NOT NULL THEN (COALESCE(cpu,0)*cnt+excluded.cpu)/(cnt+1) ELSE cpu END,
              ram_used=CASE WHEN excluded.ram_used IS NOT NULL THEN (COALESCE(ram_used,0)*cnt+excluded.ram_used)/(cnt+1) ELSE ram_used END,
              ram_total=CASE WHEN excluded.ram_total IS NOT NULL THEN (COALESCE(ram_total,0)*cnt+excluded.ram_total)/(cnt+1) ELSE ram_total END,
              load1=CASE WHEN excluded.load1 IS NOT NULL THEN (COALESCE(load1,0)*cnt+excluded.load1)/(cnt+1) ELSE load1 END,
              ctemp=CASE WHEN excluded.ctemp IS NOT NULL THEN (COALESCE(ctemp,0)*cnt+excluded.ctemp)/(cnt+1) ELSE ctemp END,
              cpu_power=CASE WHEN excluded.cpu_power IS NOT NULL THEN (COALESCE(cpu_power,0)*cnt+excluded.cpu_power)/(cnt+1) ELSE cpu_power END,
              dram_power=CASE WHEN excluded.dram_power IS NOT NULL THEN (COALESCE(dram_power,0)*cnt+excluded.dram_power)/(cnt+1) ELSE dram_power END,
              cnt=cnt+1
        """, (bucket, util, mem_used, mem_total, power, temp,
              cpu, ram_used, ram_total, load1, ctemp, cpu_power, dram_power))


def _rollup_net_now(conn, ts, net_rows):
    """Upsert the current minute and hour rollup buckets for net_samples."""
    if not net_rows:
        return
    m = (ts // 60) * 60
    h = (ts // 3600) * 3600
    # Aggregate across all ifaces for this tick
    total_in  = sum(r[2] or 0 for r in net_rows)
    total_out = sum(r[3] or 0 for r in net_rows)
    for bucket, tbl in ((m, "net_samples_1m"), (h, "net_samples_1h")):
        conn.execute(f"""
            INSERT INTO {tbl}(ts,bytes_in,bytes_out,cnt)
            VALUES(?,?,?,1)
            ON CONFLICT(ts) DO UPDATE SET
              bytes_in=(bytes_in*cnt+excluded.bytes_in)/(cnt+1),
              bytes_out=(bytes_out*cnt+excluded.bytes_out)/(cnt+1),
              cnt=cnt+1
        """, (bucket, total_in, total_out))


# ── Background workers ───────────────────────────────────────────────────────

def host_poller():
    import app as _app
    """Loop: probe every registered host whose last Test was healthy. Hosts are
    polled *concurrently* so one slow/timing-out remote can't delay the others and
    age their rows out to a false 'offline' (the flapping bug). A per-host adaptive
    timeout (issue #99) still isolates slow remotes — they self-calibrate to a
    working budget instead of going permanently dark, while fast hosts stay at the
    15s default. Errors are kept on the cache row so the UI can show a last error."""
    # Stagger the first run a touch so we don't fire before the app is fully up.
    time.sleep(2)
    while True:
        try:
            hosts = _app.list_hosts()
            if hosts:
                # Each host gets its own thread for the cycle, so the wall-clock
                # period is the slowest single probe, not the sum of all of them.
                with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
                    list(ex.map(_poll_one_host, hosts))
        except Exception as e:
            print("host_poller error:", e, flush=True)
        time.sleep(_app.INTERVAL)


def uptime_worker():
    import app as _app
    """Dedicated daemon loop: wakes every few seconds, probes due checks. Kept off the
    collector thread so a slow/hanging probe never delays metric sampling. Inert (zero
    outbound) when no checks are configured/enabled."""
    while True:
        try:
            _app._uptime_tick()
        except Exception as e:
            print("uptime_worker error:", e, flush=True)
        time.sleep(5)

# ── Notifier: Discord webhook + ntfy.sh + Telegram ─────────────────────────
# Edge-triggered: each alert key is remembered in _app._NOTIFIED so a flapping state
# doesn't spam the channel. A key clears when the underlying condition recovers
# (container becomes healthy again, disk drops below threshold, etc.), so the
# next failure re-fires exactly once.


def sample_once():
    import app as _app
    conts = _app.containers()
    nm = {c["id"]: c["name"] for c in conts}

    # ── GPU half ──────────────────────────────────────────────────────────────
    # Isolated in its own try/except so a flaky, missing or slow nvidia-_app.smi can
    # NEVER block the host metrics below. Before this, an exception here aborted
    # the whole sample, freezing CPU/RAM/temperature on every poll (and forever on
    # a GPU-less host). Now a GPU failure just degrades the GPU panel to "absent"
    # while temperature & friends keep refreshing.
    util = mem_used = mem_total = power = temp = 0.0
    gpus = []
    gpu_extra = {}
    procs = {}
    gpu_pids = {}
    gpu_avail = False
    try:
        # One CSV row per card (issue #95). Parse each field defensively: nvidia-_app.smi
        # emits the literal "[N/A]" / "[Not Supported]" for power.draw/temperature
        # on many consumer/laptop GPUs and inside _app.containers, even with `nounits` —
        # so degrade just the bad field to 0 rather than dropping the whole card.
        rows = _app.smi(["--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
                    "--format=csv,noheader,nounits"]).splitlines()
        for line in rows:
            if not line.strip():
                continue
            p = [x.strip() for x in line.split(",")]
            if len(p) < 7:
                continue
            u, mu, mt, pw, tp = (_app._gpu_num(x) for x in p[2:7])
            gpus.append({"idx": int(_app._gpu_num(p[0])), "name": p[1] or f"GPU {p[0]}",
                         "util": u, "mem_used": mu, "mem_total": mt, "power": pw, "temp": tp})
        amd = False
        if not gpus:
            # No NVIDIA card (or no nvidia-_app.smi) — fall back to the AMD amdgpu sysfs
            # back-end (issue #1). Additive: an NVIDIA host never reaches this.
            gpus = _app.amd_gpus()
            amd = bool(gpus)
            if not gpus:
                raise ValueError("no NVIDIA or AMD GPU detected")
        gpu_avail = True
        # Aggregate across cards for the existing single-GPU views: VRAM + power are
        # the pool, utilisation is averaged, temperature is the hottest card. AMD
        # cards expose the same keys, so this aggregation is vendor-agnostic.
        mem_used  = sum(g["mem_used"] for g in gpus)
        mem_total = sum(g["mem_total"] for g in gpus)
        power     = sum(g["power"] for g in gpus)
        util      = round(sum(g["util"] for g in gpus) / len(gpus))
        temp      = max(g["temp"] for g in gpus)
        if amd:
            # Per-card enrichment (clocks/throttle) and per-process VRAM attribution
            # are nvidia-_app.smi-specific; AMD shows the core panel (util/VRAM/temp/power)
            # without them. Per-process AMD attribution is a follow-up (issue #1).
            gpu_extra = {}
        else:
            _app._enrich_gpus(gpus)                 # mem-bw util, clocks, power limit, throttle reasons (best-effort)
            gpu_extra = _app._gpu_extra(gpus)
            for line in _app.smi(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]).splitlines():
                if line.strip():
                    pid, mem = (p.strip() for p in line.split(","))
                    svc = _app.service_for_pid(pid, nm)
                    procs[svc] = procs.get(svc, 0) + _app._gpu_num(mem)
                    try:
                        gpu_pids[int(pid)] = gpu_pids.get(int(pid), 0) + _app._gpu_num(mem)
                    except ValueError:
                        pass
    except Exception as e:
        # Log only on the ok→fail edge so a permanently GPU-less host doesn't spam.
        if _app.LATEST.get("gpu_avail"):
            print("GPU sample failed (continuing without GPU):", e, flush=True)

    # Detect models from EVERY recognised AI server, not just the ones holding the GPU
    # right now — so a server that has unloaded its model (e.g. OLLAMA_KEEP_ALIVE
    # expired) or sits between requests still shows up as Idle instead of vanishing.
    # Probes are independent 2 s-timeout HTTP calls, so run them in parallel.
    ai = [c for c in conts if _match_probe(c)]
    models = []
    model_catalog = []   # {service, provider, model, loaded, vram_mb} — the Installed-models registry (#219)
    if ai:
        with ThreadPoolExecutor(max_workers=min(8, len(ai))) as ex:
            found_lists = list(ex.map(probe_models, ai))
        provider_of = {c["name"]: _match_probe_key(c) for c in ai}
        for ct, found in zip(ai, found_lists):
            svc = ct["name"]
            provider = provider_of.get(svc)
            smem = procs.get(svc)                         # MB this server holds on the GPU now
            api_vram = any(v is not None for _, v in found)
            for mdl, vram in found:
                if vram is not None:                      # server reported its own VRAM (Ollama)
                    vram_val = round(vram)
                elif not api_vram and len(found) == 1 and smem:
                    vram_val = round(smem)                # single model ↔ all the server's VRAM
                else:
                    vram_val = None                        # server up but idle / can't attribute
                models.append((svc, mdl, vram_val))
                model_catalog.append({"service": svc, "provider": provider, "model": mdl,
                                       "loaded": vram_val is not None, "vram_mb": vram_val})

    # Attribute model-server traffic to its callers (who is driving Ollama, etc.).
    edges = _app.sample_callers(conts, {c["name"] for c in ai})

    # Model intelligence: per-model metadata (Ollama /api/show, cached) + live serving
    # telemetry (vLLM/TGI /metrics). Both best-effort — a slow/absent endpoint must
    # never wedge the sample, so each is isolated.
    try:
        model_meta = _app.collect_model_meta(ai, models)
    except Exception:
        model_meta = {}
    try:
        serving = _app.collect_serving(ai)
    except Exception:
        serving = []
    try:
        training = _app.collect_training(gpu_pids)
    except Exception:
        training = []
    try:
        devtools = _app.collect_devtools(gpu_pids)
    except Exception:
        devtools = []

    host = _app.read_host()
    # Measured CPU/DRAM watts (RAPL) + per-process CPU breakdown — both best-effort.
    # Call _app.collect_top_processes ONCE here (the sampler cadence) and cache it so the
    # Top-processes card + the cost attribution share one delta (_app.health_scan reuses it).
    rapl = {}
    try:
        rapl = _app.read_rapl_power()
    except Exception:
        rapl = {}
    cpu_power, dram_power = rapl.get("cpu_w"), rapl.get("dram_w")
    try:
        top_cpu = _app.collect_top_processes()
    except Exception:
        top_cpu = None
    _app.HEALTH["processes"] = top_cpu
    ts = int(time.time())
    if _app._DB_MAINTENANCE:
        return
    with _app.LOCK:
        # When the GPU is absent/failed, store NULL for the GPU columns (not 0) so
        # history charts skip the gap via AVG() instead of showing a fake 0 dip;
        # the host columns are always real.
        gcols = (util, mem_used, mem_total, power, temp) if gpu_avail else (None,)*5
        _app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp,cpu,ram_used,ram_total,load1,ctemp,cpu_power,dram_power)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (ts, *gcols, host["cpu"], host["ram_used"],
                    host["ram_total"], host["load1"], host["ctemp"], cpu_power, dram_power))
        for svc, mem in procs.items():
            _app.DB.execute("INSERT INTO proc VALUES(?,?,?)", (ts, svc, mem))
        pp_rows = _app._attribute_power_rows(ts, power, procs, cpu_power, top_cpu)
        if pp_rows:
            _app.DB.executemany("INSERT INTO power_proc(ts,kind,name,watts) VALUES(?,?,?,?)", pp_rows)
        for svc, mdl, vram in models:
            if vram is not None:          # persist only VRAM-bearing rows; idle catalogue
                _app.DB.execute("INSERT INTO models VALUES(?,?,?,?)", (ts, svc, mdl, vram))  # lives in _app.LATEST only
        for (caller, server), n in edges.items():
            _app.DB.execute("INSERT INTO edges VALUES(?,?,?,?)", (ts, caller, server, n))
        # Per-GPU history only when there's more than one card (single-GPU rigs are
        # already covered by the aggregate `samples` table) — keeps storage lean.
        if gpu_avail and len(gpus) > 1:
            for g in gpus:
                _app.DB.execute("INSERT INTO gpu_samples(ts,idx,util,mem_used,mem_total,power,temp) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (ts, g["idx"], g["util"], g["mem_used"], g["mem_total"], g["power"], g["temp"]))
        _cur_net_rows = list(_app._net_rows(ts, nm))   # host NICs + per-container talkers (#30)
        _app.DB.executemany("INSERT INTO net_samples(ts,iface,bytes_in,bytes_out) VALUES(?,?,?,?)",
                       _cur_net_rows)
        # Disk I/O moves fast, so sample it on its own tighter cadence (~45s) into
        # a dedicated 7-day ring — dense enough for per-device sparklines + the
        # anomaly baseline without bloating the _app.DB. Sourced from the _app.health_scan
        # snapshot (populated every 15s) so no extra /proc read here.
        if ts % 45 < _app.INTERVAL:
            dio = _app.HEALTH.get("disk_io") or {}
            if dio.get("available"):
                for it in (dio.get("items") or []):
                    _app.DB.execute("INSERT INTO disk_io_samples(ts,device,read_mb_s,write_mb_s,util_pct) "
                               "VALUES(?,?,?,?,?)",
                               (ts, it["device"], it.get("read_mb_s"),
                                it.get("write_mb_s"), it.get("util_pct")))
            # Persist a BOUNDED per-process I/O ring: only the top-few writers +
            # top-few readers from the attribution already computed (comm only,
            # never argv). Deduped by pid -> at most ~6 rows/poll, not all ~20
            # candidates. Feeds spike-time attribution; rides this same cadence.
            _pio = (_app.HEALTH.get("processes") or {}).get("io") or {}
            if _pio.get("available"):
                _seen_pids, _pio_rows = set(), []
                for _r in (sorted((_pio.get("writers") or []),
                                  key=lambda r: -(r.get("write_b_s") or 0))[:3]
                           + sorted((_pio.get("readers") or []),
                                    key=lambda r: -(r.get("read_b_s") or 0))[:3]):
                    _p = _r.get("pid")
                    if _p in _seen_pids:
                        continue
                    _seen_pids.add(_p)
                    _pio_rows.append((ts, _p, _r.get("name"),
                                      int(_r.get("read_b_s") or 0), int(_r.get("write_b_s") or 0)))
                if _pio_rows:
                    _app.DB.executemany("INSERT INTO proc_io_samples(ts,pid,comm,read_bps,write_bps) "
                                   "VALUES(?,?,?,?,?)", _pio_rows)
        if ts % 360 < _app.INTERVAL:
            for t in ("samples", "proc", "models", "edges", "events", "gpu_samples", "net_samples", "power_proc"):
                _app.DB.execute(f"DELETE FROM {t} WHERE ts<?", (ts - _app.RETENTION,))
            _app.DB.execute("DELETE FROM disk_io_samples WHERE ts<?", (ts - _app._DISK_IO_RETENTION,))
            _app.DB.execute("DELETE FROM proc_io_samples WHERE ts<?", (ts - _app._PROC_IO_RETENTION,))
        if ts % 60 < _app.INTERVAL:   # stale-run janitor: a crashed/disconnected push run -> killed
            _app.DB.execute("UPDATE runs SET status='killed', ended_at=COALESCE(ended_at,heartbeat_at,?) "
                       "WHERE status='running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?",
                       (ts, ts - 180))
        # Phase 1.2a: keep rollup tables current after each raw insert
        _app._rollup_now(_app.DB, ts, *gcols,
                    cpu=host["cpu"], ram_used=host["ram_used"], ram_total=host["ram_total"],
                    load1=host["load1"], ctemp=host["ctemp"],
                    cpu_power=cpu_power, dram_power=dram_power)
        _app._rollup_net_now(_app.DB, ts, _cur_net_rows)
        _app.DB.commit()
    # MLflow pull (network; outside the lock) every ~5 min when configured.
    if _app.get_settings().get("mlflow_uri") and ts % 300 < _app.INTERVAL:
        try:
            _app.sync_mlflow()
        except Exception as e:
            print("mlflow sync error:", e, flush=True)
    _app.LATEST.update(ts=ts, util=util, mem_used=mem_used, mem_total=mem_total, power=power, temp=temp,
                  cpu_power=cpu_power, dram_power=dram_power, rapl=rapl.get("domains"),
                  gpu_avail=gpu_avail, gpus=gpus, gpu_extra=gpu_extra,
                  procs=sorted(({"service": s, "mem": round(m)} for s, m in procs.items()), key=lambda x: -x["mem"]),
                  models=[{"service": s, "model": m, "vram": v} for s, m, v in models],
                  model_catalog=model_catalog,
                  model_meta=model_meta, serving=serving, training=training, devtools=devtools,
                  callers=sorted(({"caller": c, "server": s, "conns": n} for (c, s), n in edges.items()),
                                 key=lambda x: -x["conns"]), host=host)


def collector():
    import app as _app
    last_oom = last_health = last_notify = last_diskio = 0
    while True:
        try:
            sample_once()
            now = time.time()
            if now - last_oom > 60:
                _app.oom_scan(); last_oom = now
            if now - last_diskio > 60:
                try: _app.diskio_scan()
                except Exception as e: print("_app.diskio_scan error:", e, flush=True)
                last_diskio = now
            if now - last_health > 15:
                _app.health_scan(); last_health = now
            # Notifier runs *after* the latest health/oom data is in place, so
            # state-change detection sees a consistent snapshot.
            if now - last_notify > 20:
                try: _app.notify_scan()
                except Exception as e: print("_app.notify_scan error:", e, flush=True)
                last_notify = now
        except Exception as e:
            print("collector error:", e, flush=True)
        time.sleep(_app.INTERVAL)

# ── Insights ──────────────────────────────────────────────────────────────


def brief_worker():
    import app as _app
    """Dedicated daemon: every 30s, send the daily brief if it's due. Inert unless
    the brief is enabled with a configured channel."""
    while True:
        try:
            _app._brief_run_once()
        except Exception as e:
            print("brief_worker error:", e, flush=True)
        time.sleep(30)


