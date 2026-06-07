# Prometheus & Grafana

HomeLab Monitor exposes a standard Prometheus scrape endpoint at `/metrics` (port 9800
by default). It reads exclusively from the in-memory snapshot that the background
collector keeps fresh — **no extra polling, no double-sampling**.

## Metrics exposed

| Metric | Labels | Description |
|---|---|---|
| `homelab_gpu_vram_used_mb` | `gpu` | GPU VRAM currently used (MB) |
| `homelab_gpu_vram_total_mb` | `gpu` | GPU VRAM total capacity (MB) |
| `homelab_gpu_util_pct` | `gpu` | GPU utilisation (%) |
| `homelab_gpu_temp_c` | `gpu` | GPU temperature (°C) |
| `homelab_gpu_power_w` | `gpu` | GPU power draw (W) |
| `homelab_host_cpu_pct` | — | Host CPU usage (%) |
| `homelab_host_mem_used_pct` | — | Host memory used (%) |
| `homelab_host_disk_used_pct` | `mountpoint` | Disk used per mount (%) |
| `homelab_container_state` | `name`, `state` | 1 = container is in this state |
| `homelab_systemd_unit_state` | `unit`, `state` | 1 = unit is active, 0 = otherwise |
| `homelab_model_loaded_vram_mb` | `server`, `model` | VRAM used by a loaded model (MB) |

## Quick verification

```bash
curl http://<your-host-ip>:9800/metrics
```

## Sample scrape config

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'homelab_monitor'
    scrape_interval: 15s
    static_configs:
      - targets: ['<your-host-ip>:9800']
```

## Grafana dashboard

A ready-to-import dashboard is at
[`docs/grafana/homelab_prometheus_dashboard.json`](https://github.com/SikamikanikoBG/homelab-monitor/blob/main/docs/grafana/homelab_prometheus_dashboard.json).
In Grafana: **Dashboards → Import → Upload JSON file**, then select your Prometheus
datasource. The dashboard covers GPU VRAM, utilisation, temperature, host CPU/RAM, disk
usage, and model VRAM in a single view.

!!! note
    Prometheus is entirely optional — the dashboard is fully self-contained without it.
    The endpoint is there for folks who already run a Prometheus/Grafana stack.
