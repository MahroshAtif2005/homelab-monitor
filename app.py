#!/usr/bin/env python3
"""Universal GPU monitor: samples nvidia-smi, attributes VRAM to whatever
container/process is using the GPU (fully dynamic), stores in SQLite, and
serves a glanceable dashboard with downsample-on-read for long time ranges."""
import os, re, time, json, socket, sqlite3, threading, subprocess, http.client
from flask import Flask, request, jsonify

DB_PATH      = os.environ.get("DB_PATH", "/data/gpu.db")
INTERVAL     = int(os.environ.get("SAMPLE_INTERVAL", "10"))
RETENTION    = int(os.environ.get("RETENTION_DAYS", "180")) * 86400
DOCKER_SOCK  = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
PORT         = int(os.environ.get("PORT", "8099"))
MAX_POINTS   = 360
HEX64        = re.compile(r"[0-9a-f]{64}")

app = Flask(__name__, static_url_path="/static", static_folder="static")
LOCK = threading.Lock()
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY, util REAL, mem_used REAL, mem_total REAL, power REAL, temp REAL)")
DB.execute("CREATE TABLE IF NOT EXISTS proc(ts INTEGER, service TEXT, mem REAL)")
DB.execute("CREATE INDEX IF NOT EXISTS idx_proc_ts ON proc(ts)")
DB.commit()

LATEST = {"ts": 0, "util": 0, "mem_used": 0, "mem_total": 0, "power": 0, "temp": 0, "procs": []}
_cid_cache = {"map": {}, "at": 0}


def docker_name_map():
    """Map 12-char container id -> friendly name via the Docker socket (cached 30s)."""
    if time.time() - _cid_cache["at"] < 30 and _cid_cache["map"]:
        return _cid_cache["map"]
    m = {}
    try:
        c = http.client.HTTPConnection("localhost")
        c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.sock.connect(DOCKER_SOCK)
        c.request("GET", "/containers/json?all=0")
        data = json.loads(c.getresponse().read())
        for ct in data:
            name = (ct.get("Names") or ["/?"])[0].lstrip("/")
            m[ct["Id"][:12]] = name
        c.close()
    except Exception as e:
        print("docker map error:", e, flush=True)
    if m:
        _cid_cache.update(map=m, at=time.time())
    return _cid_cache["map"]


def service_for_pid(pid):
    """Resolve a host PID to a container name, or host:<comm>. Fully dynamic."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            txt = f.read()
        h = HEX64.search(txt)
        if h:
            name = docker_name_map().get(h.group(0)[:12])
            if name:
                return name
        with open(f"/proc/{pid}/comm") as f:
            return "host:" + f.read().strip()
    except Exception:
        return f"pid:{pid}"


def smi(args):
    return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=15).stdout.strip()


def sample_once():
    g = smi(["--query-gpu=utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"]).splitlines()[0].split(",")
    util, mem_used, mem_total, power, temp = (float(x.strip() or 0) for x in g)
    procs = {}
    apps = smi(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"])
    for line in apps.splitlines():
        if not line.strip():
            continue
        pid, mem = (p.strip() for p in line.split(","))
        svc = service_for_pid(pid)
        procs[svc] = procs.get(svc, 0) + float(mem or 0)
    ts = int(time.time())
    with LOCK:
        DB.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?)",
                   (ts, util, mem_used, mem_total, power, temp))
        for svc, mem in procs.items():
            DB.execute("INSERT INTO proc VALUES(?,?,?)", (ts, svc, mem))
        if ts % 360 < INTERVAL:  # prune roughly hourly
            cutoff = ts - RETENTION
            DB.execute("DELETE FROM samples WHERE ts<?", (cutoff,))
            DB.execute("DELETE FROM proc WHERE ts<?", (cutoff,))
        DB.commit()
    LATEST.update(ts=ts, util=util, mem_used=mem_used, mem_total=mem_total, power=power, temp=temp,
                  procs=sorted(({"service": s, "mem": round(m)} for s, m in procs.items()),
                               key=lambda x: -x["mem"]))


def collector():
    while True:
        try:
            sample_once()
        except Exception as e:
            print("sample error:", e, flush=True)
        time.sleep(INTERVAL)


RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000, "all": None}


@app.route("/api/data")
def api_data():
    rng = request.args.get("range", "6h")
    span = RANGES.get(rng, 21600)
    now = int(time.time())
    with LOCK:
        cur = DB.cursor()
        if span is None:
            row = cur.execute("SELECT MIN(ts) FROM samples").fetchone()
            since = row[0] or now
        else:
            since = now - span
        actual = max(1, now - since)
        bk = max(INTERVAL, round(actual / MAX_POINTS))
        tot = cur.execute(
            "SELECT (ts/?)*? b, AVG(util), AVG(mem_used), MAX(mem_used), AVG(power), AVG(temp) "
            "FROM samples WHERE ts>=? GROUP BY b ORDER BY b", (bk, bk, since)).fetchall()
        labels = [int(r[0]) for r in tot]
        idx = {b: i for i, b in enumerate(labels)}
        total = {
            "util":  [round(r[1] or 0) for r in tot],
            "mem":   [round(r[2] or 0) for r in tot],
            "mempk": [round(r[3] or 0) for r in tot],
            "power": [round(r[4] or 0) for r in tot],
            "temp":  [round(r[5] or 0) for r in tot],
        }
        services = {}
        for b, svc, mem in cur.execute(
                "SELECT (ts/?)*? b, service, AVG(mem) FROM proc WHERE ts>=? GROUP BY b, service",
                (bk, bk, since)).fetchall():
            i = idx.get(int(b))
            if i is None:
                continue
            services.setdefault(svc, [0] * len(labels))[i] = round(mem or 0)
        other = [max(0, total["mem"][i] - sum(s[i] for s in services.values())) for i in range(len(labels))]
        ticks = cur.execute("SELECT COUNT(*) FROM samples WHERE ts>=?", (since,)).fetchone()[0] or 1
        summary = []
        for svc, pk, av, cnt in cur.execute(
                "SELECT service, MAX(mem), AVG(mem), COUNT(DISTINCT ts) FROM proc WHERE ts>=? GROUP BY service",
                (since,)).fetchall():
            summary.append({"service": svc, "peak": round(pk), "avg": round(av), "present": round(100 * cnt / ticks)})
        summary.sort(key=lambda x: -x["peak"])
        mem_total = total["mem"][-1] if total["mem"] else LATEST["mem_total"] or 24576
        peak = max(total["mempk"]) if total["mempk"] else 0
    return jsonify({"range": rng, "bucket_sec": bk, "labels": labels, "total": total,
                    "services": services, "other": other, "summary": summary,
                    "mem_total": mem_total or 24576, "peak_mem": peak, "now": LATEST})


@app.route("/")
def index():
    return app.send_static_file("dashboard.html")


threading.Thread(target=collector, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
