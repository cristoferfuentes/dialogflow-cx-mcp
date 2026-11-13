#!/usr/bin/env python3
"""Sonda: que campos "fuera de esquema" se pueden ESCRIBIR de verdad.

Reglas:
- SOLO se escribe en el agente escribible (el clon).
- Cada prueba usa un recurso TEMPORAL (flow/pagina/tool/entityType/intent) que
  se borra al final, salvo los de nivel agente, que se revierten en el acto.
- Cada escritura se verifica releyendo y se revierte.
"""
from __future__ import annotations

import atexit
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi

RESULTADOS: list[tuple[str, bool, str]] = []


def main() -> int:
    cfg = load_config()
    api = ConsoleApi(cfg)
    project = cfg.project
    agent = cfg.default_agent
    base = f"projects/{project}/locations/global/agents/{agent}"

    def get(name):
        return api.request("GET", api._url("/" + name))

    def patch(name, body, mask):
        return api.request("PATCH", api._url("/" + name, updateMask=mask), body)

    def post(path, body):
        return api.request("POST", api._url(path), body)

    def borrar(path):
        return api.request("DELETE", api._url(path))

    def leer_en(ruta, obj):
        cur = obj
        for parte in ruta.split("."):
            if isinstance(cur, list):
                cur = cur[0] if cur else None
            if not isinstance(cur, dict):
                return None
            cur = cur.get(parte)
        return cur

    def probar(etiqueta, nombre, mutador, mascara, ruta, valor, revertir=True):
        try:
            before = get(nombre)
            copia = copy.deepcopy(before)
            mutador(copia, valor)
            respuesta = patch(nombre, copia, mascara)
            after = get(nombre)
            leido = leer_en(ruta, after)
            ok = leido == valor
            RESULTADOS.append((etiqueta, ok, json.dumps(leido, ensure_ascii=False)[:120]))
            print(f"  [{'OK ' if ok else 'NO '}] {etiqueta}  -> leido={json.dumps(leido, ensure_ascii=False)[:100]}")
            if not ok:
                print("        respuesta:", json.dumps(respuesta, ensure_ascii=False)[:220])
            if revertir:
                patch(nombre, before, mascara)
        except Exception as exc:
            RESULTADOS.append((etiqueta, False, f"{type(exc).__name__}: {exc}"))
            print(f"  [ERR] {etiqueta}: {str(exc)[:220]}")

    # ---------------------------------------------------------------- temporales
    flow = post(f"{api._base(project)}/agents/{agent}/flows",
                {"displayName": "zz_tmp_gap_probe", "description": "temporal"})
    flow_name = flow.get("name", "")
    flow_id = flow_name.rsplit("/", 1)[-1]
    if not flow_id:
        print("no se pudo crear el flow temporal:", flow)
        return 1
    atexit.register(lambda: borrar(f"{api._base(project)}/agents/{agent}/flows/{flow_id}"))

    page = post(f"{api._base(project)}/agents/{agent}/flows/{flow_id}/pages",
                {"displayName": "Pagina probe",
                 "entryFulfillment": {"messages": [{"text": {"text": ["hola"]}}]}})
    page_name = page.get("name", "")
    print("flow:", flow_id, "| pagina:", page_name.rsplit("/", 1)[-1])

    # un playbook real (para probar targetPlaybook)
    pbs = api.list_playbooks(agent, project)
    pb_ref = ""
    if pbs:
        pb_id = (pbs[0].get("name") or "").rsplit("/", 1)[-1]
        pb_ref = f"{base}/playbooks/{pb_id}"

    # un entity type real (para el formulario)
    ets = api.list_entity_types(agent, project)
    et_ref = ""
    if ets:
        et_ref = (ets[0].get("name") or "").split("/entityTypes/")[-1]
        et_ref = f"{base}/entityTypes/{et_ref}"

    print("\n== PAGINA ==")

    def set_adv(d, v):
        d.setdefault("advancedSettings", {})["enabled"] = v

    probar("Page.advancedSettings.enabled", page_name, set_adv, "advancedSettings",
           "advancedSettings.enabled", False)

    def set_playback(d, v):
        d.setdefault("advancedSettings", {})["playbackInterruptionSettings"] = v

    probar("Page.advancedSettings.playbackInterruptionSettings", page_name, set_playback,
           "advancedSettings", "advancedSettings.playbackInterruptionSettings",
           {"allowPlaybackInterruption": False})

    def set_speechadapt(d, v):
        d.setdefault("advancedSettings", {})["speechAdaptationSettings"] = v

    probar("Page.advancedSettings.speechAdaptationSettings", page_name, set_speechadapt,
           "advancedSettings", "advancedSettings.speechAdaptationSettings", {})

    def set_smartendpoint(d, v):
        d.setdefault("advancedSettings", {}).setdefault("speechSettings", {})["enableSmartEndpointing"] = v

    probar("Page.advancedSettings.speechSettings.enableSmartEndpointing", page_name,
           set_smartendpoint, "advancedSettings",
           "advancedSettings.speechSettings.enableSmartEndpointing", False)

    def set_speaker(d, v):
        d["speakerIdSettings"] = v

    probar("Page.speakerIdSettings", page_name, set_speaker, "speakerIdSettings",
           "speakerIdSettings", {})

    def set_form_desc(d, v):
        params = d.setdefault("form", {}).setdefault("parameters", [])
        if not params:
            params.append({"displayName": "p1", "entityType": et_ref})
        params[0]["description"] = v

    if et_ref:
        probar("Page.form.parameters[0].description", page_name, set_form_desc, "form",
               "form.parameters.description", "descripcion de prueba")

    def set_route_tp(d, v):
        rutas = d.setdefault("transitionRoutes", [])
        if not rutas:
            rutas.append({"condition": "true"})
        rutas[0]["targetPlaybook"] = v

    if pb_ref:
        probar("Page.transitionRoutes[0].targetPlaybook", page_name, set_route_tp,
               "transitionRoutes", "transitionRoutes.targetPlaybook", pb_ref)

    print("\n== FLOW ==")

    def set_nlu(d, v):
        d.setdefault("nluSettings", {})["customConfigKey"] = v

    probar("Flow.nluSettings.customConfigKey", flow_name, set_nlu, "nluSettings",
           "nluSettings.customConfigKey", "mi-clave-propia")

    def set_modelid(d, v):
        d["modelIdentifier"] = v

    probar("Flow.modelIdentifier", flow_name, set_modelid, "modelIdentifier",
           "modelIdentifier", {"modelId": "11111111-2222-3333-4444-555555555555"})

    print("\n== TOOL (temporal) ==")
    tool = post(f"{api._base(project)}/agents/{agent}/tools",
                {"displayName": "zz_tmp_tool_probe",
                 "description": "temporal",
                 "openApiSpec": {"textSchema": '{"type":"object"}'}})
    tool_name = tool.get("name", "")
    if tool_name:
        atexit.register(lambda: borrar(f"{api._base(project)}/agents/{agent}/tools/"
                                       + tool_name.rsplit("/", 1)[-1]))

        def set_actionschemas(d, v):
            d["actionSchemas"] = v

        probar("Tool.actionSchemas", tool_name, set_actionschemas, "actionSchemas",
               "actionSchemas", [{"name": "probe",
                                  "inputParameters": [{"name": "q", "schema": "{\"type\":\"string\"}"}],
                                  "outputParameters": []}])
    else:
        print("  no se pudo crear el tool temporal:", json.dumps(tool)[:200])

    print("\n== ENTITY TYPE (temporal) ==")
    et = post(f"{api._base(project)}/agents/{agent}/entityTypes",
              {"displayName": "zz_tmp_et_probe",
               "entities": [{"value": "uno", "synonyms": ["uno"], "dtmfPattern": "1"}]})
    et_name = et.get("name", "")
    if et_name:
        atexit.register(lambda: borrar(f"{api._base(project)}/agents/{agent}/entityTypes/"
                                       + et_name.rsplit("/", 1)[-1]))

        def set_dtmf(d, v):
            d["entities"][0]["dtmfPattern"] = v

        probar("EntityType.entities[0].dtmfPattern", et_name, set_dtmf, "entities",
               "entities.dtmfPattern", "9")
    else:
        print("  no se pudo crear el entityType temporal:", json.dumps(et)[:200])

    print("\n== INTENT (temporal) ==")
    intent = post(f"{api._base(project)}/agents/{agent}/intents",
                  {"displayName": "zz_tmp_intent_probe",
                   "trainingPhrases": [{"parts": [{"text": "hola"}], "repeatCount": 1}]})
    intent_name = intent.get("name", "")
    if intent_name:
        atexit.register(lambda: borrar(f"{api._base(project)}/agents/{agent}/intents/"
                                       + intent_name.rsplit("/", 1)[-1]))

        def set_auto(d, v):
            d["trainingPhrases"][0]["parts"][0]["auto"] = v

        probar("Intent.trainingPhrases[0].parts[0].auto", intent_name, set_auto,
               "trainingPhrases", "trainingPhrases.parts.auto", False)
    else:
        print("  no se pudo crear el intent temporal:", json.dumps(intent)[:200])

    print("\n== AGENTE (se revierte en el acto) ==")
    agent_name = base

    def set_webclient(d, v):
        d["webClientSettings"] = v

    probar("Agent.webClientSettings.boxBorderRadius", agent_name, set_webclient,
           "webClientSettings", "webClientSettings.boxBorderRadius", "8")

    def set_agent_adv(d, v):
        d.setdefault("advancedSettings", {})["enabled"] = v

    probar("Agent.advancedSettings.enabled", agent_name, set_agent_adv, "advancedSettings",
           "advancedSettings.enabled", True)

    print("\n== RESUMEN ==")
    for etiqueta, ok, det in RESULTADOS:
        print(f"  {'OK ' if ok else 'NO '} {etiqueta:<58} {det}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
