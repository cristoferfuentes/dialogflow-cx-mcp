#!/usr/bin/env python3
"""Probe: which "out of schema" fields can really be WRITTEN.

Rules:
- It writes ONLY on the writable agent (the clone).
- Each test uses a TEMPORARY resource (flow/page/tool/entityType/intent) that
  is deleted at the end, except the agent-level ones, which are reverted on the spot.
- Each write is verified by re-reading it and is reverted.
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

RESULTS: list[tuple[str, bool, str]] = []


def main() -> int:
    cfg = load_config()
    api = ConsoleApi(cfg)
    project = cfg.project
    agent = cfg.only_agent()
    base = f"projects/{project}/locations/global/agents/{agent}"

    def get(name):
        return api.request("GET", api._url("/" + name))

    def patch(name, body, mask):
        return api.request("PATCH", api._url("/" + name, updateMask=mask), body)

    def post(path, body):
        return api.request("POST", api._url(path), body)

    def delete(path):
        return api.request("DELETE", api._url(path))

    def read_at(path, obj):
        cur = obj
        for part in path.split("."):
            if isinstance(cur, list):
                cur = cur[0] if cur else None
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    def test_field(label, name, mutator, mask, path, value, revert=True):
        try:
            before = get(name)
            draft = copy.deepcopy(before)
            mutator(draft, value)
            response = patch(name, draft, mask)
            after = get(name)
            read = read_at(path, after)
            ok = read == value
            RESULTS.append((label, ok, json.dumps(read, ensure_ascii=False)[:120]))
            print(f"  [{'OK ' if ok else 'NO '}] {label}  -> read={json.dumps(read, ensure_ascii=False)[:100]}")
            if not ok:
                print("        response:", json.dumps(response, ensure_ascii=False)[:220])
            if revert:
                patch(name, before, mask)
        except Exception as exc:
            RESULTS.append((label, False, f"{type(exc).__name__}: {exc}"))
            print(f"  [ERR] {label}: {str(exc)[:220]}")

    # ---------------------------------------------------------------- temporary
    flow = post(f"{api._base(project)}/agents/{agent}/flows",
                {"displayName": "zz_tmp_gap_probe", "description": "temporary"})
    flow_name = flow.get("name", "")
    flow_id = flow_name.rsplit("/", 1)[-1]
    if not flow_id:
        print("the temporary flow could not be created:", flow)
        return 1
    atexit.register(lambda: delete(f"{api._base(project)}/agents/{agent}/flows/{flow_id}"))

    page = post(f"{api._base(project)}/agents/{agent}/flows/{flow_id}/pages",
                {"displayName": "Probe page",
                 "entryFulfillment": {"messages": [{"text": {"text": ["hola"]}}]}})
    page_name = page.get("name", "")
    print("flow:", flow_id, "| page:", page_name.rsplit("/", 1)[-1])

    # a real playbook (to test targetPlaybook)
    pbs = api.list_playbooks(agent, project)
    pb_ref = ""
    if pbs:
        pb_id = (pbs[0].get("name") or "").rsplit("/", 1)[-1]
        pb_ref = f"{base}/playbooks/{pb_id}"

    # a real entity type (for the form)
    ets = api.list_entity_types(agent, project)
    et_ref = ""
    if ets:
        et_ref = (ets[0].get("name") or "").split("/entityTypes/")[-1]
        et_ref = f"{base}/entityTypes/{et_ref}"

    print("\n== PAGE ==")

    def set_adv(d, v):
        d.setdefault("advancedSettings", {})["enabled"] = v

    test_field("Page.advancedSettings.enabled", page_name, set_adv, "advancedSettings",
           "advancedSettings.enabled", False)

    def set_playback(d, v):
        d.setdefault("advancedSettings", {})["playbackInterruptionSettings"] = v

    test_field("Page.advancedSettings.playbackInterruptionSettings", page_name, set_playback,
           "advancedSettings", "advancedSettings.playbackInterruptionSettings",
           {"allowPlaybackInterruption": False})

    def set_speechadapt(d, v):
        d.setdefault("advancedSettings", {})["speechAdaptationSettings"] = v

    test_field("Page.advancedSettings.speechAdaptationSettings", page_name, set_speechadapt,
           "advancedSettings", "advancedSettings.speechAdaptationSettings", {})

    def set_smartendpoint(d, v):
        d.setdefault("advancedSettings", {}).setdefault("speechSettings", {})["enableSmartEndpointing"] = v

    test_field("Page.advancedSettings.speechSettings.enableSmartEndpointing", page_name,
           set_smartendpoint, "advancedSettings",
           "advancedSettings.speechSettings.enableSmartEndpointing", False)

    def set_speaker(d, v):
        d["speakerIdSettings"] = v

    test_field("Page.speakerIdSettings", page_name, set_speaker, "speakerIdSettings",
           "speakerIdSettings", {})

    def set_form_desc(d, v):
        params = d.setdefault("form", {}).setdefault("parameters", [])
        if not params:
            params.append({"displayName": "p1", "entityType": et_ref})
        params[0]["description"] = v

    if et_ref:
        test_field("Page.form.parameters[0].description", page_name, set_form_desc, "form",
               "form.parameters.description", "test description")

    def set_route_tp(d, v):
        routes = d.setdefault("transitionRoutes", [])
        if not routes:
            routes.append({"condition": "true"})
        routes[0]["targetPlaybook"] = v

    if pb_ref:
        test_field("Page.transitionRoutes[0].targetPlaybook", page_name, set_route_tp,
               "transitionRoutes", "transitionRoutes.targetPlaybook", pb_ref)

    print("\n== FLOW ==")

    def set_nlu(d, v):
        d.setdefault("nluSettings", {})["customConfigKey"] = v

    test_field("Flow.nluSettings.customConfigKey", flow_name, set_nlu, "nluSettings",
           "nluSettings.customConfigKey", "my-own-key")

    def set_modelid(d, v):
        d["modelIdentifier"] = v

    test_field("Flow.modelIdentifier", flow_name, set_modelid, "modelIdentifier",
           "modelIdentifier", {"modelId": "11111111-2222-3333-4444-555555555555"})

    print("\n== TOOL (temporary) ==")
    tool = post(f"{api._base(project)}/agents/{agent}/tools",
                {"displayName": "zz_tmp_tool_probe",
                 "description": "temporary",
                 "openApiSpec": {"textSchema": '{"type":"object"}'}})
    tool_name = tool.get("name", "")
    if tool_name:
        atexit.register(lambda: delete(f"{api._base(project)}/agents/{agent}/tools/"
                                       + tool_name.rsplit("/", 1)[-1]))

        def set_actionschemas(d, v):
            d["actionSchemas"] = v

        test_field("Tool.actionSchemas", tool_name, set_actionschemas, "actionSchemas",
               "actionSchemas", [{"name": "probe",
                                  "inputParameters": [{"name": "q", "schema": "{\"type\":\"string\"}"}],
                                  "outputParameters": []}])
    else:
        print("  the temporary tool could not be created:", json.dumps(tool)[:200])

    print("\n== ENTITY TYPE (temporary) ==")
    et = post(f"{api._base(project)}/agents/{agent}/entityTypes",
              {"displayName": "zz_tmp_et_probe",
               "entities": [{"value": "uno", "synonyms": ["uno"], "dtmfPattern": "1"}]})
    et_name = et.get("name", "")
    if et_name:
        atexit.register(lambda: delete(f"{api._base(project)}/agents/{agent}/entityTypes/"
                                       + et_name.rsplit("/", 1)[-1]))

        def set_dtmf(d, v):
            d["entities"][0]["dtmfPattern"] = v

        test_field("EntityType.entities[0].dtmfPattern", et_name, set_dtmf, "entities",
               "entities.dtmfPattern", "9")
    else:
        print("  the temporary entityType could not be created:", json.dumps(et)[:200])

    print("\n== INTENT (temporary) ==")
    intent = post(f"{api._base(project)}/agents/{agent}/intents",
                  {"displayName": "zz_tmp_intent_probe",
                   "trainingPhrases": [{"parts": [{"text": "hola"}], "repeatCount": 1}]})
    intent_name = intent.get("name", "")
    if intent_name:
        atexit.register(lambda: delete(f"{api._base(project)}/agents/{agent}/intents/"
                                       + intent_name.rsplit("/", 1)[-1]))

        def set_auto(d, v):
            d["trainingPhrases"][0]["parts"][0]["auto"] = v

        test_field("Intent.trainingPhrases[0].parts[0].auto", intent_name, set_auto,
               "trainingPhrases", "trainingPhrases.parts.auto", False)
    else:
        print("  the temporary intent could not be created:", json.dumps(intent)[:200])

    print("\n== AGENT (reverted on the spot) ==")
    agent_name = base

    def set_webclient(d, v):
        d["webClientSettings"] = v

    test_field("Agent.webClientSettings.boxBorderRadius", agent_name, set_webclient,
           "webClientSettings", "webClientSettings.boxBorderRadius", "8")

    def set_agent_adv(d, v):
        d.setdefault("advancedSettings", {})["enabled"] = v

    test_field("Agent.advancedSettings.enabled", agent_name, set_agent_adv, "advancedSettings",
           "advancedSettings.enabled", True)

    print("\n== SUMMARY ==")
    for label, ok, det in RESULTS:
        print(f"  {'OK ' if ok else 'NO '} {label:<58} {det}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
