# Release v0.28.0 — journal

## Before-state (2026-07-31, captured before any change)

| | |
|---|---|
| `origin/next` | `159c123` docs(changelog): credit @andreahaku for the two AMD GPU features |
| `origin/main` | `7fc6e49` Merge pull request #259 from SikamikanikoBG/next |
| `next` ahead of `main` | 22 commits |
| `VERSION` in `app.py` | `0.27.1` (on **both** branches) |
| Latest tag | `v0.27.0` |
| Latest GitHub Release | v0.27.0 — *the fleet release* (2026-07-28) |
| CI on `next` | green (last 6 runs) |

## Two gaps found while scoping

1. **#243 shipped without a CHANGELOG entry.** `feat(models): RAM-spill visibility per run +
   per-model Used-by attribution` is merged into `next` (`abad26b`) but appears nowhere in the
   Unreleased section. Likely lost when `81b4e46` moved the AI-Models entries out of the
   already-shipped 0.26.0 block.
2. **v0.27.1 was bumped but never released.** `main` carries `VERSION = "0.27.1"` (PR #259 —
   the `mcp<2` pin that fixes the crash-looping MCP server after 2.0.0 removed
   `mcp.server.fastmcp`), but there is no `v0.27.1` tag, no GitHub Release and therefore no
   Docker Hub `:0.27.1`. **Nobody running the app ever received that fix.** Decision: absorb it
   into 0.28.0 rather than back-cut a patch, and give it a `Fixed` line so the pin is documented.

## Scope of 0.28.0

| PR | Feature | Author |
|----|---------|--------|
| #243 | RAM-spill visibility per run + per-model Used-by attribution | @SikamikanikoBG |
| #244 | Model memory split into weights vs context/KV + seconds-fresh AI tab | @SikamikanikoBG |
| #247 | AMD per-process VRAM from the DRM fdinfo nodes | **@andreahaku** |
| #254 | AMD GPU panel parity — clocks, perf level, caps, real card names | **@andreahaku** |
| #259 | `mcp<2` pin (the unreleased 0.27.1) | @SikamikanikoBG |

Half the headline features this cycle came from outside the repo — both from @andreahaku, both
on hardware (Strix Halo / Radeon 8060S) that isn't in this fleet.

## Log

- **CHANGELOG**: wrote the missing #243 entry (RAM-spill visibility + Used-by attribution),
  added a `Fixed` block for the `mcp>=1.9.0,<2` pin noting it was the never-released 0.27.1,
  and converted the `Unreleased` header to `0.28.0` with a summary that thanks @andreahaku.
  The inline `_(contributed by …)_` credits on #247/#254 were already in place.
- **README**: `### Contributors` rewritten to credit the whole AMD back-end to @andreahaku
  (v0.26.0 + v0.28.0), and to add @1HazyOne707's and @pehota's v0.27.0 work alongside their
  earlier contributions. Same one-paragraph shape as before — no new files.
- **VERSION** `0.27.1` → `0.28.0` (`app.py:37`).
- **Snapshot rebaseline**: local python is 3.8, so it ran in a `python:3.12-slim` container on
  ardi (`--user 1001:100`) against a scratch `--depth 1` clone of `next` with the local diff
  applied — deps installed to `--target=/tmp/pylibs` since the non-root user can't write
  site-packages. `UPDATE_SNAPSHOTS=1` → 71 passed; the clean re-run (CI's determinism check)
  also passed. 6 baselines changed: `api_changelog`, `api_data`, `api_health`,
  `api_settings_get`, `api_settings_post`, `healthz`. Scratch clone removed afterwards.
