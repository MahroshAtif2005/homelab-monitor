"""Pure-stdlib client + response shaping for the HomeLab Monitor MCP server.

This module deliberately has **no dependency on the `mcp` SDK** (or any third-party
package). It only knows how to:

  1. call the monitor's existing read-only HTTP endpoints, and
  2. trim/relabel their payloads into compact, LLM-friendly shapes.

Keeping it SDK-free means the substance — the endpoint wrapping and trimming — can
be imported and unit-tested on any Python 3.8+ (the `mcp` SDK needs 3.10+ and only
runs inside the shipped 3.12 image). `server.py` is the thin layer that turns each
function here into an MCP tool/resource.

Everything is **read-only**. There is intentionally no function that mutates the
fleet — see issue #70's guardrails.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://localhost:9800"


def base_url():
    """Monitor base URL, e.g. http://ardi:9800. Trailing slash trimmed."""
    return (os.environ.get("HOMELAB_MONITOR_URL") or DEFAULT_URL).rstrip("/")


def _timeout():
    try:
        return float(os.environ.get("HOMELAB_HTTP_TIMEOUT", "10"))
    except ValueError:
        return 10.0


class MonitorError(RuntimeError):
    """Raised when the monitor can't be reached or returns a non-2xx / bad body.

    Carries a short, human-readable message so the agent gets an actionable error
    (wrong URL, monitor down, unknown host) instead of a raw stack trace.
    """


def _get(path):
    """GET a JSON endpoint and return the decoded object."""
    url = base_url() + path
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "homelab-monitor-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise MonitorError("monitor returned HTTP %s for %s" % (e.code, path))
    except urllib.error.URLError as e:
        raise MonitorError("cannot reach monitor at %s (%s)" % (base_url(), e.reason))
    except OSError as e:
        raise MonitorError("cannot reach monitor at %s (%s)" % (base_url(), e))
    try:
        return json.loads(raw)
    except ValueError:
        raise MonitorError("monitor returned a non-JSON body for %s" % path)


def _get_text(path):
    """GET a text endpoint (e.g. /metrics) and return the raw string."""
    url = base_url() + path
    req = urllib.request.Request(url, headers={"User-Agent": "homelab-monitor-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise MonitorError("monitor returned HTTP %s for %s" % (e.code, path))
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise MonitorError("cannot reach monitor at %s (%s)" % (base_url(), reason))


# ── shaping helpers ──────────────────────────────────────────────────────────

def _host_summary(host):
    """One host's *headline* vitals — the compact form used in the fleet roster."""
    if not host:
        return None
    os_info = host.get("os") or {}
    ram_total = host.get("ram_total") or 0
    ram_used = host.get("ram_used") or 0
    os_name = os_info.get("pretty") or os_info.get("name") or os_info.get("id")
    out = {
        "os": os_name.strip() if isinstance(os_name, str) else os_name,
        "kernel": os_info.get("kernel"),
        "arch": os_info.get("arch"),
        "cpu_pct": host.get("cpu"),
        "cores": host.get("cores"),
        "load1": host.get("load1"),
        "ram_used_mb": ram_used,
        "ram_total_mb": ram_total,
        "ram_pct": round(100 * ram_used / ram_total, 1) if ram_total else None,
        "cpu_temp_c": host.get("ctemp"),
        "uptime_sec": host.get("uptime"),
    }
    # Fullest disk only, so the roster stays one line per host.
    disks = host.get("disks") or []
    if disks:
        fullest = max(disks, key=lambda d: d.get("pct") or 0)
        out["fullest_disk"] = {"mount": fullest.get("mount"), "pct": fullest.get("pct")}
    # Surface an OS-upgrade hint when enrich_os_upgrade() attached one.
    if host.get("os_upgrade") or os_info.get("upgrade"):
        out["os_upgrade"] = host.get("os_upgrade") or os_info.get("upgrade")
    if host.get("reboot_required") or host.get("reboot_pending"):
        out["reboot_required"] = True
    return {k: v for k, v in out.items() if v is not None}


# ── tool implementations (each returns plain JSON-able data) ─────────────────

def list_hosts():
    """Fleet roster + headline vitals for every registered host (local hub first)."""
    data = _get("/api/fleet")
    hosts = []
    for row in data.get("hosts", []):
        hosts.append({
            "name": row.get("name"),
            "label": row.get("label"),
            "online": row.get("online"),
            "is_local": row.get("is_local"),
            "ssh_target": row.get("ssh_target"),
            "last_seen_ts": row.get("at"),
            "overall": ((row.get("last_check") or {}).get("summary") or {}).get("overall"),
            "vitals": _host_summary(row.get("host")),
            "error": row.get("error"),
        })
    return {"count": len(hosts), "sample_interval_sec": data.get("interval"), "hosts": hosts}


def get_host(name):
    """Full System / Network / Security inventory for one host.

    `name` is the registered host name, or "local" for the hub itself.
    """
    data = _get("/api/host_data/" + urllib.parse.quote(str(name)))
    host = data.get("host")
    if host is None and not data.get("online", True):
        # Host known but no successful poll yet — return the waiting state verbatim.
        return {"name": data.get("name", name), "online": False,
                "error": data.get("error") or "no data yet", "at": data.get("at")}
    return {
        "name": data.get("name", name),
        "online": data.get("online"),
        "at": data.get("at"),
        "stale_for_sec": data.get("stale_for") or 0,
        "error": data.get("error"),
        "host": host,
    }


def get_snapshot():
    """Live, DB-free vitals: GPU, host, Docker + systemd health, and the overview.

    Mirrors the dashboard's at-a-glance state. Container/service lists are trimmed
    to *problems* plus headline counts so the payload stays small; ask `get_host`
    for full per-host detail.
    """
    h = _get("/api/health")
    docker = h.get("docker") or {}
    systemd = h.get("systemd") or {}
    containers = docker.get("containers") or []
    services = systemd.get("services") or []
    # Genuine problems only: a non-running container, or one Docker reports
    # explicitly unhealthy. (A "starting"/"" health is transient, not a problem.)
    problem_containers = [c for c in containers
                          if c.get("state") not in (None, "running")
                          or c.get("health") == "unhealthy"]
    # systemd "failed" is the real failure state. A completed oneshot is
    # "inactive", not failed — don't surface those as problems (matches the
    # monitor's own `summary.failed` count). `status == "bad"` catches anything
    # the monitor itself flags as bad even if ActiveState differs.
    failed_services = [s for s in services
                       if s.get("active") == "failed" or s.get("status") == "bad"]
    update = h.get("update") or {}
    return {
        "version": h.get("version"),
        "updated_ts": h.get("updated"),
        "overview": h.get("overview"),
        "gpu": (h.get("now") or {}).get("gpu"),
        "host": _host_summary((h.get("now") or {}).get("host")),
        "docker": {
            "available": docker.get("available"),
            "reason": docker.get("reason"),
            "summary": docker.get("summary"),
            "problem_containers": problem_containers,
        },
        "systemd": {
            "available": systemd.get("available"),
            "reason": systemd.get("reason"),
            "summary": systemd.get("summary"),
            "failed_services": failed_services,
        },
        "os_updates": h.get("os_updates"),
        "update_available": bool(update.get("available")),
        "current_version": update.get("current") or h.get("version"),
    }


def get_ai_models(range="6h"):
    """Which model servers are loaded, their VRAM, and *who is driving them*.

    `loaded` is the current snapshot of model servers; `vram_summary` is peak/avg
    VRAM per (server, model) over `range`; `callers` is connection-seconds per
    caller→server edge over `range` — the "driven by" attribution.
    """
    data = _get("/api/data?range=" + urllib.parse.quote(str(range)))
    now = data.get("now") or {}
    return {
        "range": data.get("range", range),
        "loaded": now.get("models") or [],
        "vram_summary": data.get("model_summary") or [],
        "callers": data.get("callers") or [],
    }


def get_events(range="6h"):
    """Recent edge-triggered events (OOM kills, threshold crossings) + insights.

    Each event may carry a `blame` line attributing a memory loss to the service
    that grew at the same time. `insights` are the human-readable takeaways the
    dashboard derives from the same window.
    """
    data = _get("/api/data?range=" + urllib.parse.quote(str(range)))
    return {
        "range": data.get("range", range),
        "events": data.get("events") or [],
        "insights": data.get("insights") or [],
    }


# Alias kept because the issue lists both names; alerts == edge-triggered events.
def get_alerts(range="6h"):
    """Alias for get_events — the monitor's alerts *are* its edge-triggered events."""
    return get_events(range)


def get_metrics():
    """Raw Prometheus exposition text from the monitor's /metrics endpoint."""
    return _get_text("/metrics")


def get_version():
    """Liveness + running version from /healthz (cheap, never blocks)."""
    return _get("/healthz")
