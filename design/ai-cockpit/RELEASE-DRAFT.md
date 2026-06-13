# 0.16 "The AI Lab Cockpit" — launch copy

Ready-to-post copy for the release. Tune voice to taste before posting.

## Headline (pick one)
1. **Your GPU says 100% util. This free dashboard tells you the truth — throttled, memory-bandwidth-bound, or a Jupyter kernel squatting on 9 GB of VRAM.** Plus live tokens/sec, cost-per-run, and it pings you when a 2 a.m. training run stalls.
2. **Tokens per Joule:** an open-source dashboard that measures how efficiently your home GPU serves LLMs — and what your idle models cost you in electricity.
3. **I built a free cockpit for my AI home lab** — live tokens/sec, GPU throttling truth, training-run stall alerts, and day/night electricity cost. Pure Python, no agents, no cloud.

## GitHub release notes (v0.16.0)

> ### 🧠 The AI Lab Cockpit
> HomeLab Monitor is now the at-a-glance dashboard for people who **train models and serve LLMs** at home — without adding a single dependency. It already knew your GPU and containers; now it understands your *AI work*.
>
> **🔥 GPU truth-telling.** A red banner when your card is **throttling** (power-capped or thermal), plus memory-bandwidth utilisation (mem-bound vs compute-bound), clocks, power-vs-limit headroom and performance state — all from the `nvidia-smi` it already calls. "100% util" stops lying.
>
> **⚡ Live serving telemetry.** vLLM/TGI servers show real **tokens/sec**, requests running/queued, **KV-cache fill** and TTFT, scraped from their Prometheus `/metrics`. Ollama models show **parameter size, quantization, context length** and capability badges.
>
> **🔬 Experiments tab.** Your training and fine-tuning runs (torchrun, accelerate, deepspeed, SFT/LoRA scripts, trl/axolotl/unsloth) are **auto-detected** with elapsed time and VRAM held — and a **"possible stall"** alert when a run holds VRAM but GPU util collapses. Plus **GPU activity sessions** with energy and cost per run.
>
> **🧰 Notebooks & tools.** Auto-discovers Jupyter, TensorBoard, MLflow, W&B, Streamlit and Ray and links straight to them — and flags the **idle notebook kernel squatting on your VRAM**.
>
> **🧠 AI workload band.** The Overview leads with models loaded, VRAM committed, GPU util, **tokens-per-Joule efficiency**, and today's GPU cost.
>
> **💰 Day & night electricity tariffs.** Bill your GPU energy at split day/night rates with a configurable night window — or **pick your country** to prefill a typical, sourced estimate (38-country dataset). Don't know your rates? The flat average still works.
>
> Still pure Python + Flask. No agents, no Prometheus/Grafana, no cloud. `docker compose up -d` and open the page.

## Reddit (r/LocalLLaMA, r/homelab, r/selfhosted)
**Title:** Your GPU says 100% util — but is it throttling, memory-bound, or is a notebook squatting on your VRAM? I added "GPU truth-telling" + live tokens/sec to my open-source homelab dashboard.

**Body:** I run models at home and got tired of `watch nvidia-smi` + tailing vLLM logs in three terminals. So my self-hosted dashboard (no agents, no Prometheus, pure Python) now:
- decodes nvidia-smi **throttle reasons** → red banner when you're power-capped/thermal,
- shows **memory-bandwidth util** so you can see when you're memory-bound (LLM decode usually is),
- scrapes vLLM/TGI `/metrics` for **live tokens/sec, KV-cache, queue depth, TTFT**,
- **auto-detects training runs** and warns when one **stalls** (VRAM held, util at 0 — the dead-dataloader-at-2am scenario),
- reconstructs **GPU activity sessions** with energy + cost, and shows **tokens-per-Joule**,
- finds your **Jupyter/TensorBoard/MLflow/W&B** and flags an **idle kernel hogging VRAM**.
`docker compose up -d`. Feedback and stars welcome — repo in comments.

## Hacker News (Show HN)
**Show HN: Open-source AI homelab dashboard — GPU throttle truth, live tokens/sec, stall alerts (pure Python)**

## X / Bluesky
🛰️ New in HomeLab Monitor: it tells you the **truth** about your GPU.
🔥 throttling? 🧠 memory-bound? 💤 a notebook squatting on 9GB VRAM?
+ live tok/s, tokens-per-Joule, training-run **stall alerts**, and day/night power cost.
Pure Python, self-hosted, no agents. `docker compose up -d`.

## README hero (§README — drop-in replacement for the intro tagline)
> **One page for your whole home lab & AI rig — GPU truth (throttling, mem-bandwidth, tokens/sec), training-run stall alerts, model VRAM & cost. No agents, no Prometheus/Grafana, no cloud.**
>
> Your GPU is "always busy" — but is it *working*, or throttled, memory-bound, or holding VRAM for a dead notebook? HomeLab Monitor answers the questions an AI home lab actually has: which model holds the GPU, how many tokens/sec you're serving, what a training run cost in kWh, and whether that 2 a.m. fine-tune stalled — across every box over SSH.
