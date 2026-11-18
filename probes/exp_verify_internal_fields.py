#!/usr/bin/env python3
"""Closes the INTERNAL proto fields that were still untested.

For each field TWO measurements are made, in a temporary lab agent
that is deleted at the end:

1. **DOES IT EXIST?** The backend parser **silently ignores** the fields it
   does not know (MEASURED: an invented name returns 200 and nothing is saved), but
   it **fails with the type** when the field does exist and the value does not fit:
       Invalid value at 'agent.advanced_settings.speech_settings.fix_transcripts' (TYPE_BOOL), "x"
       Invalid value at 'query_input.speech_transcripts' (type.googleapis.com/...TranscriptInput), "x"
   => the error gives away the NAME and the TYPE of the field.

2. **DOES IT PERSIST?** A VALID value of the measured type is written, the
   resource is re-read and it is checked whether the value survives. The resulting
   state is one of:
       "writable"     -> it is kept (verified by re-reading)
       "ignored"      -> the backend does NOT know it: no probe gives an error
       "dropped"      -> it knows it but EMPTIES it when writing
       "input_only"   -> it is accepted in the request, but it cannot be read back
       "not_accepted" -> it knows it but rejects the value
   (the same vocabulary as `cxp/proto_interno.py`).

Parser vocabulary (MEASURED on 2026-11-16; raw matrix in
/tmp/matriz.json):

    object value  -> "Invalid value at '<PARENT>' (<field>), Starting an object on a scalar field"
    list value    -> "Invalid JSON payload received. Unknown name \"<field>\" at '<PARENT>': Proto field is not repeating, cannot start list."
    simple value  -> "Invalid value at '<PATH>' (<TYPE>), <value>"

The `[0]` index in the path gives away a REPEATED field. Careful: the "object on a
scalar field" error ALSO appears on repeated fields (it does not prove the field
is singular); what proves it is singular is a type error WITHOUT an index.

    .venv/bin/python probes/exp_verify_internal_fields.py [filter]
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi

# --------------------------------------------------------------- the engine

# The seven values with which a field is probed. Among them there is at least
# one that is invalid for EVERY possible type (object, string, list...).
PROBES: list[tuple[str, Any]] = [
    ("obj", {"__x__": 1}),
    ("str", "x"),
    ("int", 1),
    ("bool", True),
    ("lista_obj", [{"__x__": 1}]),
    ("lista_int", [1]),
    ("lista_str", ["x"]),
]

# "Invalid value at '<path>' (<TYPE>), <value>"   (TYPE = TYPE_X or type.googleapis.com/...)
RE_TIPO = re.compile(
    r"Invalid value at '([^']+)' \((TYPE_[A-Z0-9_]+|type\.googleapis\.com/[^)]+)\)")
# "Invalid value at '<path>' (<field>), Starting an object on a scalar field"
RE_ESCALAR = re.compile(
    r"Invalid value at '([^']+)' \(([a-z0-9_]+)\), Starting an object on a scalar field")
# "Unknown name \"<field>\" at '<path>': Proto field is not repeating, cannot start list."
RE_REPETIDO = re.compile(r"Proto field is not repeating")

TYPE_NAME = {
    "TYPE_BOOL": "bool", "TYPE_INT32": "int32", "TYPE_INT64": "int64",
    "TYPE_UINT32": "uint32", "TYPE_UINT64": "uint64",
    "TYPE_DOUBLE": "double", "TYPE_FLOAT": "float", "TYPE_STRING": "string",
    "TYPE_ENUM": "enum",
}


def error_message(exc: Exception) -> str:
    """The text of the backend error (the `message` field of the JSON)."""
    txt = str(exc)
    m = re.search(r'"message": "(.*?)",\n', txt, re.S)
    return (m.group(1) if m else txt).replace('\\"', '"').replace("\n", " ")


def classify(probes: dict[str, str]) -> dict[str, Any]:
    """Classifies a field from the errors of the probes.

    Returns {"status": "unknown"|"scalar"|"message"|"repeated",
             "type": str, "repeated": bool, "proof": str}
    """
    types: list[tuple[str, str]] = []
    for _name, err in probes.items():
        m = RE_TIPO.search(err)
        if m:
            types.append((m.group(2), m.group(1)))
    if types:
        # An index `[0]` in the path gives away a REPEATED field.
        for type_, path in types:
            if path.endswith("[0]"):
                return {"status": "repeated", "type": type_, "repeated": True,
                        "proof": f"type {type_} at {path}"}
        type_, path = types[0]
        return {"status": "message" if type_.startswith("type.googleapis.com")
                else "scalar", "type": type_, "repeated": False,
                "proof": f"type {type_} at {path}"}
    for name, err in probes.items():
        if RE_ESCALAR.search(err):
            return {"status": "scalar", "type": "TYPE_?", "repeated": False,
                    "proof": f"{name}: object on a scalar field"}
    for name, err in probes.items():
        if RE_REPETIDO.search(err):
            return {"status": "scalar", "type": "TYPE_?", "repeated": False,
                    "proof": f"{name}: list on a singular field"}
    return {"status": "unknown", "type": "", "repeated": False,
            "proof": "no probe gives an error (the backend does not know it)"}


def value_for_type(type_: str) -> Any:
    """A VALID value for the measured type."""
    if type_ == "TYPE_BOOL":
        return True
    if type_.startswith("TYPE_INT") or type_.startswith("TYPE_UINT"):
        return 1
    if type_.startswith("TYPE_DOUBLE") or type_.startswith("TYPE_FLOAT"):
        return 1.0
    if type_.startswith("TYPE_STRING") or type_.startswith("TYPE_ENUM"):
        return "x"
    if type_.endswith("google.protobuf.Duration"):
        return "1s"
    if type_.startswith("type.googleapis.com/"):
        return {}
    return "x"


def nest(path: str, value: Any) -> dict:
    """'a.b.c' -> {'a': {'b': {'c': value}}}"""
    out: Any = value
    for p in reversed(path.split(".")):
        out = {p: out}
    return out


def _find(obj: Any, parts: list[str]) -> Any:
    """Looks for a value even if the JSON comes in snake_case."""
    if not isinstance(obj, dict):
        return None
    for k in obj:
        if k.lower() == parts[0].lower():
            if len(parts) == 1:
                return obj[k]
            return _find(obj[k], parts[1:])
    return None


def read_at(obj: Any, path: str) -> Any:
    """Reads a value by path. Accepts `a.b[0].c` (indices) and snake_case."""
    if obj is None:
        return None
    parts = re.findall(r"([^.\[\]]+)|\[(\d+)\]", path)
    cur: Any = obj
    for name, index in parts:
        if index:
            if not isinstance(cur, list) or int(index) >= len(cur):
                return None
            cur = cur[int(index)]
            continue
        if not isinstance(cur, dict):
            return None
        if name in cur:
            cur = cur[name]
            continue
        following = None
        for k in cur:
            if k.lower() == name.lower():
                following = cur[k]
                break
        if following is None:
            return None
        cur = following
    return cur


# ------------------------------------------------------- the write strategy
# Each strategy knows how to put a value in a path and read it back.

class Ctx(dict):
    """Measurement context (the resources created for the case)."""


Strategy = Callable[[ConsoleApi, str, Ctx, str, Any], Any]


def _agent_advanced_settings(cons: ConsoleApi, abase: str, ctx: Ctx,
                              path: str, value: Any) -> Any:
    """PATCH of the agent with updateMask=advancedSettings and re-read."""
    cons.request("PATCH", cons._url(abase), body=nest(path, value),
                 update_mask="advancedSettings")
    return cons.request("GET", cons._url(abase))


def _flow_advanced_settings(cons: ConsoleApi, abase: str, ctx: Ctx,
                           path: str, value: Any) -> Any:
    """PATCH of the FLOW with updateMask=advancedSettings and re-read."""
    cons.request("PATCH", cons._url(f"{abase}/flows/{ctx['flow_id']}"),
                 body=nest(path, value), update_mask="advancedSettings")
    return cons.request("GET", cons._url(f"{abase}/flows/{ctx['flow_id']}"))


def _query_input(cons: ConsoleApi, abase: str, ctx: Ctx,
                 path: str, value: Any) -> Any:
    """detectIntent: the field goes in `queryInput` (and it is INPUT-ONLY).

    MEASURED (2026-11-16): there are two traps
      * `detectIntent` requires an input type: without text it answers
        "Unknown type for QueryInput." and that error HIDES the field one.
      * some voice fields (e.g. `partialTranscript`) are in the `oneof`
        `input` TOGETHER with the text: sending them with text gives "oneof field
        'input' is already set".
    => it is tried with text and, if it clashes with the `oneof`, without text.
    """
    body = nest("queryInput." + path, value)
    body["queryInput"]["languageCode"] = "es"
    url = cons._url(f"{abase}/sessions/{ctx['session']}:detectIntent")
    try:
        cons.request("POST", url, body={"queryInput": {
            **body["queryInput"], "text": {"text": "hola"}}})
        return None
    except Exception as exc:
        if "oneof" not in str(exc):
            raise
    cons.request("POST", url, body=body)
    return None


def _webhook(cons: ConsoleApi, abase: str, ctx: Ctx,
             path: str, value: Any) -> Any:
    """PATCH of the webhook with updateMask=`<field>` and re-read.

    MEASURED: `cloudFunction` is in a `oneof` with `genericWebService`: if the
    webhook already has a `genericWebService`, it has to be EMPTIED ("URI of
    generic web service should be specified.").
    """
    url = f"{abase}/webhooks/{ctx['webhook_id']}"
    body: dict[str, Any] = {path: value}
    mask = path
    if path == "cloudFunction":
        body["genericWebService"] = None
        mask = "cloudFunction,genericWebService"
    cons.request("PATCH", cons._url(url), body=body, update_mask=mask)
    return cons.request("GET", cons._url(url))


def _event_handler(cons: ConsoleApi, abase: str, ctx: Ctx,
                   path: str, value: Any) -> Any:
    """PATCH of the flow with updateMask=eventHandlers and re-read.

    MEASURED: the PATCH of `eventHandlers` REPLACES the whole array, and the
    server rejects it if the default handler is missing ("You cannot delete
    event handler for 'sys.no-input-default' in the flow"). That is why the
    original list is sent back and ONLY the own handler is modified.
    """
    url = f"{abase}/flows/{ctx['flow_id']}"
    handler = dict(ctx["event_handler"])
    handler[path] = value
    base = list(ctx["event_handlers_base"])
    cons.request("PATCH", cons._url(url),
                 body={"eventHandlers": [handler] + base[1:]},
                 update_mask="eventHandlers")
    read_obj = cons.request("GET", cons._url(url))
    for h in read_at(read_obj, "eventHandlers") or []:
        if h.get("name") == handler.get("name"):
            return {"eventHandler": h}
    return None


def _test_case(cons: ConsoleApi, abase: str, ctx: Ctx,
               path: str, value: Any) -> Any:
    """POST of a test case with the turn and re-read.

    `path` is relative to the turn: `expectations`, `userInput.parameterScope`,
    `userInput.input.dtmf.transformed`.
    """
    turn = nest(path, value)
    input_ = {"text": {"text": "hola"}, "languageCode": "es"}
    ui = turn.get("userInput")
    if isinstance(ui, dict):
        ui.setdefault("input", input_)
    else:
        turn = {"userInput": {"input": input_}, **turn}
    created = cons.request("POST", cons._url(f"{abase}/testCases"), body={
        "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
        "notes": "temporary: measuring an internal field",
        "testCaseConversationTurns": [turn],
    })
    name = created.get("name") or ""
    ctx.setdefault("created_cases", []).append(name)
    if not name:
        raise RuntimeError("the test case could not be created")
    return cons.request("GET", cons._url("/" + name))


def _intent(cons: ConsoleApi, abase: str, ctx: Ctx,
            path: str, value: Any) -> Any:
    """PATCH of the intent with updateMask=trainingPhrases and re-read.

    MEASURED: the training phrase requires `repeatCount >= 1` ("Training
    phrase repeat count should be greater than 0.").
    """
    url = f"{abase}/intents/{ctx['intent_id']}"
    parts = ctx["training_parts"]
    new_parts = dict(parts)
    new_parts[path.split(".")[-1]] = value
    cons.request("PATCH", cons._url(url),
                 body={"trainingPhrases": [{"parts": [new_parts],
                                            "repeatCount": 1}]},
                 update_mask="trainingPhrases")
    return cons.request("GET", cons._url(url))


STRATEGIES: dict[str, Strategy] = {
    "agent": _agent_advanced_settings,
    "flow": _flow_advanced_settings,
    "queryinput": _query_input,
    "webhook": _webhook,
    "eventhandler": _event_handler,
    "testcase": _test_case,
    "intent": _intent,
}

# Where the value is re-read inside the resource (None = it is not re-read).
READ_PATH: dict[str, str] = {
    "agent": "",            # the same path that was written
    "flow": "",
    "queryinput": "",       # not re-read: it is input-only
    "webhook": "",
    "eventhandler": "eventHandler.",
    "testcase": "testCaseConversationTurns[0].",
    "intent": "trainingPhrases[0].parts[0].",
}


# --------------------------------------------------------------- the cases

CASES: list[dict[str, Any]] = [
    # --- AdvancedSettingsSpeechSettings (13) ---------------------------
    {"id": "speech.disableLlmEndpointing", "strategy": "agent",
     "path": "advancedSettings.speechSettings.disableLlmEndpointing"},
    {"id": "speech.disableTimeoutBasedEndpointing", "strategy": "agent",
     "path": "advancedSettings.speechSettings.disableTimeoutBasedEndpointing"},
    {"id": "speech.enableAggressiveEndpointing", "strategy": "agent",
     "path": "advancedSettings.speechSettings.enableAggressiveEndpointing"},
    {"id": "speech.enableLlmEndpointing", "strategy": "agent",
     "path": "advancedSettings.speechSettings.enableLlmEndpointing"},
    {"id": "speech.enableSmartEndpointing", "strategy": "agent",
     "path": "advancedSettings.speechSettings.enableSmartEndpointing"},
    {"id": "speech.fallbackModels", "strategy": "agent",
     "path": "advancedSettings.speechSettings.fallbackModels"},
    {"id": "speech.fixTranscripts", "strategy": "agent",
     "path": "advancedSettings.speechSettings.fixTranscripts"},
    {"id": "speech.geminiAsrConfig", "strategy": "agent",
     "path": "advancedSettings.speechSettings.geminiAsrConfig"},
    {"id": "speech.minDigits", "strategy": "agent",
     "path": "advancedSettings.speechSettings.minDigits"},
    {"id": "speech.overrideRequestSpeechModel", "strategy": "agent",
     "path": "advancedSettings.speechSettings.overrideRequestSpeechModel"},
    {"id": "speech.useGeminiAsr", "strategy": "agent",
     "path": "advancedSettings.speechSettings.useGeminiAsr"},
    {"id": "speech.useSttV2", "strategy": "agent",
     "path": "advancedSettings.speechSettings.useSttV2"},
    {"id": "speech.utteranceWaitTimeout", "strategy": "agent",
     "path": "advancedSettings.speechSettings.utteranceWaitTimeout"},
    # --- AdvancedSettingsDtmfSettings (4) ------------------------------
    {"id": "dtmf.customRule", "strategy": "flow",
     "path": "advancedSettings.dtmfSettings.customRule"},
    {"id": "dtmf.disableSpeechDetection", "strategy": "flow",
     "path": "advancedSettings.dtmfSettings.disableSpeechDetection"},
    {"id": "dtmf.entityType", "strategy": "flow",
     "path": "advancedSettings.dtmfSettings.entityType"},
    {"id": "dtmf.useSystemEntityRule", "strategy": "flow",
     "path": "advancedSettings.dtmfSettings.useSystemEntityRule"},
    # --- QueryInput (5): INPUT-ONLY fields of detectIntent -------------
    {"id": "queryinput.alternativeLanguageCodes", "strategy": "queryinput",
     "path": "alternativeLanguageCodes"},
    {"id": "queryinput.multiPartyTranscripts", "strategy": "queryinput",
     "path": "multiPartyTranscripts"},
    {"id": "queryinput.partialTranscript", "strategy": "queryinput",
     "path": "partialTranscript"},
    {"id": "queryinput.speechTranscripts", "strategy": "queryinput",
     "path": "speechTranscripts"},
    {"id": "queryinput.tokenizedAudio", "strategy": "queryinput",
     "path": "tokenizedAudio"},
    # --- Webhook (3) ----------------------------------------------------
    {"id": "webhook.cloudFunction", "strategy": "webhook",
     "path": "cloudFunction"},
    {"id": "webhook.webhookFakeConfig", "strategy": "webhook",
     "path": "webhookFakeConfig"},
    {"id": "webhook.etag", "strategy": "webhook", "path": "etag"},
    # --- EventHandler (2) ----------------------------------------------
    {"id": "eventhandler.condition", "strategy": "eventhandler",
     "path": "condition"},
    {"id": "eventhandler.enableNonConsecutiveTriggering", "strategy": "eventhandler",
     "path": "enableNonConsecutiveTriggering"},
    # --- TestCase (3) ---------------------------------------------------
    {"id": "testcase.expectations", "strategy": "testcase",
     "path": "expectations"},
    {"id": "testcase.parameterScope", "strategy": "testcase",
     "path": "userInput.parameterScope"},
    {"id": "testcase.dtmf.transformed", "strategy": "testcase",
     "path": "userInput.input.dtmf.transformed"},
    # --- IntentTrainingPhrasePart (1) ----------------------------------
    {"id": "intent.multiIntentIndicator", "strategy": "intent",
     "path": "multiIntentIndicator"},
    # --- CONTROLS: PUBLIC fields that DO exist (they validate the probe) --
    {"id": "control.agent", "strategy": "agent", "control": True,
     "path": "advancedSettings.speechSettings.useTimeoutBasedEndpointing"},
    {"id": "control.flow", "strategy": "flow", "control": True,
     "path": "advancedSettings.speechSettings.useTimeoutBasedEndpointing"},
    {"id": "control.queryinput", "strategy": "queryinput", "control": True,
     "path": "languageCode"},
    {"id": "control.webhook", "strategy": "webhook", "control": True,
     "path": "timeout"},
    {"id": "control.eventhandler", "strategy": "eventhandler", "control": True,
     "path": "event"},
    {"id": "control.testcase", "strategy": "testcase", "control": True,
     "path": "userInput.isWebhookEnabled"},
    {"id": "control.testcase.turn", "strategy": "testcase", "control": True,
     "path": "userInput"},
    {"id": "control.intent", "strategy": "intent", "control": True,
     "path": "text"},
]


def _prepare(cons: ConsoleApi, abase: str, ctx: Ctx) -> None:
    """Creates the resources needed by the webhook, event
    handler and intent strategies, and fills in the context."""
    flow = cons.request("POST", cons._url(f"{abase}/flows"), body={
        "displayName": "lab_flow", "transitionRoutes": [], "eventHandlers": []})
    ctx["flow_id"] = (flow.get("name") or "").rsplit("/", 1)[-1]
    # MEASURED: a new flow ALREADY brings two default handlers
    # ('sys.no-match-default' and 'sys.no-input-default'). The PATCH of
    # `eventHandlers` REPLACES the array: if they are not sent back, the server
    # rejects it for deleting the default one ("You cannot delete event handler
    # for 'sys.no-input-default' in the flow"); and if one is added with a
    # repeated event, "Event handlers should be unique.". => the first one of
    # the list is modified.
    base_handlers = read_at(cons.request(
        "GET", cons._url(f"{abase}/flows/{ctx['flow_id']}")),
        "eventHandlers") or []
    ctx["event_handlers_base"] = base_handlers
    ctx["event_handler"] = (dict(base_handlers[0]) if base_handlers else {
        "event": "sys.no-match-default",
        "triggerFulfillment": {"messages": [{"text": {"text": "x"}}]}})
    webhook = cons.request("POST", cons._url(f"{abase}/webhooks"), body={
        "displayName": "lab_webhook",
        "genericWebService": {"uri": "https://example.invalid/hook"}})
    ctx["webhook_id"] = (webhook.get("name") or "").rsplit("/", 1)[-1]
    intent = cons.request("POST", cons._url(f"{abase}/intents"), body={
        "displayName": "lab_intent",
        "trainingPhrases": [{"parts": [{"text": "hola"}], "repeatCount": 1}]})
    ctx["intent_id"] = (intent.get("name") or "").rsplit("/", 1)[-1]
    ctx["training_parts"] = {"text": "hola"}


def main() -> int:
    filter_text = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = load_config()
    cons = ConsoleApi(cfg)
    base = f"/projects/{cfg.project}/locations/{cfg.location}/agents"
    res: dict[str, Any] = {"cases": []}

    created = cons.request("POST", cons._url(base), body={
        "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
        "defaultLanguageCode": "es", "timeZone": "America/Santiago",
        "description": "temporary: verifying internal fields",
    })
    agent = created.get("name") or ""
    if not agent:
        print("the agent could not be created")
        return 1
    cfg.agent_readwrite = list(cfg.agent_readwrite) + [agent.rsplit("/", 1)[-1]]
    abase = base + "/" + agent.rsplit("/", 1)[-1]
    print("lab agent:", agent)
    ctx = Ctx({"session": f"lab-{uuid.uuid4().hex[:8]}", "created_cases": []})

    # ---- resources needed by the other strategies ---------------------
    try:
        _prepare(cons, abase, ctx)
    except Exception:
        cons.request("DELETE", cons._url("/" + agent))
        raise
    print("flow", ctx["flow_id"], "| webhook", ctx["webhook_id"],
          "| intent", ctx["intent_id"])

    try:
        for case in CASES:
            if filter_text and filter_text not in case["id"] and not case.get("control"):
                continue
            write = STRATEGIES[case["strategy"]]
            row: dict[str, Any] = {"id": case["id"], "path": case["path"],
                                     "strategy": case["strategy"]}

            # ---- 1) DOES IT EXIST? (and of what type) --------------------
            probes: dict[str, str] = {}
            for name, value in PROBES:
                try:
                    write(cons, abase, ctx, case["path"], value)
                except Exception as exc:
                    probes[name] = error_message(exc)
                else:
                    probes[name] = ""
            info = classify(probes)
            row.update({"status": info["status"], "type": info["type"],
                         "repeated": info["repeated"], "proof": info["proof"],
                         "probes": {k: v[:220] for k, v in probes.items() if v}})
            if info["status"] == "unknown":
                row["write"] = "ignored"
                row["note"] = ("the backend does NOT know it: no probe gives an "
                                "error (same behaviour as an invented name) and "
                                "nothing is saved")
                print(f"{case['id']:<40} -> IGNORED (silently ignored)")
                res["cases"].append(row)
                continue

            # ---- 2) IS IT ACCEPTED / DOES IT PERSIST? -------------------
            value = value_for_type(info["type"])
            try:
                read_obj = write(cons, abase, ctx, case["path"], value)
            except Exception as exc:
                row["write"] = "not_accepted"
                row["error"] = error_message(exc)[:300]
                print(f"{case['id']:<40} -> {info['type'][:40]:<40} NOT ACCEPTED: "
                      f"{row['error'][:80]}")
                res["cases"].append(row)
                continue
            prefix = READ_PATH[case["strategy"]]
            read_value = read_at(read_obj, prefix + case["path"]) if read_obj else None
            if read_value is None and read_obj:
                read_value = _find(read_obj, (prefix + case["path"]).split("."))
            row["written_value"] = value
            row["read_value"] = read_value
            if case["strategy"] == "queryinput":
                row["write"] = "input_only"
            elif read_value is None:
                row["write"] = "dropped"
            else:
                row["write"] = "writable"
            print(f"{case['id']:<40} -> {info['type'][:40]:<40} "
                  f"{row['write']:<14} read="
                  f"{json.dumps(read_value, ensure_ascii=False)[:40]}")
            res["cases"].append(row)
    finally:
        for name in ctx.get("created_cases", []):
            try:
                cons.request("POST", cons._url(f"{abase}/testCases:batchDelete"),
                             body={"names": [name]})
            except Exception:
                pass
        try:
            cons.request("DELETE", cons._url("/" + agent))
            print("lab agent deleted")
        except Exception as exc:
            print("Could NOT delete the agent:", str(exc)[:200])

    Path("/tmp/campos_internos.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    # It is accumulated in a single file so that no measurement is lost per group.
    all_file = Path("/tmp/campos_internos_all.json")
    acumulado: list[dict[str, Any]] = []
    if all_file.exists():
        try:
            acumulado = json.loads(all_file.read_text(encoding="utf-8")).get("cases", [])
        except Exception:
            acumulado = []
    por_id = {c["id"]: c for c in acumulado}
    por_id.update({c["id"]: c for c in res["cases"]})
    all_file.write_text(json.dumps({"cases": list(por_id.values())},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    print("-> /tmp/campos_internos.json and /tmp/campos_internos_all.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
