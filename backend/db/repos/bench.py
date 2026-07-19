"""backend/db/repos/bench.py — CRUD for LLM benchmark runs + points.

Two tables (created in app.py's _DB_SCHEMA):
  bench_runs   — one row per (model, execution): status, config, derived summary,
                 gpu inventory, energy/cost, timestamps.
  bench_points — one row per (run, context-size/gpu-layers) measurement.
Thin SQL only (Phase 4.1 convention) — all logic lives in backend/bench.py.
"""
from backend.db import connection


def insert_run(id, host, endpoint, model, family, param_size, quant, size_bytes,
               status, config, gpu, created_at, started_at, conn=None):
    """Insert a new benchmark run (queued/running)."""
    c = conn or connection()
    c.execute(
        "INSERT INTO bench_runs(id,host,endpoint,model,family,param_size,quant,"
        "size_bytes,status,config,gpu,created_at,started_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (id, host, endpoint, model, family, param_size, quant, size_bytes,
         status, config, gpu, created_at, started_at))
    c.commit()


def update_run(fields_sql, args, conn=None):
    """Dynamic UPDATE bench_runs SET <fields_sql> WHERE id=?. Returns rowcount."""
    c = conn or connection()
    cur = c.execute(f"UPDATE bench_runs SET {fields_sql} WHERE id=?", args)
    c.commit()
    return cur.rowcount


def finish_run(id, status, summary, ended_at, energy_kwh, cost, avg_w, error, conn=None):
    """Mark a run done/error and stamp its derived summary + energy/cost."""
    c = conn or connection()
    cur = c.execute(
        "UPDATE bench_runs SET status=?, summary=?, ended_at=?, energy_kwh=?, "
        "cost=?, avg_w=?, error=? WHERE id=?",
        (status, summary, ended_at, energy_kwh, cost, avg_w, error, id))
    c.commit()
    return cur.rowcount


def set_status(id, status, conn=None):
    c = conn or connection()
    cur = c.execute("UPDATE bench_runs SET status=? WHERE id=?", (status, id))
    c.commit()
    return cur.rowcount


def exists_run(id, conn=None):
    c = conn or connection()
    return bool(c.execute("SELECT 1 FROM bench_runs WHERE id=?", (id,)).fetchone())


def insert_point(run_id, ctx, num_gpu, gen_tps, prompt_tps, load_ms, ttft_ms,
                 total_ms, eval_count, prompt_eval_count, vram_mb, ram_offload_mb,
                 total_size_mb, gpu_fraction, fit, gpus_json, ok, err, conn=None):
    """Insert one measured point."""
    c = conn or connection()
    c.execute(
        "INSERT INTO bench_points(run_id,ctx,num_gpu,gen_tps,prompt_tps,load_ms,"
        "ttft_ms,total_ms,eval_count,prompt_eval_count,vram_mb,ram_offload_mb,"
        "total_size_mb,gpu_fraction,fit,gpus,ok,err) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, ctx, num_gpu, gen_tps, prompt_tps, load_ms, ttft_ms, total_ms,
         eval_count, prompt_eval_count, vram_mb, ram_offload_mb, total_size_mb,
         gpu_fraction, fit, gpus_json, 1 if ok else 0, err))
    c.commit()


def list_runs(since, now, model=None, status=None, limit=500, conn=None):
    """Runs created within [since, now], newest first, optional model/status."""
    c = conn or connection()
    q = ("SELECT id,host,endpoint,model,family,param_size,quant,size_bytes,status,"
         "config,summary,gpu,error,created_at,started_at,ended_at,energy_kwh,cost,avg_w "
         "FROM bench_runs WHERE created_at>=? AND created_at<=? ")
    args = [since, now]
    if model:
        q += "AND model=? "
        args.append(model)
    if status:
        q += "AND status=? "
        args.append(status)
    q += f"ORDER BY created_at DESC LIMIT {int(limit)}"
    return c.execute(q, args).fetchall()


def get_run(id, conn=None):
    c = conn or connection()
    return c.execute(
        "SELECT id,host,endpoint,model,family,param_size,quant,size_bytes,status,"
        "config,summary,gpu,error,created_at,started_at,ended_at,energy_kwh,cost,avg_w "
        "FROM bench_runs WHERE id=?", (id,)).fetchone()


def get_points(run_id, conn=None):
    c = conn or connection()
    return c.execute(
        "SELECT ctx,num_gpu,gen_tps,prompt_tps,load_ms,ttft_ms,total_ms,eval_count,"
        "prompt_eval_count,vram_mb,ram_offload_mb,total_size_mb,gpu_fraction,fit,"
        "gpus,ok,err FROM bench_points WHERE run_id=? ORDER BY ctx", (run_id,)).fetchall()


def delete_run_with_points(id, conn=None):
    """Remove a run and its points. Returns rowcount of the run delete."""
    c = conn or connection()
    cur = c.execute("DELETE FROM bench_runs WHERE id=?", (id,))
    c.execute("DELETE FROM bench_points WHERE run_id=?", (id,))
    c.commit()
    return cur.rowcount


def history_for_model(model, limit=20, conn=None):
    """Chronological (id, created_at, summary) for one model — powers the
    per-model trend sparkline across reruns."""
    c = conn or connection()
    return c.execute(
        "SELECT id,created_at,summary FROM bench_runs WHERE model=? AND status='done' "
        "ORDER BY created_at DESC LIMIT ?", (model, int(limit))).fetchall()
