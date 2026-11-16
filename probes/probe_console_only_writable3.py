#!/usr/bin/env python3
"""Probe 3 (v2): write matrix of the out of schema fields.

It uses the production code (`cxp.console_fields.set_field`). Everything is done on
TEMPORARY resources or with an immediate revert. At the end it prints the matrix
(writable / not writable) that feeds the MCP catalog.
"""
from __future__ import annotations

import atexit
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi
from cxp.console_fields import set_field
from cxp.gaps import read_path

MATRIZ: list[tuple[str, bool, str]] = []


def main() -> int:
    cfg = load_config()
    api = ConsoleApi(cfg)
    project = cfg.project
    agent = cfg.default_agent
    base = f"projects/{project}/locations/global/agents/{agent}"
    ab = api._base(project)

    def probar(etiqueta, recurso, campo, valor, revertir=True, ruta_lectura=None):
        """`ruta_lectura` = where to check the value after writing."""
        try:
            res = set_field(api, recurso, campo, valor, dry_run=False, project=project)
            if ruta_lectura:
                actual = api.request("GET", api._url("/" + recurso))
                res.despues = read_path(actual, ruta_lectura)
                res.verificado = res.despues == valor
            ok = res.verificado
            MATRIZ.append((etiqueta, ok, json.dumps(res.despues, ensure_ascii=False)[:100]))
            print(f"  [{'OK ' if ok else 'NO '}] {etiqueta}")
            if not ok:
                print(f"        read: {json.dumps(res.despues, ensure_ascii=False)[:150]}")
                if res.error:
                    print(f"        error: {res.error[:200]}")
            if revertir and res.escrito:
                set_field(api, recurso, campo, res.antes, dry_run=False, project=project)
            return res
        except Exception as exc:
            MATRIZ.append((etiqueta, False, f"{type(exc).__name__}: {exc}"))
            print(f"  [ERR] {etiqueta}: {str(exc)[:220]}")
            return None

    # ------------------------------------------------------------ temporary
    flow = api.request("POST", api._url(f"{ab}/agents/{agent}/flows"),
                       {"displayName": "zz_tmp_gap_probe", "description": "temporal"})
    flow_id = flow["name"].rsplit("/", 1)[-1]
    atexit.register(lambda: api.request(
        "DELETE", api._url(f"{ab}/agents/{agent}/flows/{flow_id}")))
    page = api.request("POST", api._url(f"{ab}/agents/{agent}/flows/{flow_id}/pages"),
                       {"displayName": "Pagina probe",
                        "entryFulfillment": {"messages": [{"text": {"text": ["hola"]}}]}})
    page_name = page.get("name", "")

    pbs = api.list_playbooks(agent, project)
    pb_ref = f"{base}/playbooks/" + (pbs[0].get("name") or "").rsplit("/", 1)[-1]
    ets = api.list_entity_types(agent, project)
    et_ref = f"{base}/entityTypes/" + (ets[0].get("name") or "").rsplit("/", 1)[-1]
    print(f"flow={flow_id} page={page_name.rsplit('/',1)[-1]}")

    print("\n== PAGE ==")
    probar("Page.transitionRoutes (full array with targetPlaybook)", page_name,
           "transitionRoutes", [{"condition": "true", "targetPlaybook": pb_ref}],
           revertir=False,
           ruta_lectura="transitionRoutes[0].targetPlaybook")

    # existing route -> change only the target
    probar("Page.transitionRoutes[0].targetPlaybook (existing route)", page_name,
           "transitionRoutes[0].targetPlaybook", pb_ref, revertir=False,
           ruta_lectura="transitionRoutes[0].targetPlaybook")

    probar("Page.form.parameters[0].description (full array)", page_name, "form",
           {"parameters": [{"displayName": "p1", "entityType": et_ref,
                            "description": "descripcion de prueba"}]},
           revertir=False, ruta_lectura="form.parameters[0].description")

    probar("Page.speakerIdSettings", page_name, "speakerIdSettings",
           {"enableSpeakerId": True})
    probar("Page.advancedSettings.speechAdaptationSettings", page_name,
           "advancedSettings", {"speechAdaptationSettings": {"adaptation": True}})
    probar("Page.advancedSettings.enabled", page_name, "advancedSettings", {"enabled": False})

    print("\n== TOOL ==")
    tools = api.list_tools(agent, project)
    origen = next((t for t in tools if t.get("openApiSpec")), None)
    if origen:
        cuerpo = {k: v for k, v in origen.items()
                  if k not in ("name", "displayName", "etag", "createTime", "updateTime",
                               "actionSchemas", "sharedSchemas")}
        cuerpo["displayName"] = "zz_tmp_tool_probe"
        cuerpo["description"] = "temporal"
        try:
            nuevo = api.request("POST", api._url(f"{ab}/agents/{agent}/tools"), cuerpo)
            tool_name = nuevo.get("name", "")
            atexit.register(lambda: api.request(
                "DELETE", api._url(f"{ab}/agents/{agent}/tools/"
                                   + tool_name.rsplit("/", 1)[-1])))
            esquemas = [{"name": "probe_accion",
                         "inputParameters": [{"name": "q", "schema": '{"type":"string"}'}],
                         "outputParameters": []}]
            probar("Tool.actionSchemas (tool with openApiSpec)", tool_name,
                   "actionSchemas", esquemas)
        except Exception as exc:
            print("  [ERR] temporary tool:", str(exc)[:240])

    # data store tool (without openApiSpec): the summarization model
    origen_ds = next((t for t in tools if t.get("dataStoreSpec")), None)
    if origen_ds:
        cuerpo = {k: v for k, v in origen_ds.items()
                  if k not in ("name", "displayName", "etag", "createTime", "updateTime",
                               "actionSchemas", "sharedSchemas")}
        cuerpo["displayName"] = "zz_tmp_ds_probe"
        try:
            nuevo = api.request("POST", api._url(f"{ab}/agents/{agent}/tools"), cuerpo)
            ds_name = nuevo.get("name", "")
            atexit.register(lambda: api.request(
                "DELETE", api._url(f"{ab}/agents/{agent}/tools/"
                                   + ds_name.rsplit("/", 1)[-1])))
            ds = dict(cuerpo.get("dataStoreSpec") or {})
            ds["models"] = {"summarizationModel": {"model": "default"}}
            probar("Tool.dataStoreSpec.models.summarizationModel.model", ds_name,
                   "dataStoreSpec", ds, revertir=False,
                   ruta_lectura="dataStoreSpec.models.summarizationModel.model")
        except Exception as exc:
            print("  [ERR] temporary ds tool:", str(exc)[:240])

    print("\n== ENTITY TYPE ==")
    try:
        et = api.request("POST", api._url(f"{ab}/agents/{agent}/entityTypes"),
                         {"displayName": "zz_tmp_et_probe", "kind": "KIND_MAP",
                          "entities": [{"value": "uno", "synonyms": ["uno"],
                                        "dtmfPattern": "1"}]})
        et_name = et.get("name", "")
        atexit.register(lambda: api.request(
            "DELETE", api._url(f"{ab}/agents/{agent}/entityTypes/"
                               + et_name.rsplit("/", 1)[-1])))
        probar("EntityType.entities[0].dtmfPattern", et_name,
               "entities[0].dtmfPattern", "9")
    except Exception as exc:
        print("  [ERR] entityType:", str(exc)[:240])

    print("\n== INTENT ==")
    try:
        intent = api.request("POST", api._url(f"{ab}/agents/{agent}/intents"),
                             {"displayName": "zz_tmp_intent_probe",
                              "trainingPhrases": [{"parts": [{"text": "hola"}],
                                                   "repeatCount": 1}]})
        intent_name = intent.get("name", "")
        atexit.register(lambda: api.request(
            "DELETE", api._url(f"{ab}/agents/{agent}/intents/"
                               + intent_name.rsplit("/", 1)[-1])))
        probar("Intent.trainingPhrases (full array with auto=False)", intent_name,
               "trainingPhrases",
               [{"parts": [{"text": "hola", "auto": False}], "repeatCount": 1}],
               revertir=False,
               ruta_lectura="trainingPhrases[0].parts[0].auto")
    except Exception as exc:
        print("  [ERR] intent:", str(exc)[:240])

    print("\n== AGENT ==")
    probar("Agent.webClientSettings", base, "webClientSettings",
           {"boxBorderRadius": "8"})
    probar("Agent.advancedSettings (playbackInterruption)", base, "advancedSettings",
           {"playbackInterruptionSettings": {"allowPlaybackInterruption": False}})
    probar("Agent.advancedSettings (speechSettings)", base, "advancedSettings",
           {"speechSettings": {"enableSmartEndpointing": False}})
    probar("Agent.advancedSettings (enabled)", base, "advancedSettings",
           {"enabled": True})

    print("\n== MATRIX ==")
    for etiqueta, ok, det in MATRIZ:
        print(f"  {'WRITABLE' if ok else 'not      '} | {etiqueta:<58} | {det}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
