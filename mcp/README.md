# HomeLab Monitor — MCP server

A small [Model Context Protocol](https://modelcontextprotocol.io) server that lets
Claude (or any MCP client) **connect to a running HomeLab Monitor and explore the
whole homelab** — hosts, containers, systemd services, GPU, AI model servers,
alerts and host posture.

It's a thin, well-described wrapper over the monitor's existing **read-only** HTTP
endpoints. No collectors are touched and **nothing is mutated** — see the guardrails
below.

## Tools

| Tool | What it answers | Wraps |
|------|-----------------|-------|
| `list_hosts()` | What's in the fleet and is it healthy? | `GET /api/fleet` |
| `get_host(name)` | One host's System / Network / Security inventory (`"local"` = the hub) | `GET /api/host_data/<name>` |
| `get_snapshot()` | Live GPU / host / Docker / systemd vitals right now | `GET /api/health` |
| `get_ai_models(range="6h")` | Which models are loaded, VRAM, and *who is driving them* | `GET /api/data` |
| `get_events(range="6h")` / `get_alerts(...)` | Recent OOM kills / threshold crossings + insights | `GET /api/data` |

## Resources

| Resource | Content |
|----------|---------|
| `homelab://metrics` | Prometheus exposition text (`GET /metrics`) |
| `homelab://health` | Liveness + running version (`GET /healthz`) |
| `homelab://changelog` | The bundled CHANGELOG, for version context |

## Configure

| Env | Default | Meaning |
|-----|---------|---------|
| `HOMELAB_MONITOR_URL` | `http://localhost:9800` | Base URL of the monitor to read |
| `HOMELAB_HTTP_TIMEOUT` | `10` | Per-request timeout (seconds) |
| `MCP_TRANSPORT` | `stdio` | `stdio`, or `http` (streamable-http) for the sidecar |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `9810` | Bind address for the `http` transport |

## Run it

### A. Add to Claude Code via Docker (stdio)

```bash
docker build -f mcp/Dockerfile -t homelab-monitor-mcp .
claude mcp add homelab -- \
  docker run -i --rm -e HOMELAB_MONITOR_URL=http://YOUR-HUB:9800 homelab-monitor-mcp
```

### B. Local Python (stdio)

```bash
pip install -r mcp/requirements.txt   # Python 3.10+
HOMELAB_MONITOR_URL=http://YOUR-HUB:9800 python mcp/server.py
```

### C. As an optional docker-compose sidecar (HTTP)

The root `docker-compose.yml` ships a `homelab-monitor-mcp` service behind a
profile so it stays opt-in:

```bash
docker compose --profile mcp up -d homelab-monitor-mcp
# then point a client at the streamable-http endpoint:
claude mcp add --transport http homelab http://YOUR-HUB:9810/mcp
```

## Guardrails

This server is **read-only**. There are no write tools. Any future write capability
(run a probe, restart a container, apply an OS update) must be opt-in, clearly
labelled destructive, and gated behind explicit config — same philosophy as the
rest of the project (issue #70).

## Tests

`homelab_client.py` is pure stdlib and unit-tested against a stub monitor, so the
endpoint-wrapping/trimming logic runs on any Python 3.8+:

```bash
python mcp/tests/test_client.py
```
