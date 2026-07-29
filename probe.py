#!/usr/bin/env python3
"""HomeLab Monitor — run-on-remote probe.

The hub pipes this file over SSH (`ssh host python3 -`) every poll cycle.
Nothing persists on the remote: stdin is the script, stdout is one JSON blob,
exit code is 0 on partial-but-useful output. Plain stdlib only — works on any
Linux with Python 3.6+.

JSON shape is a deliberate subset of the hub's own /api/health.now block so the
UI can render local and remote with the same code paths.
"""
import json, os, re, socket, subprocess, sys, time, glob, errno
import http.client

# ss listen-row parser, mirrored from app.py so service ports can be attributed
# on the remote without depending on the iproute2 *Python* bindings.
_LISTEN_RE = re.compile(r"^LISTEN\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+\s+users:\((?P<users>.*)\)\s*$")
_LISTEN_PORT_RE = re.compile(r":(\d+)$")
_LISTEN_PID_RE = re.compile(r"pid=(\d+)")


def read_loadavg():
    try:
        a, b, c, *_ = open("/proc/loadavg").read().split()
        return {"load1": float(a), "load5": float(b), "load15": float(c)}
    except Exception:
        return {}


def read_meminfo():
    try:
        m = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                if not v:
                    continue
                m[k.strip()] = int(v.strip().split()[0])  # kB
        total = m.get("MemTotal", 0)
        avail = m.get("MemAvailable", m.get("MemFree", 0) + m.get("Cached", 0))
        used = max(0, total - avail)
        # Non-reclaimable kernel memory (slab/page-tables/stacks): inside "used" RAM but
        # tied to no container/service, so the hub treemap can carve it out of the
        # "Host & other" bucket. SReclaimable is left out (it counts as available).
        kernel = m.get("SUnreclaim", 0) + m.get("KernelStack", 0) + m.get("PageTables", 0)
        # MB to match the hub's units.
        return {"ram_total": total // 1024, "ram_used": used // 1024,
                "ram_kernel": kernel // 1024}
    except Exception:
        return {}


def read_uptime():
    try:
        up, _ = open("/proc/uptime").read().split()
        return {"uptime": int(float(up))}
    except Exception:
        return {}


def _cpu_snapshot():
    """Sum across the aggregate `cpu` line of /proc/stat. Returns (total, idle)."""
    parts = open("/proc/stat").readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def read_cpu():
    """Sample twice with a short pause, return delta % busy."""
    try:
        t1, i1 = _cpu_snapshot()
        time.sleep(0.4)
        t2, i2 = _cpu_snapshot()
        td = t2 - t1
        idd = i2 - i1
        if td <= 0:
            return {}
        pct = max(0.0, min(100.0, (td - idd) * 100.0 / td))
        return {"cpu": round(pct, 1), "cores": os.cpu_count() or 1}
    except Exception:
        return {}


# hwmon drivers that expose the actual CPU die/core sensors (Intel/AMD/ARM).
_CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal")
# thermal_zone *types* that are the CPU (never acpitz/pch/nvme/wifi).
_CPU_ZONE  = ("x86_pkg_temp", "cpu_thermal", "cpu-thermal")

def _cpu_temp_c():
    """CPU temperature in °C that matches what `sensors` shows for the CPU cores.

    The old logic took the max of *every* /sys/class/thermal zone, which on many
    boards grabs a chipset/PCH/NVMe or an mis-calibrated package sensor reading
    10-20 °C hotter than the cores — so the dashboard showed e.g. 51 °C while
    `sensors` showed Core N at 37 °C. We now prefer the coretemp/k10temp hwmon and
    report the *average core* temperature across every CPU package; then a
    CPU-typed thermal zone; and only as a last resort the old hottest-plausible-
    zone (so exotic/ARM boards still report).

    Average, not max: on a many-core server (e.g. a 56-core dual-socket Xeon) a
    single busy core spiking to 45 °C while the other 55 sit at 39 °C should still
    read ~40, matching the bulk of what `sensors` shows — max-of-N-cores is noisy
    and biased high the more cores you have."""
    best = None
    # 1) hwmon coretemp/k10temp/zenpower — the real die/core sensors. Pool the
    #    per-core readings from *every* CPU package and report their average.
    try:
        cores, allt = [], []
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                name = open(hw + "/name").read().strip()
            except Exception:
                continue
            if name not in _CPU_HWMON:
                continue
            for inp in glob.glob(hw + "/temp*_input"):
                try:
                    t = int(open(inp).read().strip()) / 1000.0
                except Exception:
                    continue
                if not (0 < t < 130):
                    continue
                allt.append(t)
                try:
                    lbl = open(inp[:-6] + "_label").read().strip().lower()
                except Exception:
                    lbl = ""
                if lbl.startswith("core"):          # Intel "Core N" — exclude Package
                    cores.append(t)
        pool = cores or allt                        # cores if labelled, else whatever the die reports
        if pool:
            return round(sum(pool) / len(pool), 1)
    except Exception:
        pass
    # 2) thermal zones explicitly typed as the CPU.
    try:
        for z in glob.glob("/sys/class/thermal/thermal_zone*"):
            try:
                ztype = open(z + "/type").read().strip().lower()
            except Exception:
                continue
            if ztype in _CPU_ZONE or "cpu" in ztype:
                try:
                    t = int(open(z + "/temp").read().strip()) / 1000.0
                except Exception:
                    continue
                if 0 < t < 130 and (best is None or t > best):
                    best = t
        if best is not None:
            return round(best, 1)
    except Exception:
        pass
    # 3) last resort: hottest plausible zone (original behaviour) so we still
    #    report *something* on boards without coretemp or a CPU-typed zone.
    try:
        for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                t = int(open(z).read().strip()) / 1000.0
                if 10 < t < 130 and (best is None or t > best):
                    best = t
            except Exception:
                continue
    except Exception:
        pass
    return round(best, 1) if best is not None else None


def read_temp():
    t = _cpu_temp_c()
    return {"ctemp": t} if t is not None else {}


def read_disks():
    """Real filesystem mounts only — skip overlay/squashfs/tmpfs noise."""
    real_fs = {"ext4", "ext3", "xfs", "btrfs", "zfs", "vfat", "f2fs"}
    seen = set()
    out = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                _, mnt, fst = parts[:3]
                if fst not in real_fs:
                    continue
                if mnt in seen:
                    continue
                seen.add(mnt)
                try:
                    s = os.statvfs(mnt)
                    total = s.f_blocks * s.f_frsize
                    free = s.f_bavail * s.f_frsize
                    used = total - free
                    if total <= 0:
                        continue
                    out.append({
                        "mount": mnt,
                        "total": round(total / (1024 ** 3), 1),
                        "used":  round(used  / (1024 ** 3), 1),
                        "pct":   round(used * 100 / total, 1),
                    })
                except Exception:
                    continue
    except Exception:
        pass
    out.sort(key=lambda d: d["mount"])
    return out


def _collect_listen_ports():
    """{pid: [ports]} from `ss -Hlntp` (IPv4 + IPv6). Best-effort: if `ss`
    isn't installed on the remote, returns {} and ports just stay empty."""
    by_pid = {}
    for args in (["ss", "-Hlntp"], ["ss", "-Hln6tp"]):
        try:
            r = subprocess.run(args, capture_output=True, timeout=3)
        except Exception:
            continue
        for ln in r.stdout.decode("utf-8", "replace").splitlines():
            m = _LISTEN_RE.match(ln)
            if not m:
                continue
            pm = _LISTEN_PORT_RE.search(m.group("local"))
            if not pm:
                continue
            port = int(pm.group(1))
            for pid in (int(x) for x in _LISTEN_PID_RE.findall(m.group("users"))):
                by_pid.setdefault(pid, set()).add(port)
    return {pid: sorted(ports) for pid, ports in by_pid.items()}


def _systemd_props(unit):
    """systemctl show on a single unit, parsed into a flat key=value dict."""
    try:
        r = subprocess.run(
            ["systemctl", "show", "--no-pager", unit,
             "-p", "MainPID", "-p", "MemoryCurrent",
             "-p", "ActiveEnterTimestampMonotonic", "-p", "ExecMainStatus"],
            capture_output=True, timeout=3,
        )
    except Exception:
        return {}
    out = {}
    for ln in r.stdout.decode("utf-8", "replace").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def read_systemd():
    """Inventory systemd services via the CLI — no D-Bus client needed on the
    remote. Output matches the hub's HH.systemd shape so the existing Services
    tab renderer can show this verbatim.

    Returns only admin-deployed (`/etc/systemd/system/*.service`) units and any
    failed unit, mirroring how the local renderer filters its table — the full
    list of vendor services would just be noise."""
    try:
        r = subprocess.run(
            ["systemctl", "--no-pager", "--no-legend", "--plain",
             "list-units", "--type=service", "--all"],
            capture_output=True, timeout=6,
        )
        if r.returncode != 0:
            return {}
    except Exception:
        return {}

    admin_units = set()
    try:
        for f in os.listdir("/etc/systemd/system"):
            if f.endswith(".service"):
                admin_units.add(f)
    except Exception:
        pass

    loaded = running = failed = admin_total = 0
    services = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        s = line.strip()
        if not s:
            continue
        # Some systemd versions add a bullet at the start for failed/etc.
        if s[:2] in ("● ", "* "):
            s = s[2:].lstrip()
        parts = s.split(None, 4)
        if len(parts) < 4:
            continue
        unit, load, active, sub = parts[:4]
        desc = parts[4] if len(parts) > 4 else ""
        if not unit.endswith(".service"):
            continue
        if load == "loaded":   loaded  += 1
        if active == "active" and sub == "running": running += 1
        if active == "failed": failed  += 1
        is_admin = unit in admin_units
        if is_admin: admin_total += 1

        if active == "failed":
            status = "crit"
        elif active == "active" and sub == "running":
            status = "ok"
        elif active == "inactive":
            status = "info"
        else:
            status = "warn"

        if is_admin or status == "crit":
            services.append({
                "name": unit, "status": status,
                "active": active, "sub": sub, "desc": desc,
                "admin": is_admin, "watched": False,
            })

    # Enrich only the shown rows (admin/failed) — calling `systemctl show` per
    # unit on the full list would be a lot of fork-exec on a busy host.
    try:
        boot_uptime_s = float(open("/proc/uptime").read().split()[0])
    except Exception:
        boot_uptime_s = 0
    listen = _collect_listen_ports()
    for s in services:
        props = _systemd_props(s["name"])
        try:
            pid = int(props.get("MainPID") or 0)
        except ValueError:
            pid = 0
        try:
            mem = int(props.get("MemoryCurrent") or 0)
        except ValueError:
            mem = 0
        # 2^64-1 is systemd's sentinel for "no accounting / unset".
        s["mem_bytes"] = mem if 0 < mem < 0xFFFFFFFFFFFFFFFF else None
        try:
            enter_us = int(props.get("ActiveEnterTimestampMonotonic") or 0)
        except ValueError:
            enter_us = 0
        s["uptime_s"] = max(0, int(boot_uptime_s - enter_us / 1_000_000)) if (enter_us and boot_uptime_s) else 0
        if s["status"] == "crit":
            try:
                s["exit_status"] = int(props.get("ExecMainStatus", "0"))
            except ValueError:
                pass
        s["ports"] = listen.get(pid, []) if pid else []

    # Failed first, then admin units (running first), alphabetical within.
    def k(x):
        if x["status"] == "crit": return (0, x["name"])
        if x["status"] == "ok":   return (1, x["name"])
        return (2, x["name"])
    services.sort(key=k)

    return {"systemd": {
        "available": True,
        "summary": {"loaded": loaded, "running": running,
                    "failed": failed, "admin": admin_total},
        "services": services,
    }}


def _smi_int(v):
    """Tolerant int for nvidia-smi fields: '[N/A]' / '[Not Supported]' / blank → 0
    so one unreadable field (e.g. temperature on some cards) doesn't drop the
    whole GPU from the remote's report."""
    try:
        return int(float((v or "").strip()))
    except ValueError:
        return 0


def _nvidia_cards():
    """Every NVIDIA GPU via nvidia-smi as [{idx,name,util,mem_used,mem_total,
    power,temp}], [] if no driver / no GPU. Same query and field order as the
    hub's own collector, so per-card shapes match across the fleet."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=3,
        )
        if r.returncode != 0:
            return []
        cards = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            cards.append({
                "idx":       _smi_int(parts[0]),
                "name":      parts[1] or "GPU %s" % parts[0],
                "util":      _smi_int(parts[2]),   # %
                "mem_used":  _smi_int(parts[3]),   # MB
                "mem_total": _smi_int(parts[4]),   # MB
                "power":     _smi_int(parts[5]),   # W
                "temp":      _smi_int(parts[6]),   # °C
                "vendor":    "nvidia",
            })
        return cards
    except Exception:
        return []


def _nvidia_procs():
    """GPU compute processes via nvidia-smi as [{pid,name,mem}] (MB), heaviest
    first, [] when unavailable. process_name is a full path and may contain
    commas — pid is the first field and used_memory the last, so the middle is
    rejoined before taking the basename."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=3,
        )
        if r.returncode != 0:
            return []
        agg = {}
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            name = ",".join(parts[1:-1]).replace("\\", "/").rsplit("/", 1)[-1]
            pid = _smi_int(parts[0])
            # nvidia-smi emits one row per (process, GPU) — a process spanning
            # several cards appears once per card, so pool its VRAM per pid.
            key = (pid, name[:64] or "?")
            agg[key] = agg.get(key, 0) + _smi_int(parts[-1])
        procs = [{"pid": k[0], "name": k[1], "mem": v} for k, v in agg.items()]
        procs.sort(key=lambda p: -p["mem"])
        return procs[:20]
    except Exception:
        return []


def _amd_gpu_sysfs(drm_root="/sys/class/drm"):
    """Every AMD GPU from the in-kernel amdgpu sysfs interface — present on any
    host with the open `amdgpu` driver, no ROCm tools required. Same card shape
    as _nvidia_cards() (idx is assigned by read_gpu); [] when no AMD card is
    present or sysfs is unreadable. `drm_root` is injectable for tests."""
    def _int(path):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except OSError as e:
            # gpu_busy_percent intermittently returns EBUSY on amdgpu; retry once.
            if getattr(e, "errno", None) == errno.EBUSY:
                try:
                    with open(path) as f:
                        return int(f.read().strip())
                except (OSError, ValueError):
                    return None
            return None
        except ValueError:
            return None
    try:
        entries = sorted(os.listdir(drm_root))
    except OSError:
        return []
    cards = []
    for nm in entries:
        m = re.fullmatch(r"card(\d+)", nm or "")
        if not m:
            continue
        dev = os.path.join(drm_root, nm, "device")
        try:
            with open(os.path.join(dev, "vendor")) as f:
                if f.read().strip().lower() != "0x1002":   # PCI vendor id 0x1002 = AMD/ATI
                    continue
        except OSError:
            continue
        vram_total = _int(os.path.join(dev, "mem_info_vram_total"))  # bytes
        vram_used  = _int(os.path.join(dev, "mem_info_vram_used"))   # bytes
        # APU / unified-memory iGPU (e.g. Ryzen AI Max / Strix Halo): the dedicated
        # VRAM is a tiny BIOS carve-out (<= ~1 GiB) while the real working set — where
        # models actually load — lives in GTT (system RAM mapped to the GPU). Reporting
        # the 512 MB carve-out makes an idle 128 GB box read "29% full / VRAM ran low".
        # When this looks like an APU (tiny VRAM + large GTT), report GTT so residency +
        # pressure reflect reality. Discrete cards (large VRAM) are unaffected. Kept in
        # lockstep with app.amd_gpus() — same heuristic, same numbers.
        gtt_total = _int(os.path.join(dev, "mem_info_gtt_total"))    # bytes
        gtt_used  = _int(os.path.join(dev, "mem_info_gtt_used"))     # bytes
        if vram_total and gtt_total and vram_total <= (1 << 30):  # VRAM <= 1 GiB -> iGPU
            total, used = gtt_total, (gtt_used or 0)
        else:
            total, used = vram_total, vram_used
        busy  = _int(os.path.join(dev, "gpu_busy_percent"))      # %
        temp = 0
        try:
            hwroot = os.path.join(dev, "hwmon")
            for h in sorted(os.listdir(hwroot)):
                t = _int(os.path.join(hwroot, h, "temp1_input"))  # millidegrees C
                if t is not None:
                    temp = int(round(t / 1000.0))
                    break
        except OSError:
            pass
        name = None
        try:
            with open(os.path.join(dev, "product_name")) as f:   # newer kernels only
                name = f.read().strip() or None
        except OSError:
            pass
        cards.append({
            "name":      name or "AMD GPU %s" % m.group(1),
            "mem_used":  int(round(used / 1048576.0)) if used is not None else 0,
            "mem_total": int(round(total / 1048576.0)) if total is not None else 0,
            "util":      busy if busy is not None else 0,
            "power":     0,   # amdgpu sysfs has no cheap universal power counter
            "temp":      temp,
            "vendor":    "amd",
        })
    return cards


_MEM_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2,
              "gb": 1000**3, "gib": 1024**3, "tb": 1000**4, "tib": 1024**4}

def _stats_mem_bytes(v):
    """'1.552GiB / 62.72GiB' (docker stats MemUsage) → used bytes, or None."""
    m = re.match(r"\s*([\d.]+)\s*([KMGT]i?B|B)", (v or ""), re.I)
    if not m:
        return None
    try:
        return int(float(m.group(1)) * _MEM_UNITS[m.group(2).lower()])
    except (ValueError, KeyError):
        return None


def _pid_container_id(pid):
    """The 64-hex docker container id a pid runs in, from /proc/<pid>/cgroup —
    the same signal the hub's service_for_pid uses. None for host processes."""
    try:
        with open("/proc/%d/cgroup" % int(pid)) as f:
            m = re.search(r"[0-9a-f]{64}", f.read())
        return m.group(0) if m else None
    except (OSError, ValueError):
        return None


def read_docker(gpu_procs=None):
    """Docker inventory for the per-host Containers tab: `docker ps -a` for the
    list, one bounded `docker stats` pass for the running containers' RAM and
    CPU%, a `docker ps -s` pass for writable-layer disk size, and — when the
    GPU reader found compute processes — per-container VRAM attribution via
    each pid's cgroup, mirroring what the hub does locally. {} when docker is
    absent or the SSH user can't reach the socket — the Hosts-tab capability
    check explains which of the two it is. Read-only by design: the probe never
    starts, stops or inspects beyond `ps`/`stats`."""
    try:
        r = subprocess.run(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"],
                           capture_output=True, timeout=5)
        if r.returncode != 0:
            return {}
        conts = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            state = (d.get("State") or "").lower()
            conts.append({
                "id":     (d.get("ID") or "")[:64],
                "name":   (d.get("Names") or "?").split(",")[0],
                "image":  d.get("Image") or "",
                "state":  state,
                "status": d.get("Status") or "",
                "ports":  d.get("Ports") or "",
                "uptime": (d.get("RunningFor") or "").replace(" ago", "") if state == "running" else "",
            })
        # Writable-layer disk per container ("2.5MB (virtual 1.2GB)" → rw part).
        # Sizes make the daemon walk layers, so this pass is separate and its
        # failure only costs the Disk column.
        try:
            r3 = subprocess.run(["docker", "ps", "-a", "-s", "--no-trunc",
                                 "--format", "{{.ID}}\t{{.Size}}"],
                                capture_output=True, timeout=8)
            if r3.returncode == 0:
                sizes = {}
                for line in r3.stdout.decode("utf-8", "replace").splitlines():
                    cid, _, sz = line.partition("\t")
                    b = _stats_mem_bytes(sz)
                    if b is not None:
                        sizes[cid.strip()[:64]] = b
                for c in conts:
                    if c["id"] in sizes:
                        c["disk_bytes"] = sizes[c["id"]]
        except Exception:
            pass
        # VRAM per container: the GPU reader's compute pids, mapped through
        # /proc/<pid>/cgroup to a container id. Host processes simply don't match.
        if gpu_procs:
            by_id = {c["id"]: c for c in conts if c["id"]}
            for p in gpu_procs:
                cid = _pid_container_id(p.get("pid") or 0)
                c = by_id.get(cid)
                if c is not None:
                    c["vram_mb"] = c.get("vram_mb", 0) + (p.get("mem") or 0)
        running = sum(1 for c in conts if c["state"] == "running")
        if running:
            # One stats pass for RAM/CPU%. `docker stats` blocks ~1.5s to sample;
            # a wedged daemon must not sink the whole probe, so degrade silently.
            try:
                r2 = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                                    capture_output=True, timeout=8)
                if r2.returncode == 0:
                    stats = {}
                    for line in r2.stdout.decode("utf-8", "replace").splitlines():
                        if not line.strip():
                            continue
                        try:
                            s = json.loads(line)
                        except ValueError:
                            continue
                        stats[s.get("Name")] = s
                    for c in conts:
                        s = stats.get(c["name"])
                        if not s:
                            continue
                        mb = _stats_mem_bytes(s.get("MemUsage"))
                        if mb is not None:
                            c["mem_bytes"] = mb
                        try:
                            c["cpu_pct"] = float((s.get("CPUPerc") or "").rstrip("%"))
                        except ValueError:
                            pass
            except Exception:
                pass
        problems = sum(1 for c in conts
                       if "unhealthy" in c["status"].lower()
                       or c["state"] == "restarting"
                       or (c["state"] == "exited" and not re.search(r"Exited \(0\)", c["status"])))
        return {"docker": {"available": True, "containers": conts,
                           "summary": {"total": len(conts), "running": running,
                                       "problems": problems}}}
    except Exception:
        return {}


def read_gpu():
    """All of a host's GPUs for the hub. `gpus` is the per-card list (NVIDIA via
    nvidia-smi, AMD via the amdgpu sysfs interface — no ROCm needed; a hybrid
    box shows both, AMD re-indexed above the NVIDIA range like the hub's own
    collector). `gpu` keeps the legacy aggregate shape for older hubs, now
    pooled the same way the hub pools its local cards: VRAM + power summed,
    util averaged, temp = hottest card. `gpu_procs` is the nvidia-smi
    compute-apps list. {} on a host with neither vendor — the GPU panel is
    simply hidden."""
    nv, amd = _nvidia_cards(), _amd_gpu_sysfs()
    base = (max(g["idx"] for g in nv) + 1) if nv else 0
    for i, g in enumerate(amd):
        g["idx"] = base + i
    gpus = nv + amd
    if not gpus:
        return {}
    agg = {
        "count":     len(gpus),
        "name":      gpus[0]["name"],
        "vendor":    ("hybrid" if (nv and amd) else gpus[0]["vendor"]),
        "mem_used":  sum(g["mem_used"] for g in gpus),
        "mem_total": sum(g["mem_total"] for g in gpus),
        "util":      int(round(sum(g["util"] for g in gpus) / len(gpus))),
        "power":     sum(g["power"] for g in gpus),
        "temp":      max(g["temp"] for g in gpus),
    }
    out = {"gpu": agg, "gpus": gpus}
    if nv:
        procs = _nvidia_procs()
        if procs:
            out["gpu_procs"] = procs
    return out


# ── System / Hardware / Network / Security inventory ──────────────────────────
# Everything below is slow-changing context (OS, CPU model, NICs, firewall…) that
# the hub renders on the System / Network / Security tabs. All best-effort: every
# reader degrades to a partial dict (or drops the field) rather than raising, so
# the probe works the same on x86_64, aarch64 (Pi), armv7 and i686, with or
# without `ip`, `ss`, dmidecode, systemd or root. Anything that genuinely needs
# root and can't read returns None → the UI shows a neutral "needs elevated read".

def _which(name):
    for p in (os.environ.get("PATH") or "/usr/sbin:/usr/bin:/sbin:/bin").split(":"):
        if p and os.path.exists(os.path.join(p, name)):
            return os.path.join(p, name)
    return None


def _run(args, timeout=3):
    """subprocess.run → (rc, stdout_str) or (None, '') on any failure. Forces
    LC_ALL/LANG=C so package-manager output (zypper/apt/dnf sentinels like
    'No updates found') and other parsed text stay in English regardless of the
    SSH login shell's locale."""
    try:
        env = dict(os.environ, LC_ALL="C", LANG="C")
        r = subprocess.run(args, capture_output=True, timeout=timeout, env=env)
        return r.returncode, r.stdout.decode("utf-8", "replace")
    except Exception:
        return None, ""


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def _read_dt_string(*paths):
    """Read a NUL-terminated device-tree string (Raspberry Pi etc.)."""
    for p in paths:
        try:
            v = open(p, "rb").read().decode("utf-8", "replace").strip("\x00").strip()
            if v:
                return v
        except Exception:
            continue
    return None


def _os_release(base=""):
    data = {}
    for path in (base + "/etc/os-release", base + "/usr/lib/os-release"):
        txt = _read_text(path)
        if not txt:
            continue
        for line in txt.splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"').strip("'")
        if data:
            break
    return data


def os_family(osid, id_like="", uname=""):
    """Normalize an os-release ID into a family the remedies/UI branch on.
    Mirrors app.py's _detect_os normalization so both ends agree."""
    osid, id_like, uname = (osid or "").lower(), (id_like or "").lower(), (uname or "").lower()
    if uname == "darwin":
        return "macos"
    if osid == "alpine":
        return "alpine"
    if osid in ("opensuse-leap", "opensuse-tumbleweed", "sles", "sled") or "suse" in id_like:
        return "suse"
    if osid in ("debian", "ubuntu", "raspbian", "pop", "linuxmint") or "debian" in id_like:
        return "debian"
    if osid in ("fedora", "rhel", "centos", "rocky", "almalinux") or "rhel" in id_like or "fedora" in id_like:
        return "rhel"
    if osid in ("arch", "manjaro", "endeavouros") or "arch" in id_like:
        return "arch"
    return "linux"


def _detect_init():
    if os.path.isdir("/run/systemd/system"):
        return "systemd"
    comm = (_read_text("/proc/1/comm") or "").strip()
    if comm == "systemd":
        return "systemd"
    if os.path.exists("/run/openrc") or _which("rc-service"):
        return "openrc"
    return comm or None


def _detect_virt():
    if _which("systemd-detect-virt"):
        # Note: systemd-detect-virt exits 1 on bare metal (output "none"), 0 when
        # a virt tech is detected — so branch on the *output*, not the exit code.
        rc, out = _run(["systemd-detect-virt"], timeout=2)
        v = out.strip()
        if v == "none":
            return "bare-metal"
        if v and rc is not None:
            return v
    if os.path.exists("/.dockerenv"):
        return "docker"
    cg = _read_text("/proc/1/cgroup") or ""
    if "docker" in cg:
        return "docker"
    if "lxc" in cg:
        return "lxc"
    blob = ((_read_text("/sys/class/dmi/id/product_name") or "") + " " +
            (_read_text("/sys/class/dmi/id/sys_vendor") or "")).lower()
    for key, name in (("kvm", "kvm"), ("virtualbox", "virtualbox"), ("vmware", "vmware"),
                      ("qemu", "qemu"), ("xen", "xen"), ("microsoft", "hyper-v")):
        if key in blob:
            return name
    if " hypervisor" in (_read_text("/proc/cpuinfo") or ""):
        return "vm"
    return "bare-metal" if blob.strip() else None


def read_os():
    info = {}
    try:
        u = os.uname()
        info["kernel"] = u.release
        info["arch"] = u.machine
        info["hostname"] = u.nodename
    except Exception:
        pass
    rel = _os_release()
    osid = (rel.get("ID") or "").lower()
    info["id"] = osid or None
    info["pretty"] = rel.get("PRETTY_NAME") or rel.get("NAME") or None
    info["version_id"] = rel.get("VERSION_ID") or None
    info["family"] = os_family(osid, rel.get("ID_LIKE"))
    info["init"] = _detect_init()
    info["virt"] = _detect_virt()
    try:
        info["fqdn"] = socket.getfqdn()
    except Exception:
        pass
    for line in (_read_text("/proc/stat") or "").splitlines():
        if line.startswith("btime"):
            try:
                info["boot_time"] = int(line.split()[1])
            except (ValueError, IndexError):
                pass
            break
    pretty = info.get("pretty") or osid or info.get("kernel") or "unknown"
    info["label"] = pretty + (" · " + info["init"] if info.get("init") else "")
    return {"os": {k: v for k, v in info.items() if v is not None}}


def _count_physical_cores(cpuinfo):
    pairs, cur = set(), None
    for line in cpuinfo.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "physical id":
            cur = v
        elif k == "core id":
            pairs.add((cur, v))
    return len(pairs)


def read_hw(gpu_agg=None):
    hw = {}
    ci = _read_text("/proc/cpuinfo") or ""
    mname = arm = vendor = None
    phys = set()
    for line in ci.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if not v:
            continue
        if k == "model name" and not mname:        # x86 / most ARM full CPU string
            mname = v
        elif k == "vendor_id" and not vendor:
            vendor = v
        elif k == "physical id":
            phys.add(v)
        elif k == "hardware" and not arm:          # ARM SoC name (no "model name")
            arm = v
    # Never use the numeric x86 "model :" field; fall back arm-field → device tree.
    model = mname or arm or _read_dt_string("/proc/device-tree/model",
                                            "/sys/firmware/devicetree/base/model")
    threads = os.cpu_count() or 1
    cores = _count_physical_cores(ci) or threads
    if model:
        hw["cpu_model"] = model
    if vendor:
        hw["cpu_vendor"] = vendor
    hw["sockets"] = len(phys) or 1
    hw["cores"] = cores
    hw["threads"] = threads
    khz = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    try:
        hw["cpu_mhz_max"] = round(int(khz.strip()) / 1000)
    except (AttributeError, ValueError):
        m = re.search(r"cpu MHz\s*:\s*([\d.]+)", ci)
        if m:
            hw["cpu_mhz_max"] = round(float(m.group(1)))
    try:
        mi = {}
        for line in (_read_text("/proc/meminfo") or "").splitlines():
            k, _, v = line.partition(":")
            if v:
                mi[k.strip()] = int(v.split()[0])
        if mi.get("MemTotal"):
            hw["ram_total"] = mi["MemTotal"] // 1024     # MB
        if mi.get("SwapTotal"):
            hw["swap_total"] = mi["SwapTotal"] // 1024
    except Exception:
        pass
    machine = (_read_text("/sys/class/dmi/id/product_name") or "").strip()
    if machine.lower() in ("", "to be filled by o.e.m.", "system product name", "default string"):
        machine = _read_dt_string("/proc/device-tree/model",
                                  "/sys/firmware/devicetree/base/model") or ""
    if machine:
        hw["machine"] = machine
    # main() passes the aggregate it already read (possibly {} on a GPU-less
    # host) so nvidia-smi isn't shelled out to twice per probe cycle; only a
    # standalone read_hw() call falls back to reading the GPU itself.
    g = gpu_agg if gpu_agg is not None else read_gpu().get("gpu")
    if g and g.get("name"):
        hw["gpu_name"] = g["name"]
        if g.get("mem_total"):
            hw["gpu_mem_total"] = g["mem_total"]
    return {"hw": hw}


def _iface_type(name, d):
    if name == "lo":
        return "loopback"
    if os.path.isdir(d + "/wireless"):
        return "wifi"
    if name.startswith("wg"):
        return "wireguard"
    if name.startswith(("tun", "tap")):
        return "tunnel"
    if name.startswith(("docker", "br-", "veth", "virbr")):
        return "virtual"
    if name.startswith("bond"):
        return "bond"
    if os.path.exists(d + "/device"):
        return "ethernet"
    return "other"


def _net_ifaces():
    out = []
    base = "/sys/class/net"
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return out
    for n in names:
        # Skip container plumbing: per-container veth pairs and Docker's
        # per-network bridges (br-<hex>) are pure noise and dominate the list
        # on any Docker host. Keep docker0, named bridges (br0), eth*, wg*, etc.
        if n.startswith("veth") or re.match(r"br-[0-9a-f]{8,}$", n):
            continue
        d = base + "/" + n
        rd = lambda f: (_read_text(d + "/" + f) or "").strip() or None
        iface = {"name": n, "ipv4": [], "ipv6": [], "type": _iface_type(n, d)}
        mac = rd("address")
        if mac and mac != "00:00:00:00:00:00":
            iface["mac"] = mac
        st = rd("operstate")
        if st:
            iface["state"] = st
        try:
            iface["mtu"] = int(rd("mtu"))
        except (TypeError, ValueError):
            pass
        try:
            sp = int(rd("speed"))           # -1 / raises on virtual or down links
            if sp > 0:
                iface["speed_mbps"] = sp
        except (TypeError, ValueError):
            pass
        for stat, key in (("statistics/rx_bytes", "rx_bytes"), ("statistics/tx_bytes", "tx_bytes")):
            try:
                iface[key] = int(rd(stat))
            except (TypeError, ValueError):
                pass
        out.append(iface)
    return out


def _default_route():
    """(iface, gateway_ipv4) for the default route, or (None, None)."""
    for line in (_read_text("/proc/net/route") or "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 3 and p[1] == "00000000":
            try:
                gw = ".".join(str(int(p[2][i:i + 2], 16)) for i in (6, 4, 2, 0))
                return p[0], gw
            except ValueError:
                return p[0], None
    return None, None


def _primary_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))      # no packet is sent — just picks the source IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _collect_ips(ifaces):
    """Populate each iface's ipv4/ipv6 via `ip -o addr` when present. Falls back
    to attaching just the primary source IP to its iface if iproute2 is absent."""
    idx = {i["name"]: i for i in ifaces}
    rc, out = _run(["ip", "-o", "addr", "show"], timeout=3)
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
                i = idx.get(parts[1])
                if not i:
                    continue
                addr = parts[3].split("/")[0]
                if parts[2] == "inet":
                    i["ipv4"].append(addr)
                elif not addr.startswith("fe80"):
                    i["ipv6"].append(addr)
        return
    pip = _primary_ip()
    route_if, _ = _default_route()
    if pip and route_if and route_if in idx and pip not in idx[route_if]["ipv4"]:
        idx[route_if]["ipv4"].append(pip)


def _dns():
    ns, search = [], []
    for line in (_read_text("/etc/resolv.conf") or "").splitlines():
        line = line.strip()
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) > 1:
                ns.append(parts[1])
        elif line.startswith(("search", "domain")):
            search += line.split()[1:]
    return ns, search


_SS_NAME_RE = re.compile(r'"([^"]+)",pid=\d+')


def _hex_to_ip(h, fam):
    try:
        if fam == 4:
            return socket.inet_ntoa(bytes(int(h[i:i + 2], 16) for i in (6, 4, 2, 0)))
        raw = bytes(int(h[i:i + 2], 16) for i in range(0, 32, 2))
        return socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1] for i in (0, 4, 8, 12)))
    except Exception:
        return h


def _listen_from_proc():
    """Fallback listen list straight from /proc/net/{tcp,udp}[6] — used when `ss`
    isn't installed or isn't on the non-interactive PATH (common on minimal or
    old hosts). No process attribution, but bind address + exposure still work."""
    out, seen = [], set()
    for path, proto, fam, want in (("/proc/net/tcp", "tcp", 4, "0A"), ("/proc/net/tcp6", "tcp", 6, "0A"),
                                   ("/proc/net/udp", "udp", 4, "07"), ("/proc/net/udp6", "udp", 6, "07")):
        txt = _read_text(path)
        if not txt:
            continue
        for line in txt.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 4 or cols[3] != want or ":" not in cols[1]:
                continue
            hexip, hexport = cols[1].rsplit(":", 1)
            try:
                port = int(hexport, 16)
            except ValueError:
                continue
            allzero = set(hexip) <= {"0"}
            addr = (("0.0.0.0" if fam == 4 else "::") if allzero else _hex_to_ip(hexip, fam))
            key = (proto, addr, port)
            if key in seen:
                continue
            seen.add(key)
            out.append({"proto": proto, "addr": addr, "port": port, "exposed": allzero, "proc": None})
    return out


def _listen_sockets():
    """Listening TCP/UDP sockets with bind address + owning process, from `ss`.
    Each row flags `exposed` when bound to all interfaces (0.0.0.0 / ::) — the
    signal the Security tab uses to show which services face the network. Falls
    back to /proc/net parsing when `ss` is unavailable."""
    socks, seen = [], set()
    for args in (["ss", "-Hlntp"], ["ss", "-Hln6tp"], ["ss", "-Hlunp"], ["ss", "-Hlun6p"]):
        proto = "tcp" if "t" in args[1] else "udp"
        rc, out = _run(args, timeout=3)
        if rc is None:
            continue
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            pm = _LISTEN_PORT_RE.search(local)
            if not pm:
                continue
            port = int(pm.group(1))
            addr = local[:pm.start()].rstrip(":") or "*"
            key = (proto, addr, port)
            if key in seen:
                continue
            seen.add(key)
            nm = _SS_NAME_RE.search(ln)
            socks.append({"proto": proto, "addr": addr, "port": port,
                          "exposed": addr in ("0.0.0.0", "*", "::", "[::]"),
                          "proc": nm.group(1) if nm else None})
    if not socks:
        socks = _listen_from_proc()
    socks.sort(key=lambda s: (not s["exposed"], s["port"]))
    return socks


def _established_count():
    n = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        txt = _read_text(path)
        if not txt:
            continue
        for line in txt.splitlines()[1:]:
            cols = line.split()
            if len(cols) > 3 and cols[3] == "01":   # 01 = ESTABLISHED
                n += 1
    return n


def read_net():
    net = {}
    ifaces = _net_ifaces()
    _collect_ips(ifaces)
    route_if, gw = _default_route()
    if gw:
        net["gateway"] = gw
    pip = _primary_ip()
    if pip:
        net["primary_ip"] = pip
    primary = route_if or next((i["name"] for i in ifaces if pip and pip in i["ipv4"]), None)
    if primary:
        net["primary_iface"] = primary
    ns, search = _dns()
    if ns:
        net["dns"] = ns
    if search:
        net["search"] = search
    try:
        net["fqdn"] = socket.getfqdn()
    except Exception:
        pass
    net["ifaces"] = ifaces
    net["listen"] = _listen_sockets()
    net["established_count"] = _established_count()
    return {"net": net}


def _firewall():
    if _which("ufw"):
        rc, out = _run(["ufw", "status"], timeout=3)
        o = out.lower()
        if rc == 0 and "status: active" in o:
            return {"backend": "ufw", "active": True, "open_ports": o.count("allow") or None}
        if rc == 0 and "status: inactive" in o:
            return {"backend": "ufw", "active": False}
        return {"backend": "ufw", "active": None}        # needs root
    if _which("firewall-cmd"):
        rc, out = _run(["firewall-cmd", "--state"], timeout=3)
        if rc is not None and out.strip() in ("running", "not running"):
            return {"backend": "firewalld", "active": out.strip() == "running"}
        return {"backend": "firewalld", "active": None}
    if _which("nft"):
        rc, out = _run(["nft", "list", "ruleset"], timeout=3)
        if rc == 0:
            return {"backend": "nftables", "active": bool(out.strip())}
        return {"backend": "nftables", "active": None}
    if _which("iptables"):
        rc, out = _run(["iptables", "-S"], timeout=3)
        if rc == 0:
            return {"backend": "iptables",
                    "active": any(l.startswith("-A") for l in out.splitlines())}
        return {"backend": "iptables", "active": None}
    return {"backend": None, "active": False}            # none detected


def _ssh_config():
    cfg = {}
    sshd = _which("sshd")
    if sshd:
        rc, out = _run([sshd, "-T"], timeout=3)          # resolves defaults; needs root
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    cfg[parts[0].lower()] = parts[1].strip()
    if not cfg:
        for line in (_read_text("/etc/ssh/sshd_config") or "").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    cfg.setdefault(parts[0].lower(), parts[1].strip())
    if not cfg:
        return None
    out = {}
    if "permitrootlogin" in cfg:
        out["permit_root"] = cfg["permitrootlogin"].split()[0]
    if "passwordauthentication" in cfg:
        out["password_auth"] = cfg["passwordauthentication"].split()[0]
    try:
        out["port"] = int(cfg["port"].split()[0])
    except (KeyError, ValueError):
        pass
    return out or None


def _selinux():
    if os.path.exists("/sys/fs/selinux/enforce"):
        v = (_read_text("/sys/fs/selinux/enforce") or "").strip()
        return "enforcing" if v == "1" else "permissive" if v == "0" else None
    if _which("getenforce"):
        rc, out = _run(["getenforce"], timeout=2)
        return (out.strip().lower() or "disabled") if rc is not None else None
    return "disabled"


def _apparmor():
    v = (_read_text("/sys/module/apparmor/parameters/enabled") or "").strip()
    if v:
        return "enabled" if v in ("Y", "y") else "disabled"
    return "enabled" if os.path.isdir("/sys/kernel/security/apparmor") else "disabled"


def _fail2ban():
    installed = bool(_which("fail2ban-client") or _which("fail2ban-server")
                     or os.path.exists("/lib/systemd/system/fail2ban.service")
                     or os.path.exists("/etc/systemd/system/fail2ban.service"))
    if not installed:
        return {"installed": False}
    active = None
    rc, out = _run(["systemctl", "is-active", "fail2ban"], timeout=3)
    if rc is not None and out.strip() in ("active", "inactive", "failed"):
        active = out.strip() == "active"
    return {"installed": True, "active": active}


def _reboot_required():
    if os.path.exists("/var/run/reboot-required") or os.path.exists("/run/reboot-required"):
        return True
    if _which("needs-restarting"):
        rc, _out = _run(["needs-restarting", "-r"], timeout=5)
        return rc == 1 if rc is not None else None        # rc 1 = reboot needed
    return False


def _auto_updates():
    txt = _read_text("/etc/apt/apt.conf.d/20auto-upgrades")
    if txt:
        m = re.search(r'Unattended-Upgrade"\s+"(\d+)"', txt)
        if m:
            return m.group(1) != "0"
    for unit in ("unattended-upgrades.service", "dnf-automatic.timer", "apt-daily-upgrade.timer"):
        rc, out = _run(["systemctl", "is-enabled", unit], timeout=3)
        if rc is not None and out.strip() == "enabled":
            return True
    if _which("unattended-upgrade") or _which("dnf-automatic"):
        return False
    return None


# ── Pending package updates ───────────────────────────────────────────────────
# Strictly cached / offline: we read what the host's package manager already
# computed (its daily timer), never triggering a network refresh and never
# assuming root. Each reader returns {count, security, kernel, source}; any field
# we can't determine stays None so the UI shows a neutral "needs elevated read"
# instead of a misleading zero. The hub adds the "newer OS release available"
# signal separately (it needs the network, which the probe deliberately avoids).

def _parse_updates_file(txt):
    """Parse update-notifier's pre-rendered text. Its wording varies across
    releases ('N updates can be applied immediately.' / 'M of these updates are
    standard security updates.'), so we go line-by-line: the line that mentions
    security gives the security count, the first other count-bearing line gives
    the total. Returns (count, security), either possibly None."""
    count = security = None
    for line in txt.splitlines():
        m = re.search(r"(\d+)", line)
        if not m:
            continue
        n, low = int(m.group(1)), line.lower()
        if "securit" in low:
            security = n
        elif count is None and ("update" in low or "package" in low or "can be" in low):
            count = n
    return count, security


def _apt_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "apt"}
    # update-notifier pre-renders the counts (with the security split) into a file
    # any user can read — no apt invocation, no lock, no refresh.
    txt = _read_text("/var/lib/update-notifier/updates-available")
    if txt:
        out["count"], out["security"] = _parse_updates_file(txt)
    # If the file was missing (no update-notifier) or we still need the kernel
    # signal it can't give, fall back to the cached upgradable list. `apt list
    # --upgradable` reads only the on-disk lists — it does not hit the network.
    if out["count"] is None or out["kernel"] is None:
        rc, txt2 = _run(["apt", "list", "--upgradable"], timeout=6)
        if rc is not None and txt2:
            pkgs = [l for l in txt2.splitlines()
                    if "/" in l.split(" ", 1)[0] and "]" in l]
            if out["count"] is None:
                out["count"] = len(pkgs)
            if out["security"] is None:
                out["security"] = sum(1 for l in pkgs if "-security" in l.lower())
            out["kernel"] = any(l.split("/", 1)[0].startswith(("linux-image", "linux-generic"))
                                for l in pkgs)
    return out


def _zypper_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "zypper"}
    # --no-refresh keeps it offline; status column 'v' marks an available update.
    rc, txt = _run(["zypper", "--non-interactive", "--no-refresh", "--quiet",
                    "list-updates"], timeout=8)
    # Gate on rc only (like dnf/pacman). With --quiet a zero-update host prints
    # nothing and no "No updates found" banner, so the old `and txt` / banner
    # check left count=None ("needs elevated read") for an up-to-date SUSE host.
    if rc is not None:
        names = []
        for l in txt.splitlines():
            parts = [p.strip() for p in l.split("|")]
            if len(parts) >= 3 and parts[0] == "v":
                names.append(parts[2])
        out["count"] = len(names)
        out["kernel"] = any(n.startswith("kernel-") for n in names)
    rc2, txt2 = _run(["zypper", "--non-interactive", "--no-refresh", "--quiet",
                      "list-patches", "--category", "security"], timeout=8)
    if rc2 is not None and txt2:
        out["security"] = sum(1 for l in txt2.splitlines()
                              if "security" in l.lower() and "|" in l)
    return out


def _dnf_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "dnf"}
    bin_ = "dnf" if _which("dnf") else "yum"
    # -C = cache-only (no network).
    if bin_ == "dnf":
        # repoquery emits one clean package name per line — unlike `check-update`,
        # which wraps a row across two lines when name.arch is wide (narrow,
        # non-tty output), dropping those packages from the count.
        rc, txt = _run(["dnf", "-C", "-q", "repoquery", "--upgrades", "--qf", "%{name}"],
                       timeout=10)
        if rc == 0:
            names = [l.strip() for l in txt.splitlines() if l.strip()]
            out["count"] = len(names)
            out["kernel"] = any(n.startswith("kernel") for n in names)
    else:
        # yum (RHEL7) has no builtin repoquery --upgrades; parse check-update but
        # reassemble wrapped rows (bare name.arch on one line, version/repo on the
        # next, indented). rc 100 = updates available, 0 = none.
        rc, txt = _run(["yum", "-C", "-q", "check-update"], timeout=10)
        if rc in (0, 100):
            names, pending = [], None
            for raw in txt.splitlines():
                if not raw.strip() or raw.startswith(("Obsoleting", "Last metadata", "Security:")):
                    continue
                parts = raw.split()
                if raw[:1].isspace() and pending:           # continuation: version repo
                    names.append(pending); pending = None
                elif len(parts) == 1 and "." in parts[0]:   # wrapped: name.arch only
                    pending = parts[0]
                elif len(parts) >= 3 and "." in parts[0]:   # name.arch version repo
                    names.append(parts[0]); pending = None
            out["count"] = len(names)
            out["kernel"] = any(n.startswith("kernel") for n in names)
    rc2, txt2 = _run([bin_, "-C", "-q", "updateinfo", "list", "security"], timeout=10)
    if rc2 == 0 and txt2:
        # Count distinct advisory IDs (first column), not package-instances — one
        # advisory fixing N packages would otherwise be counted N times.
        adv = set()
        for l in txt2.splitlines():
            f = l.split()
            if f and ("-" in f[0] or ":" in f[0]):   # FEDORA-2024-xxxx / RHSA-2024:xxxx
                adv.add(f[0])
        out["security"] = len(adv)
    return out


def _pacman_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "pacman"}
    # `pacman -Qu` compares against the cached sync DB — no refresh. Arch has no
    # security categorisation, so `security` stays None by design.
    rc, txt = _run(["pacman", "-Qu"], timeout=6)
    if rc is not None:
        names = [l.split()[0] for l in txt.splitlines() if l.strip()]
        out["count"] = len(names)
        out["kernel"] = any(n.startswith("linux") for n in names)
    return out


def _apk_updates():
    out = {"count": None, "security": None, "kernel": None, "source": "apk"}
    rc, txt = _run(["apk", "version", "-l", "<"], timeout=6)
    if rc is not None and txt:
        names = []
        for l in txt.splitlines():
            l = l.strip()
            if not l or l.startswith(("Installed", "WARNING")):
                continue
            names.append(l.split()[0])
        out["count"] = len(names)
        out["kernel"] = any(n.startswith("linux-") for n in names)
    return out


def read_updates():
    try:
        if _which("apt") or _which("apt-get"):
            res = _apt_updates()
        elif _which("zypper"):
            res = _zypper_updates()
        elif _which("dnf") or _which("yum"):
            res = _dnf_updates()
        elif _which("pacman"):
            res = _pacman_updates()
        elif _which("apk"):
            res = _apk_updates()
        else:
            return None
    except Exception:
        return None
    if res is not None:
        res["checked"] = "cached"
    return res


def read_sec():
    return {"sec": {
        "firewall":        _firewall(),
        "ssh":             _ssh_config(),
        "selinux":         _selinux(),
        "apparmor":        _apparmor(),
        "fail2ban":        _fail2ban(),
        "reboot_required": _reboot_required(),
        "auto_updates":    _auto_updates(),
        "updates":         read_updates(),
    }}


# ── CPU package power via RAPL (Intel/AMD powercap) ──────────────────────────
# app.py keeps cross-call state to derive watts; the probe runs once per poll
# over SSH and can't, so it reads the cumulative energy counter twice around a
# short dwell (same trick read_cpu uses) and derives instantaneous watts.
# Package-only, MEASURED — not wall power. Degrades to {} when RAPL is absent.
RAPL_ROOT = os.environ.get("RAPL_ROOT", "/sys/class/powercap")

def _rapl_uj(path):
    try:
        with open(os.path.join(path, "energy_uj")) as f:
            e = int(f.read().strip())
        with open(os.path.join(path, "max_energy_range_uj")) as f:
            m = int(f.read().strip())
        return e, m
    except (OSError, ValueError):
        return None

def _rapl_domains():
    out = {}
    try:
        for top in sorted(glob.glob(os.path.join(RAPL_ROOT, "intel-rapl:*"))):
            if os.path.basename(top).startswith("intel-rapl-mmio"):
                continue
            nm = (_read_text(os.path.join(top, "name")) or "").strip()
            if nm:
                out[top] = nm
            for sub in sorted(glob.glob(os.path.join(top, "intel-rapl:*:*"))):
                snm = (_read_text(os.path.join(sub, "name")) or "").strip()
                if snm:
                    out[sub] = snm
    except Exception:
        pass
    return out

def read_power(dwell=0.3):
    """Instantaneous RAPL watts via two reads dwell seconds apart. Returns
    {"cpu_power": W, "dram_power": W} (key omitted if that domain is absent),
    or {} when RAPL is unavailable. Mirrors app.read_rapl_power: psys if present
    else sum(package-*); dram = sum of dram sub-domains."""
    domains = _rapl_domains()
    if not domains:
        return {}
    first = {}
    for path in domains:
        rd = _rapl_uj(path)
        if rd is not None:
            first[path] = (rd[0], rd[1], time.monotonic())
    if not first:
        return {}
    time.sleep(dwell)
    per = {}
    for path, name in domains.items():
        if path not in first:
            continue
        rd = _rapl_uj(path)
        if rd is None:
            continue
        e1, mrange = rd
        e0, _m0, t0 = first[path]
        dt = time.monotonic() - t0
        if dt <= 0:
            continue
        de = e1 - e0
        if de < 0:                 # uint counter wraparound
            de += mrange
        per[name] = max(0.0, de / 1e6 / dt)
    if not per:
        return {}
    psys = per.get("psys")
    pkgs = [w for n, w in per.items() if n.startswith("package")]
    cpu_w = psys if psys is not None else (sum(pkgs) if pkgs else None)
    drams = [w for n, w in per.items() if n == "dram"]
    out = {}
    if cpu_w is not None:
        out["cpu_power"] = round(cpu_w, 1)
    if drams:
        out["dram_power"] = round(sum(drams), 1)
    return out


def read_ollama_models():
    """Detect ollama on this host (#219 fleet-wide, Ollama slice): loaded models
    from /api/ps (with live VRAM), falling back to the on-disk catalogue
    (/api/tags) so models still show up even when none are currently loaded.
    Read-only, short timeout, stdlib only -- silently returns [] if ollama isn't
    running here. Shape matches what backend/probes' local model_catalog uses,
    so the hub can merge local + remote entries with the same code path."""
    def _http_json(path, timeout=2):
        try:
            c = http.client.HTTPConnection("127.0.0.1", 11434, timeout=timeout)
            try:
                c.request("GET", path)
                r = c.getresponse()
                body = r.read()
                status = r.status
            finally:
                c.close()
            return json.loads(body) if status < 400 else None
        except Exception:
            return None

    hostname = socket.gethostname()
    # Resident set first (/api/ps): name -> live VRAM MB.
    loaded = {}
    ps = _http_json("/api/ps")
    for m in (ps or {}).get("models", []) if isinstance(ps, dict) else []:
        name = m.get("name")
        if not name:
            continue
        vram = m.get("size_vram") or 0
        loaded[name] = round(vram / 1048576) if vram else None
    # Full on-disk catalogue (/api/tags) with registry detail, cross-referenced
    # with the resident set — previously a loaded model SUPPRESSED the
    # catalogue (ps-then-tags fallback), so a busy host's registry went thin
    # exactly when it was interesting.
    out = []
    tags = _http_json("/api/tags")
    for m in (tags or {}).get("models", []) if isinstance(tags, dict) else []:
        name = m.get("name")
        if not name:
            continue
        det = m.get("details") or {}
        out.append({
            "host": hostname, "service": "ollama", "provider": "ollama",
            "model": name, "loaded": name in loaded, "vram_mb": loaded.get(name),
            "size_bytes": m.get("size"), "family": det.get("family"),
            "param_size": det.get("parameter_size"),
            "quant": det.get("quantization_level"),
            "modified": m.get("modified_at"),
        })
    # A model can be resident without appearing on disk (deleted while loaded,
    # pulled through another path) — keep it visible rather than vanishing.
    known = {m["model"] for m in out}
    for name, vram in loaded.items():
        if name not in known:
            out.append({"host": hostname, "service": "ollama", "provider": "ollama",
                        "model": name, "loaded": True, "vram_mb": vram})
    return out


def main():
    gpu_block = read_gpu()
    data = {
        "host": {
            **read_cpu(),
            **read_power(),
            **read_meminfo(),
            **read_loadavg(),
            **read_uptime(),
            **read_temp(),
            **gpu_block,
            **read_docker(gpu_procs=gpu_block.get("gpu_procs")),
            **read_systemd(),
            **read_os(),
            **read_hw(gpu_agg=gpu_block.get("gpu") or {}),
            **read_net(),
            **read_sec(),
            "disks": read_disks(),
            "hostname": socket.gethostname(),
        },
        "model_catalog": read_ollama_models(),
        "at": int(time.time()),
        "probe_version": "0.12",
    }
    json.dump(data, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
