# Contributing

The bar is intentionally low — a typo fix is just as welcome as a new GPU
back-end. The full guide lives in
[**CONTRIBUTING.md**](https://github.com/SikamikanikoBG/homelab-monitor/blob/main/CONTRIBUTING.md)
in the repo; the highlights are below.

## Run it locally

```bash
git clone https://github.com/SikamikanikoBG/homelab-monitor.git
cd homelab-monitor
docker compose up -d --build
```

Open `http://localhost:9800`. Rebuild after a code change with the same
command. History at `./data/gpu.db` survives.

## The "add a monitor" pattern

The codebase is small and predictable. Adding a new subsystem is a short,
modular change:

1. **Backend collector** in `app.py`. Add a `collect_<thing>()` that returns:
   ```python
   {"available": bool, "summary": {...}, "items": [...]}
   ```
   Return `{"available": False, ...}` rather than raising when the subsystem
   isn't present — the dashboard should degrade gracefully, not error.
2. **Wire it into the scan.** Call `collect_<thing>()` from `health_scan()`
   and include it in the `/api/health` response.
3. **Frontend tab** in `static/dashboard.html`:
   - one entry in the `TABS` array,
   - one `<section data-tab="...">` for the body,
   - a small renderer that reads from `/api/health`.

No build step, no framework — vanilla JS and the vendored Chart.js.

## Extending the multi-host probe

Since 0.8, the hub can collect from other boxes too. To add a new reader for
remotes:

1. Add a function to **`probe.py`** that returns a small dict. Keep it pure
   stdlib (Python 3.6+) and short-timeout — a stuck command can't be allowed
   to block the poll cycle:
   ```python
   def read_smart():
       try:
           r = subprocess.run(["smartctl", "-A", "/dev/sda", "-j"],
                              capture_output=True, timeout=2)
           ...
       except Exception:
           return {}
   ```
2. Add the key to the `data["host"]` block in `main()`.
3. Surface it: add a column to the All-hosts table (`/api/fleet`) or render
   it inside a per-host tab.

Keep `probe.py` self-contained — the hub pipes it over SSH on every cycle,
so external deps would defeat the "agentless" promise.

## Style

- **Plug-and-play** — anything new should work with `docker compose up -d`
  and no extra config. Features that need config should have safe defaults.
- **Humble &amp; welcoming tone** in UI copy, README and commits. This is for
  hobbyists; don't make anyone feel small for not knowing something.
- **Match the existing code style** — short functions, plain names, comments
  only where the *why* isn't obvious from the code.

## Submitting a PR

- Open an **issue first** for anything bigger than a small fix, so the shape
  can be agreed before you sink time into it.
- Keep PRs **focused** — one feature or fix per PR is easier to review.
- Leave the **version bump** to the maintainer; it happens on release, not
  per-PR.

Thanks. Every issue, suggestion and PR makes it better. 🛰️
