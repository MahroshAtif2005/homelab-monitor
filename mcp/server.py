#!/usr/bin/env python3
"""HomeLab Monitor — MCP server (issue #70).

A thin, well-described Model Context Protocol wrapper over the monitor's existing
**read-only** HTTP endpoints, so Claude (or any MCP-capable client) can connect to
the monitor and explore the whole homelab: hosts, containers, systemd services,
GPU, AI model servers, alerts and host posture.

All the actual HTTP + payload-trimming lives in `homelab_client.py` (pure stdlib,
no `mcp` dependency, unit-tested on its own). This file just registers each of
those functions as an MCP tool/resource and picks a transport.

Transports (env `MCP_TRANSPORT`):
  * `stdio` (default)            — for `claude mcp add` / Claude Desktop.
  * `http` / `streamable-http`   — for the optional docker-compose sidecar; listens
                                    on `MCP_HOST`:`MCP_PORT` (default 0.0.0.0:9810).

Config:
  * `HOMELAB_MONITOR_URL`  base URL of the monitor   (default http://localhost:9800)
  * `HOMELAB_HTTP_TIMEOUT` per-request timeout, sec  (default 10)

Guardrails: **read-only**. There are no write tools. Any future write capability
(run a probe, restart a container, apply an OS update) must be opt-in, clearly
labelled destructive, and gated behind explicit config — per issue #70.
"""

import json
import os
import sys

# Make `import homelab_client` work regardless of the caller's cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import homelab_client as hc  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

INSTRUCTIONS = (
    "Read-only access to a HomeLab Monitor instance. Use `list_hosts` to see the "
    "fleet, `get_host` for one machine's full System/Network/Security inventory, "
    "`get_snapshot` for live GPU/host/Docker/systemd vitals, `get_ai_models` to see "
    "which model servers are loaded and who is driving them, and `get_events` for "
    "recent OOM kills / threshold crossings. Resources expose Prometheus `/metrics` "
    "and the CHANGELOG for version context. This server never mutates the fleet."
)

mcp = FastMCP("homelab-monitor", instructions=INSTRUCTIONS)


# ── tools (read-only) ─────────────────────────────────────────────────────────

@mcp.tool()
def list_hosts() -> dict:
    """List every host in the fleet with headline vitals (hub listed first).

    Returns the roster: name, online status, OS, CPU/RAM load, fullest disk and any
    OS-upgrade/reboot hint per host. Start here, then drill in with `get_host`.
    """
    return hc.list_hosts()


@mcp.tool()
def get_host(name: str) -> dict:
    """Full System / Network / Security inventory for a single host.

    `name` is the host's registered name, or "local" for the hub itself.
    """
    return hc.get_host(name)


@mcp.tool()
def get_snapshot() -> dict:
    """Current live vitals across the hub: GPU, host RAM/CPU, Docker and systemd
    health summaries (with any problem containers / failed units), pending OS
    updates and whether a monitor update is available. DB-free and cheap.
    """
    return hc.get_snapshot()


@mcp.tool()
def get_ai_models(range: str = "6h") -> dict:
    """AI model servers: which models are loaded, their VRAM use, and who is driving
    them (caller→server connection-seconds attribution over `range`, e.g. "6h",
    "24h", "7d"). Answers "why is the GPU pinned, and which service is calling it?".
    """
    return hc.get_ai_models(range)


@mcp.tool()
def get_events(range: str = "6h") -> dict:
    """Recent edge-triggered events over `range` (OOM kills, threshold crossings),
    each with a blame line where one can be attributed, plus derived insights.
    """
    return hc.get_events(range)


@mcp.tool()
def get_alerts(range: str = "6h") -> dict:
    """Alias for `get_events` — the monitor's alerts are its edge-triggered events."""
    return hc.get_alerts(range)


# ── resources ────────────────────────────────────────────────────────────────

@mcp.resource("homelab://metrics", mime_type="text/plain")
def metrics_resource() -> str:
    """Prometheus exposition text from the monitor's /metrics endpoint."""
    return hc.get_metrics()


@mcp.resource("homelab://health", mime_type="application/json")
def health_resource() -> str:
    """Monitor liveness + running version (from /healthz)."""
    return json.dumps(hc.get_version(), indent=2)


def _changelog_path():
    """CHANGELOG.md location: explicit env, else bundled next to this file / in /app."""
    candidates = [
        os.environ.get("HOMELAB_CHANGELOG_PATH"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md"),
        "/app/CHANGELOG.md",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


@mcp.resource("homelab://changelog", mime_type="text/markdown")
def changelog_resource() -> str:
    """The monitor's CHANGELOG, bundled into the image, for version context."""
    path = _changelog_path()
    if not path:
        return "# CHANGELOG unavailable\nNo CHANGELOG.md was bundled with this MCP server."
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ── entrypoint ───────────────────────────────────────────────────────────────

def main():
    transport = (os.environ.get("MCP_TRANSPORT") or "stdio").strip().lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "9810"))
        print("homelab-monitor MCP on http://%s:%s/mcp -> %s"
              % (mcp.settings.host, mcp.settings.port, hc.base_url()), file=sys.stderr)
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "9810"))
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
