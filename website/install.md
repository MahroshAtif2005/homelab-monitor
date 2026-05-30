# Install

## Requirements

- **Docker** + **docker compose** (any modern version).
- For the GPU panels: an **NVIDIA GPU** and the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
- **No GPU?** That's fine — the container, service and host panels still work.
  Skip the toolkit; the dashboard auto-hides the GPU sections.

## Option A — pre-built image (recommended)

No clone, no build:

```bash
curl -fsSLO https://raw.githubusercontent.com/SikamikanikoBG/homelab-monitor/main/docker-compose.yml
docker compose pull
docker compose up -d
```

Multi-arch images (`linux/amd64`, `linux/arm64`) are published on every release
to [`sikamikaniko123/homelab-monitor`](https://hub.docker.com/r/sikamikaniko123/homelab-monitor).

Open **`http://<your-host-ip>:9800`** from any device on your LAN or VPN.

??? tip "Upgrade later"
    ```bash
    docker compose pull
    docker compose up -d
    ```
    Your SQLite history at `./data/gpu.db` survives — it's a bind mount.

## Option B — from source

Handy if you're tweaking the code or contributing:

```bash
git clone https://github.com/SikamikanikoBG/homelab-monitor.git
cd homelab-monitor
docker compose up -d --build
```

Same URL: **`http://<your-host-ip>:9800`**.

## Verify

```bash
curl -s http://localhost:9800/healthz
# {"status":"ok","version":"0.8.0"}
```

## Uninstall

```bash
docker compose down
# To also drop the SQLite history:
rm -rf ./data
```

## Next steps

- Add your other boxes to the cockpit → [**Multi-machine guide**](multi-host.md)
- Tune sample interval, retention, alert thresholds → [**Configuration**](configuration.md)
