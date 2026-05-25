# 🛰️ HomeLab Monitor — GPU, Local-AI & Host health in one container

![version](https://img.shields.io/badge/version-0.2.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![docker](https://img.shields.io/badge/deploy-docker--compose-2496ED?logo=docker&logoColor=white)
![gpu](https://img.shields.io/badge/GPU-NVIDIA-76B900?logo=nvidia&logoColor=white)

**The missing dashboard for a GPU home lab.** One small container shows you —
at a glance, from your phone over the VPN — *which model and which container is
eating your VRAM right now*, whether anything is starving, and whether the whole
box is healthy.

No Prometheus. No Grafana. No agents. **One container, one web page.**

> Built for the era of self-hosted AI: Ollama, vLLM, Stable Diffusion, ComfyUI,
> Immich ML, Whisper… all fighting over the same GPU. This tells you who's
> winning, who's losing, and what to do about it.

---

## ✨ What makes it different

Most GPU tools are either **terminal apps** (nvtop, nvitop, gpustat — no history,
no container names) or a **full Prometheus + Grafana stack** (powerful, heavy).
This sits in the gap and adds things neither does:

- 🔍 **Automatic, no-config service discovery.** Every GPU process is mapped back
  to its **Docker container by name** (`/proc/<pid>/cgroup` + Docker API). Start a
  new GPU container tomorrow — it just appears. Nothing is hardcoded.
- 🧠 **Model-level drill-down.** For recognised model servers it shows *which model*
  is loaded and its VRAM — live from the server's own API. Ollama is validated
  (real per-model VRAM via `/api/ps`); vLLM, HF TGI, llama.cpp, Automatic1111 and
  ComfyUI are detected best-effort.
- 🚦 **Contention intelligence.** It detects VRAM-pressure periods, scans GPU
  containers' logs for out-of-memory events, and **tells you who lost to whom**
  ("immich-ML lost to ollama, holding 20 GB, at 22:29").
- 💡 **Plain-language recommendations** — "VRAM peaked at 92%, only 1.9 GB free;
  ollama held 20 GB — try a shorter `OLLAMA_KEEP_ALIVE` or a smaller model."
- 🖥️ **Whole-host health.** CPU, RAM, load, uptime, temperature and disk usage —
  so one page tells you the server is fine, not just the GPU.
- 📈 **History that scales.** SQLite + **downsample-on-read**: a 6-month view is
  as fast and readable as the last hour. Retention is configurable.

## 🆚 How it compares

|                                        | **HomeLab Monitor** | nvtop / nvitop | DCGM + Grafana | gpu-hot |
|----------------------------------------|:---:|:---:|:---:|:---:|
| Web dashboard                          | ✅ | ❌ (TUI) | ✅ (needs Grafana) | ✅ |
| Per-container attribution **by name**  | ✅ | ❌ | ⚠️ k8s only | ❌ |
| Which **model** is loaded              | ✅ | ❌ | ❌ | ❌ |
| OOM / contention detection + advice    | ✅ | ❌ | ❌ | ❌ |
| Persistent, downsampled history        | ✅ | ❌ | ✅ | ⚠️ |
| Host CPU / RAM / disk too              | ✅ | ❌ | ⚠️ node-exporter | ❌ |
| Setup                                  | **1 container** | binary | full stack | 1 container |

## 🚀 Quick start

Requirements: an NVIDIA GPU, Docker, and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

```bash
git clone https://github.com/SikamikanikoBG/nvidia-gpu-monitor.git
cd nvidia-gpu-monitor
docker compose up -d --build
```

Open **http://<your-host-ip>:9800** from any machine on your LAN or VPN. Done.

## 🧠 Supported model servers

| Server | Detection | Model name | Per-model VRAM |
|---|---|---|---|
| **Ollama** | image/name | ✅ | ✅ (`/api/ps`) — *validated* |
| **vLLM** | image/name | ✅ (`/v1/models`) | — |
| **HF TGI** | image/name | ✅ (`/info`) | — |
| **llama.cpp** | image/name | ✅ (`/v1/models`) | — |
| **Automatic1111 SD** | image/name | ✅ (`/sdapi/v1/options`) | — |
| **ComfyUI** | image/name | detected | — |

Adding another server is a one-liner — append a probe to `PROBES` in `app.py`.

## ⚙️ Configuration (`docker-compose.yml` → `environment`)

| Variable | Default | Meaning |
|---|---|---|
| `SAMPLE_INTERVAL` | `10` | Seconds between samples |
| `RETENTION_DAYS` | `180` | How long history is kept |
| `PRESSURE_FREE_MB` | `2048` | Free VRAM below this counts as "pressure" |
| `PORT` | `9800` | Dashboard port |
| `WATCH_CONTAINERS` | — | Extra containers to scan for OOM (comma-separated) |

History lives in `./data/gpu.db` and survives restarts/upgrades.

## 🏗️ How it works

```
nvidia-smi ─► per-process VRAM + PID ─► /proc/<pid>/cgroup ─► Docker API ─► container name
model servers ─► their own API (/api/ps, /v1/models, …) ─► which model + VRAM
container logs ─► OOM scan ─► correlate with VRAM pressure ─► "who lost to whom"
host /proc, /sys, statvfs ─► CPU / RAM / load / temp / disk
        │
     SQLite ─► downsample-on-read ─► single-page dashboard (Chart.js, vendored)
```

A background thread samples every `SAMPLE_INTERVAL`s; the web layer buckets any
range down to ~360 points so it stays snappy over months.

## 🔒 Security notes

It runs with `pid: host`, `network_mode: host`, a **read-only** Docker socket
mount (to read container names + query model APIs) and a **read-only** mount of
`/` (for disk usage). That's the standard footprint for a host monitor — keep it
behind your LAN/VPN/firewall; **don't expose it to the public internet.**

## 🗺️ Roadmap

- Per-model VRAM history timeline
- Multi-GPU layout
- Optional alerting (Discord / Telegram / ntfy)
- AMD / Intel GPU back-ends

## 🤝 Contributing

Issues and PRs welcome — especially new model-server probes and GPU back-ends.

## License

MIT — see [LICENSE](LICENSE).
