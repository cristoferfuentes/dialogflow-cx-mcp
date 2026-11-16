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

    .venv/bin/python probes/exp_proto_interno_bundle.py [--json /tmp/proto_interno.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

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


def descargar_bundle() -> str:
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


def clases_del_bundle(bundle: str) -> dict[str, list[str]]:
    """class -> fields (in order, without repeats)."""
    out: dict[str, list[str]] = {}
    for m in CLASE_RE.finditer(bundle):
        clase, campo = m.group(1), m.group(2)
        campos = out.setdefault(clase, [])
        if campo not in campos:
            campos.append(campo)
    return out


def claves_por_clase(bundle: str) -> dict[str, list[str]]:
    """class -> the `keys:[...]` list its prototype declares."""
    out: dict[str, list[str]] = {}
    for m in KEYS_RE.finditer(bundle):
        clase = m.group(1)
        if m.group(2) is not None:
            campos = re.findall(r'"([^"]+)"', m.group(2))
        else:
            campos = (m.group(3) or "").split()
        if campos:
            out.setdefault(clase, campos)
    return out


def schemas_publicos() -> dict[str, set[str]]:
    """short schema name -> declared fields (v3beta1)."""
    doc = json.loads((RAIZ / ".discovery" / "v3beta1.json").read_text())
    out: dict[str, set[str]] = {}
    for nombre, cuerpo in (doc.get("schemas") or {}).items():
        corto = nombre.split("V3beta1")[-1] if "V3beta1" in nombre else nombre
        props = set((cuerpo.get("properties") or {}).keys())
        if props:
            out[corto] = props
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="/tmp/proto_interno.json")
    args = ap.parse_args()

    bundle = descargar_bundle()
    clases = clases_del_bundle(bundle)
    claves = claves_por_clase(bundle)
    publicos = schemas_publicos()
    print(f"classes in the bundle: {len(clases)} | public schemas: {len(publicos)}")

    # The declared keys are more reliable than the assignment order:
    # the two sources are combined per class.
    campos_por_clase: dict[str, set[str]] = {}
    for clase, campos in clases.items():
        campos_por_clase[clase] = set(campos) | set(claves.get(clase, []))

    # 1) The field names the console has and the public doc does not.
    publicos_todos: set[str] = set()
    for props in publicos.values():
        publicos_todos |= props
    consola_todos: set[str] = set()
    for campos in campos_por_clase.values():
        consola_todos |= campos
    solo_consola = sorted(consola_todos - publicos_todos)
    print(f"\nfields the console has and the public doc does NOT declare: "
          f"{len(solo_consola)}")
    for c in solo_consola:
        print("  -", c)

    # 2) Match each PUBLIC schema with its internal class.
    #    Criterion: ALL the public fields of the schema are in the class, and
    #    the class brings few extra fields (the internal ones). If two classes
    #    tie, the match is discarded (ambiguous).
    por_schema: dict[str, list[dict]] = {}
    for clase, campos in campos_por_clase.items():
        if len(campos) < 2:
            continue
        for nombre, props in publicos.items():
            if props <= campos:
                por_schema.setdefault(nombre, []).append({
                    "clase": clase,
                    "extra": sorted(campos - props),
                    "n_publicos": len(props),
                })
    emparejamientos: list[dict] = []
    for nombre, cands in por_schema.items():
        cands.sort(key=lambda c: (len(c["extra"]), -c["n_publicos"]))
        if len(cands) > 1 and (len(cands[0]["extra"]) == len(cands[1]["extra"])
                               and cands[0]["n_publicos"] == cands[1]["n_publicos"]):
            continue                      # ambiguous: nothing is asserted
        mejor = cands[0]
        if not mejor["extra"]:
            continue                      # no internal fields: it adds nothing
        if mejor["n_publicos"] < 2:
            continue                      # too weak a match
        emparejamientos.append({
            "schema_publico": nombre,
            "clase": mejor["clase"],
            "n_publicos": mejor["n_publicos"],
            "n_internos": len(mejor["extra"]),
            "campos_internos_no_publicos": mejor["extra"],
            "alternativas": len(cands),
        })
    emparejamientos.sort(key=lambda e: (-e["n_publicos"], e["n_internos"]))
    print(f"\npublic schemas with INTERNAL fields that are not declared: "
          f"{len(emparejamientos)}")
    for e in emparejamientos:
        print(f"  {e['schema_publico']:<34} {e['n_publicos']:>2} public, "
              f"{e['n_internos']:>2} internal: {e['campos_internos_no_publicos']}")

    Path(args.json).write_text(json.dumps({
        "solo_consola": solo_consola,
        "emparejamientos": emparejamientos,
        "n_clases": len(clases),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
