#!/usr/bin/env python3
"""HomeLab Monitor — run-on-remote probe.

The hub pipes this file over SSH (`ssh host python3 -`) every poll cycle.
Nothing persists on the remote: stdin is the script, stdout is one JSON blob,
exit code is 0 on partial-but-useful output. Plain stdlib only — works on any
Linux with Python 3.6+.

JSON shape is a deliberate subset of the hub's own /api/health.now block so the
UI can render local and remote with the same code paths.
"""
import json, os, socket, sys, time, glob


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


def main():
    data = {
        "host": {
            **read_cpu(),
            **read_meminfo(),
            **read_loadavg(),
            **read_uptime(),
            **read_temp(),
            "disks": read_disks(),
            "hostname": socket.gethostname(),
        },
        "at": int(time.time()),
        "probe_version": "0.1",
    }
    json.dump(data, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
