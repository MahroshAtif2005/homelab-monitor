#!/usr/bin/env python3
"""i18n locale sync + coverage tool (issue #148).

`locales/en.json` is the single source of truth: it lists every translatable
key and its canonical English string. This tool keeps the other locale files
in lock-step with it, so a translator never has to diff by hand:

  * adds keys that are new in en.json (pre-filled with the English text),
  * drops keys that no longer exist in en.json,
  * preserves every translation already written,
  * records the still-English keys in `_meta.untranslated`, which the dashboard
    reads to compute coverage — a locale only appears in the language switcher
    once it crosses the coverage bar (90%), so a half-finished translation is
    never shown to users.

Usage
-----
  python scripts/i18n-sync.py                     # sync every locales/*.json, print coverage
  python scripts/i18n-sync.py --check             # CI: non-zero exit if anything is out of sync
  python scripts/i18n-sync.py add zh-CN "简体中文"   # scaffold a brand-new locale

Translator workflow: run the tool (or just open the file), translate the values
whose key is listed in `_meta.untranslated`, re-run the tool to refresh the list,
and you're done when the list is empty.

Pure standard library — no dependencies.
"""
import json
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "locales"
BASE = "en"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=OrderedDict)


def dump(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def canonical():
    en = load(LOCALES / f"{BASE}.json")
    keys = [k for k in en if k != "_meta"]
    return en, keys


def sync_one(code, en, keys, native=None, write=True):
    """Return (new_dict, untranslated_keys). Writes the file when write=True."""
    path = LOCALES / f"{code}.json"
    existing = load(path) if path.exists() else OrderedDict()
    meta_in = existing.get("_meta", {}) if isinstance(existing, dict) else {}

    new = OrderedDict()
    new["_meta"] = OrderedDict(
        code=code,
        name=meta_in.get("name", native or code),
        nativeName=native or meta_in.get("nativeName", code),
        dir=meta_in.get("dir", "ltr"),
    )

    untranslated = []
    for k in keys:
        val = existing.get(k)
        if val is None or val == "":
            val = en[k]                 # carry English as a placeholder
        new[k] = val
        if val == en[k]:                # still English == not yet translated
            untranslated.append(k)

    new["_meta"]["untranslated"] = untranslated
    new["_meta"]["coverage"] = round((len(keys) - len(untranslated)) / len(keys), 4) if keys else 0

    if write:
        dump(path, new)
    return new, untranslated


def cmd_sync(check=False):
    en, keys = canonical()
    targets = sorted(p.stem for p in LOCALES.glob("*.json") if p.stem != BASE)
    if not targets:
        print("No non-English locales yet. Scaffold one:\n"
              "  python scripts/i18n-sync.py add zh-CN \"简体中文\"")
        return 0

    drift = False
    print(f"Canonical: {len(keys)} keys in {BASE}.json\n")
    for code in targets:
        before = load(LOCALES / f"{code}.json")
        new, todo = sync_one(code, en, keys, write=not check)
        cov = new["_meta"]["coverage"]
        done = len(keys) - len(todo)
        bar = "#" * int(cov * 20)
        print(f"  {code:<8} {done:>3}/{len(keys)} translated  [{bar:<20}] {cov*100:5.1f}%"
              + ("   ✓ in switcher" if cov >= 0.9 else "   · hidden (<90%)"))
        if check and json.dumps(before, ensure_ascii=False) != json.dumps(new, ensure_ascii=False):
            drift = True
            print(f"           ↳ OUT OF SYNC — run `python scripts/i18n-sync.py` and commit")

    if check and drift:
        return 1
    return 0


def cmd_add(code, native):
    path = LOCALES / f"{code}.json"
    if path.exists():
        print(f"{path} already exists — running a sync instead.")
    en, keys = canonical()
    new, todo = sync_one(code, en, keys, native=native, write=True)
    print(f"Wrote {path} — {len(keys)} keys, all {len(todo)} awaiting translation.")
    print(f"Translate the values listed in _meta.untranslated, then re-run this tool.")
    return 0


def main(argv):
    if len(argv) >= 1 and argv[0] == "add":
        if len(argv) < 3:
            print('Usage: i18n-sync.py add <code> "<Native name>"')
            return 2
        return cmd_add(argv[1], argv[2])
    if argv and argv[0] in ("--check", "-c"):
        return cmd_sync(check=True)
    return cmd_sync(check=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
