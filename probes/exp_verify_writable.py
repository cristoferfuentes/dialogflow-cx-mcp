#!/usr/bin/env python3
"""WRITE PARITY: which internal fields the backend really accepts.

The console's internal proto (extracted from its bundle, see
`probes/exp_internal_proto_bundle.py`) declares fields that the public v3beta1
discovery doc does NOT declare: `FormParameter.description`,
`IntentParameter.description`, `ResponseMessageText.ssml`,
`AdvancedSettingsLoggingSettings.disableLogging`, ...

That the backend KNOWS them does not mean that it ACCEPTS them: the public proto
silently drops what it does not declare. The only proof is to **write and
re-read**.

This script does that in a **temporary lab agent** (it does not touch the
working agent) and deletes everything at the end.

For each field: it is written, re-read and checked whether it survives.

    .venv/bin/python probes/exp_verify_writable.py [--json /tmp/writable.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi

# (experiment id, schema, path of the field inside the resource, value)
TESTS = [
    ("loggingSettings.disableLogging", "Agent",
     "advancedSettings.loggingSettings.disableLogging", True),
    ("FormParameter.description", "Page",
     "form.parameters[0].description", "internal description"),
    ("FormParameter.convertPhoneticAlphabets", "Page",
     "form.parameters[0].convertPhoneticAlphabets", True),
    ("IntentParameter.description", "Intent",
     "parameters[0].description", "parameter description"),
    ("ResponseMessageText.ssml", "Page",
     "entryFulfillment.messages[0].text.ssml", "<speak>hola</speak>"),
]


def read_value(cxp, route: str, value: Any, data: Any) -> Any:
    """Reads `route` (notation `a.b[0].c`) inside `data`."""
    from cxp.gaps import read_path
    return read_path(data, route)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--json", default="/tmp/writable.json")
    args = ap.parse_args()

    cfg = load_config()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    base = f"/projects/{project}/locations/{cfg.location}/agents"
    res: dict = {"project": project, "tests": [], "agent": ""}

    # ---------------------------------------------------- lab agent
    created = cons.request("POST", cons._url(base), body={
        "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
        "defaultLanguageCode": "es", "timeZone": "America/Santiago",
        "description": "temporary: write parity",
    })
    agent = created.get("name") or ""
    if not agent:
        print("the agent could not be created:", json.dumps(created,
                                                         ensure_ascii=False)[:300])
        return 1
    res["agent"] = agent
    cfg.agent_readwrite = list(cfg.agent_readwrite) + [agent.rsplit("/", 1)[-1]]
    abase = base + "/" + agent.rsplit("/", 1)[-1]
    print("lab agent:", agent)

    try:
        # An entity type OWN to the lab agent (the system one cannot be listed
        # with this filter and the parameter requires it).
        entity_type = ""
        try:
            created_entity_type = cons.request(
                "POST", cons._url(f"{abase}/entityTypes"),
                body={"displayName": "lab_type", "kind": "KIND_MAP",
                      "entities": [{"value": "uno", "synonyms": ["uno", "1"]}]})
            entity_type = created_entity_type.get("name") or ""
        except Exception as exc:
            print("the entity type could not be created:", str(exc)[:150])
        print("entity type:", entity_type or "(none)")

        # ------------------------------------------- 1) agent: disableLogging
        print("\n== Agent.advancedSettings.loggingSettings.disableLogging ==")
        body = {"advancedSettings": {"loggingSettings": {"disableLogging": True}}}
        try:
            cons.request("PATCH", cons._url(f"{abase}"), body=body,
                         update_mask="advancedSettings")
            read = cons.request("GET", cons._url(f"{abase}"))
            value = read_value("x",
                               "advancedSettings.loggingSettings.disableLogging",
                               True, read)
            print("   -> after the PATCH:",
                  json.dumps(read.get("advancedSettings"),
                             ensure_ascii=False)[:200])
            print("   -> disableLogging kept:", value is True)
            res["tests"].append({
                "field": "Agent.advancedSettings.loggingSettings.disableLogging",
                "schema": "Agent", "survives": value is True, "value": value})
        except Exception as exc:
            print("   ERROR:", str(exc)[:200])
            res["tests"].append({"field": "disableLogging", "error": str(exc)[:200]})

        # ------------------------------------------ 2) intent: description
        print("\n== Intent.parameters[0].description ==")
        try:
            body = {"displayName": "lab_intent",
                      "parameters": [{"id": "lab_param", "entityType": entity_type,
                                      "description": "parameter description"}],
                      "trainingPhrases": []}
            created_intent = cons.request("POST", cons._url(f"{abase}/intents"),
                                        body=body)
            intent_name = created_intent.get("name") or ""
            print("   intent created:", bool(intent_name))
            if intent_name:
                read = cons.request("GET", cons._url("/" + intent_name))
                value = read_value("x", "parameters[0].description", None, read)
                print("   -> parameters:",
                      json.dumps(read.get("parameters"),
                                 ensure_ascii=False)[:200])
                print("   -> description kept:", value is not None)
                res["tests"].append({
                    "field": "Intent.parameters[0].description",
                    "schema": "IntentParameter",
                    "survives": value is not None, "value": value})
        except Exception as exc:
            print("   ERROR:", str(exc)[:250])
            res["tests"].append({"field": "IntentParameter.description",
                                   "error": str(exc)[:250]})

        # ---------------------------- 3) page: form parameter + ssml
        print("\n== Page.form.parameters[0].description / "
              ".convertPhoneticAlphabets / entryFulfillment...ssml ==")
        try:
            f = cons.request("POST", cons._url(f"{abase}/flows"),
                             body={"displayName": "lab_flow",
                                   "transitionRoutes": [], "eventHandlers": []})
            flow = f.get("name") or ""
            fid = flow.rsplit("/", 1)[-1]
            body = {
                "displayName": "lab_page",
                "entryPoints": [{"languageCode": "es", "name": "lab.entry"}],
                "form": {"parameters": [
                    {"displayName": "lab_param", "name": "lab_param",
                     "entityType": entity_type, "required": True,
                     "description": "internal description",
                     "convertPhoneticAlphabets": True}],
                    "fillBehavior": "FILL_BEHAVIOR_UNSPECIFIED"},
                "entryFulfillment": {"messages": [
                    {"channel": "CHANNEL_UNSPECIFIED",
                     "text": {"text": ["hola"], "ssml": "<speak>hola</speak>"}}]},
                "transitionRoutes": [], "eventHandlers": [],
            }
            created_page = cons.request("POST",
                                     cons._url(f"{abase}/flows/{fid}/pages"),
                                     body=body)
            page_name = created_page.get("name") or ""
            print("   page created:", bool(page_name))
            if page_name:
                read = cons.request("GET", cons._url("/" + page_name))
                desc = read_value("x", "form.parameters[0].description", None, read)
                conv = read_value("x",
                                  "form.parameters[0].convertPhoneticAlphabets",
                                  None, read)
                ssml = read_value("x",
                                  "entryFulfillment.messages[0].text.ssml",
                                  None, read)
                print("   -> form.parameters:", json.dumps(
                    (read.get("form") or {}).get("parameters"),
                    ensure_ascii=False)[:220])
                print("   -> entryFulfillment:", json.dumps(
                    read.get("entryFulfillment"), ensure_ascii=False)[:220])
                for field, value in (("FormParameter.description", desc),
                                     ("FormParameter.convertPhoneticAlphabets", conv),
                                     ("ResponseMessageText.ssml", ssml)):
                    print(f"   -> {field}: {'KEPT' if value is not None else 'dropped'}")
                    res["tests"].append({
                        "field": field, "schema": field.split(".")[0],
                        "survives": value is not None, "value": value})
        except Exception as exc:
            print("   ERROR:", str(exc)[:250])
            res["tests"].append({"field": "Page.form/entryFulfillment",
                                   "error": str(exc)[:250]})
    finally:
        try:
            cons.request("DELETE", cons._url("/" + agent))
            print("\nlab agent deleted")
        except Exception as exc:
            print("Could NOT delete the agent:", str(exc)[:200])

    print("\n== SUMMARY ==")
    for p in res["tests"]:
        if "error" in p:
            print(f"  {p['field']:<52} ERROR")
        else:
            print(f"  {p['field']:<52} "
                  f"{'KEPT' if p.get('survives') else 'DROPPED'}")
    Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    print(f"-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
