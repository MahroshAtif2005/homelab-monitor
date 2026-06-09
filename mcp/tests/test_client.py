"""Unit tests for homelab_client.py against a stub monitor.

Pure stdlib so it runs on the same Python 3.8+ as the client module (the `mcp`
SDK / server.py are not exercised here — that layer is a thin pass-through and
needs 3.10+). Run directly:

    python mcp/tests/test_client.py

Exits non-zero on the first failure.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# import the module under test (sibling-of-parent dir)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import homelab_client as hc  # noqa: E402


# ── canned payloads shaped like the real endpoints ───────────────────────────

def _host(cpu, ram_used, ram_total, disks, name="testbox"):
    return {
        "cpu": cpu, "cores": 8, "ram_used": ram_used, "ram_total": ram_total,
        "ram_kernel": 512, "load1": 1.2, "uptime": 86400, "ctemp": 47,
        "disks": disks,
        "os": {"kernel": "6.8.0", "arch": "x86_64", "pretty": "openSUSE Leap 16.1", "id": "opensuse-leap"},
        "net": {"hostname": name}, "sec": {"firewall": "firewalld"},
    }


FLEET = {
    "interval": 10,
    "hosts": [
        {"name": "local", "label": "ardi (this hub)", "ssh_target": None, "is_local": True,
         "online": True, "at": 1000, "last_check": {"summary": {"overall": "ok"}},
         "host": _host(12, 64000, 128000,
                       [{"mount": "/", "pct": 41}, {"mount": "/backup", "pct": 88}])},
        {"name": "cloudy", "label": "cloudy", "ssh_target": "anakin@cloudy", "is_local": False,
         "online": False, "at": 500, "last_check": {"summary": {"overall": "warn"}},
         "error": "ssh timeout", "host": None},
    ],
}

HOST_LOCAL = {"name": "local", "online": True, "at": 1000,
              "host": _host(9, 30000, 64000, [{"mount": "/", "pct": 55}])}
HOST_GHOST = {"name": "ghost", "online": False, "error": "no data yet", "at": None, "host": None}

HEALTH = {
    "version": "0.13.1", "updated": 1700,
    "now": {"gpu": {"util": 73, "mem_used": 9000, "mem_total": 24576, "available": True},
            "host": _host(15, 40000, 128000, [{"mount": "/", "pct": 60}])},
    "docker": {"available": True, "reason": None,
               "summary": {"total": 12, "running": 11, "problems": 1},
               "containers": [
                   {"name": "immich", "state": "running", "health": "healthy"},
                   {"name": "n8n", "state": "exited", "health": None},
                   {"name": "searxng", "state": "running", "health": "unhealthy"},
               ]},
    "systemd": {"available": True, "reason": None, "summary": {"failed": 1},
                "services": [
                    {"name": "sshd", "active": "active", "status": "ok"},
                    {"name": "borgbackup", "active": "failed", "status": "bad"},
                    # completed oneshot — inactive but NOT a failure; must be ignored
                    {"name": "nvidia-cdi-refresh", "active": "inactive", "status": "info"},
                    # flagged bad by the monitor even though ActiveState != failed
                    {"name": "weird", "active": "active", "status": "bad"},
                ]},
    "update": {"available": True, "current": "0.13.1"},
    "os_updates": {"count": 3}, "diagnostics": {}, "overview": {"status": "warn"},
}

DATA = {
    "version": "0.13.1", "range": "6h",
    "now": {"models": [{"service": "ollama", "model": "llama3:70b", "vram": 8200, "state": "loaded"}]},
    "model_summary": [{"service": "ollama", "model": "llama3:70b", "peak": 8200, "avg": 6100}],
    "callers": [{"caller": "open-webui", "server": "ollama", "seconds": 3600, "samples": 360}],
    "events": [{"ts": 1650, "service": "immich_ml", "kind": "oom", "detail": "killed",
                "blame": "immich_ml lost to ollama"}],
    "insights": ["GPU VRAM peaked at 8.2 GB driven by open-webui"],
}

METRICS = "# HELP gpu_util GPU utilization\ngpu_util{gpu=\"gpu0\"} 73\n"
HEALTHZ = {"status": "ok", "version": "0.13.1"}

ROUTES = {
    "/api/fleet": FLEET,
    "/api/host_data/local": HOST_LOCAL,
    "/api/host_data/ghost": HOST_GHOST,
    "/api/health": HEALTH,
    "/api/data": DATA,
    "/healthz": HEALTHZ,
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            body = METRICS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ROUTES:
            body = json.dumps(ROUTES[path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'{"error":"not found"}')


# ── tiny test harness ────────────────────────────────────────────────────────

_FAILS = []


def check(cond, msg):
    if cond:
        print("  ok  -", msg)
    else:
        print("  FAIL-", msg)
        _FAILS.append(msg)


def run():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    os.environ["HOMELAB_MONITOR_URL"] = "http://127.0.0.1:%d" % srv.server_address[1]
    os.environ["HOMELAB_HTTP_TIMEOUT"] = "5"
    try:
        print("list_hosts")
        r = hc.list_hosts()
        check(r["count"] == 2, "counts both hosts")
        check(r["sample_interval_sec"] == 10, "passes through interval")
        loc = r["hosts"][0]
        check(loc["name"] == "local" and loc["is_local"] is True, "local first, flagged")
        check(loc["overall"] == "ok", "pulls overall from last_check")
        check(loc["vitals"]["ram_pct"] == 50.0, "computes ram_pct (64000/128000)")
        check(loc["vitals"]["fullest_disk"]["mount"] == "/backup", "picks fullest disk (88%)")
        check(loc["vitals"]["os"] == "openSUSE Leap 16.1", "os pretty name")
        offl = r["hosts"][1]
        check(offl["online"] is False and offl["error"] == "ssh timeout", "offline host carries error")
        check(offl["vitals"] is None, "no vitals when host snapshot missing")

        print("get_host (online)")
        r = hc.get_host("local")
        check(r["online"] is True and r["host"]["os"]["arch"] == "x86_64", "returns full host inventory")

        print("get_host (no data yet)")
        r = hc.get_host("ghost")
        check(r["online"] is False and r["error"] == "no data yet", "waiting state surfaced")

        print("get_snapshot")
        r = hc.get_snapshot()
        check(r["version"] == "0.13.1", "version")
        check(r["gpu"]["util"] == 73, "gpu vitals")
        check(r["host"]["ram_pct"] is not None, "host summarized")
        names = {c["name"] for c in r["docker"]["problem_containers"]}
        check(names == {"n8n", "searxng"}, "problem containers = exited + unhealthy only")
        failed = {s["name"] for s in r["systemd"]["failed_services"]}
        check(failed == {"borgbackup", "weird"}, "failed = active==failed or status==bad")
        check("nvidia-cdi-refresh" not in failed, "completed oneshot (inactive) not flagged")
        check(r["update_available"] is True, "update flag")

        print("get_ai_models")
        r = hc.get_ai_models("24h")
        check(r["loaded"][0]["model"] == "llama3:70b", "loaded models")
        check(r["callers"][0]["caller"] == "open-webui", "caller attribution")
        check(r["vram_summary"][0]["peak"] == 8200, "vram summary")

        print("get_events / get_alerts")
        r = hc.get_events()
        check(r["events"][0]["kind"] == "oom", "events surfaced")
        check("blame" in r["events"][0], "blame preserved")
        check(len(r["insights"]) == 1, "insights surfaced")
        check(hc.get_alerts()["events"] == r["events"], "get_alerts aliases get_events")

        print("resources")
        check("gpu_util" in hc.get_metrics(), "metrics text")
        check(hc.get_version()["status"] == "ok", "healthz")

        print("error handling")
        os.environ["HOMELAB_MONITOR_URL"] = "http://127.0.0.1:1"  # refused
        try:
            hc.list_hosts()
            check(False, "unreachable monitor raises MonitorError")
        except hc.MonitorError as e:
            check("cannot reach monitor" in str(e), "unreachable monitor raises MonitorError")
    finally:
        srv.shutdown()

    print()
    if _FAILS:
        print("%d FAILURE(S)" % len(_FAILS))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    run()
