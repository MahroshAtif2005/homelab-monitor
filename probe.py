#!/usr/bin/env python3
"""HomeLab Monitor — run-on-remote probe.

The hub pipes this file over SSH (`ssh host python3 -`) every poll cycle.
Nothing persists on the remote: stdin is the script, stdout is one JSON blob,
exit code is 0 on partial-but-useful output. Plain stdlib only — works on any
Linux with Python 3.6+.

JSON shape is a deliberate subset of the hub's own /api/health.now block so the
UI can render local and remote with the same code paths.
"""
import json, os, socket, subprocess, sys, time, glob


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
        # MB to match the hub's units.
        return {"ram_total": total // 1024, "ram_used": used // 1024}
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


def read_temp():
    """First plausible CPU temp from /sys/class/thermal. Coarse, but matches
    what the hub itself reports for its own box."""
    try:
        for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
            try:
                t = int(open(z).read().strip()) / 1000.0
                if 10 < t < 130:
                    return {"ctemp": round(t, 1)}
            except Exception:
                continue
    except Exception:
        pass
    return {}


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


def read_gpu():
    """First NVIDIA GPU's snapshot via nvidia-smi. Returns {} if no driver or
    no GPU. We treat the first GPU as the 'representative' for the table; the
    detailed per-GPU view lives in the future GPU tab."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=3,
        )
        if r.returncode != 0:
            return {}
        lines = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        if not lines:
            return {}
        parts = [p.strip() for p in lines[0].split(",")]
        if len(parts) < 5:
            return {}
        return {"gpu": {
            "count":     len(lines),
            "name":      parts[4],
            "mem_used":  int(parts[0]),   # MB
            "mem_total": int(parts[1]),   # MB
            "util":      int(parts[2]),   # %
            "temp":      int(parts[3]),   # °C
        }}
    except Exception:
        return {}


def main():
    data = {
        "host": {
            **read_cpu(),
            **read_meminfo(),
            **read_loadavg(),
            **read_uptime(),
            **read_temp(),
            **read_gpu(),
            **read_systemd(),
            "disks": read_disks(),
            "hostname": socket.gethostname(),
        },
        "at": int(time.time()),
        "probe_version": "0.3",
    }
    json.dump(data, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
