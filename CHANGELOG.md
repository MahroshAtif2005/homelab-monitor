# Changelog

All notable changes to **HomeLab Monitor** are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/), and the
project follows semantic-ish versioning. Each entry links to its full GitHub
release notes.

## [0.12.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.12.0) — 2026-06-02
- **Container disk that's actually right** — the Containers tab now counts each
  container's true footprint (writable layer **plus** its volumes & bind mounts;
  mounts shared between containers are excluded), so heavy containers like Ollama
  and Immich show their real GB instead of a near-empty writable layer.
- **AI Models** now recognises **WhisperX / whisper-asr-webservice** and a dozen
  more servers (SGLang, Triton, Wyoming voice, OpenLLM, LiteLLM, GPUStack,
  Cortex/Jan, …).
- Time-range picker now shows on **every** tab.
- Sidebar brand no longer truncates.

## [0.11.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.11.0) — 2026-05-31
- **AI Models: detect-all + caller attribution** — every recognised model server
  is listed (and stays listed as **Idle** when its model unloads), with a
  **"Driven by"** breakdown showing which services are calling each server.

## [0.10.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.10.0) — 2026-05-31
- **System / Network / Security tabs** — host inventory (OS, kernel, arch, init
  system, hardware), per-host network interfaces & listening sockets, and a
  read-only security posture check (firewall, SSH hardening, SELinux/AppArmor,
  fail2ban, reboot-pending, auto-updates).

## [0.9.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.9.1) — 2026-05-31
- Accurate remote CPU temperatures.

## [0.9.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.9.0) — 2026-05-30
- **Container & Service vital signs** — per-container and per-systemd-unit health,
  uptime, memory and ports.

## [0.8.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.8.0) — 2026-05-30
- **Multi-machine monitoring** — register other boxes over SSH from the Hosts tab
  and see every host's vitals side-by-side. No agents, just SSH + Python 3.

## [0.7.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.7.0) — 2026-05-29
- **Self-healthcheck** — `/healthz` liveness endpoint + Docker `HEALTHCHECK`.

## [0.6.3](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.3) — 2026-05-29
- Rendered release notes in the in-app update modal.

## [0.6.2](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.2) — 2026-05-29
- Faster recovery from negative update-check results.

## [0.6.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.1) — 2026-05-29
- Branded GitHub star button + contribution nudge.

## [0.6.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.6.0) — 2026-05-29
- In-UI update notification + favicon.

## [0.5.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.5.0) — 2026-05-28
- **Alerting (Discord + ntfy.sh)** — edge-triggered push notifications, configured
  entirely from the Alerts tab.

## [0.4.1](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.4.1) — 2026-05-26
- Accurate Prometheus label series.

## [0.4.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.4.0) — 2026-05-26
- **Prometheus `/metrics` endpoint** — standard scrape endpoint reading from the
  in-memory snapshot (no extra polling).

## [0.2.0](https://github.com/SikamikanikoBG/homelab-monitor/releases/tag/v0.2.0) — 2026-05-25
- Local-AI model drill-down, contention intelligence & host health.

---

_Older tags exist in the repository history. For the authoritative, complete
notes of any release see the
[GitHub releases page](https://github.com/SikamikanikoBG/homelab-monitor/releases)._
