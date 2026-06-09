# MCP server

HomeLab Monitor ships an optional **[Model Context Protocol](https://modelcontextprotocol.io)
server** so an AI agent — Claude, or any MCP-capable client — can connect to the
monitor and *explore the whole homelab ecosystem* through it: hosts, containers,
systemd services, GPU, AI model servers, alerts and host posture.

It's a thin, well-described wrapper over the monitor's existing **read-only** HTTP
endpoints. No collectors are touched, and **nothing is mutated**.

!!! info "Read-only by design"
    There are no write tools. Any future write capability (run a probe, restart a
    container, apply an OS update) must be opt-in, clearly labelled destructive, and
    gated behind explicit config — same philosophy as the rest of the project.

## Tools

| Tool | What it answers | Wraps |
|------|-----------------|-------|
| `list_hosts()` | What's in the fleet, and is it healthy? | `/api/fleet` |
| `get_host(name)` | One host's System / Network / Security inventory (`"local"` = the hub) | `/api/host_data/<name>` |
| `get_snapshot()` | Live GPU / host / Docker / systemd vitals right now | `/api/health` |
| `get_ai_models(range)` | Which models are loaded, their VRAM, and *who is driving them* | `/api/data` |
| `get_events(range)` / `get_alerts(range)` | Recent OOM kills / threshold crossings + insights | `/api/data` |

`range` accepts the same windows as the dashboard, e.g. `6h`, `24h`, `7d`.

## Resources

| Resource | Content |
|----------|---------|
| `homelab://metrics` | Prometheus exposition text (`/metrics`) |
| `homelab://health` | Liveness + running version (`/healthz`) |
| `homelab://changelog` | The bundled CHANGELOG, for version context |

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `HOMELAB_MONITOR_URL` | `http://localhost:9800` | Base URL of the monitor to read |
| `HOMELAB_HTTP_TIMEOUT` | `10` | Per-request timeout (seconds) |
| `MCP_TRANSPORT` | `stdio` | `stdio`, or `http` (streamable-http) for the sidecar |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `9810` | Bind address for the `http` transport |

## Run it

=== "Claude Code (Docker, stdio)"

    ```bash
    docker build -f mcp/Dockerfile -t homelab-monitor-mcp .
    claude mcp add homelab -- \
      docker run -i --rm -e HOMELAB_MONITOR_URL=http://YOUR-HUB:9800 homelab-monitor-mcp
    ```

=== "Local Python (stdio)"

    ```bash
    pip install -r mcp/requirements.txt   # Python 3.10+
    HOMELAB_MONITOR_URL=http://YOUR-HUB:9800 python mcp/server.py
    ```

=== "docker-compose sidecar (HTTP)"

    The root `docker-compose.yml` ships a `homelab-monitor-mcp` service behind a
    profile so it stays opt-in:

    ```bash
    docker compose --profile mcp up -d homelab-monitor-mcp
    claude mcp add --transport http homelab http://YOUR-HUB:9810/mcp
    ```

## Try it

Once connected, ask your agent natural questions and let it pick the tools:

- *"Which host has a reboot pending **and** an OS upgrade available — what's the safe order to apply it?"*
- *"Why is the GPU pinned right now, and which service is calling the model server?"*
- *"Any OOM kills in the last 24h? What got blamed?"*

!!! warning "Keep it on your LAN/VPN"
    Like the dashboard, the MCP server gives broad visibility into your hosts.
    Don't expose either to the public internet.
