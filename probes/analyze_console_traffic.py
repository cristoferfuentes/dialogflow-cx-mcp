#!/usr/bin/env python3
"""Analyzes the traffic captured from the console and contrasts it with the 3
discovery docs (v3beta1, v3alpha1, v3).

It gives two things:

1. **Endpoints the console calls that are NOT in v3beta1**: those that only
   exist in v3alpha1 (or in no version at all) are **method gaps**.
2. **Body fields the console sends that the public proto does NOT declare**:
   those the backend keeps and the public API drops are **field gaps**
   (the ones that can be written).

    .venv/bin/python probes/analyze_console_traffic.py --traffic /tmp/console_traffic.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cxp.gaps import SchemaIndex, find_gaps  # noqa: E402

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ID_LIKE = re.compile(r"^(bg-[a-z-]+-|projects-|projects$)")


def _is_id(seg: str) -> bool:
    return bool(UUID.match(seg)) or seg in ("-", "null", "me") or seg.startswith("bg-cx-")


def _strip_version(route: str) -> str:
    """Strips the version and the leading slash: '/v3alpha1/projects/x' -> 'projects/x'."""
    r = route.split("?", 1)[0]
    r = re.sub(r"^/v[0-9a-z]+", "", r)
    return r.strip("/")


def canon_route(route: str) -> str:
    """Concrete route -> canonical form used for grouping (the ids are anonymized)."""
    r = _strip_version(route)
    parts = [p for p in r.split("/") if p != ""]
    return "/" + "/".join("{x}" if _is_id(p) else p for p in parts)


def doc_to_regex(tpl: str, params: dict | None = None):
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
        base, suffix = t.rsplit(":", 1)
        custom = ":" + suffix
        t = base

    def rep(m):
        var = m.group(1)
        p = params.get(var) or {}
        pattern = p.get("pattern")
        if pattern:
            return pattern.strip("^$")
        return "[^/]+"

    t = re.sub(r"\{\+?([A-Za-z0-9_]+)\}", rep, t)
    t = re.sub(r"\{[A-Za-z0-9_.+]+\}", "[^/]+", t)
    return re.compile("^" + t + "$"), custom


def matches(route: str, info: dict) -> bool:
    """Does the concrete route correspond to that method of the discovery doc?

    Rules:
      * the number of ':' must be the same (a resource has no ':'; a method
        with a suffix has exactly one);
      * if the method has a suffix, the route has to end with it.
    """
    if route.count(":") != info["custom_colons"]:
        return False
    custom = info["custom"]
    if custom:
        if not route.endswith(custom):
            return False
        return bool(info["regex"].fullmatch(route[: -len(custom)]))
    return bool(info["regex"].fullmatch(route))


def canon_doc(tpl: str) -> str:
    """Discovery doc template -> the same shape as `canon_route`."""
    t = re.sub(r"^v[0-9a-z]+/", "", tpl)
    # {var=projects/*/locations/*/agents/*} -> projects/*/locations/*/agents/*
    t = re.sub(r"\{[A-Za-z0-9_]+=([^}]*)\}", r"\1", t)
    t = t.replace("{+", "").replace("}", "")
    parts = [p for p in t.split("/") if p != ""]
    out = []
    for p in parts:
        out.append("{x}" if p == "*" else p)
    return "/" + "/".join(out)


def index_methods(version: str) -> dict[tuple[str, str], dict]:
    """(verb, canonical route) -> {name, request, response, path}."""
    docs = json.loads((ROOT / ".discovery" / f"{version}.json").read_text())
    out: dict[tuple[str, str], dict] = {}

    def walk(node, path=""):
        for name, res in (node or {}).items():
            for m, mdef in (res.get("methods") or {}).items():
                tpl = mdef.get("path", "")
                rx, custom = doc_to_regex(tpl, mdef.get("parameters") or {})
                # The key is the regex: two methods can share a template
                # (`{+name}`) and still be different routes.
                key = (mdef.get("httpMethod", ""), rx.pattern, custom)
                out.setdefault(key, {
                    "regex": rx,
                    "custom": custom,
                    "custom_colons": tpl.count(":"),
                    "name": f"{path}.{name}.{m}" if path else f"{name}.{m}",
                    "request": (mdef.get("request") or {}).get("$ref", "").split("/")[-1],
                    "response": (mdef.get("response") or {}).get("$ref", "").split("/")[-1],
                    "path": tpl,
                })
            walk(res.get("resources") or {}, f"{path}.{name}" if path else name)

    walk(docs.get("resources"))
    return out


def _method_for(idx: dict, verb: str, route: str) -> dict | None:
    """The method of the discovery doc that corresponds to a REAL concrete route."""
    real = _strip_version(route)
    for (mverb, _mcanon, _custom), info in idx.items():
        if mverb == verb and matches(real, info):
            return info
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traffic", default="/tmp/console_traffic.json")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cap = json.loads(Path(args.traffic).read_text())
    versions = ("v3beta1", "v3alpha1", "v3")
    idxs = {v: index_methods(v) for v in versions}

    # ---------------------------------------------------- 1) METHOD gaps
    calls: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for req in cap:
        if req["method"] == "OPTIONS":
            continue
        calls[(req["method"], canon_route(req["route"]))].append(req)

    methods = []
    for (verb, route), reqs in sorted(calls.items()):
        where = []
        for v in versions:
            for (mverb, _mcanon, _custom), info in idxs[v].items():
                if mverb != verb:
                    continue
                if matches(_strip_version(route), info):
                    where.append(v)
                    break
        methods.append({
            "verb": verb, "route": route, "n": len(reqs),
            "in": where,
            "only_alpha1": where == ["v3alpha1"],
            "outside_v3beta1": "v3beta1" not in where,
            "outside_all": not where,
        })

    # ------------------------------------------- 2) FIELD gaps (request bodies)
    idx_b = SchemaIndex.load(ROOT / ".discovery" / "v3beta1.json", version="v3beta1")
    idx_a = SchemaIndex.load(ROOT / ".discovery" / "v3alpha1.json", version="v3alpha1")
    field_gaps: dict[str, dict] = {}

    for (verb, route), reqs in calls.items():
        # equivalent method (same verb and REAL route) in v3beta1 / v3alpha1
        real = reqs[0].get("route") or ""
        ref = _method_for(idxs["v3beta1"], verb, real)
        ref_alpha = _method_for(idxs["v3alpha1"], verb, real)
        if not ref and not ref_alpha:
            continue
        schema = (ref or ref_alpha or {}).get("request") or ""
        if not schema:
            continue
        full = (idx_b.props_of(schema) and schema) or (idx_a.props_of(schema) and schema)
        if not full:
            continue
        idx = idx_b if idx_b.props_of(schema) else idx_a
        for req in reqs:
            body = req.get("body")
            if not body:
                continue
            try:
                obj = json.loads(body)
            except ValueError:
                continue
            for g in find_gaps(obj, schema, idx):
                key = f"{g.schema}.{g.field}"
                if key not in field_gaps:
                    field_gaps[key] = {
                        "schema": g.schema, "field": g.field,
                        "verb": verb, "route": route,
                        "method": ref["name"] if ref else ref_alpha["name"],
                        "example": g.route, "value": g.value, "count": 0,
                    }
                field_gaps[key]["count"] += 1

    print("=" * 78)
    print("1) ENDPOINTS THE CONSOLE CALLS (captured live)")
    print("=" * 78)
    for m in methods:
        where = ",".join(m["in"]) or "NO VERSION"
        mark = ""
        if m["outside_all"]:
            mark = "   <== does not exist in ANY discovery doc"
        elif m["outside_v3beta1"]:
            mark = "   <== only in " + where
        print(f"  {m['verb']:<6} {m['route']:<62} x{m['n']:<4} [{where}]{mark}")

    print()
    print("=" * 78)
    print("2) FIELDS THE CONSOLE SENDS AND THE PUBLIC PROTO DOES NOT DECLARE")
    print("=" * 78)
    if not field_gaps:
        print("  (none in the captured bodies)")
    for key, h in sorted(field_gaps.items(), key=lambda kv: -kv[1]["count"]):  # noqa: B007
        print(f"  {h['schema']}.{h['field']:<34} x{h['count']:<4} "
              f"{h['verb']} {h['route'][:44]}")
        print(f"      method: {h['method']}  example: {h['example']}")
        print(f"      value the console sends: {json.dumps(h['value'], ensure_ascii=False)[:160]}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"methods": methods, "fields": list(field_gaps.values())},
            ensure_ascii=False, indent=1))
        print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
