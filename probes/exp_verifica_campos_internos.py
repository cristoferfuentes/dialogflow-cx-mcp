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
       "escribible"   -> it is kept (verified by re-reading)
       "ignorado"     -> the backend does NOT know it: no probe gives an error
       "descartado"   -> it knows it but EMPTIES it when writing
       "solo_entrada" -> it is accepted in the request, but it cannot be read back
       "no_aceptado"  -> it knows it but rejects the value
   (the same vocabulary as `cxp/proto_interno.py`).

Parser vocabulary (MEASURED on 2026-11-16; raw matrix in
/tmp/matriz.json):

    object value  -> "Invalid value at '<PARENT>' (<field>), Starting an object on a scalar field"
    list value    -> "Invalid JSON payload received. Unknown name \"<field>\" at '<PARENT>': Proto field is not repeating, cannot start list."
    simple value  -> "Invalid value at '<PATH>' (<TYPE>), <value>"

The `[0]` index in the path gives away a REPEATED field. Careful: the "object on a
scalar field" error ALSO appears on repeated fields (it does not prove the field
is singular); what proves it is singular is a type error WITHOUT an index.

    .venv/bin/python probes/exp_verifica_campos_internos.py [filter]
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
SONDAS: list[tuple[str, Any]] = [
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

NOMBRE_DE_TIPO = {
    "TYPE_BOOL": "bool", "TYPE_INT32": "int32", "TYPE_INT64": "int64",
    "TYPE_UINT32": "uint32", "TYPE_UINT64": "uint64",
    "TYPE_DOUBLE": "double", "TYPE_FLOAT": "float", "TYPE_STRING": "string",
    "TYPE_ENUM": "enum",
}


def mensaje_de(exc: Exception) -> str:
    """The text of the backend error (the `message` field of the JSON)."""
    txt = str(exc)
    m = re.search(r'"message": "(.*?)",\n', txt, re.S)
    return (m.group(1) if m else txt).replace('\\"', '"').replace("\n", " ")


def clasificar(sondas: dict[str, str]) -> dict[str, Any]:
    """Classifies a field from the errors of the probes.

    Returns {"estado": "unknown"|"scalar"|"message"|"repeated",
             "type": str, "repeated": bool, "proof": str}
    """
    tipos: list[tuple[str, str]] = []
    for _nombre, err in sondas.items():
        m = RE_TIPO.search(err)
        if m:
            tipos.append((m.group(2), m.group(1)))
    if tipos:
        # An index `[0]` in the path gives away a REPEATED field.
        for tipo, ruta in tipos:
            if ruta.endswith("[0]"):
                return {"estado": "repetido", "tipo": tipo, "repetido": True,
                        "prueba": f"type {tipo} at {ruta}"}
        tipo, ruta = tipos[0]
        return {"estado": "mensaje" if tipo.startswith("type.googleapis.com")
                else "escalar", "tipo": tipo, "repetido": False,
                "prueba": f"type {tipo} at {ruta}"}
    for nombre, err in sondas.items():
        if RE_ESCALAR.search(err):
            return {"estado": "escalar", "tipo": "TYPE_?", "repetido": False,
                    "prueba": f"{nombre}: object on a scalar field"}
    for nombre, err in sondas.items():
        if RE_REPETIDO.search(err):
            return {"estado": "escalar", "tipo": "TYPE_?", "repetido": False,
                    "prueba": f"{nombre}: list on a singular field"}
    return {"estado": "desconocido", "tipo": "", "repetido": False,
            "prueba": "no probe gives an error (the backend does not know it)"}


def valor_por_tipo(tipo: str) -> Any:
    """A VALID value for the measured type."""
    if tipo == "TYPE_BOOL":
        return True
    if tipo.startswith("TYPE_INT") or tipo.startswith("TYPE_UINT"):
        return 1
    if tipo.startswith("TYPE_DOUBLE") or tipo.startswith("TYPE_FLOAT"):
        return 1.0
    if tipo.startswith("TYPE_STRING") or tipo.startswith("TYPE_ENUM"):
        return "x"
    if tipo.endswith("google.protobuf.Duration"):
        return "1s"
    if tipo.startswith("type.googleapis.com/"):
        return {}
    return "x"


def anida(camino: str, valor: Any) -> dict:
    """'a.b.c' -> {'a': {'b': {'c': valor}}}"""
    out: Any = valor
    for p in reversed(camino.split(".")):
        out = {p: out}
    return out


def _buscar(obj: Any, partes: list[str]) -> Any:
    """Looks for a value even if the JSON comes in snake_case."""
    if not isinstance(obj, dict):
        return None
    for k in obj:
        if k.lower() == partes[0].lower():
            if len(partes) == 1:
                return obj[k]
            return _buscar(obj[k], partes[1:])
    return None


def leer(obj: Any, camino: str) -> Any:
    """Reads a value by path. Accepts `a.b[0].c` (indices) and snake_case."""
    if obj is None:
        return None
    partes = re.findall(r"([^.\[\]]+)|\[(\d+)\]", camino)
    cur: Any = obj
    for nombre, indice in partes:
        if indice:
            if not isinstance(cur, list) or int(indice) >= len(cur):
                return None
            cur = cur[int(indice)]
            continue
        if not isinstance(cur, dict):
            return None
        if nombre in cur:
            cur = cur[nombre]
            continue
        siguiente = None
        for k in cur:
            if k.lower() == nombre.lower():
                siguiente = cur[k]
                break
        if siguiente is None:
            return None
        cur = siguiente
    return cur


# ------------------------------------------------------- the write strategy
# Each strategy knows how to put a value in a path and read it back.

class Ctx(dict):
    """Measurement context (the resources created for the case)."""


Estrategia = Callable[[ConsoleApi, str, Ctx, str, Any], Any]


def _agente_advanced_settings(cons: ConsoleApi, abase: str, ctx: Ctx,
                               camino: str, valor: Any) -> Any:
    """PATCH of the agent with updateMask=advancedSettings and re-read."""
    cons.request("PATCH", cons._url(abase), body=anida(camino, valor),
                 update_mask="advancedSettings")
    return cons.request("GET", cons._url(abase))


def _flow_advanced_settings(cons: ConsoleApi, abase: str, ctx: Ctx,
                            camino: str, valor: Any) -> Any:
    """PATCH of the FLOW with updateMask=advancedSettings and re-read."""
    cons.request("PATCH", cons._url(f"{abase}/flows/{ctx['flow_id']}"),
                 body=anida(camino, valor), update_mask="advancedSettings")
    return cons.request("GET", cons._url(f"{abase}/flows/{ctx['flow_id']}"))


def _query_input(cons: ConsoleApi, abase: str, ctx: Ctx,
                 camino: str, valor: Any) -> Any:
    """detectIntent: the field goes in `queryInput` (and it is INPUT-ONLY).

    MEASURED (2026-11-16): there are two traps
      * `detectIntent` requires an input type: without text it answers
        "Unknown type for QueryInput." and that error HIDES the field one.
      * some voice fields (e.g. `partialTranscript`) are in the `oneof`
        `input` TOGETHER with the text: sending them with text gives "oneof field
        'input' is already set".
    => it is tried with text and, if it clashes with the `oneof`, without text.
    """
    cuerpo = anida("queryInput." + camino, valor)
    cuerpo["queryInput"]["languageCode"] = "es"
    url = cons._url(f"{abase}/sessions/{ctx['session']}:detectIntent")
    try:
        cons.request("POST", url, body={"queryInput": {
            **cuerpo["queryInput"], "text": {"text": "hola"}}})
        return None
    except Exception as exc:
        if "oneof" not in str(exc):
            raise
    cons.request("POST", url, body=cuerpo)
    return None


def _webhook(cons: ConsoleApi, abase: str, ctx: Ctx,
             camino: str, valor: Any) -> Any:
    """PATCH of the webhook with updateMask=`<field>` and re-read.

    MEASURED: `cloudFunction` is in a `oneof` with `genericWebService`: if the
    webhook already has a `genericWebService`, it has to be EMPTIED ("URI of
    generic web service should be specified.").
    """
    url = f"{abase}/webhooks/{ctx['webhook_id']}"
    cuerpo: dict[str, Any] = {camino: valor}
    mascara = camino
    if camino == "cloudFunction":
        cuerpo["genericWebService"] = None
        mascara = "cloudFunction,genericWebService"
    cons.request("PATCH", cons._url(url), body=cuerpo, update_mask=mascara)
    return cons.request("GET", cons._url(url))


def _event_handler(cons: ConsoleApi, abase: str, ctx: Ctx,
                   camino: str, valor: Any) -> Any:
    """PATCH of the flow with updateMask=eventHandlers and re-read.

    MEASURED: the PATCH of `eventHandlers` REPLACES the whole array, and the
    server rejects it if the default handler is missing ("You cannot delete
    event handler for 'sys.no-input-default' in the flow"). That is why the
    original list is sent back and ONLY the own handler is modified.
    """
    url = f"{abase}/flows/{ctx['flow_id']}"
    manejador = dict(ctx["event_handler"])
    manejador[camino] = valor
    base = list(ctx["event_handlers_base"])
    cons.request("PATCH", cons._url(url),
                 body={"eventHandlers": [manejador] + base[1:]},
                 update_mask="eventHandlers")
    leido = cons.request("GET", cons._url(url))
    for h in leer(leido, "eventHandlers") or []:
        if h.get("name") == manejador.get("name"):
            return {"eventHandler": h}
    return None


def _test_case(cons: ConsoleApi, abase: str, ctx: Ctx,
               camino: str, valor: Any) -> Any:
    """POST of a test case with the turn and re-read.

    `camino` is relative to the turn: `expectations`, `userInput.parameterScope`,
    `userInput.input.dtmf.transformed`.
    """
    turno = anida(camino, valor)
    entrada = {"text": {"text": "hola"}, "languageCode": "es"}
    ui = turno.get("userInput")
    if isinstance(ui, dict):
        ui.setdefault("input", entrada)
    else:
        turno = {"userInput": {"input": entrada}, **turno}
    creado = cons.request("POST", cons._url(f"{abase}/testCases"), body={
        "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
        "notes": "temporal: medir campo interno",
        "testCaseConversationTurns": [turno],
    })
    nombre = creado.get("name") or ""
    ctx.setdefault("casos_creados", []).append(nombre)
    if not nombre:
        raise RuntimeError("the test case could not be created")
    return cons.request("GET", cons._url("/" + nombre))


def _intent(cons: ConsoleApi, abase: str, ctx: Ctx,
            camino: str, valor: Any) -> Any:
    """PATCH of the intent with updateMask=trainingPhrases and re-read.

    MEASURED: the training phrase requires `repeatCount >= 1` ("Training
    phrase repeat count should be greater than 0.").
    """
    url = f"{abase}/intents/{ctx['intent_id']}"
    partes = ctx["training_parts"]
    nueva = dict(partes)
    nueva[camino.split(".")[-1]] = valor
    cons.request("PATCH", cons._url(url),
                 body={"trainingPhrases": [{"parts": [nueva],
                                            "repeatCount": 1}]},
                 update_mask="trainingPhrases")
    return cons.request("GET", cons._url(url))


ESTRATEGIAS: dict[str, Estrategia] = {
    "agent": _agente_advanced_settings,
    "flow": _flow_advanced_settings,
    "queryinput": _query_input,
    "webhook": _webhook,
    "eventhandler": _event_handler,
    "testcase": _test_case,
    "intent": _intent,
}

# Where the value is re-read inside the resource (None = it is not re-read).
LECTURA: dict[str, str] = {
    "agent": "",            # the same path that was written
    "flow": "",
    "queryinput": "",       # not re-read: it is input-only
    "webhook": "",
    "eventhandler": "eventHandler.",
    "testcase": "testCaseConversationTurns[0].",
    "intent": "trainingPhrases[0].parts[0].",
}


# --------------------------------------------------------------- the cases

CASOS: list[dict[str, Any]] = [
    # --- AdvancedSettingsSpeechSettings (13) ---------------------------
    {"id": "speech.disableLlmEndpointing", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.disableLlmEndpointing"},
    {"id": "speech.disableTimeoutBasedEndpointing", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.disableTimeoutBasedEndpointing"},
    {"id": "speech.enableAggressiveEndpointing", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.enableAggressiveEndpointing"},
    {"id": "speech.enableLlmEndpointing", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.enableLlmEndpointing"},
    {"id": "speech.enableSmartEndpointing", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.enableSmartEndpointing"},
    {"id": "speech.fallbackModels", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.fallbackModels"},
    {"id": "speech.fixTranscripts", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.fixTranscripts"},
    {"id": "speech.geminiAsrConfig", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.geminiAsrConfig"},
    {"id": "speech.minDigits", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.minDigits"},
    {"id": "speech.overrideRequestSpeechModel", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.overrideRequestSpeechModel"},
    {"id": "speech.useGeminiAsr", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.useGeminiAsr"},
    {"id": "speech.useSttV2", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.useSttV2"},
    {"id": "speech.utteranceWaitTimeout", "estrategia": "agent",
     "camino": "advancedSettings.speechSettings.utteranceWaitTimeout"},
    # --- AdvancedSettingsDtmfSettings (4) ------------------------------
    {"id": "dtmf.customRule", "estrategia": "flow",
     "camino": "advancedSettings.dtmfSettings.customRule"},
    {"id": "dtmf.disableSpeechDetection", "estrategia": "flow",
     "camino": "advancedSettings.dtmfSettings.disableSpeechDetection"},
    {"id": "dtmf.entityType", "estrategia": "flow",
     "camino": "advancedSettings.dtmfSettings.entityType"},
    {"id": "dtmf.useSystemEntityRule", "estrategia": "flow",
     "camino": "advancedSettings.dtmfSettings.useSystemEntityRule"},
    # --- QueryInput (5): INPUT-ONLY fields of detectIntent -------------
    {"id": "queryinput.alternativeLanguageCodes", "estrategia": "queryinput",
     "camino": "alternativeLanguageCodes"},
    {"id": "queryinput.multiPartyTranscripts", "estrategia": "queryinput",
     "camino": "multiPartyTranscripts"},
    {"id": "queryinput.partialTranscript", "estrategia": "queryinput",
     "camino": "partialTranscript"},
    {"id": "queryinput.speechTranscripts", "estrategia": "queryinput",
     "camino": "speechTranscripts"},
    {"id": "queryinput.tokenizedAudio", "estrategia": "queryinput",
     "camino": "tokenizedAudio"},
    # --- Webhook (3) ----------------------------------------------------
    {"id": "webhook.cloudFunction", "estrategia": "webhook",
     "camino": "cloudFunction"},
    {"id": "webhook.webhookFakeConfig", "estrategia": "webhook",
     "camino": "webhookFakeConfig"},
    {"id": "webhook.etag", "estrategia": "webhook", "camino": "etag"},
    # --- EventHandler (2) ----------------------------------------------
    {"id": "eventhandler.condition", "estrategia": "eventhandler",
     "camino": "condition"},
    {"id": "eventhandler.enableNonConsecutiveTriggering", "estrategia": "eventhandler",
     "camino": "enableNonConsecutiveTriggering"},
    # --- TestCase (3) ---------------------------------------------------
    {"id": "testcase.expectations", "estrategia": "testcase",
     "camino": "expectations"},
    {"id": "testcase.parameterScope", "estrategia": "testcase",
     "camino": "userInput.parameterScope"},
    {"id": "testcase.dtmf.transformed", "estrategia": "testcase",
     "camino": "userInput.input.dtmf.transformed"},
    # --- IntentTrainingPhrasePart (1) ----------------------------------
    {"id": "intent.multiIntentIndicator", "estrategia": "intent",
     "camino": "multiIntentIndicator"},
    # --- CONTROLS: PUBLIC fields that DO exist (they validate the probe) --
    {"id": "control.agent", "estrategia": "agent", "control": True,
     "camino": "advancedSettings.speechSettings.useTimeoutBasedEndpointing"},
    {"id": "control.flow", "estrategia": "flow", "control": True,
     "camino": "advancedSettings.speechSettings.useTimeoutBasedEndpointing"},
    {"id": "control.queryinput", "estrategia": "queryinput", "control": True,
     "camino": "languageCode"},
    {"id": "control.webhook", "estrategia": "webhook", "control": True,
     "camino": "timeout"},
    {"id": "control.eventhandler", "estrategia": "eventhandler", "control": True,
     "camino": "event"},
    {"id": "control.testcase", "estrategia": "testcase", "control": True,
     "camino": "userInput.isWebhookEnabled"},
    {"id": "control.testcase.turno", "estrategia": "testcase", "control": True,
     "camino": "userInput"},
    {"id": "control.intent", "estrategia": "intent", "control": True,
     "camino": "text"},
]


def _preparar(cons: ConsoleApi, abase: str, ctx: Ctx) -> None:
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
    base_handlers = leer(cons.request(
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
    filtro = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = load_config()
    cons = ConsoleApi(cfg)
    base = f"/projects/{cfg.project}/locations/{cfg.location}/agents"
    res: dict[str, Any] = {"casos": []}

    creado = cons.request("POST", cons._url(base), body={
        "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
        "defaultLanguageCode": "es", "timeZone": "America/Santiago",
        "description": "temporal: verificar campos internos",
    })
    agente = creado.get("name") or ""
    if not agente:
        print("the agent could not be created")
        return 1
    cfg.agent_readwrite = list(cfg.agent_readwrite) + [agente.rsplit("/", 1)[-1]]
    abase = base + "/" + agente.rsplit("/", 1)[-1]
    print("lab agent:", agente)
    ctx = Ctx({"session": f"lab-{uuid.uuid4().hex[:8]}", "casos_creados": []})

    # ---- resources needed by the other strategies ---------------------
    try:
        _preparar(cons, abase, ctx)
    except Exception:
        cons.request("DELETE", cons._url("/" + agente))
        raise
    print("flow", ctx["flow_id"], "| webhook", ctx["webhook_id"],
          "| intent", ctx["intent_id"])

    try:
        for caso in CASOS:
            if filtro and filtro not in caso["id"] and not caso.get("control"):
                continue
            escribir = ESTRATEGIAS[caso["estrategia"]]
            fila: dict[str, Any] = {"id": caso["id"], "camino": caso["camino"],
                                     "estrategia": caso["estrategia"]}

            # ---- 1) DOES IT EXIST? (and of what type) --------------------
            sondas: dict[str, str] = {}
            for nombre, valor in SONDAS:
                try:
                    escribir(cons, abase, ctx, caso["camino"], valor)
                except Exception as exc:
                    sondas[nombre] = mensaje_de(exc)
                else:
                    sondas[nombre] = ""
            info = clasificar(sondas)
            fila.update({"estado": info["estado"], "tipo": info["tipo"],
                         "repetido": info["repetido"], "prueba": info["prueba"],
                         "sondas": {k: v[:220] for k, v in sondas.items() if v}})
            if info["estado"] == "desconocido":
                fila["escritura"] = "ignorado"
                fila["nota"] = ("the backend does NOT know it: no probe gives an "
                                "error (same behaviour as an invented name) and "
                                "nothing is saved")
                print(f"{caso['id']:<40} -> IGNORED (silently ignored)")
                res["casos"].append(fila)
                continue

            # ---- 2) IS IT ACCEPTED / DOES IT PERSIST? -------------------
            valor = valor_por_tipo(info["tipo"])
            try:
                leido = escribir(cons, abase, ctx, caso["camino"], valor)
            except Exception as exc:
                fila["escritura"] = "no_aceptado"
                fila["error"] = mensaje_de(exc)[:300]
                print(f"{caso['id']:<40} -> {info['tipo'][:40]:<40} NOT ACCEPTED: "
                      f"{fila['error'][:80]}")
                res["casos"].append(fila)
                continue
            prefijo = LECTURA[caso["estrategia"]]
            valor_leido = leer(leido, prefijo + caso["camino"]) if leido else None
            if valor_leido is None and leido:
                valor_leido = _buscar(leido, (prefijo + caso["camino"]).split("."))
            fila["valor_escrito"] = valor
            fila["valor_leido"] = valor_leido
            if caso["estrategia"] == "queryinput":
                fila["escritura"] = "solo_entrada"
            elif valor_leido is None:
                fila["escritura"] = "descartado"
            else:
                fila["escritura"] = "escribible"
            print(f"{caso['id']:<40} -> {info['tipo'][:40]:<40} "
                  f"{fila['escritura']:<14} read="
                  f"{json.dumps(valor_leido, ensure_ascii=False)[:40]}")
            res["casos"].append(fila)
    finally:
        for nombre in ctx.get("casos_creados", []):
            try:
                cons.request("POST", cons._url(f"{abase}/testCases:batchDelete"),
                             body={"names": [nombre]})
            except Exception:
                pass
        try:
            cons.request("DELETE", cons._url("/" + agente))
            print("lab agent deleted")
        except Exception as exc:
            print("Could NOT delete the agent:", str(exc)[:200])

    Path("/tmp/campos_internos.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    # It is accumulated in a single file so that no measurement is lost per group.
    todo = Path("/tmp/campos_internos_all.json")
    acumulado: list[dict[str, Any]] = []
    if todo.exists():
        try:
            acumulado = json.loads(todo.read_text(encoding="utf-8")).get("casos", [])
        except Exception:
            acumulado = []
    por_id = {c["id"]: c for c in acumulado}
    por_id.update({c["id"]: c for c in res["casos"]})
    todo.write_text(json.dumps({"casos": list(por_id.values())},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    print("-> /tmp/campos_internos.json and /tmp/campos_internos_all.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
