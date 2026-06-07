---
hide:
  - navigation
  - toc
---

<div class="hl-hero" markdown>

# <span class="hl-title">HomeLab <span class="em">Monitor</span></span>

<p class="hl-tag">
One small container for your home lab — GPU, AI VRAM, Docker, systemd and host
health, all on one page. <strong>Multi-machine since 0.8</strong> — register
your other boxes over SSH and see every host's vitals side-by-side in one
cockpit.
</p>

<div class="hl-cta">
  <a class="md-button md-button--primary" href="install/">Get started</a>
  <a class="md-button" href="https://github.com/SikamikanikoBG/homelab-monitor" target="_blank" rel="noopener">GitHub →</a>
  <a class="md-button" href="https://hub.docker.com/r/sikamikaniko123/homelab-monitor" target="_blank" rel="noopener">Docker Hub →</a>
</div>

<div class="hl-video">
  <iframe src="https://www.youtube-nocookie.com/embed/5uf2rG-RzcU?rel=0"
          title="HomeLab Monitor — dashboard demo"
          loading="lazy"
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
          allowfullscreen></iframe>
</div>

<p class="hl-badges">
  <span><span class="hl-dot"></span>One container · <code>docker compose up -d</code></span>
  <span>No Prometheus / Grafana / cloud</span>
  <span>NVIDIA GPU friendly</span>
  <span>Pull <code>sikamikaniko123/homelab-monitor</code></span>
</p>

</div>

<div class="hl-section-title hl-reveal">What it does</div>

<div class="hl-features" markdown>

<div class="hl-feature hl-reveal delay-1" markdown>
### <span class="ico">🪄</span> Plug-and-play
One Docker container. No agents, no Prometheus/Grafana stack, no cloud account.
Sane defaults; everything else is in the Settings tab.
</div>

<div class="hl-feature hl-reveal delay-2" markdown>
### <span class="ico">🎮</span> GPU attribution
Live VRAM, utilisation, power, temperature — plus *which container or process*
is holding the card, mapped automatically via `/proc/<pid>/cgroup` + the Docker API.
</div>

<div class="hl-feature hl-reveal delay-3" markdown>
### <span class="ico">🧠</span> AI model awareness
Detects the major local-AI servers (Ollama, vLLM, TGI, llama.cpp, A1111,
ComfyUI) and reports *which model is loaded* with per-model VRAM.
</div>

<div class="hl-feature hl-reveal delay-4" markdown>
### <span class="ico">📦</span> Containers &amp; services
Health of every Docker container and every systemd service in one glance.
Your own units highlighted, failed ones surfaced first.
</div>

<div class="hl-feature hl-reveal delay-5" markdown>
### <span class="ico">🌐</span> Multi-machine, agentless
Register other boxes over SSH and they appear in the fleet table. Just `python3`
on the remote — nothing to install. [Walkthrough →](multi-host.md)
</div>

<div class="hl-feature hl-reveal delay-6" markdown>
### <span class="ico">🛡️</span> System, Network &amp; Security
Per host: OS, kernel, **architecture**, machine model and CPU/GPU; interfaces,
DNS and listening sockets with exposure flags; and a read-only security posture
check (firewall, SSH hardening, SELinux/AppArmor, fail2ban) — issues first.
</div>

<div class="hl-feature hl-reveal delay-6" markdown>
### <span class="ico">🔔</span> Alerts
Discord webhook or [ntfy.sh](https://ntfy.sh). Edge-triggered: one ping per
state change, never a spam flood. Configured from the UI.
</div>

</div>

<div class="hl-section-title hl-reveal">A look around</div>

<div class="hl-shots" markdown>

<a class="hl-reveal delay-1" href="screenshots/overview.png" target="_blank">
  <img src="screenshots/overview.png" alt="Overview / All-hosts table">
  <span class="lbl">Overview — every host at a glance</span>
</a>

<a class="hl-reveal delay-2" href="screenshots/hosts.png" target="_blank">
  <img src="screenshots/hosts.png" alt="Hosts tab with onboarding wizard">
  <span class="lbl">Hosts — onboarding wizard + capability checklist</span>
</a>

<a class="hl-reveal delay-3" href="screenshots/gpu.png" target="_blank">
  <img src="screenshots/gpu.png" alt="GPU tab">
  <span class="lbl">GPU — VRAM attribution by service</span>
</a>

<a class="hl-reveal delay-4" href="screenshots/services.png" target="_blank">
  <img src="screenshots/services.png" alt="Services tab">
  <span class="lbl">Services — systemd, yours highlighted</span>
</a>

<a class="hl-reveal delay-5" href="screenshots/containers.png" target="_blank">
  <img src="screenshots/containers.png" alt="Containers tab">
  <span class="lbl">Containers — every Docker container</span>
</a>

<a class="hl-reveal delay-6" href="screenshots/system.png" target="_blank">
  <img src="screenshots/system.png" alt="System tab">
  <span class="lbl">System — OS, architecture &amp; hardware inventory</span>
</a>

<a class="hl-reveal delay-1" href="screenshots/network.png" target="_blank">
  <img src="screenshots/network.png" alt="Network tab">
  <span class="lbl">Network — interfaces, DNS &amp; listening sockets</span>
</a>

<a class="hl-reveal delay-2" href="screenshots/security.png" target="_blank">
  <img src="screenshots/security.png" alt="Security tab">
  <span class="lbl">Security — posture check, issues surfaced first</span>
</a>

</div>

<div class="hl-section-title hl-reveal">60-second install</div>

<div class="hl-install hl-reveal" markdown>

### Pre-built image, no clone

```bash
curl -fsSLO https://raw.githubusercontent.com/SikamikanikoBG/homelab-monitor/main/docker-compose.yml
docker compose pull
docker compose up -d
```

Then open **`http://<your-host-ip>:9800`** from any browser on your LAN or VPN.

Full install options (NVIDIA Container Toolkit, from-source, upgrade) →
[**Install**](install.md){ .md-button }

</div>
