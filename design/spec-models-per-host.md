# Spec — make the AI Models tab correct and reliable for every host

**Status:** ready to implement · **Target branch:** `next` (feature branch `fix/models-per-host`)
**Baseline:** `next` @ `572f946` (post-v0.28.0). Everything below was verified against
the live prod instance (`ardi:9800`, v0.28.0) and the code on `next` on 2026-07-31.

---

## 1. What the user sees

Two reproducible complaints on the **AI Models** tab, both about remote hosts:

1. **False empty state.** Selecting host `vader` and clicking *AI Models* shows
   *"No AI models reported on vader yet"* — while `GET /api/models` returns 4 vader
   models at that same moment (1 loaded: `MichelRosselli/GLM-4.5-Air:Q3_K_M`,
   53 933 MB VRAM). Navigating away and back later shows them correctly.
2. **Cross-host bleed.** Switching from `vader` to another host leaves **vader's**
   panel on screen, under the new host's selection.

Requirement from the user: *"in order to feel reliable for users — should say per host
once clicked."* The tab must always show the selected host, or an honest
"still loading" — never another host's data and never a wrong empty state.

None of this is a data problem. The data is correct in the API. All three root
causes below are in the client, plus two coverage gaps in the probes.

---

## 2. Root causes

### RC-1 — the throttle is armed by calls that never paint (causes symptom 1)

`static/dashboard.html`

```
5673  function setHost(name){
5678    refreshLocalOnlyNotices();     // runs for ALL local-only tabs, visible or not
7758  function refreshLocalOnlyNotices(){ LOCAL_ONLY_TABS.forEach(t => … renderLocalOnlyNotice(t)) }
7723    maybeRenderRemoteModels(sec);  // fired even when TAB !== 'models'
7653  let RMODELS_AT=0, RMODELS_HOST=null;
7656    if(Date.now()-RMODELS_AT<15000 && RMODELS_HOST===host) return;
7657    RMODELS_AT=Date.now(); RMODELS_HOST=host;      // ← throttle armed BEFORE the fetch
7659    if(host!==CURRENT_HOST || TAB!=='models') return;   // ← bails without painting
```

Sequence that produces the bug:

1. User is on any tab **other than** AI Models and picks `vader`.
2. `setHost` → `refreshLocalOnlyNotices` → `renderLocalOnlyNotice('models')` →
   `maybeRenderRemoteModels()` stamps `RMODELS_AT = now`, `RMODELS_HOST = 'vader'`,
   fetches `/api/models`, then **returns at 7659** because `TAB !== 'models'`.
   Nothing is painted; the throttle is now armed for 15 s.
3. Within those 15 s the user clicks **AI Models** → `showTab` →
   `renderLocalOnlyNotice('models')` → `maybeRenderRemoteModels()` short-circuits on
   the throttle at 7656 → no panel is built → control falls through to line 7734+
   and renders `describeMissingCapability('models')` = *"No AI models reported on
   vader yet"*.
4. Recovery depends solely on the 15 s auto-refresh tick at line 8231. The throttle
   window (`<15000`) and the poll period (`15000`) are the same number, so the tick
   frequently loses the race and recovery slips to 30 s+ — and never happens at all
   if the *auto* checkbox is off.

**Fix principle:** a call that does not paint must not consume the throttle, and the
result of a fetch must be kept even when the tab is not visible.

### RC-2 — the panel is not identity-checked against the selected host (symptom 2)

```
7728    if(sec.querySelector('.remote-models')){   // any panel counts as "mine"
7730      if(reg) reg.style.display='';
7731      return;                                   // ← early return keeps the OLD host's card
```

The panel carries no record of which host it was rendered for, so
`renderLocalOnlyNotice` treats a stale `vader` card as valid for the newly selected
host and returns early. If the follow-up fetch then bails at 7659 (user switched host
while on another tab — the RC-1 path), the stale card is never replaced.

### RC-3 — remote entries are keyed by the probe's `hostname`, not the registered host name

`backend/api/gpu.py:63-68` extends the registry with each remote's catalog verbatim:

```python
for _name, entry in remote_items:
    remote_catalog = (entry.get("data") or {}).get("model_catalog")
    if remote_catalog:
        catalog.extend(remote_catalog)          # ← `_name` (registered) is discarded
```

Each entry's `host` comes from `socket.gethostname()` on the remote
(`probe.py:1548`, carried through `app.py::_merge_registry` as `chost`). The UI filters
with `m.host === CURRENT_HOST` (line 7660), where `CURRENT_HOST` is the **registered**
fleet name. These agree for `vader` by luck. They will not agree for any host whose
registered label differs from its `hostname` — e.g. a host registered as `Work` whose
`$env:COMPUTERNAME` is `DESKTOP-…`. Latent silent blindness; fix it while here.

### RC-4 — Windows hosts never report a model catalog at all

`probe.ps1` assembles its payload at lines 412-431 with `host` / `at` /
`probe_version = 'win-0.1'` and **no `model_catalog` key** — there is no ollama read
anywhere in the file (`grep -n 11434 probe.ps1` → no match). A Windows host running
ollama can therefore never appear on the AI Models tab. `probe.py` (POSIX) has
`read_ollama_models()` at 1527 and ships `model_catalog` at 1607.

### RC-5 — remotes are ollama-only

`probe.py::read_ollama_models` only talks to `127.0.0.1:11434`. The hub detects ~40
server types (`backend/probes/__init__.py::PROBES`, lines 154-205), but remotes get
none of them. This is the documented "later probe slice" (caption at
`static/dashboard.html:7694`). It is the remaining part of "add this for all hosts"
and is specified as an optional third phase below.

---

## 3. The fix

Implement in three commits, in this order. Phases 1-2 close the reported bugs; Phase 3
is the coverage extension.

### Phase 1 — client: per-host cache, host-stamped panel, honest loading state

All in `static/dashboard.html`, in the `── Per-host AI Models (remote hosts) ──`
block (7647-7697) and the `models` branch of `renderLocalOnlyNotice` (7722-7744).

**1a. Replace the two globals with a per-host cache.**

```js
// host -> {at, models}. One /api/models fetch is fleet-wide, so it warms every
// host the user might click next — switching hosts then repaints from cache with
// no network round-trip and no window in which the previous host's card is shown.
let RMODELS = Object.create(null);
let RMODELS_INFLIGHT = null;       // host name of the fetch currently in flight
const RMODELS_TTL = 12000;         // < the 15s poll period, so the tick always refreshes
```

Deliberately **12 000 ms, not 15 000** — a TTL equal to the caller's interval is the
knife-edge that made RC-1's recovery unreliable.

**1b. Rewrite `maybeRenderRemoteModels` so the fetch and the paint are separate.**

```js
async function maybeRenderRemoteModels(sec){
  const host = CURRENT_HOST;
  // Never leave another host's card on screen, not even for one frame.
  const stale = sec.querySelector('.remote-models');
  if(stale && stale.dataset.host !== host) stale.remove();

  const c = RMODELS[host];
  if(c) paintRemoteModels(sec, host, c.models);          // instant, correct host
  if(c && Date.now()-c.at < RMODELS_TTL) return;         // fresh enough
  if(RMODELS_INFLIGHT === host) return;                  // one fetch per host at a time

  RMODELS_INFLIGHT = host;
  let j;
  try{ j = await(await fetch('/api/models')).json(); }
  catch(e){ RMODELS_INFLIGHT = null; return; }           // keep the last good cache
  finally{ if(RMODELS_INFLIGHT === host) RMODELS_INFLIGHT = null; }

  // Cache the whole fleet regardless of which tab is visible — this is what makes
  // "switch host on another tab, then open AI Models" paint instantly (RC-1).
  const at = Date.now(), byHost = Object.create(null);
  (j.models||[]).forEach(m => (byHost[m.host] = byHost[m.host] || []).push(m));
  Object.keys(byHost).forEach(h => RMODELS[h] = {at, models: byHost[h]});
  if(!RMODELS[host]) RMODELS[host] = {at, models: []};   // resolved: this host has none

  if(CURRENT_HOST !== host || TAB !== 'models') return;  // cached above; just don't paint
  paintRemoteModels(sec, host, RMODELS[host].models);
}
```

**1c. Extract the DOM work into `paintRemoteModels(sec, host, mine)`** — the body of
today's 7660-7696, with three changes:

- stamp the card: `box.dataset.host = host;`
- guard against a late paint: `if(host !== CURRENT_HOST) return;` at the top
- the empty branch must call **`showScopeNotice(sec,'models')`**, *not*
  `renderLocalOnlyNotice('models')` as line 7663 does today.

> ⚠️ **Do not skip the last point.** `renderLocalOnlyNotice` calls
> `maybeRenderRemoteModels`, which would call it back — with the cache now warm the
> two would recurse. Extract the notice-rendering tail of `renderLocalOnlyNotice`
> (7734-7743) into a standalone `showScopeNotice(sec, tabId)` and call that from both
> places.

**1d. Host-check the early return in `renderLocalOnlyNotice`** (7728):

```js
const panel = sec.querySelector('.remote-models');
if(panel && panel.dataset.host === CURRENT_HOST){
  const reg=document.getElementById('registry-card');
  if(reg) reg.style.display='';
  return;
}
```

**1e. Loading state instead of a wrong empty state.** In the `models` branch, when
`RMODELS[CURRENT_HOST]` is `undefined` (never resolved for this host), render a
pending notice rather than `describeMissingCapability`:

```js
{ic:'⏳', title:`Reading ${CURRENT_HOST}'s model servers…`,
 detail:'Asking the host what it has loaded. This takes a moment on first view.'}
```

Only show *"No AI models reported on …"* once a fetch has resolved with an empty list
for that host. Route both through `describeMissingCapability(tabId)` by giving it the
resolved/unresolved distinction, or branch before calling it — implementer's choice.

**1f. Scope the Installed-models registry card to the selected host.** `renderRegistry`
(3685-3754) groups by host and shows the whole fleet even when one host is selected,
which contradicts "per host once clicked". Filter `all` to `CURRENT_HOST` when a
specific host is selected (hub entries carry `host: "local"`, see
`app.py::_merge_registry`), keep the per-host `<h3>` heading, and recompute the
summary line from the filtered set so the count matches what is on screen. Add an
**"All hosts"** checkbox next to `#registry-filter` (default off) for the fleet view,
and re-render on host switch — `renderRegistry(null)` re-filters `REGISTRY_LAST` with
no refetch.

### Phase 2 — server: key remote entries by their registered name (RC-3)

`backend/api/gpu.py`, in `api_models`:

```python
for _name, entry in remote_items:
    remote_catalog = (entry.get("data") or {}).get("model_catalog")
    if remote_catalog:
        # Key every remote entry by its REGISTERED fleet name — that is what the
        # host selector sends and what the UI filters on. The probe's own
        # socket.gethostname() may differ (registered "Work" vs "DESKTOP-…"),
        # which silently hid that host's models.
        catalog.extend(dict(m, host=_name) for m in remote_catalog)
```

Keep the probe-reported name if it is useful for display: `dict(m, host=_name,
host_reported=m.get("host"))`. Do **not** touch `_merge_registry`'s handling of
`provider == "ollama" and chost in ("local", hub)` — that dedupe guard against the
hub's own richer registry must keep working, and it is exercised by
`tests/test_registry.py`.

### Phase 3 — probe coverage (optional but this is what "all hosts" finally means)

**3a. Windows parity (RC-4).** Add `Read-OllamaModels` to `probe.ps1` mirroring
`probe.py:1527-1584`: `GET http://127.0.0.1:11434/api/ps` and `/api/tags` via
`Invoke-RestMethod -TimeoutSec 2`, emit the same row shape
(`host, service, provider, model, loaded, vram_mb, size_bytes, family, param_size,
quant, modified`), wrap in `try/catch` returning `@()`, add
`model_catalog = (Read-OllamaModels)` to the `$payload` at line 427 and bump
`probe_version` to `win-0.2`. `$data.host` there is the merged host block, so the new
key goes at payload level, next to `at` — matching `probe.py:1607`.

**3b. Multi-provider on remotes (RC-5).** Extend `probe.py` with a port-driven scan
that reuses what the probe already collects. `_listen_sockets()` (in `read_net`)
returns every listening TCP port with its owning process name — probe **only** ports
that are actually listening and appear in a known-server port map, then accept the
answer only if it parses as a known shape:

- OpenAI-compatible: `GET /v1/models` → `data[].id` (covers vLLM, llama.cpp,
  LocalAI, LM Studio, tabbyAPI, xinference, SGLang, LiteLLM, …)
- ollama: existing `/api/ps` + `/api/tags`
- specials worth carrying: ComfyUI `/system_stats` + `/object_info/…`,
  A1111 `/sdapi/v1/options`, TGI `/info`

Port map and provider labels: copy from `backend/probes/__init__.py::PROBES`
(154-205) so hub and remote report identical provider ids. Constraints: stdlib only
(the probe is shipped as a single file over SSH), 2 s timeout per request, self-
validating responses so a random web server on `:80` is never mistaken for GPUStack.
Bump `probe_version` to `0.13`.

Then update the two captions that currently promise this as future work:
`static/dashboard.html:7694` (`rmod.cap`) and the `models` empty-state text at 7436.

---

## 4. Acceptance criteria

Manual, against dev (`ardi:9801` — never prod; see the dev/prod split):

1. On the **Overview** tab, select `vader`, then click **AI Models** *immediately*
   (< 2 s). Vader's models appear. Repeat 5× — no *"No AI models reported"* even once.
2. Same, with the **auto-refresh checkbox off**. Still correct (today it can never
   recover in this state).
3. On **AI Models** with `vader` selected, switch to `Work`/`local`/an offline host.
   Vader's card disappears in the same frame; it is never shown under another host's
   name.
4. Select an online host with genuinely no AI server. First paint is the ⏳ loading
   notice; after the fetch resolves it becomes the empty state — never the reverse.
5. Rapid host switching (`local → vader → Work → vader`, ~1/s) ends on a card whose
   `dataset.host` equals the selector. No duplicate `.remote-models` nodes, no console
   errors.
6. The **Installed models** card lists only the selected host; ticking *All hosts*
   restores the fleet grouping; the summary count matches the visible rows.
7. `local` is unaffected: the hub keeps the full `#modelsbody` view with VRAM
   timelines, weights/context split and the 5 s `/api/ai/now` fast poll.

Automated:

- `tests/test_registry.py` must stay green (it pins `_merge_registry`).
- Add a case for Phase 2: a remote registered as `Work` whose probe reports
  `host: "DESKTOP-ABC"` must surface as `host == "Work"` from `/api/models`.
- Remember the CI scope gotcha: `ci.yml` runs only `tests/test_snapshots.py` +
  `py_compile` + a docker boot/healthz smoke. Per-feature tests are **not** run in
  CI — execute them by hand on ardi (Python 3.13; local Windows Python 3.8 cannot
  even import `app.py`).

## 5. Non-goals

- No new endpoint. `/api/models` already returns everything Phase 1 needs.
- No change to the hub-local AI Models view (`renderModels`, `/api/ai/now`).
- No change to the sampler cadence or to `LOCAL_ONLY_TABS` membership — `models`
  stays in that set; it is the per-host panel inside it that gets fixed.
- Nothing mutating: the monitor still never pulls, loads or unloads a model.

## 6. Verification data (2026-07-31, prod v0.28.0)

`GET http://ardi:9800/api/models` → `ollama_reachable: false` (the hub's own ollama is
containerised and not on the hub's loopback), `providers: ["ollama"]`,
`totals: {count: 4, loaded: 1, total_gb: 123.77}`, all four entries `host: "vader"`.
Fleet at the time: `local` (ardi) and `Work` online, `vader` online,
`cloudy` / `oldie` / `JarvisVM` offline. The panel code (`maybeRenderRemoteModels`,
PR #258) *is* present in the deployed v0.28.0 bundle — this is a logic bug, not a
missing deploy.
