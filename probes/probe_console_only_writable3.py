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
from cxp.gaps import read_path as read_path_at

MATRIX: list[tuple[str, bool, str]] = []


def _get(res, *names, default=None):
    """Reads an attribute of the result (the core renamed some of them)."""
    for name in names:
        if hasattr(res, name):
            return getattr(res, name)
    return default


def _set(res, *names, value):
    """Writes the attribute that exists (the core renamed some of them)."""
    for name in names:
        if hasattr(res, name):
            setattr(res, name, value)
            return


def main() -> int:
    cfg = load_config()
    api = ConsoleApi(cfg)
    project = cfg.project
    agent = cfg.only_agent()
    base = f"projects/{project}/locations/global/agents/{agent}"
    ab = api._base(project)

    def test_field(label, resource, field, value, revert=True, read_path=None):
        """`read_path` = where to check the value after writing."""
        try:
            res = set_field(api, resource, field, value, dry_run=False, project=project)
            if read_path:
                actual = api.request("GET", api._url("/" + resource))
                _set(res, "after", "despues", read_path_at(actual, read_path))
                _set(res, "verified", "verificado",
                     _get(res, "after", "despues") == value)
            ok = bool(_get(res, "verified", "verificado"))
            MATRIX.append((label, ok, json.dumps(_get(res, "after", "despues"), ensure_ascii=False)[:100]))
            print(f"  [{'OK ' if ok else 'NO '}] {label}")
            if not ok:
                print(f"        read: {json.dumps(_get(res, 'after', 'despues'), ensure_ascii=False)[:150]}")
                if res.error:
                    print(f"        error: {res.error[:200]}")
            if revert and _get(res, "written", "escrito"):
                set_field(api, resource, field, _get(res, "before", "antes"),
                          dry_run=False, project=project)
            return res
        except Exception as exc:
            MATRIX.append((label, False, f"{type(exc).__name__}: {exc}"))
            print(f"  [ERR] {label}: {str(exc)[:220]}")
            return None

    # ------------------------------------------------------------ temporary
    flow = api.request("POST", api._url(f"{ab}/agents/{agent}/flows"),
                       {"displayName": "zz_tmp_gap_probe", "description": "temporary"})
    flow_id = flow["name"].rsplit("/", 1)[-1]
    atexit.register(lambda: api.request(
        "DELETE", api._url(f"{ab}/agents/{agent}/flows/{flow_id}")))
    page = api.request("POST", api._url(f"{ab}/agents/{agent}/flows/{flow_id}/pages"),
                       {"displayName": "Probe page",
                        "entryFulfillment": {"messages": [{"text": {"text": ["hola"]}}]}})
    page_name = page.get("name", "")

    pbs = api.list_playbooks(agent, project)
    pb_ref = f"{base}/playbooks/" + (pbs[0].get("name") or "").rsplit("/", 1)[-1]
    ets = api.list_entity_types(agent, project)
    et_ref = f"{base}/entityTypes/" + (ets[0].get("name") or "").rsplit("/", 1)[-1]
    print(f"flow={flow_id} page={page_name.rsplit('/',1)[-1]}")

    print("\n== PAGE ==")
    test_field("Page.transitionRoutes (full array with targetPlaybook)", page_name,
           "transitionRoutes", [{"condition": "true", "targetPlaybook": pb_ref}],
           revert=False,
           read_path="transitionRoutes[0].targetPlaybook")

    # existing route -> change only the target
    test_field("Page.transitionRoutes[0].targetPlaybook (existing route)", page_name,
           "transitionRoutes[0].targetPlaybook", pb_ref, revert=False,
           read_path="transitionRoutes[0].targetPlaybook")

    test_field("Page.form.parameters[0].description (full array)", page_name, "form",
           {"parameters": [{"displayName": "p1", "entityType": et_ref,
                            "description": "test description"}]},
           revert=False, read_path="form.parameters[0].description")

    test_field("Page.speakerIdSettings", page_name, "speakerIdSettings",
           {"enableSpeakerId": True})
    test_field("Page.advancedSettings.speechAdaptationSettings", page_name,
           "advancedSettings", {"speechAdaptationSettings": {"adaptation": True}})
    test_field("Page.advancedSettings.enabled", page_name, "advancedSettings", {"enabled": False})

    print("\n== TOOL ==")
    tools = api.list_tools(agent, project)
    source = next((t for t in tools if t.get("openApiSpec")), None)
    if source:
        body = {k: v for k, v in source.items()
                if k not in ("name", "displayName", "etag", "createTime", "updateTime",
                             "actionSchemas", "sharedSchemas")}
        body["displayName"] = "zz_tmp_tool_probe"
        body["description"] = "temporary"
        try:
            new_tool = api.request("POST", api._url(f"{ab}/agents/{agent}/tools"), body)
            tool_name = new_tool.get("name", "")
            atexit.register(lambda: api.request(
                "DELETE", api._url(f"{ab}/agents/{agent}/tools/"
                                   + tool_name.rsplit("/", 1)[-1])))
            schemas = [{"name": "probe_action",
                         "inputParameters": [{"name": "q", "schema": '{"type":"string"}'}],
                         "outputParameters": []}]
            test_field("Tool.actionSchemas (tool with openApiSpec)", tool_name,
                        "actionSchemas", schemas)
        except Exception as exc:
            print("  [ERR] temporary tool:", str(exc)[:240])

    # data store tool (without openApiSpec): the summarization model
    source_ds = next((t for t in tools if t.get("dataStoreSpec")), None)
    if source_ds:
        body = {k: v for k, v in source_ds.items()
                if k not in ("name", "displayName", "etag", "createTime", "updateTime",
                             "actionSchemas", "sharedSchemas")}
        body["displayName"] = "zz_tmp_ds_probe"
        try:
            new_tool = api.request("POST", api._url(f"{ab}/agents/{agent}/tools"), body)
            ds_name = new_tool.get("name", "")
            atexit.register(lambda: api.request(
                "DELETE", api._url(f"{ab}/agents/{agent}/tools/"
                                   + ds_name.rsplit("/", 1)[-1])))
            ds = dict(body.get("dataStoreSpec") or {})
            ds["models"] = {"summarizationModel": {"model": "default"}}
            test_field("Tool.dataStoreSpec.models.summarizationModel.model", ds_name,
                        "dataStoreSpec", ds, revert=False,
                        read_path="dataStoreSpec.models.summarizationModel.model")
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
        test_field("EntityType.entities[0].dtmfPattern", et_name,
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
        test_field("Intent.trainingPhrases (full array with auto=False)", intent_name,
               "trainingPhrases",
               [{"parts": [{"text": "hola", "auto": False}], "repeatCount": 1}],
               revert=False,
               read_path="trainingPhrases[0].parts[0].auto")
    except Exception as exc:
        print("  [ERR] intent:", str(exc)[:240])

    print("\n== AGENT ==")
    test_field("Agent.webClientSettings", base, "webClientSettings",
           {"boxBorderRadius": "8"})
    test_field("Agent.advancedSettings (playbackInterruption)", base, "advancedSettings",
           {"playbackInterruptionSettings": {"allowPlaybackInterruption": False}})
    test_field("Agent.advancedSettings (speechSettings)", base, "advancedSettings",
           {"speechSettings": {"enableSmartEndpointing": False}})
    test_field("Agent.advancedSettings (enabled)", base, "advancedSettings",
           {"enabled": True})

    print("\n== MATRIX ==")
    for label, ok, det in MATRIX:
        print(f"  {'WRITABLE' if ok else 'not      '} | {label:<58} | {det}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
