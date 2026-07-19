"""backend/bench.py — LLM Benchmark Lab engine.

Actively benchmarks ollama models (unlike the rest of the monitor, which is
passive/read-only). Everything network-touching is *injected* as a callable so
the orchestration is fully unit-testable without a GPU or a live ollama:

    run_model_benchmark(model, cfg, generate_fn, ps_fn, smi_fn, ...)

Pure helpers (parse_generate_timing, resident_for, classify_fit, parse_smi_gpus,
attribute_gpu, summarize, plan_ctx_list, build_prompt) carry the real logic and
are covered by tests/test_bench.py.

Measured per (model × context-size × gpu-layers):
  • generation tokens/sec, prompt-eval tokens/sec, time-to-first-token, load time
  • VRAM resident vs RAM offload (ollama /api/ps size vs size_vram)
  • fit verdict (fully in VRAM / partial spill / CPU) and the offloaded MB
  • which physical GPU the weights landed on (nvidia-smi mem.used delta)
  • power / energy / cost for the run window (priced by the cost engine)
"""
import time

# Context sizes we sweep by default (tokens). Filtered to <= the model's native
# context, with the native size appended so the ceiling is always probed.
DEFAULT_CTX_LADDER = (2048, 4096, 8192, 16384, 32768, 65536)
DEFAULT_GEN_TOKENS = 128
DEFAULT_PROMPT_TOKENS = 512
MAX_MODELS_PER_JOB = 12
MAX_CTX_PER_MODEL = 10

# Fit thresholds on the VRAM-resident fraction (size_vram / size).
_FIT_FULL = 0.999
_FIT_CPU = 0.02


# ── pure helpers ──────────────────────────────────────────────────────────────
def build_prompt(approx_tokens):
    """A deterministic filler prompt of roughly `approx_tokens` tokens (~0.75
    words/token heuristic). Used to give prompt-eval speed something to chew on."""
    n = max(8, int(approx_tokens))
    words = max(1, int(n * 0.75))
    base = ("Summarize the following homelab telemetry note in one sentence. "
            "The GPU served several models while power and memory were sampled. ").split()
    out = []
    i = 0
    while len(out) < words:
        out.append(base[i % len(base)])
        i += 1
    return " ".join(out)


def plan_ctx_list(requested, native_ctx):
    """Resolve the context ladder for one model. `requested` may be None (use the
    default ladder) or an explicit list. Always clamps to the model's native
    context (when known) and guarantees the native size is probed. Returns a
    sorted, de-duped list capped at MAX_CTX_PER_MODEL."""
    native = None
    try:
        native = int(native_ctx) if native_ctx else None
    except (TypeError, ValueError):
        native = None
    explicit = bool(requested)
    ladder = list(requested) if explicit else list(DEFAULT_CTX_LADDER)
    vals = set()
    for v in ladder:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv < 256:
            continue
        if native and iv > native:
            continue
        vals.add(iv)
    # For the default sweep, always probe the model's native ceiling too — the
    # whole point is to find how far context can grow before it spills. An
    # explicit user list is respected as-is.
    if native and not explicit:
        vals.add(native)
    if not vals:
        vals.add(native or 4096)
    return sorted(vals)[:MAX_CTX_PER_MODEL]


def parse_generate_timing(resp):
    """Pure: turn an ollama /api/generate (stream=false) response into a timing
    dict. ollama reports nanoseconds. Missing fields degrade to None, never raise."""
    d = resp or {}

    def ns_ms(k):
        v = d.get(k)
        return round(v / 1e6, 2) if isinstance(v, (int, float)) and v else None

    def tps(count_k, dur_k):
        c = d.get(count_k)
        dur = d.get(dur_k)
        if isinstance(c, (int, float)) and isinstance(dur, (int, float)) and dur > 0:
            return round(c / (dur / 1e9), 2)
        return None

    load_ms = ns_ms("load_duration")
    prompt_ms = ns_ms("prompt_eval_duration")
    # Time to first token ≈ load + prompt-eval (the user waits for both before
    # the first generated token appears).
    ttft_ms = None
    if load_ms is not None or prompt_ms is not None:
        ttft_ms = round((load_ms or 0) + (prompt_ms or 0), 2)
    return {
        "gen_tps": tps("eval_count", "eval_duration"),
        "prompt_tps": tps("prompt_eval_count", "prompt_eval_duration"),
        "load_ms": load_ms,
        "ttft_ms": ttft_ms,
        "total_ms": ns_ms("total_duration"),
        "eval_count": d.get("eval_count"),
        "prompt_eval_count": d.get("prompt_eval_count"),
    }


def classify_fit(size, size_vram):
    """'vram' (fully resident), 'partial' (some spilled to RAM), or 'cpu'."""
    if not size or size <= 0:
        return None
    frac = (size_vram or 0) / size
    if frac >= _FIT_FULL:
        return "vram"
    if frac <= _FIT_CPU:
        return "cpu"
    return "partial"


def resident_for(ps_resp, model):
    """Pure: locate `model` in an ollama /api/ps payload and derive the VRAM/RAM
    split. Returns a dict (or None if the model isn't resident)."""
    for m in (ps_resp or {}).get("models", []) if isinstance(ps_resp, dict) else []:
        name = m.get("name") or m.get("model")
        if name != model:
            continue
        size = m.get("size") or 0
        vram = m.get("size_vram") or 0
        offload = max(0, size - vram)
        return {
            "total_size_mb": round(size / 1048576) if size else None,
            "vram_mb": round(vram / 1048576) if vram else 0,
            "ram_offload_mb": round(offload / 1048576) if size else None,
            "gpu_fraction": round(vram / size, 4) if size > 0 else None,
            "fit": classify_fit(size, vram),
        }
    return None


def parse_smi_gpus(text):
    """Pure: parse `nvidia-smi --query-gpu=index,name,memory.used,memory.total,
    power.draw --format=csv,noheader,nounits` output into a list of dicts.
    Tolerates '[N/A]' fields. Never raises."""
    out = []
    for ln in (text or "").splitlines():
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 3 or not parts[0]:
            continue

        def num(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        try:
            idx = int(parts[0])
        except (TypeError, ValueError):
            continue
        out.append({
            "idx": idx,
            "name": parts[1] if len(parts) > 1 else None,
            "mem_used": num(parts[2]) if len(parts) > 2 else None,
            "mem_total": num(parts[3]) if len(parts) > 3 else None,
            "power_w": num(parts[4]) if len(parts) > 4 else None,
        })
    return out


def attribute_gpu(before, after, min_delta_mb=64):
    """Pure: given per-GPU memory.used snapshots (parse_smi_gpus lists) from before
    and after a model load, return the cards whose VRAM grew — i.e. where the
    weights actually landed. Each entry: {idx, name, delta_mb}. Sorted biggest
    first. This is how we answer "which GPU did it use?" on a multi-card box."""
    by_idx = {g["idx"]: g for g in (before or [])}
    landed = []
    for g in (after or []):
        b = by_idx.get(g["idx"])
        if not b:
            continue
        delta = (g.get("mem_used") or 0) - (b.get("mem_used") or 0)
        if delta >= min_delta_mb:
            landed.append({"idx": g["idx"], "name": g.get("name"),
                           "delta_mb": round(delta)})
    landed.sort(key=lambda x: -x["delta_mb"])
    return landed


def gpu_advice(landed, gpus):
    """Pure: one-line recommendation about GPU placement, or None. On a multi-GPU
    box, warn when weights spread onto a small/slow secondary card."""
    if not landed or not gpus or len(gpus) < 2:
        return None
    if len(landed) < 2:
        return None
    # Smallest card by total VRAM is the likely weak link.
    smallest = min(gpus, key=lambda g: (g.get("mem_total") or 0))
    if any(l["idx"] == smallest["idx"] for l in landed):
        nm = smallest.get("name") or f"GPU {smallest['idx']}"
        return (f"Weights spread onto {nm} — pin ollama to your larger card "
                f"(CUDA_VISIBLE_DEVICES) for steadier throughput.")
    return None


def summarize(points, native_ctx=None):
    """Pure: fold the per-config points into a headline summary for a model.
    Picks the fastest usable config, the largest context that still fits fully in
    VRAM (the recommended cap), and the overall fit verdict."""
    ok = [p for p in points if p.get("ok") and p.get("gen_tps")]
    summary = {
        "best_gen_tps": None, "best_prompt_tps": None, "best_ctx": None,
        "min_load_ms": None, "max_fit_ctx": None, "recommended_ctx": None,
        "fit": None, "vram_mb": None, "ram_offload_mb": None,
        "total_size_mb": None, "points": len(points), "ok_points": len(ok),
    }
    if not ok:
        return summary
    best = max(ok, key=lambda p: p["gen_tps"])
    summary["best_gen_tps"] = best["gen_tps"]
    summary["best_ctx"] = best.get("ctx")
    summary["best_prompt_tps"] = max((p.get("prompt_tps") or 0) for p in ok) or None
    loads = [p["load_ms"] for p in ok if p.get("load_ms")]
    summary["min_load_ms"] = round(min(loads), 1) if loads else None
    summary["total_size_mb"] = next((p.get("total_size_mb") for p in points
                                     if p.get("total_size_mb")), None)
    fitting = [p for p in points if p.get("fit") == "vram" and p.get("ctx")]
    if fitting:
        top = max(fitting, key=lambda p: p["ctx"])
        summary["max_fit_ctx"] = top["ctx"]
        summary["recommended_ctx"] = top["ctx"]
        summary["vram_mb"] = top.get("vram_mb")
        summary["ram_offload_mb"] = top.get("ram_offload_mb")
        summary["fit"] = "vram"
    else:
        # Nothing fits fully — report the least-offloaded config we saw.
        spill = [p for p in points if p.get("ram_offload_mb") is not None]
        if spill:
            least = min(spill, key=lambda p: p["ram_offload_mb"])
            summary["recommended_ctx"] = least.get("ctx")
            summary["vram_mb"] = least.get("vram_mb")
            summary["ram_offload_mb"] = least.get("ram_offload_mb")
            summary["fit"] = least.get("fit") or "partial"
    return summary


# ── orchestration (I/O injected) ─────────────────────────────────────────────
def run_model_benchmark(model, cfg, generate_fn, ps_fn, smi_fn,
                        on_point=None, should_cancel=None, sleep_fn=None,
                        meta=None):
    """Benchmark one model across its context ladder. All external effects are
    injected:
      generate_fn(model, prompt, num_ctx, num_predict, num_gpu, keep_alive) -> resp dict
      ps_fn() -> ollama /api/ps dict
      smi_fn() -> raw nvidia-smi csv text (or "" when no GPU)
      on_point(point_dict)            optional per-point sink (persist/progress)
      should_cancel() -> bool         optional cooperative cancel
      sleep_fn(seconds)               optional (defaults to time.sleep)

    Returns (points, summary). Each point is a fully-formed row dict. A failed
    config yields a point with ok=False and an err string rather than aborting
    the whole model — one bad context size never sinks the run."""
    sleep = sleep_fn or time.sleep
    meta = meta or {}
    native = meta.get("ctx")
    ctx_list = plan_ctx_list(cfg.get("ctx_list"), native)
    gen_tokens = int(cfg.get("gen_tokens") or DEFAULT_GEN_TOKENS)
    prompt = build_prompt(cfg.get("prompt_tokens") or DEFAULT_PROMPT_TOKENS)
    num_gpu = cfg.get("num_gpu")  # None -> ollama auto; int -> forced layer count
    points = []

    for ctx in ctx_list:
        if should_cancel and should_cancel():
            break
        point = {"ctx": ctx, "num_gpu": num_gpu, "ok": False, "err": None}
        try:
            before = parse_smi_gpus(smi_fn() if smi_fn else "")
            # Warm-up load at this context (also forces a reload when num_ctx
            # changed). This call pays the real cold-load cost, so we keep its
            # load_duration; its throughput (num_predict=8) is discarded.
            warm = parse_generate_timing(generate_fn(model, "warm up", ctx, 8, num_gpu, "5m"))
            if should_cancel and should_cancel():
                break
            # Measured run — model already resident, so gen/prompt tok/s and a
            # steady-state TTFT come from here (its own load_ms is ~0).
            resp = generate_fn(model, prompt, ctx, gen_tokens, num_gpu, "5m")
            timing = parse_generate_timing(resp)
            after = parse_smi_gpus(smi_fn() if smi_fn else "")
            resident = resident_for(ps_fn() if ps_fn else {}, model) or {}
            landed = attribute_gpu(before, after)
            point.update(timing)
            # Prefer the cold-load time from the warm-up call.
            if warm.get("load_ms"):
                point["load_ms"] = warm["load_ms"]
            point.update({
                "vram_mb": resident.get("vram_mb"),
                "ram_offload_mb": resident.get("ram_offload_mb"),
                "total_size_mb": resident.get("total_size_mb"),
                "gpu_fraction": resident.get("gpu_fraction"),
                "fit": resident.get("fit"),
                "gpus": landed,
                "ok": bool(timing.get("gen_tps")),
            })
            if not point["ok"] and point["err"] is None:
                point["err"] = "no timing returned"
        except Exception as e:  # one context failing must not sink the model
            point["err"] = str(e)[:200]
        points.append(point)
        if on_point:
            on_point(point)
        sleep(0.2)

    summary = summarize(points, native)
    return points, summary
