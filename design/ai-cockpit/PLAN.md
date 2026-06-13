# HomeLab Monitor 0.16 — "The AI Lab Cockpit" release plan

> Strategic goal: turn HomeLab Monitor from a great **sysadmin** dashboard into **the** at-a-glance
> cockpit for people who *train models and serve LLMs* at home. Grow ~106★ → ~1000★ in a week with a
> release that hits r/LocalLLaMA, r/homelab, Hacker News and Towards Data Science.
>
> Hard rules: **everything lands on `next`, never `main`.** Pure Python stdlib + Flask (no new pip
> deps). Chart.js frontend. Every change ships through CI → `:next` → Watchtower on `ardi:9801`,
> with unit tests. Backward-compatible, degrades gracefully on consumer GPUs / GPU-less hosts.

## The headline (chosen)

**“Your GPU says 100% util. This free dashboard tells you the truth — throttled, memory-bandwidth-bound,
or a Jupyter kernel squatting on 9 GB of VRAM. Plus live tokens/sec, cost-per-run, and it pings you when
a 2 a.m. training run stalls.”**

Sub-line: *Pure Python, no agents, no cloud. Reads nvidia-smi, /proc and your model servers directly.*

## Why this wins (research synthesis)

Three specialist agents researched the feature space, audited every dashboard tab from a DS/AI-engineer
POV, and designed the cost upgrade. Full artifacts in this folder:
- `research-A-feature-catalog.md` — ranked feature catalog + data sources + wow list.
- `research-B-tab-gaps.md` — per-tab gap analysis + proposed new tabs.
- `research-C-cost-tariffs.md` + `tariffs.json` — dual-tariff design + 38-country dataset.

The single highest-leverage insight: the app already collects rich AI signals (per-process VRAM,
model servers, power) but **throws most of the detail away**. We surface it, enrich it, and add the
3 things that make people screenshot it: **throttle truth, live serving telemetry, and stall alerts.**

## Build order (each = one PR → `next`, tested, deployed, verified on 9801)

| # | PR | Why it matters | Source |
|---|----|----|----|
| A | **Cost: day/night tariffs + country prefill** | Explicit ask. Honest dual-tariff billing; pick country → prefilled rates. | research-C |
| B | **GPU telemetry enrichment** | Foundation. mem-bandwidth util, clocks, **throttle reasons**, power-limit headroom, mem temp, energy. Unlocks the truth-telling headline. | A#2, B-GPU |
| C | **Model intelligence** | Ollama `/api/show` (param size, quant, ctx len) + **vLLM/TGI `/metrics`** live tokens/sec, queue depth, KV-cache. | A#1/#5, B-Models |
| D | **AI cockpit Overview band + tokens-per-Joule** | At-a-glance: models loaded, VRAM committed/free, GPU efficiency, today's GPU cost. Screenshot bait. | A#4, B-Overview |
| E | **Experiments / Training tab** | Auto-detect training runs (torchrun/accelerate/deepspeed), GPU-activity sessions, energy/cost per run, **stall detection**. The feature that makes it an ML cockpit. | A#3, B-newtabs |
| F | **Notebooks & Endpoints tab** | Jupyter kernels + idle-VRAM-squatter, experiment-tracker discovery (TensorBoard/MLflow/W&B). | A#6/#7, B-newtabs |

Must-haves if the night runs short: **A (requested), B, C.** D–F are high-value extensions.

## Conventions
- Branch from `origin/next`, PR into `next`, squash/merge after `smoke` passes.
- Unit tests under `tests/` (mocked nvidia-smi / HTTP / /proc), run locally with flask installed.
- GPU-dependent behavior verified live on `ardi:9801` after Watchtower redeploys (~210–315 s).
- Issues stay open until shipped to `main` (maintainer convention).

## Status log
- [ ] A  Cost dual-tariff
- [ ] B  GPU telemetry
- [ ] C  Model intelligence
- [ ] D  Overview AI band + efficiency
- [ ] E  Experiments/Training tab
- [ ] F  Notebooks/Endpoints tab
