# Cost Monitoring Revamp — Day/Night (Dual) Tariffs

Research + buildable design for the HomeLab Monitor electricity-cost upgrade.
Stack constraints honored: **pure Python stdlib + Flask, Chart.js, no new pip deps.**
Single-tariff behavior is preserved 1:1 when `tariff_mode=single` or the night price is blank.

Target files in the live app:
- `app.py` — `SETTING_DEFAULTS` (~2937), `save_settings()` (~2988), `/api/cost` (~3526).
- `static/dashboard.html` — settings fields (~784), `renderCost()` (~1342), cost card markup (~903), `loadAlerts()/saveAlerts()` (~1782/1814).
- New static asset: `static/tariffs.json` (ship the dataset from `design/ai-cockpit/tariffs.json`).

---

## 1. Backend design — dual tariff

### 1.1 New settings keys (add to `SETTING_DEFAULTS`)

```python
SETTING_DEFAULTS = {
    # ...existing keys...
    "kwh_price":        "",        # EXISTING: flat / day price per kWh; empty hides the card
    "currency":         "$",       # EXISTING
    # ── new for dual tariff ──
    "tariff_mode":      "single",  # "single" | "dual"  (default = today's behavior)
    "kwh_price_night":  "",        # night price per kWh; blank => behaves as single
    "night_start":      "22:00",   # local time-of-day, "HH:MM", window may wrap midnight
    "night_end":        "06:00",   # local time-of-day, "HH:MM"
    "country":          "",        # ISO-3166 alpha-2 for the prefill helper (UI hint only)
}
```

Notes:
- `kwh_price` keeps its exact current meaning. In dual mode it is the **day/peak** price.
- All values are persisted as strings (matches `save_settings()` which `str()`-casts everything).
  `save_settings()` needs **no change** — it already persists any subset of keys present in
  `SETTING_DEFAULTS`. Just adding the keys above is enough for them to round-trip.
- `country` is purely a convenience memo for the settings UI (records the user's last pick);
  the backend never resolves a country to a price — the frontend does the prefill from
  `tariffs.json`. This keeps the server offline and dependency-free.

### 1.2 When is dual mode "active"?

Dual mode applies only when **both** conditions hold; otherwise we fall back to the existing
single-price path with byte-for-byte identical output plus additive (null) dual fields:

```python
dual = (s.get("tariff_mode") == "dual") and (night_price is not None) and (day_price > 0)
```

So: dual is opt-in, and a blank night price silently degrades to single. This satisfies
"if the user doesn't know them, fall back to average (as now)."

### 1.3 Time-of-day classification of a unix `ts` (the core of the change)

We only store `ts` (unix epoch) + `power` per sample — that is sufficient, because day/night is a
pure function of the sample's **local** wall-clock time-of-day. We classify each sample at query
time. The window is defined by `night_start`/`night_end` as minutes-after-local-midnight and may
**wrap midnight** (e.g. 22:00→06:00).

```python
def _hhmm_to_min(s, default):
    """'HH:MM' -> minutes since midnight [0,1440). Falls back to `default` on junk."""
    try:
        h, m = s.split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v < 1440 else default
    except Exception:
        return default

def _make_is_night(night_start, night_end):
    """Return a fast predicate is_night(ts) using the SERVER's local time.
    Handles a midnight-wrapping window (start > end) and the degenerate
    start == end (treated as 'no night window' -> always day)."""
    ns = _hhmm_to_min(night_start, 22 * 60)   # default 22:00
    ne = _hhmm_to_min(night_end,    6 * 60)    # default 06:00
    if ns == ne:
        return lambda ts: False                # empty window => everything is day
    if ns < ne:
        # same-day window, e.g. 01:00–05:00
        def is_night(ts):
            lt = time.localtime(ts)
            mins = lt.tm_hour * 60 + lt.tm_min
            return ns <= mins < ne
    else:
        # wraps midnight, e.g. 22:00–06:00  => night if mins >= ns OR mins < ne
        def is_night(ts):
            lt = time.localtime(ts)
            mins = lt.tm_hour * 60 + lt.tm_min
            return mins >= ns or mins < ne
    return is_night
```

`time.localtime(ts)` uses the server's local timezone (incl. DST as the OS sees it), which is the
right reference for a self-hosted homelab box. No `zoneinfo`/`pytz` needed — stdlib only.

### 1.4 Splitting kWh/cost into day vs night at query time

Today the energy windows use a single SQL `SUM(power)` per window. For dual mode we cannot bucket
inside SQL (SQLite can't cheaply do local-time-of-day classification with a wrapping window), so we
**stream the rows and classify in Python**. The sample count per window is small (10s interval →
~8.6k rows/day, ~260k/30d) and we already pull comparable volumes elsewhere; one pass over `ts,power`
per window is fine. We fold the three windows in a single scan to keep it cheap:

```python
KWH_PER_WSAMPLE = INTERVAL / 3_600_000.0   # one power sample -> kWh (existing constant inline)

def split_kwh(since, is_night):
    """One pass over (ts,power) >= since -> (day_kwh, night_kwh)."""
    day_w = night_w = 0.0
    for ts, p in cur.execute("SELECT ts, power FROM samples WHERE ts>=? AND power IS NOT NULL",
                             (since,)):
        if is_night(ts):
            night_w += p
        else:
            day_w += p
    return day_w * KWH_PER_WSAMPLE, night_w * KWH_PER_WSAMPLE
```

For the **single** path, keep the existing `SUM(power)` query unchanged (no per-row Python) so the
common case stays as fast as today.

### 1.5 The new `/api/cost` response shape

Backward-compatible: every field the current frontend reads is still present and unchanged in single
mode. Dual mode **adds** a `day`/`night` breakdown and a `tariff` block; single mode emits the same
additive fields with `night` zeroed so the frontend has one code path.

```jsonc
{
  "enabled": true,
  "currency": "€",
  "range": "7d",
  "bucket_sec": 600,
  "current_w": 240,
  "avg_24h_w": 180,
  "avg_7d_w": 175,

  // EXISTING fields — total kWh / total cost per window (unchanged meaning):
  "kwh":  { "today": 1.234, "d7": 9.1, "d30": 38.7 },
  "cost": { "today": 0.37,  "d7": 2.73, "d30": 11.6 },

  // EXISTING: cumulative TOTAL cost series (now tariff-aware in dual mode):
  "series": { "labels": [...], "cost_cum": [...] },

  // ── NEW: tariff descriptor (lets the UI caption + legend itself) ──
  "tariff": {
    "mode": "dual",              // "single" | "dual"
    "price_day": 0.21,           // == kwh_price (the day/peak price)
    "price_night": 0.16,         // null in single mode
    "night_start": "22:00",
    "night_end": "06:00"
  },
  "kwh_price": 0.21,             // EXISTING field kept (== price_day) for old captions

  // ── NEW: per-window day/night split (always present; night all-zero in single) ──
  "split": {
    "today": { "day_kwh": 0.9, "night_kwh": 0.33, "day_cost": 0.19, "night_cost": 0.05 },
    "d7":    { "day_kwh": 6.2, "night_kwh": 2.9,  "day_cost": 1.30, "night_cost": 0.46 },
    "d30":   { "day_kwh": 26.1,"night_kwh": 12.6, "day_cost": 5.48, "night_cost": 2.02 }
  }
}
```

Cost math:
- single: `cost[w] = kwh[w] * price_day` (exactly as today).
- dual:   `cost[w] = day_kwh*price_day + night_kwh*price_night`, and the split block carries the
  component costs. `kwh[w]` stays `day_kwh + night_kwh` so the existing KPI keeps working.

### 1.6 Reference implementation of the new `/api/cost`

```python
@app.route("/api/cost")
def api_cost():
    s = get_settings()
    def fnum(key):
        v = (s.get(key) or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None

    day_price   = fnum("kwh_price") or 0.0
    night_price = fnum("kwh_price_night")
    mode        = "dual" if (s.get("tariff_mode") == "dual" and night_price is not None
                             and day_price > 0) else "single"
    currency    = s.get("currency") or "$"
    is_night    = _make_is_night(s.get("night_start", "22:00"), s.get("night_end", "06:00"))

    rng  = request.args.get("range", "7d")
    span = RANGES.get(rng, 604800)
    now  = int(time.time())
    kwh_per_wsample = INTERVAL / 3_600_000.0

    with LOCK:
        cur = DB.cursor()

        def avg_w(since):
            return round(cur.execute("SELECT AVG(power) FROM samples WHERE ts>=?",
                                     (since,)).fetchone()[0] or 0)

        def total_kwh(since):
            tot = cur.execute("SELECT SUM(power) FROM samples WHERE ts>=?",
                              (since,)).fetchone()[0] or 0
            return tot * kwh_per_wsample

        def split_kwh(since):
            day_w = night_w = 0.0
            for ts, p in cur.execute(
                    "SELECT ts,power FROM samples WHERE ts>=? AND power IS NOT NULL", (since,)):
                if is_night(ts):
                    night_w += p
                else:
                    day_w += p
            return day_w * kwh_per_wsample, night_w * kwh_per_wsample

        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        wins = {"today": midnight, "d7": now - 604800, "d30": now - 2592000}

        kwh, split, cost = {}, {}, {}
        for w, since in wins.items():
            if mode == "dual":
                dk, nk = split_kwh(since)
            else:
                dk, nk = total_kwh(since), 0.0           # single: one SUM, no per-row loop
            dc, nc = dk * day_price, nk * (night_price or 0.0)
            kwh[w]   = round(dk + nk, 3)
            cost[w]  = round(dc + nc, 2)
            split[w] = {"day_kwh": round(dk, 3), "night_kwh": round(nk, 3),
                        "day_cost": round(dc, 2), "night_cost": round(nc, 2)}

        # Cumulative-cost series. Single: keep the cheap SQL-bucketed path.
        # Dual: stream rows, classify, accumulate per bucket (one pass).
        since = (cur.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or now) \
                if span is None else now - span
        bk = max(INTERVAL, round(max(1, now - since) / MAX_POINTS))
        labels, cost_cum, running = [], [], 0.0
        if mode == "dual":
            acc = {}
            for ts, p in cur.execute(
                    "SELECT ts,power FROM samples WHERE ts>=? AND power IS NOT NULL ORDER BY ts",
                    (since,)):
                b = (ts // bk) * bk
                price = night_price if is_night(ts) else day_price
                acc[b] = acc.get(b, 0.0) + (p or 0) * kwh_per_wsample * price
            for b in sorted(acc):
                running += acc[b]
                labels.append(int(b)); cost_cum.append(round(running, 4))
        else:
            rows = cur.execute(
                "SELECT (ts/?)*? b, SUM(power) FROM samples WHERE ts>=? GROUP BY b ORDER BY b",
                (bk, bk, since)).fetchall()
            for b, p in rows:
                running += (p or 0) * kwh_per_wsample * day_price
                labels.append(int(b)); cost_cum.append(round(running, 4))

    return jsonify({
        "enabled": day_price > 0, "kwh_price": day_price, "currency": currency,
        "range": rng, "bucket_sec": bk,
        "current_w": round(LATEST.get("power") or 0),
        "avg_24h_w": avg_w(now - 86400), "avg_7d_w": avg_w(now - 604800),
        "kwh": kwh, "cost": cost, "split": split,
        "tariff": {"mode": mode, "price_day": day_price, "price_night": night_price,
                   "night_start": s.get("night_start", "22:00"),
                   "night_end": s.get("night_end", "06:00")},
        "series": {"labels": labels, "cost_cum": cost_cum},
    })
```

Backward-compat checklist: in single mode the SQL is identical to today, `kwh`/`cost`/`series`/
`enabled`/`kwh_price` are unchanged, and `split.*.night_*` are 0. The card still hides when
`day_price <= 0` (blank `kwh_price`), exactly as before.

---

## 2. Country tariff helper — is there a universal API? (researched)

**Claim under test:** "There is no universal free real-time API for residential day/night
electricity tariffs across countries."

**Verdict: CONFIRMED (with nuance).** What exists publicly is *wholesale / spot / day-ahead*
electricity prices for market bidding zones, **not** residential retail day/night unit rates:

- **ENTSO-E Transparency** (and free wrappers like *euenergy.live*, *Elecz*) publish hourly
  **day-ahead wholesale** prices per European bidding zone. That is the spot market, not what a
  household pays — retail tariffs add network charges, taxes, levies and supplier margin, and the
  day/night *split* is a supplier product, not a market datum.
- **GlobalPetrolPrices / Eurostat / Statista** give **retail household averages** but as periodic
  (semi-annual / quarterly) statistics, behind paid API tiers or as published tables — and they do
  **not** break out residential day vs night unit rates per country.
- Dual-rate products (UK **Economy 7**, France **Heures Creuses**, Spain **PVPC 2.0TD**, German
  **HT/NT**, Belgian **bi-horaire**, etc.) are **per-supplier and per-region**; there is no single
  authority publishing a machine-readable day/night residential rate for "every country."

So a live cross-country residential day/night API would be (a) paid, (b) a new network dependency,
and (c) still wrong for any specific user's bill. That contradicts the no-deps / offline-homelab
constraint.

**Pragmatic, honest solution:** ship a **curated static JSON dataset** in the repo, presented in the
UI as an *indicative estimate with a "last updated" date + source*, that the frontend uses to
**prefill** the settings on country select. The user then edits to match their actual bill. This is
truthful (no false precision), offline, dependency-free, and easy to refresh by editing one file.

Sources used to build the dataset:
- Eurostat household electricity prices, band DC, **H2 2024** (dataset NRG_PC_204) —
  https://ec.europa.eu/eurostat/statistics-explained/index.php?title=Electricity_price_statistics
  (EU avg ≈ €0.2872/100kWh; DE highest ≈ €0.3943, HU lowest ≈ €0.1032).
- GlobalPetrolPrices residential prices, **Q1 2026** —
  https://www.globalpetrolprices.com/electricity_prices/ (world avg ≈ $0.174/kWh; per-country table).
- Ofgem price cap unit rates **2025** + Economy 7 (night ~7h, often 00:30–07:30) —
  https://www.ofgem.gov.uk/information-consumers/energy-advice-households/energy-price-cap-unit-rates-and-standing-charges
- EDF **Tarif Bleu** HP/HC **2026**: HP ≈ €0.2065, HC ≈ €0.1579, HC window typ. 22:00–06:00 —
  https://particulier.edf.fr/
- Spain **PVPC 2.0TD** 2025: punta ≈ €0.15–0.20, valle ≈ €0.04–0.06, valle 00:00–08:00 + weekends —
  https://tarifaluzhora.es/info/pvpc-discriminacion-horaria
- Germany **HT/NT** 2025: household avg ≈ €0.36, NT window typ. 22:00–06:00 —
  https://www.polarstern-energie.de/

### 2.1 The dataset

38 countries; **24** marked `dual_common: true`. Where a real dual product is common, day/night
are representative TOU/economy unit rates; where only a single average is known, day == night
(flat). Values are per-kWh in **the row's local currency** (`currency` field). Shipped as
`static/tariffs.json` (raw copy at `design/ai-cockpit/tariffs.json`). Schema per row:
`{country_code, country_name, currency, price_day, price_night, night_start, night_end,
dual_common, source, year}`.

```json
{
  "schema_version": 1,
  "last_updated": "2026-06-14",
  "disclaimer": "Indicative residential electricity tariffs for prefilling the cost settings. These are representative national/all-in averages compiled from public sources (Eurostat H2-2024, GlobalPetrolPrices Q1-2026, Ofgem, EDF Tarif Bleu, Spain PVPC 2.0TD, German HT/NT). Real bills vary by supplier, region, contract and time. Day/night values for dual-tariff rows are typical TOU/economy splits, not a regulated quote. Always edit to match your own bill. Prices are per kWh in the row's local currency.",
  "sources": [
    {"name": "Eurostat household electricity prices, band DC, H2 2024 (NRG_PC_204)", "url": "https://ec.europa.eu/eurostat/statistics-explained/index.php?title=Electricity_price_statistics", "year": 2024},
    {"name": "GlobalPetrolPrices residential electricity prices", "url": "https://www.globalpetrolprices.com/electricity_prices/", "year": 2026},
    {"name": "Ofgem energy price cap unit rates", "url": "https://www.ofgem.gov.uk/information-consumers/energy-advice-households/energy-price-cap-unit-rates-and-standing-charges", "year": 2025},
    {"name": "EDF Tarif Bleu (Heures Pleines / Heures Creuses)", "url": "https://particulier.edf.fr/fr/accueil/gestion-contrat/options/heures-creuses.html", "year": 2026},
    {"name": "Spain PVPC 2.0TD (punta/llano/valle)", "url": "https://tarifaluzhora.es/info/pvpc-discriminacion-horaria", "year": 2025},
    {"name": "Germany HT/NT Doppeltarif / Nachtstrom", "url": "https://www.polarstern-energie.de/magazin/artikel/htnt-informationen-zum-sonderstromtarif/", "year": 2025},
    {"name": "U.S. EIA average residential price", "url": "https://www.eia.gov/electricity/monthly/", "year": 2025}
  ],
  "tariffs": [
    {"country_code": "GB", "country_name": "United Kingdom", "currency": "£", "price_day": 0.27, "price_night": 0.13, "night_start": "00:30", "night_end": "07:30", "dual_common": true, "source": "Ofgem price cap 2025 + Economy 7", "year": 2025},
    {"country_code": "FR", "country_name": "France", "currency": "€", "price_day": 0.2065, "price_night": 0.1579, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "EDF Tarif Bleu HP/HC 2026", "year": 2026},
    {"country_code": "DE", "country_name": "Germany", "currency": "€", "price_day": 0.38, "price_night": 0.27, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "Eurostat H2-2024 + HT/NT typical split", "year": 2025},
    {"country_code": "ES", "country_name": "Spain", "currency": "€", "price_day": 0.20, "price_night": 0.10, "night_start": "00:00", "night_end": "08:00", "dual_common": true, "source": "PVPC 2.0TD punta/valle 2025", "year": 2025},
    {"country_code": "IT", "country_name": "Italy", "currency": "€", "price_day": 0.42, "price_night": 0.36, "night_start": "23:00", "night_end": "07:00", "dual_common": true, "source": "Eurostat H2-2024 + F1/F23 fasce", "year": 2024},
    {"country_code": "IE", "country_name": "Ireland", "currency": "€", "price_day": 0.42, "price_night": 0.20, "night_start": "23:00", "night_end": "08:00", "dual_common": true, "source": "Eurostat H2-2024 + Night Saver", "year": 2024},
    {"country_code": "BE", "country_name": "Belgium", "currency": "€", "price_day": 0.43, "price_night": 0.33, "night_start": "22:00", "night_end": "07:00", "dual_common": true, "source": "Eurostat H2-2024 + bi-horaire", "year": 2024},
    {"country_code": "NL", "country_name": "Netherlands", "currency": "€", "price_day": 0.30, "price_night": 0.26, "night_start": "23:00", "night_end": "07:00", "dual_common": true, "source": "Eurostat H2-2024 + dal-tarief", "year": 2024},
    {"country_code": "AT", "country_name": "Austria", "currency": "€", "price_day": 0.35, "price_night": 0.35, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "GlobalPetrolPrices 2026", "year": 2026},
    {"country_code": "PT", "country_name": "Portugal", "currency": "€", "price_day": 0.24, "price_night": 0.12, "night_start": "22:00", "night_end": "08:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + bi-horario", "year": 2026},
    {"country_code": "PL", "country_name": "Poland", "currency": "zł", "price_day": 1.05, "price_night": 0.70, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "Eurostat H2-2024 + taryfa G12", "year": 2024},
    {"country_code": "CZ", "country_name": "Czechia", "currency": "Kč", "price_day": 7.50, "price_night": 4.50, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + nízký tarif", "year": 2026},
    {"country_code": "GR", "country_name": "Greece", "currency": "€", "price_day": 0.25, "price_night": 0.25, "night_start": "23:00", "night_end": "07:00", "dual_common": false, "source": "Eurostat H2-2024", "year": 2024},
    {"country_code": "RO", "country_name": "Romania", "currency": "lei", "price_day": 1.05, "price_night": 1.05, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "Eurostat H2-2024", "year": 2024},
    {"country_code": "HU", "country_name": "Hungary", "currency": "Ft", "price_day": 36.0, "price_night": 36.0, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "Eurostat H2-2024 (regulated rezsicsökkentés)", "year": 2024},
    {"country_code": "BG", "country_name": "Bulgaria", "currency": "лв", "price_day": 0.30, "price_night": 0.18, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "Eurostat H2-2024 + dual-zone meter", "year": 2024},
    {"country_code": "SE", "country_name": "Sweden", "currency": "kr", "price_day": 2.50, "price_night": 2.50, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "GlobalPetrolPrices 2026 (spot + grid)", "year": 2026},
    {"country_code": "NO", "country_name": "Norway", "currency": "kr", "price_day": 1.70, "price_night": 1.20, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + nettleie day/night", "year": 2026},
    {"country_code": "FI", "country_name": "Finland", "currency": "€", "price_day": 0.18, "price_night": 0.12, "night_start": "22:00", "night_end": "07:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + yösähkö", "year": 2026},
    {"country_code": "DK", "country_name": "Denmark", "currency": "kr", "price_day": 2.70, "price_night": 2.70, "night_start": "21:00", "night_end": "06:00", "dual_common": false, "source": "GlobalPetrolPrices 2026", "year": 2026},
    {"country_code": "CH", "country_name": "Switzerland", "currency": "CHF", "price_day": 0.36, "price_night": 0.26, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + Hochtarif/Niedertarif", "year": 2026},
    {"country_code": "US", "country_name": "United States", "currency": "$", "price_day": 0.18, "price_night": 0.18, "night_start": "21:00", "night_end": "07:00", "dual_common": false, "source": "EIA / GlobalPetrolPrices 2026 (avg; some TOU plans)", "year": 2026},
    {"country_code": "CA", "country_name": "Canada", "currency": "C$", "price_day": 0.16, "price_night": 0.10, "night_start": "19:00", "night_end": "07:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + Ontario TOU", "year": 2026},
    {"country_code": "AU", "country_name": "Australia", "currency": "A$", "price_day": 0.32, "price_night": 0.22, "night_start": "22:00", "night_end": "07:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + off-peak TOU", "year": 2026},
    {"country_code": "NZ", "country_name": "New Zealand", "currency": "NZ$", "price_day": 0.30, "price_night": 0.18, "night_start": "23:00", "night_end": "07:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + night plans", "year": 2026},
    {"country_code": "JP", "country_name": "Japan", "currency": "¥", "price_day": 35.0, "price_night": 25.0, "night_start": "23:00", "night_end": "07:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + TEPCO night plan", "year": 2026},
    {"country_code": "MX", "country_name": "Mexico", "currency": "$", "price_day": 2.00, "price_night": 2.00, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "GlobalPetrolPrices 2026 (CFE tariff)", "year": 2026},
    {"country_code": "BR", "country_name": "Brazil", "currency": "R$", "price_day": 0.95, "price_night": 0.95, "night_start": "21:30", "night_end": "06:00", "dual_common": false, "source": "GlobalPetrolPrices 2026", "year": 2026},
    {"country_code": "IN", "country_name": "India", "currency": "₹", "price_day": 6.50, "price_night": 6.50, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "GlobalPetrolPrices 2026 (state avg)", "year": 2026},
    {"country_code": "CN", "country_name": "China", "currency": "¥", "price_day": 0.55, "price_night": 0.35, "night_start": "22:00", "night_end": "08:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + peak/valley", "year": 2026},
    {"country_code": "ZA", "country_name": "South Africa", "currency": "R", "price_day": 3.50, "price_night": 3.50, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "GlobalPetrolPrices 2026 (Eskom)", "year": 2026},
    {"country_code": "TR", "country_name": "Turkey", "currency": "₺", "price_day": 2.20, "price_night": 1.30, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "GlobalPetrolPrices 2026 + 3-zaman tarife", "year": 2026},
    {"country_code": "SG", "country_name": "Singapore", "currency": "S$", "price_day": 0.32, "price_night": 0.32, "night_start": "23:00", "night_end": "07:00", "dual_common": false, "source": "GlobalPetrolPrices 2026 (SP Group)", "year": 2026},
    {"country_code": "AE", "country_name": "United Arab Emirates", "currency": "AED", "price_day": 0.30, "price_night": 0.30, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "GlobalPetrolPrices 2026 (DEWA)", "year": 2026},
    {"country_code": "SK", "country_name": "Slovakia", "currency": "€", "price_day": 0.25, "price_night": 0.17, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "Eurostat H2-2024 + nízka tarifa", "year": 2024},
    {"country_code": "SI", "country_name": "Slovenia", "currency": "€", "price_day": 0.18, "price_night": 0.13, "night_start": "22:00", "night_end": "06:00", "dual_common": true, "source": "Eurostat H2-2024 + dnevna/nočna", "year": 2024},
    {"country_code": "HR", "country_name": "Croatia", "currency": "€", "price_day": 0.16, "price_night": 0.08, "night_start": "22:00", "night_end": "07:00", "dual_common": true, "source": "Eurostat H2-2024 + viša/niža tarifa", "year": 2024},
    {"country_code": "LU", "country_name": "Luxembourg", "currency": "€", "price_day": 0.22, "price_night": 0.22, "night_start": "22:00", "night_end": "06:00", "dual_common": false, "source": "Eurostat H2-2024", "year": 2024}
  ]
}
```

---

## 3. Frontend UX design

### 3.1 Settings (Alerts/settings tab) — replace the 2-field price block

Replace the existing `al_kwh` / `al_currency` grid (~line 784) with a customer-centric tariff block.
IDs follow the `al_*` convention so `loadAlerts()`/`saveAlerts()` extend naturally.

Markup (conceptual):

```html
<div class="card-sub">💰 Electricity cost</div>

<!-- Country prefill -->
<div>
  <div class="muted ...">Country (prefill estimate)</div>
  <select id="al_country" style="...">
    <option value="">— Select to prefill typical rates —</option>
    <!-- options injected from tariffs.json, e.g. <option value="FR">France (€)</option> -->
  </select>
  <div class="cap" id="al_tariff_src">Indicative estimate — edit to match your bill.</div>
</div>

<!-- Mode toggle: Single average / Day & Night -->
<div class="seg" role="tablist">
  <button id="al_mode_single" class="rb on">Single average</button>
  <button id="al_mode_dual"   class="rb">Day &amp; Night</button>
</div>

<!-- Prices -->
<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
  <div>
    <div class="muted ..." id="al_kwh_label">Price per kWh</div>   <!-- "Day price" in dual -->
    <input type="number" id="al_kwh" min="0" step="0.0001" placeholder="e.g. 0.30">
  </div>
  <div>
    <div class="muted ...">Currency symbol</div>
    <input type="text" id="al_currency" maxlength="4" placeholder="$">
  </div>
</div>

<!-- Night fields: shown only in dual mode -->
<div id="al_night_wrap" hidden style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px">
  <div>
    <div class="muted ...">Night price per kWh</div>
    <input type="number" id="al_kwh_night" min="0" step="0.0001" placeholder="e.g. 0.15">
  </div>
  <div>
    <div class="muted ...">Night starts</div>
    <input type="time" id="al_night_start" value="22:00">
  </div>
  <div>
    <div class="muted ...">Night ends</div>
    <input type="time" id="al_night_end" value="06:00">
  </div>
</div>
<p class="cap">Day &amp; Night bills your samples at the night rate inside the window
   (it may cross midnight) and the day rate otherwise. Leave the night price blank to use the
   single average.</p>
```

Behavior:
- **Mode toggle** flips `al_night_wrap.hidden` and swaps the day-price label between
  "Price per kWh" (single) and "Day price per kWh" (dual). Persists `tariff_mode`.
- **Country select** loads `static/tariffs.json` once (cache in JS). On change, find the row and
  prefill: `al_currency`←currency, `al_kwh`←price_day, `al_kwh_night`←price_night,
  `al_night_start/end`←window, and if `dual_common` flip the toggle to Day & Night. Update the
  caption `al_tariff_src` to e.g. *"Indicative — France, EDF Tarif Bleu HP/HC 2026. Edit to match
  your bill."* using the row's `source`/`year` and dataset `last_updated`. Prefill must never
  silently overwrite without making clear it is an editable estimate (the caption does this).
- **Validation:** night fields are `type=time` so they yield "HH:MM" directly. Day/night prices use
  `step=0.0001` (some rates are 4-dp, e.g. FR 0.2065).

`loadAlerts()` additions (~1785):
```js
document.getElementById('al_kwh').value         = s.kwh_price||'';
document.getElementById('al_currency').value    = s.currency||'$';
document.getElementById('al_kwh_night').value   = s.kwh_price_night||'';
document.getElementById('al_night_start').value = s.night_start||'22:00';
document.getElementById('al_night_end').value   = s.night_end||'06:00';
document.getElementById('al_country').value     = s.country||'';
setTariffMode(s.tariff_mode==='dual');   // toggles UI + label
```

`saveAlerts()` additions (~1817):
```js
kwh_price:       document.getElementById('al_kwh').value.trim(),
currency:        document.getElementById('al_currency').value.trim()||'$',
tariff_mode:     dualOn ? 'dual' : 'single',
kwh_price_night: document.getElementById('al_kwh_night').value.trim(),
night_start:     document.getElementById('al_night_start').value || '22:00',
night_end:       document.getElementById('al_night_end').value   || '06:00',
country:         document.getElementById('al_country').value,
```

### 3.2 `renderCost()` — show the day/night split

Keep the existing card; make it tariff-aware using the new `j.tariff` + `j.split`.

- **Single mode** (`j.tariff.mode === 'single'`): render *exactly today's* 6 KPIs and the single
  green cumulative-cost line. No visual change. (Guarantees backward-compat UX.)
- **Dual mode**: enrich the three cost KPIs ("Cost today / 7d / 30d") with a day/night sub-split,
  and color the cost chart as a **stacked area** (day + night).

KPI enrichment (dual): under each cost KPI's existing `.s` line, append a compact split, e.g.

```
Cost today
€0.24
1.23 kWh
day €0.19 · night €0.05      <-- new .s2 line, muted
```

Implementation sketch inside `renderCost()`:
```js
const dual = j.tariff && j.tariff.mode === 'dual';
const sp = j.split || {};
const splitLine = w => {
  const x = sp[w]; if(!dual || !x) return '';
  return `<div class="s s2">day ${money(x.day_cost)} · night ${money(x.night_cost)}</div>`;
};
// ...in the cost KPIs (today/d7/d30) append splitLine('today') etc.
```

Chart (dual): swap the single dataset for two stacked datasets driven by a new optional
`series.cost_cum_day` / `cost_cum_night` — OR, to avoid extra backend series, keep the single
cumulative **total** line (already tariff-correct) and add a small caption breakdown. Recommended,
lowest-risk: **keep one cumulative total line** (it already reflects dual pricing because the series
is computed with per-sample price in §1.6), and communicate the split via the KPIs + caption. If a
visibly stacked chart is wanted, add `cost_cum_day`/`cost_cum_night` arrays to the response
(accumulate the per-bucket day/night components in the same loop) and render two stacked area
datasets (`#3fb950` day, `#388bfd` night) with `stacked:true` on the y-scale.

Caption (`cost_cap`) in dual mode:
```
At €0.21 day / €0.16 night per kWh · night 22:00–06:00 · integrated from GPU power samples · 7d range
```
In single mode keep the current caption verbatim.

### 3.3 Customer-centric copy

- Country select header: *"Don't know your rates? Pick your country for a typical estimate."*
- Prefill caption always shows source + year + "edit to match your bill" so we never imply the
  number is the user's exact tariff.
- Toggle labels are plain language: **Single average** vs **Day & Night**.

---

## 4. Build checklist (order)

1. `app.py`: add the 6 new keys to `SETTING_DEFAULTS` (single line edit). `save_settings()` unchanged.
2. `app.py`: add `_hhmm_to_min`, `_make_is_night`, and the rewritten `/api/cost` (§1.6).
3. Copy `design/ai-cockpit/tariffs.json` → `static/tariffs.json` (served as a static asset).
4. `static/dashboard.html`: settings markup (§3.1), `loadAlerts`/`saveAlerts` field wiring,
   `setTariffMode()` + country-prefill JS.
5. `static/dashboard.html`: `renderCost()` dual-aware KPIs + caption (§3.2).
6. Smoke test: blank night price ⇒ identical to today; set dual ⇒ split sums to total; window
   wrapping midnight classifies a 02:00 sample as night and a 14:00 sample as day.

Risks / notes:
- The dual per-row scan is O(rows in window); at 10s interval the 30d window is ~260k rows. If that
  proves heavy, add a covering index `CREATE INDEX IF NOT EXISTS ix_samples_ts ON samples(ts)`
  (likely already implied by usage) — the scan still has to classify in Python, but it stays a single
  pass and only runs in dual mode.
- `time.localtime` follows the server box's TZ/DST; document that the night window is evaluated in
  **server local time**, which is what a homelab user expects.
