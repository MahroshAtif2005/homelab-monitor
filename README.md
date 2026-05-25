# GPU Monitor

A tiny, self-hosted **NVIDIA GPU monitoring tool** that runs as a single Docker
container. It samples your GPU and shows — at a glance — **which container or
process is using your VRAM right now** and how that usage has trended over time.

No agents, no Prometheus/Grafana stack, no cloud. One container, one web page.

## Why

`nvidia-smi` gives you a snapshot, but it can't tell you *which Docker service*
is holding the VRAM, and it keeps no history. This tool does both:

- **Automatic service discovery** — every process on the GPU is mapped back to
  its container by reading `/proc/<pid>/cgroup` and asking the Docker API for the
  friendly name. **Nothing is hardcoded.** Start a new GPU container tomorrow and
  it just appears, with its own colour.
- **History that scales** — samples are stored in SQLite and **downsampled on
  read**, so the charts stay readable and fast whether you look at the last hour
  or the last six months.
- **Glanceable** — a live "who's using the GPU now" bar, current VRAM / util /
  power / temp, plus a stacked VRAM-by-service timeline against the card's
  capacity so you can instantly see your free headroom.

## Quick start

Requirements: an NVIDIA GPU, Docker, and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

```bash
git clone https://github.com/<you>/nvidia-gpu-monitor.git
cd nvidia-gpu-monitor
docker compose up -d --build
```

Then open **http://<host-ip>:9800** from any machine on your LAN or VPN.

## Configuration

Set these in `docker-compose.yml` under `environment:`

| Variable           | Default | Meaning                                   |
|--------------------|---------|-------------------------------------------|
| `SAMPLE_INTERVAL`  | `10`    | Seconds between samples                   |
| `RETENTION_DAYS`   | `180`   | How long history is kept before pruning   |
| `PORT`             | `8099`  | In-container port (change the host mapping in `ports:` to move it) |

History is stored in `./data/gpu.db` (a bind mount), so it survives restarts and
upgrades.

## How it works

```
nvidia-smi ──► per-process VRAM + PID
                     │
   /proc/<pid>/cgroup ──► container id ──► Docker API ──► service name
                     │
                  SQLite (samples + per-service)
                     │
         Flask API  ──► downsample-on-read ──► dashboard (Chart.js)
```

A background thread samples every `SAMPLE_INTERVAL` seconds. The web layer
buckets whatever range you ask for down to ~360 points, so a query over six
months is just as snappy as one over an hour.

## Security notes

- The container mounts the **Docker socket read-only**, used only to translate
  container IDs into names. If you don't want that, names will fall back to the
  short container ID / process name.
- It uses **`pid: host`** so it can resolve GPU PIDs to containers via `/proc`.
- It binds to `0.0.0.0` so it's reachable on your network — put it behind your
  VPN/firewall and don't expose it to the public internet.

## License

MIT — see [LICENSE](LICENSE).
