#!/usr/bin/env python3
"""The console's INTERNAL proto, extracted from its bundle, vs the discovery doc.

Why: the public discovery doc says which fields the v3beta1 API declares. The
console's **internal model** (the proto it really uses) has more
fields: `targetPlaybook`, `deploymentConfigs`, `versionTrafficAllocation`...
Those are field gaps that the public discovery doc does not declare.

The console bundle carries the MINIFIED internal proto (the class names are
`xC`, `fid`...), but the **field names are NOT minified** (`this.j.versionConfigs=...`).
And it also carries the list of keys of each message (`keys:[...]`).

This script:
  1. downloads the console bundle (cached) and extracts its classes;
  2. extracts the fields of the public discovery doc (v3beta1);
  3. computes the field names that exist in the console and **not** in
     any public schema (the field gap candidates);
  4. proposes the class<->public schema matching by field containment, to say
     WHICH MESSAGE each gap belongs to.

    .venv/bin/python probes/exp_internal_proto_bundle.py [--json /tmp/internal_proto.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONSOLE_HTML = "https://conversational-agents.cloud.google.com/"
BUNDLE_CACHE = Path("/tmp/cx_console_bundle.js")

# The bundle: var <class>=function(a){a=a===void 0?{}:a;this.j={};this.j.<field>=
CLASE_RE = re.compile(
    r"var\s+(\w+)=function\(a\)\{a=a===void 0\?\{\}:a;this\.j=\{\};this\.j\.(\w+)=")
# The key list of each message. There are TWO forms in the minified bundle:
#   keys:["a","b"]                  (array form)
#   keys:"a b c".split(" ")         (string form)
KEYS_RE = re.compile(
    r"(\w+)\.prototype\.ma=function\(\)\{return\{(?:keys:\[([^\]]*)\]"
    r"|keys:\"([^\"]*)\"\.split)")


def download_bundle() -> str:
    """The console HTML points to the bundle; it is cached in /tmp."""
    if BUNDLE_CACHE.exists() and BUNDLE_CACHE.stat().st_size > 1_000_000:
        return BUNDLE_CACHE.read_text(encoding="utf-8", errors="replace")
    with urllib.request.urlopen(CONSOLE_HTML, timeout=60) as r:
        html = r.read().decode("utf-8", errors="replace")
    urls = sorted(set(re.findall(
        r"https://www\.gstatic\.com/_/dialogflow-console/_/js/[^\\\"' ]+", html)))
    if not urls:
        raise SystemExit("the bundle was not found in the console HTML")
    print(f"bundle: {urls[0][:120]}...")
    with urllib.request.urlopen(urls[0], timeout=180) as r:
        data = r.read()
    BUNDLE_CACHE.write_bytes(data)
    return data.decode("utf-8", errors="replace")


def bundle_classes(bundle: str) -> dict[str, list[str]]:
    """class -> fields (in order, without repeats)."""
    out: dict[str, list[str]] = {}
    for m in CLASE_RE.finditer(bundle):
        class_name, field = m.group(1), m.group(2)
        fields = out.setdefault(class_name, [])
        if field not in fields:
            fields.append(field)
    return out


def keys_by_class(bundle: str) -> dict[str, list[str]]:
    """class -> the `keys:[...]` list its prototype declares."""
    out: dict[str, list[str]] = {}
    for m in KEYS_RE.finditer(bundle):
        class_name = m.group(1)
        if m.group(2) is not None:
            fields = re.findall(r'"([^"]+)"', m.group(2))
        else:
            fields = (m.group(3) or "").split()
        if fields:
            out.setdefault(class_name, fields)
    return out


def public_schemas() -> dict[str, set[str]]:
    """short schema name -> declared fields (v3beta1)."""
    doc = json.loads((ROOT / ".discovery" / "v3beta1.json").read_text())
    out: dict[str, set[str]] = {}
    for name, body in (doc.get("schemas") or {}).items():
        short = name.split("V3beta1")[-1] if "V3beta1" in name else name
        props = set((body.get("properties") or {}).keys())
        if props:
            out[short] = props
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="/tmp/internal_proto.json")
    args = ap.parse_args()

    bundle = download_bundle()
    classes = bundle_classes(bundle)
    keys = keys_by_class(bundle)
    public = public_schemas()
    print(f"classes in the bundle: {len(classes)} | public schemas: {len(public)}")

    # The declared keys are more reliable than the assignment order:
    # the two sources are combined per class.
    fields_by_class: dict[str, set[str]] = {}
    for class_name, fields in classes.items():
        fields_by_class[class_name] = set(fields) | set(keys.get(class_name, []))

    # 1) The field names the console has and the public doc does not.
    all_public: set[str] = set()
    for props in public.values():
        all_public |= props
    all_console: set[str] = set()
    for fields in fields_by_class.values():
        all_console |= fields
    console_only = sorted(all_console - all_public)
    print(f"\nfields the console has and the public doc does NOT declare: "
          f"{len(console_only)}")
    for c in console_only:
        print("  -", c)

    # 2) Match each PUBLIC schema with its internal class.
    #    Criterion: ALL the public fields of the schema are in the class, and
    #    the class brings few extra fields (the internal ones). If two classes
    #    tie, the match is discarded (ambiguous).
    by_schema: dict[str, list[dict]] = {}
    for class_name, fields in fields_by_class.items():
        if len(fields) < 2:
            continue
        for name, props in public.items():
            if props <= fields:
                by_schema.setdefault(name, []).append({
                    "class": class_name,
                    "extra": sorted(fields - props),
                    "n_public": len(props),
                })
    matches: list[dict] = []
    for name, cands in by_schema.items():
        cands.sort(key=lambda c: (len(c["extra"]), -c["n_public"]))
        if len(cands) > 1 and (len(cands[0]["extra"]) == len(cands[1]["extra"])
                               and cands[0]["n_public"] == cands[1]["n_public"]):
            continue                      # ambiguous: nothing is asserted
        best = cands[0]
        if not best["extra"]:
            continue                      # no internal fields: it adds nothing
        if best["n_public"] < 2:
            continue                      # too weak a match
        matches.append({
            "public_schema": name,
            "class": best["class"],
            "n_public": best["n_public"],
            "n_internal": len(best["extra"]),
            "internal_fields_not_public": best["extra"],
            "alternatives": len(cands),
        })
    matches.sort(key=lambda e: (-e["n_public"], e["n_internal"]))
    print(f"\npublic schemas with INTERNAL fields that are not declared: "
          f"{len(matches)}")
    for e in matches:
        print(f"  {e['public_schema']:<34} {e['n_public']:>2} public, "
              f"{e['n_internal']:>2} internal: {e['internal_fields_not_public']}")

    Path(args.json).write_text(json.dumps({
        "console_only": console_only,
        "matches": matches,
        "n_classes": len(classes),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
