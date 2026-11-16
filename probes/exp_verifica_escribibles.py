#!/usr/bin/env python3
"""WRITE PARITY: which internal fields the backend really accepts.

The console's internal proto (extracted from its bundle, see
`probes/exp_proto_interno_bundle.py`) declares fields that the public v3beta1
discovery doc does NOT declare: `FormParameter.description`,
`IntentParameter.description`, `ResponseMessageText.ssml`,
`AdvancedSettingsLoggingSettings.disableLogging`, ...

That the backend KNOWS them does not mean that it ACCEPTS them: the public proto
silently drops what it does not declare. The only proof is to **write and
re-read**.

This script does that in a **temporary lab agent** (it does not touch the
working agent) and deletes everything at the end.

For each field: it is written, re-read and checked whether it survives.

    .venv/bin/python probes/exp_verifica_escribibles.py [--json /tmp/escribibles.json]
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
PRUEBAS = [
    ("loggingSettings.disableLogging", "Agent",
     "advancedSettings.loggingSettings.disableLogging", True),
    ("FormParameter.description", "Page",
     "form.parameters[0].description", "descripcion interna"),
    ("FormParameter.convertPhoneticAlphabets", "Page",
     "form.parameters[0].convertPhoneticAlphabets", True),
    ("IntentParameter.description", "Intent",
     "parameters[0].description", "descripcion del parametro"),
    ("ResponseMessageText.ssml", "Page",
     "entryFulfillment.messages[0].text.ssml", "<speak>hola</speak>"),
]


def leer(cxp, ruta: str, valor: Any, datos: Any) -> Any:
    """Reads `ruta` (notation `a.b[0].c`) inside `datos`."""
    from cxp.gaps import read_path
    return read_path(datos, ruta)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proyecto", default="")
    ap.add_argument("--json", default="/tmp/escribibles.json")
    args = ap.parse_args()

    cfg = load_config()
    proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    base = f"/projects/{proyecto}/locations/{cfg.location}/agents"
    res: dict = {"proyecto": proyecto, "pruebas": [], "agente": ""}

    # ---------------------------------------------------- lab agent
    creado = cons.request("POST", cons._url(base), body={
        "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
        "defaultLanguageCode": "es", "timeZone": "America/Santiago",
        "description": "temporal: paridad de escritura",
    })
    agente = creado.get("name") or ""
    if not agente:
        print("the agent could not be created:", json.dumps(creado,
                                                         ensure_ascii=False)[:300])
        return 1
    res["agente"] = agente
    cfg.agent_readwrite = list(cfg.agent_readwrite) + [agente.rsplit("/", 1)[-1]]
    abase = base + "/" + agente.rsplit("/", 1)[-1]
    print("lab agent:", agente)

    try:
        # An entity type OWN to the lab agent (the system one cannot be listed
        # with this filter and the parameter requires it).
        entity_type = ""
        try:
            creado_et = cons.request("POST", cons._url(f"{abase}/entityTypes"), body={
                "displayName": "lab_tipo", "kind": "KIND_MAP",
                "entities": [{"value": "uno", "synonyms": ["uno", "1"]}]})
            entity_type = creado_et.get("name") or ""
        except Exception as exc:
            print("the entity type could not be created:", str(exc)[:150])
        print("entity type:", entity_type or "(none)")

        # ------------------------------------------- 1) agent: disableLogging
        print("\n== Agent.advancedSettings.loggingSettings.disableLogging ==")
        cuerpo = {"advancedSettings": {"loggingSettings": {"disableLogging": True}}}
        try:
            cons.request("PATCH", cons._url(f"{abase}"), body=cuerpo,
                         update_mask="advancedSettings")
            leido = cons.request("GET", cons._url(f"{abase}"))
            valor = leer("x", "advancedSettings.loggingSettings.disableLogging",
                         True, leido)
            print("   -> after the PATCH:", json.dumps(leido.get("advancedSettings"),
                                                      ensure_ascii=False)[:200])
            print("   -> disableLogging kept:", valor is True)
            res["pruebas"].append({
                "campo": "Agent.advancedSettings.loggingSettings.disableLogging",
                "esquema": "Agent", "sobrevive": valor is True, "valor": valor})
        except Exception as exc:
            print("   ERROR:", str(exc)[:200])
            res["pruebas"].append({"campo": "disableLogging", "error": str(exc)[:200]})

        # ------------------------------------------ 2) intent: description
        print("\n== Intent.parameters[0].description ==")
        try:
            cuerpo = {"displayName": "lab_intent",
                      "parameters": [{"id": "lab_param", "entityType": entity_type,
                                      "description": "descripcion del parametro"}],
                      "trainingPhrases": []}
            creado_i = cons.request("POST", cons._url(f"{abase}/intents"), body=cuerpo)
            nombre_i = creado_i.get("name") or ""
            print("   intent created:", bool(nombre_i))
            if nombre_i:
                leido = cons.request("GET", cons._url("/" + nombre_i))
                valor = leer("x", "parameters[0].description", None, leido)
                print("   -> parameters:", json.dumps(leido.get("parameters"),
                                                      ensure_ascii=False)[:200])
                print("   -> description kept:", valor is not None)
                res["pruebas"].append({
                    "campo": "Intent.parameters[0].description",
                    "esquema": "IntentParameter",
                    "sobrevive": valor is not None, "valor": valor})
        except Exception as exc:
            print("   ERROR:", str(exc)[:250])
            res["pruebas"].append({"campo": "IntentParameter.description",
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
            cuerpo = {
                "displayName": "lab_page",
                "entryPoints": [{"languageCode": "es", "name": "lab.entry"}],
                "form": {"parameters": [
                    {"displayName": "lab_param", "name": "lab_param",
                     "entityType": entity_type, "required": True,
                     "description": "descripcion interna",
                     "convertPhoneticAlphabets": True}],
                    "fillBehavior": "FILL_BEHAVIOR_UNSPECIFIED"},
                "entryFulfillment": {"messages": [
                    {"channel": "CHANNEL_UNSPECIFIED",
                     "text": {"text": ["hola"], "ssml": "<speak>hola</speak>"}}]},
                "transitionRoutes": [], "eventHandlers": [],
            }
            creado_p = cons.request("POST",
                                    cons._url(f"{abase}/flows/{fid}/pages"),
                                    body=cuerpo)
            nombre_p = creado_p.get("name") or ""
            print("   page created:", bool(nombre_p))
            if nombre_p:
                leido = cons.request("GET", cons._url("/" + nombre_p))
                desc = leer("x", "form.parameters[0].description", None, leido)
                conv = leer("x", "form.parameters[0].convertPhoneticAlphabets",
                            None, leido)
                ssml = leer("x", "entryFulfillment.messages[0].text.ssml",
                            None, leido)
                print("   -> form.parameters:", json.dumps(
                    (leido.get("form") or {}).get("parameters"),
                    ensure_ascii=False)[:220])
                print("   -> entryFulfillment:", json.dumps(
                    leido.get("entryFulfillment"), ensure_ascii=False)[:220])
                for campo, valor in (("FormParameter.description", desc),
                                     ("FormParameter.convertPhoneticAlphabets", conv),
                                     ("ResponseMessageText.ssml", ssml)):
                    print(f"   -> {campo}: {'KEPT' if valor is not None else 'dropped'}")
                    res["pruebas"].append({
                        "campo": campo, "esquema": campo.split(".")[0],
                        "sobrevive": valor is not None, "valor": valor})
        except Exception as exc:
            print("   ERROR:", str(exc)[:250])
            res["pruebas"].append({"campo": "Page.form/entryFulfillment",
                                   "error": str(exc)[:250]})
    finally:
        try:
            cons.request("DELETE", cons._url("/" + agente))
            print("\nlab agent deleted")
        except Exception as exc:
            print("Could NOT delete the agent:", str(exc)[:200])

    print("\n== SUMMARY ==")
    for p in res["pruebas"]:
        if "error" in p:
            print(f"  {p['campo']:<52} ERROR")
        else:
            print(f"  {p['campo']:<52} "
                  f"{'KEPT' if p.get('sobrevive') else 'DROPPED'}")
    Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    print(f"-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
