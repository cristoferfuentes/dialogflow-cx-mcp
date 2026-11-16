#!/usr/bin/env python3
"""Analyzes the traffic captured from the console and contrasts it with the 3
discovery docs (v3beta1, v3alpha1, v3).

It gives two things:

1. **Endpoints the console calls that are NOT in v3beta1**: those that only
   exist in v3alpha1 (or in no version at all) are **method gaps**.
2. **Body fields the console sends that the public proto does NOT declare**:
   those the backend keeps and the public API drops are **field gaps**
   (the ones that can be written).

    .venv/bin/python probes/analiza_trafico_consola.py --trafico /tmp/trafico_consola.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from cxp.gaps import SchemaIndex, find_gaps  # noqa: E402

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ID_LIKE = re.compile(r"^(bg-[a-z-]+-|projects-|projects$)")


def _es_id(seg: str) -> bool:
    return bool(UUID.match(seg)) or seg in ("-", "null", "me") or seg.startswith("bg-cx-")


def _sin_version(ruta: str) -> str:
    """Strips the version and the leading slash: '/v3alpha1/projects/x' -> 'projects/x'."""
    r = ruta.split("?", 1)[0]
    r = re.sub(r"^/v[0-9a-z]+", "", r)
    return r.strip("/")


def canon_ruta(ruta: str) -> str:
    """Concrete route -> canonical form used for grouping (the ids are anonymized)."""
    r = _sin_version(ruta)
    partes = [p for p in r.split("/") if p != ""]
    return "/" + "/".join("{x}" if _es_id(p) else p for p in partes)


def doc_a_regex(tpl: str, params: dict | None = None):
    """Discovery doc template -> regex comparable with a concrete route.

    The discovery doc does not give the full route: it gives `v3beta1/{+name}` and the
    `pattern` of the `name` parameter (e.g. `^projects/[^/]+/locations/[^/]+/agents/[^/]+$`).
    Every placeholder is expanded with the pattern of its parameter.

    TRAP (measured on 2026-11-15): a method with its own suffix (`:listAvailableModels`)
    CANNOT be compared with the regex of the base resource, because `[^/]+` SWALLOWS
    the suffix (`.../agents/abc:listAvailableModels` matches `agents/[^/]+`).
    That is why the `custom` suffix is returned separately and compared with the method.
    """
    t = re.sub(r"^v[0-9a-z]+/", "", tpl)
    params = params or {}

    custom = ""
    if ":" in t:
        base, sufijo = t.rsplit(":", 1)
        custom = ":" + sufijo
        t = base

    def rep(m):
        var = m.group(1)
        p = params.get(var) or {}
        patron = p.get("pattern")
        if patron:
            return patron.strip("^$")
        return "[^/]+"

    t = re.sub(r"\{\+?([A-Za-z0-9_]+)\}", rep, t)
    t = re.sub(r"\{[A-Za-z0-9_.+]+\}", "[^/]+", t)
    return re.compile("^" + t + "$"), custom


def coincide(ruta: str, info: dict) -> bool:
    """Does the concrete route correspond to that method of the discovery doc?

    Rules:
      * the number of ':' must be the same (a resource has no ':'; a method
        with a suffix has exactly one);
      * if the method has a suffix, the route has to end with it.
    """
    if ruta.count(":") != info["puntos"]:
        return False
    custom = info["custom"]
    if custom:
        if not ruta.endswith(custom):
            return False
        return bool(info["regex"].fullmatch(ruta[: -len(custom)]))
    return bool(info["regex"].fullmatch(ruta))


def canon_doc(tpl: str) -> str:
    """Discovery doc template -> the same shape as `canon_ruta`."""
    t = re.sub(r"^v[0-9a-z]+/", "", tpl)
    # {var=projects/*/locations/*/agents/*} -> projects/*/locations/*/agents/*
    t = re.sub(r"\{[A-Za-z0-9_]+=([^}]*)\}", r"\1", t)
    t = t.replace("{+", "").replace("}", "")
    partes = [p for p in t.split("/") if p != ""]
    out = []
    for p in partes:
        out.append("{x}" if p == "*" else p)
    return "/" + "/".join(out)


def index_metodos(version: str) -> dict[tuple[str, str], dict]:
    """(verb, canonical route) -> {name, request, response, path}."""
    docs = json.loads((RAIZ / ".discovery" / f"{version}.json").read_text())
    out: dict[tuple[str, str], dict] = {}

    def walk(node, path=""):
        for name, res in (node or {}).items():
            for m, mdef in (res.get("methods") or {}).items():
                tpl = mdef.get("path", "")
                rx, custom = doc_a_regex(tpl, mdef.get("parameters") or {})
                # The key is the regex: two methods can share a template
                # (`{+name}`) and still be different routes.
                key = (mdef.get("httpMethod", ""), rx.pattern, custom)
                out.setdefault(key, {
                    "regex": rx,
                    "custom": custom,
                    "puntos": tpl.count(":"),
                    "nombre": f"{path}.{name}.{m}" if path else f"{name}.{m}",
                    "request": (mdef.get("request") or {}).get("$ref", "").split("/")[-1],
                    "response": (mdef.get("response") or {}).get("$ref", "").split("/")[-1],
                    "path": tpl,
                })
            walk(res.get("resources") or {}, f"{path}.{name}" if path else name)

    walk(docs.get("resources"))
    return out


def _metodo_de(idx: dict, verb: str, ruta: str) -> dict | None:
    """The method of the discovery doc that corresponds to a REAL concrete route."""
    real = _sin_version(ruta)
    for (mverb, _mcanon, _custom), info in idx.items():
        if mverb == verb and coincide(real, info):
            return info
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trafico", default="/tmp/trafico_consola.json")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cap = json.loads(Path(args.trafico).read_text())
    versiones = ("v3beta1", "v3alpha1", "v3")
    idxs = {v: index_metodos(v) for v in versiones}

    # ---------------------------------------------------- 1) METHOD gaps
    llamadas: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for req in cap:
        if req["metodo"] == "OPTIONS":
            continue
        llamadas[(req["metodo"], canon_ruta(req["ruta"]))].append(req)

    metodos = []
    for (verb, ruta), reqs in sorted(llamadas.items()):
        donde = []
        for v in versiones:
            for (mverb, _mcanon, _custom), info in idxs[v].items():
                if mverb != verb:
                    continue
                if coincide(_sin_version(ruta), info):
                    donde.append(v)
                    break
        metodos.append({
            "verbo": verb, "ruta": ruta, "n": len(reqs),
            "en": donde,
            "solo_alpha1": donde == ["v3alpha1"],
            "fuera_de_v3beta1": "v3beta1" not in donde,
            "fuera_de_todas": not donde,
        })

    # ------------------------------------------- 2) FIELD gaps (request bodies)
    idx_b = SchemaIndex.load(RAIZ / ".discovery" / "v3beta1.json", version="v3beta1")
    idx_a = SchemaIndex.load(RAIZ / ".discovery" / "v3alpha1.json", version="v3alpha1")
    huecos_campo: dict[str, dict] = {}

    for (verb, ruta), reqs in llamadas.items():
        # equivalent method (same verb and REAL route) in v3beta1 / v3alpha1
        real = reqs[0].get("ruta") or ""
        ref = _metodo_de(idxs["v3beta1"], verb, real)
        ref_a = _metodo_de(idxs["v3alpha1"], verb, real)
        if not ref and not ref_a:
            continue
        esquema = (ref or ref_a or {}).get("request") or ""
        if not esquema:
            continue
        full = (idx_b.props.get(esquema) and esquema) or (idx_a.props.get(esquema) and esquema)
        if not full:
            continue
        idx = idx_b if esquema in idx_b.props else idx_a
        for req in reqs:
            cuerpo = req.get("cuerpo")
            if not cuerpo:
                continue
            try:
                obj = json.loads(cuerpo)
            except ValueError:
                continue
            for g in find_gaps(obj, esquema, idx):
                clave = f"{g.schema}.{g.campo}"
                if clave not in huecos_campo:
                    huecos_campo[clave] = {
                        "esquema": g.schema, "campo": g.campo,
                        "verbo": verb, "ruta": ruta, "metodo": ref["nombre"] if ref else ref_a["nombre"],
                        "ejemplo": g.ruta, "valor": g.valor, "veces": 0,
                    }
                huecos_campo[clave]["veces"] += 1

    print("=" * 78)
    print("1) ENDPOINTS THE CONSOLE CALLS (captured live)")
    print("=" * 78)
    for m in metodos:
        donde = ",".join(m["en"]) or "NO VERSION"
        marca = ""
        if m["fuera_de_todas"]:
            marca = "   <== does not exist in ANY discovery doc"
        elif m["fuera_de_v3beta1"]:
            marca = "   <== only in " + donde
        print(f"  {m['verbo']:<6} {m['ruta']:<62} x{m['n']:<4} [{donde}]{marca}")

    print()
    print("=" * 78)
    print("2) FIELDS THE CONSOLE SENDS AND THE PUBLIC PROTO DOES NOT DECLARE")
    print("=" * 78)
    if not huecos_campo:
        print("  (none in the captured bodies)")
    for clave, h in sorted(huecos_campo.items(), key=lambda kv: -kv[1]["veces"]):  # noqa: B007
        print(f"  {h['esquema']}.{h['campo']:<34} x{h['veces']:<4} "
              f"{h['verbo']} {h['ruta'][:44]}")
        print(f"      method: {h['metodo']}  example: {h['ejemplo']}")
        print(f"      value the console sends: {json.dumps(h['valor'], ensure_ascii=False)[:160]}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"metodos": metodos, "campos": list(huecos_campo.values())},
            ensure_ascii=False, indent=1))
        print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
