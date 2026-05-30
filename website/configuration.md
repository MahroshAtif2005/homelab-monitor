# Configuration

Almost nothing needs to be configured to get started. Two layers exist for
when you do want to tune things:

1. **Environment variables** for sample cadence, retention, paths.
2. **The Settings tab in the UI** for alerts (saved into SQLite, no env vars
   or config files).

## Environment variables

Set these under `environment:` in `docker-compose.yml`. All optional.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `9800` | Dashboard listens on `0.0.0.0:$PORT`. With host networking, this is the LAN port too. |
| `SAMPLE_INTERVAL` | `10` | Seconds between collector cycles (also the multi-host probe cadence). |
| `RETENTION_DAYS` | `180` | How long SQLite history is kept. Downsampled on read, so longer ranges stay cheap. |
| `PRESSURE_FREE_MB` | `2048` | Free VRAM below this counts as "pressure" for the insights / alerts. |
| `HOST_ROOT` | `/rootfs` | Where host `/` is bind-mounted into the container (for disk usage). |
| `DOCKER_SOCK` | `/var/run/docker.sock` | Path to the Docker socket inside the container. |
| `DB_PATH` | `/data/gpu.db` | SQLite history file. Default lives under the `./data` bind mount. |
| `WATCH_CONTAINERS` | *(empty)* | Comma-separated container names to always scan for OOM events, even if not GPU-attributed. |
| `WATCH_SERVICES` | *(empty)* | Comma-separated systemd units to always surface in the Services tab. |
| `CHECK_UPDATES` | `true` | Whether to poll GitHub releases for "update available" banner. |
| `SSH_DIR` | `/data/.ssh` | Where the multi-host SSH keypair lives. Persists across rebuilds. |

## Alerts (configured in the UI)

Open the **Alerts** tab and fill in either or both:

- **Discord webhook URL** (works with any Discord channel webhook)
- **ntfy.sh topic** (use the public server or self-hosted)

Then set:

- **Minimum severity** — `warning` or `critical only`
- **Disk alert threshold** — fires when any real filesystem crosses this %

Alerts are **edge-triggered**: one ping per state change, not a flood. Each
alert key is remembered until the underlying condition recovers, then the
next failure re-fires exactly once.

## Triggers

Built-in triggers (no config needed beyond enabling alerts):

- Container goes unhealthy / exits non-zero / is dead
- systemd unit fails
- GPU VRAM pressure (free below `PRESSURE_FREE_MB`)
- GPU OOM events scraped from container logs
- Disk usage crossing the threshold above

Add your own by extending `dispatch_alert` in `app.py`.

## Compose excerpt

A trimmed `docker-compose.yml` for the curious — see the real one in
the [repo](https://github.com/SikamikanikoBG/homelab-monitor/blob/main/docker-compose.yml).

```yaml
services:
  homelab-monitor:
    image: sikamikaniko123/homelab-monitor:latest
    container_name: homelab-monitor
    restart: unless-stopped
    network_mode: host          # for direct LAN access + model-server APIs
    pid: host                   # to map GPU PIDs → containers
    environment:
      PORT: "9800"
      SAMPLE_INTERVAL: "10"
      RETENTION_DAYS: "180"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /:/rootfs:ro
      - ./data:/data
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```
