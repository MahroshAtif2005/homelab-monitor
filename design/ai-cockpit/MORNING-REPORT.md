# Good morning ☀️ — 0.16 "AI Lab Cockpit" is built

You went out and asked for a revamp of cost monitoring plus an AI/Data-Science
rethink of the whole dashboard, worked out with specialist agents, shipped one
by one, on `next` only. Here's what landed while you slept.

## TL;DR
**6 features, 6 merged PRs, 94 unit tests, 0 changes to `main`.** Everything is on
`next` and deployable via the `:next` Docker image (Watchtower-redeployed on
`ardi:9801`). No new dependencies — still pure Python + Flask.

## How the night went (research → debate → build)
1. **Researched** with three specialist agents in parallel: an AI/DS feature
   catalog, a per-tab gap audit from a data-scientist's POV, and a dual-tariff
   cost design with a real 38-country dataset. Artifacts: `research-A/B/C` +
   `tariffs.json` in this folder.
2. **Synthesised** into `PLAN.md` — the "AI Lab Cockpit" thesis and a 6-PR roadmap.
3. **Built** each PR off `origin/next`: implement → local unit tests → PR → CI
   `smoke` → merge → `:next` publish → Watchtower redeploy → verify.

## What shipped

| PR | Feature | The headline bit |
|----|---------|------------------|
| #113 | **Cost: day & night tariffs** | Pick your country → prefilled day/night rates (38-country sourced dataset). Single-average still the default. |
| #114 | **GPU truth-telling** | Red banner when the GPU is **throttling** (power-capped/thermal); memory-bandwidth %, clocks, power headroom, p-state. ✅ *verified on your real RTX.* |
| #115 | **Model intelligence** | Ollama param-size/quant/context badges + **live vLLM/TGI tokens/sec, queue, KV-cache, TTFT**. |
| #116 | **AI workload band** | Overview hero: models loaded, VRAM, **tokens-per-Joule**, today's GPU cost, throttle. |
| #117 | **Experiments tab** | Auto-detected **training runs** + **stall alert**; **GPU activity sessions** with energy & cost. |
| #118 | **Notebooks & tools** | Auto-discovered Jupyter/TensorBoard/MLflow/W&B/Streamlit/Ray tiles + **idle-VRAM squatter** flag. |

## The release headline (for the post)
> **Your GPU says 100% util. This free dashboard tells you the truth — throttled,
> memory-bandwidth-bound, or a Jupyter kernel squatting on 9 GB of VRAM. Plus live
> tokens/sec, cost-per-run, and it pings you when a 2 a.m. training run stalls.**

Full launch copy (Reddit / HN / Towards Data Science / X) in `RELEASE-DRAFT.md`.

## How to see it
- Preview: **http://ardi:9801** — open the new **Experiments** tab, the enriched
  **GPU** and **AI Models** tabs, and the **Overview** AI band. Settings → Alerts
  has the new day/night tariff + country picker.
- The throttle banner only appears when the card actually throttles; the serving
  strip appears when a vLLM/TGI server is up; training cards appear when a job runs.

## Verification notes (honest status)
- **#114 GPU telemetry is verified on your real hardware** — `/api/health` returned
  the correct RTX fields (350 W limit, P8 idle, mem-BW %, clocks).
- The other PRs passed CI `smoke` + full local unit tests. The GPU-/Docker-dependent
  UIs can't be exercised on the Windows dev box, so they're best-effort + isolated
  (any nvidia-smi / endpoint failure degrades that panel and never wedges the
  sampler). When you read this, Watchtower should have pulled the final `:next`;
  if `ardi:9801` shows an older build, it's just the 5-minute poll catching up.

## Suggested next steps (your call — I didn't touch these)
1. **Merge `next` → `main` and cut v0.16** when you've eyeballed the preview. The
   CHANGELOG `Unreleased` section and `RELEASE-DRAFT.md` are ready.
2. **README hero update** for the storefront — draft in `RELEASE-DRAFT.md` (§README).
3. **Star-growth post** using the headline + a screenshot of the throttle banner or
   the AI band.
4. A couple of stretch ideas I scoped but didn't build (kept the night focused):
   per-PID GPU *utilisation* (needs `nvidia-smi pmon`), an opt-in Ollama tok/s
   benchmark, and a persistent run-history journal. Notes in `research-A`.

Nothing is broken on `main`. Hope this puts a smile on your face. 🛰️
