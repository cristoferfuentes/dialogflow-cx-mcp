#!/usr/bin/env python3
"""Dumps the REAL shape of the fields that the public v3beta1 schema does not declare."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi

INTERESAN = {
    "Agent": ["speakerIdSettings", "webClientSettings", "projectNumber"],
    "AdvancedSettings": ["enabled", "playbackInterruptionSettings", "speechAdaptationSettings"],
    "AdvancedSettingsSpeechSettings": ["enableSmartEndpointing"],
    "Page": ["speakerIdSettings", "advancedSettings"],
    "Flow": ["modelIdentifier", "nluSettingsList", "nluSettings"],
    "Playbook": ["speechAdaptationSettings"],
    "Tool": ["actionSchemas", "sharedSchemas"],
    "EntityTypeEntity": ["dtmfPattern"],
    "FormParameter": ["description"],
    "IntentTrainingPhrasePart": ["auto"],
    "ToolDataStoreTool": ["models", "groundingSettings"],
    "TransitionRoute": ["targetPlaybook"],
    "DataStoreConnection": ["connectorConfig", "engineType", "displayName"],
}


def main() -> int:
    cfg = load_config()
    api = ConsoleApi(cfg)
    agent = cfg.only_agent()
    project = cfg.project
    base = api._base(project)

    def ver(etiqueta, obj, esquema):
        print(f"\n### {etiqueta} ({esquema})")
        for campo in INTERESAN.get(esquema, []):
            valor = obj.get(campo, "<MISSING>")
            print(f"   {campo}: {json.dumps(valor, ensure_ascii=False)[:400]}")

    ag = api.request("GET", api._url(f"{base}/agents/{agent}"))
    ver("Agent", ag, "Agent")
    ver("Agent.advancedSettings", ag.get("advancedSettings") or {}, "AdvancedSettings")
    ver("Agent.advancedSettings.speechSettings",
        (ag.get("advancedSettings") or {}).get("speechSettings") or {}, "AdvancedSettingsSpeechSettings")

    for f in api.list_flows(agent, project):
        fid = f["name"].rsplit("/", 1)[-1]
        fl = api.get_flow(agent, fid, project)
        ver(f"Flow {fid}", fl, "Flow")
        ver(f"Flow.nluSettings {fid}", fl.get("nluSettings") or {}, "NluSettings")
        pages = api.list_pages(agent, fid, project)
        if pages:
            pid = pages[0]["name"].rsplit("/", 1)[-1]
            pg = api.get_page(agent, fid, pid, project)
            ver(f"Page {pid}", pg, "Page")
            ver(f"Page.advancedSettings {pid}", pg.get("advancedSettings") or {}, "AdvancedSettings")
            rutas = pg.get("transitionRoutes") or []
            if rutas:
                ver(f"Page.route[0] {pid}", rutas[0], "TransitionRoute")
            form = pg.get("form") or {}
            for par in form.get("parameters") or []:
                ver("Page.form.parameters[0]", par, "FormParameter")
                break
        break

    for pb in api.list_playbooks(agent, project):
        ver(f"Playbook {pb.get('displayName')}", pb, "Playbook")
        break

    for t in api.list_tools(agent, project):
        ver(f"Tool {t.get('displayName')}", t, "Tool")
        break

    for e in api.list_entity_types(agent, project):
        ver(f"EntityType {e.get('displayName')}", e, "EntityType")
        for ent in (e.get("entities") or [])[:2]:
            ver("EntityType.entities[0]", ent, "EntityTypeEntity")
        break

    for i in api.list_intents(agent, project):
        ver(f"Intent {i.get('displayName')}", i, "Intent")
        for fr in (i.get("trainingPhrases") or [])[:1]:
            for part in (fr.get("parts") or [])[:2]:
                ver("Intent.trainingPhrases[0].parts[0]", part, "IntentTrainingPhrasePart")
        break

    for g in api.list_generators(agent, project):
        ver(f"Generator {g.get('displayName')}", g, "Generator")
        break

    for rg in api.list_transition_route_groups(agent, project):
        ver(f"TransitionRouteGroup {rg.get('displayName')}", rg, "TransitionRouteGroup")
        for r in (rg.get("transitionRoutes") or [])[:1]:
            ver("TransitionRouteGroup.transitionRoutes[0]", r, "TransitionRoute")
        break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
