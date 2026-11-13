#!/usr/bin/env python3
"""Sonda: campos que el BACKEND devuelve y el ESQUEMA PUBLICO v3beta1 no declara.

Un campo que sale en el JSON real y no esta en el discovery doc publico es un
AGUJERO de la API publica: la API publica no lo puede pedir ni escribir (o lo
descarta en silencio), pero el backend interno de la consola si lo expone.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi

DOC = Path(__file__).resolve().parent.parent / ".discovery" / "v3beta1.json"
PREFIXES = ("GoogleCloudDialogflowCxV3beta1", "GoogleCloudDialogflowCxV3",
            "GoogleCloudDialogflowV2beta1", "GoogleCloudDialogflowV2")


def norm(name: str) -> str:
    for pref in PREFIXES:
        if name.startswith(pref):
            return name[len(pref):]
    return name


class SchemaIndex:
    def __init__(self, doc: dict):
        self.doc = doc
        self.props: dict[str, dict] = {}
        for full, body in doc.get("schemas", {}).items():
            short = norm(full)
            self.props.setdefault(short, {}).update(body.get("properties") or {})

    def sub_schema(self, schema: str, field: str) -> str:
        pv = self.props.get(schema, {}).get(field)
        if not pv:
            return ""
        ref = pv.get("$ref") or (pv.get("items") or {}).get("$ref")
        if ref:
            return norm(ref.split("/")[-1])
        return ""

    def declared(self, schema: str) -> set[str]:
        return set(self.props.get(schema, {}))


def walk(node, schema: str, idx: SchemaIndex, path: str,
         out: list[tuple[str, str]]) -> None:
    if isinstance(node, list):
        for i, item in enumerate(node):
            walk(item, schema, idx, f"{path}[{i}]", out)
        return
    if not isinstance(node, dict):
        return
    declared = idx.declared(schema)
    for key, value in node.items():
        here = f"{path}.{key}" if path else key
        if key not in declared:
            out.append((here, schema))
        sub = idx.sub_schema(schema, key)
        if sub:
            walk(value, sub, idx, here, out)
        elif isinstance(value, (dict, list)):
            walk(value, "", idx, here, out)


def collect(idx: SchemaIndex, obj, schema: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    walk(obj, schema, idx, "", out)
    return out


def main() -> int:
    doc = json.loads(DOC.read_text())
    idx = SchemaIndex(doc)
    cfg = load_config()
    api = ConsoleApi(cfg)
    agent = cfg.default_agent
    project = cfg.project

    seen_field: dict[tuple[str, str], set[str]] = defaultdict(set)
    samples = 0

    def feed(label: str, obj, schema: str) -> None:
        nonlocal samples
        samples += 1
        for path, sch in collect(idx, obj, schema):
            field = path.split(".")[-1].split("[")[0]
            seen_field[(sch or "?", field)].add(path)

    feed("Agent", api.request("GET", api._url(f"{api._base(project)}/agents/{agent}")), "Agent")
    for f in api.list_flows(agent, project):
        fid = f["name"].split("/")[-1]
        feed("Flow", api.get_flow(agent, fid, project), "Flow")
        for p in api.list_pages(agent, fid, project):
            pid = p["name"].split("/")[-1]
            feed("Page", api.get_page(agent, fid, pid, project), "Page")
    for pb in api.list_playbooks(agent, project):
        feed("Playbook", pb, "Playbook")
    for name, getter in (
        ("Intent", api.list_intents),
        ("EntityType", api.list_entity_types),
        ("Tool", api.list_tools),
        ("Webhook", api.list_webhooks),
        ("Generator", api.list_generators),
        ("TransitionRouteGroup", api.list_transition_route_groups),
    ):
        try:
            for item in getter(agent, project):
                feed(name, item, name)
        except Exception as exc:
            print(f"# {name}: no se pudo leer ({exc})")

    print(f"\n== {samples} muestras, {len(seen_field)} campos no declarados ==\n")
    for (schema, field), paths in sorted(seen_field.items()):
        ej = sorted(paths)[0]
        print(f"  {schema:<24}.{field:<36} p.ej. {ej}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
