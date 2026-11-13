#!/usr/bin/env python3
"""Sonda 2: escritura de campos fuera de esquema en TOOL / ENTITYTYPE / INTENT / AGENTE.

Copia recursos existentes del clon (o crea temporales) y los borra al final.
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
    api_base = api._base(project)

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
            print(f"  [{'OK ' if ok else 'NO '}] {etiqueta} -> {json.dumps(leido, ensure_ascii=False)[:110]}")
            if not ok:
                print("        respuesta:", json.dumps(respuesta, ensure_ascii=False)[:260])
            if revertir:
                patch(nombre, before, mascara)
        except Exception as exc:
            RESULTADOS.append((etiqueta, False, f"{type(exc).__name__}: {exc}"))
            print(f"  [ERR] {etiqueta}: {str(exc)[:260]}")

    # ------------------------------------------------------------------ TOOL
    print("== TOOL ==")
    tools = api.list_tools(agent, project)
    origen = next((t for t in tools if t.get("openApiSpec")), None)
    if origen:
        cuerpo = {k: v for k, v in origen.items()
                  if k not in ("name", "displayName", "etag", "createTime", "updateTime")}
        cuerpo["displayName"] = "zz_tmp_tool_probe"
        cuerpo["description"] = "temporal - sonda de huecos"
        try:
            nuevo = post(f"{api_base}/agents/{agent}/tools", cuerpo)
            tool_name = nuevo.get("name", "")
            print("  tool temporal:", tool_name.rsplit("/", 1)[-1])
            if tool_name:
                atexit.register(lambda: borrar(
                    f"{api_base}/agents/{agent}/tools/" + tool_name.rsplit("/", 1)[-1]))

                def set_actionschemas(d, v):
                    d["actionSchemas"] = v

                esquemas = [{"name": "probe_accion",
                             "inputParameters": [{"name": "q",
                                                  "schema": '{"type":"string"}'}],
                             "outputParameters": []}]
                probar("Tool.actionSchemas", tool_name, set_actionschemas, "actionSchemas",
                       "actionSchemas", esquemas)
        except Exception as exc:
            print("  [ERR] no se pudo crear el tool temporal:", str(exc)[:300])

    # ------------------------------------------------------------ ENTITY TYPE
    print("\n== ENTITY TYPE ==")
    try:
        et = post(f"{api_base}/agents/{agent}/entityTypes",
                  {"displayName": "zz_tmp_et_probe",
                   "entities": [{"value": "uno", "synonyms": ["uno"], "dtmfPattern": "1"}]})
        et_name = et.get("name", "")
        print("  entityType temporal:", et_name.rsplit("/", 1)[-1])

        def set_dtmf(d, v):
            d["entities"][0]["dtmfPattern"] = v

        if et_name:
            atexit.register(lambda: borrar(
                f"{api_base}/agents/{agent}/entityTypes/" + et_name.rsplit("/", 1)[-1]))
            probar("EntityType.entities[0].dtmfPattern", et_name, set_dtmf, "entities",
                   "entities.dtmfPattern", "9")
    except Exception as exc:
        print("  [ERR] entityType:", str(exc)[:300])

    # ---------------------------------------------------------------- INTENT
    print("\n== INTENT ==")
    try:
        intent = post(f"{api_base}/agents/{agent}/intents",
                      {"displayName": "zz_tmp_intent_probe",
                       "trainingPhrases": [{"parts": [{"text": "hola"}], "repeatCount": 1}]})
        intent_name = intent.get("name", "")
        print("  intent temporal:", intent_name.rsplit("/", 1)[-1])

        def set_auto(d, v):
            d["trainingPhrases"][0]["parts"][0]["auto"] = v

        if intent_name:
            atexit.register(lambda: borrar(
                f"{api_base}/agents/{agent}/intents/" + intent_name.rsplit("/", 1)[-1]))
            probar("Intent.trainingPhrases[0].parts[0].auto", intent_name, set_auto,
                   "trainingPhrases", "trainingPhrases.parts.auto", False)
    except Exception as exc:
        print("  [ERR] intent:", str(exc)[:300])

    # ---------------------------------------------------------------- AGENTE
    print("\n== AGENTE (se revierte en el acto) ==")

    def set_webclient(d, v):
        d["webClientSettings"] = v

    probar("Agent.webClientSettings.boxBorderRadius", base, set_webclient,
           "webClientSettings", "webClientSettings.boxBorderRadius", "8")

    def set_playback(d, v):
        d.setdefault("advancedSettings", {})["playbackInterruptionSettings"] = v

    probar("Agent.advancedSettings.playbackInterruptionSettings", base, set_playback,
           "advancedSettings", "advancedSettings.playbackInterruptionSettings",
           {"allowPlaybackInterruption": False})

    def set_smartendpoint(d, v):
        d.setdefault("advancedSettings", {}).setdefault("speechSettings", {})["enableSmartEndpointing"] = v

    probar("Agent.advancedSettings.speechSettings.enableSmartEndpointing", base,
           set_smartendpoint, "advancedSettings",
           "advancedSettings.speechSettings.enableSmartEndpointing", False)

    print("\n== RESUMEN ==")
    for etiqueta, ok, det in RESULTADOS:
        print(f"  {'OK ' if ok else 'NO '} {etiqueta:<58} {det}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
