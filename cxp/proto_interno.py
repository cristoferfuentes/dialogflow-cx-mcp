"""The gaps of FIELD in the Dialogflow CX proto INTERNAL (v3beta1).

Why it exists
-------------
The public *discovery doc* says which fields the API v3beta1 declares. The
**internal proto** of the console has more: `targetPlaybook`, `ssml`, `description` in
the intent parameters, `convertPhoneticAlphabets`, the 13 options of
`AdvancedSettingsSpeechSettings`...

Where the list comes from
-------------------------
From the **console bundle**: its client code carries the internal proto
minified. The class names are minified (`xC`, `fid`...), but
the **field names are not** (`this.j.versionConfigs=...`) and each message
declares its list of keys (`keys:[...]` or `keys:"...".split(" ")`).

The method (`probes/exp_proto_interno_bundle.py`):
  1. the bundle is downloaded and its 1536 classes and their fields are extracted;
  2. the 709 schemas of the public discovery doc (v3beta1) are read;
  3. each public schema is matched with the internal class that contains ALL
     its public fields and additionally provides extra fields (the internal ones). If two
     classes tie, the match is dropped as ambiguous;
  4. the extra fields are the gaps.

What this list does NOT say
---------------------------
That the backend **knows** a field does not mean it **accepts** it: the public
proto silently drops what it does not declare. The write status of each
gap is measured **by writing and re-reading** (in a temporary lab agent, `probes/exp_verifica_campos_internos.py`):

    "escribible"   -> the backend KEEPS it (verified by reading it back)
    "ignorado"     -> the backend does NOT know it: it ignores it silently, just
                      like an invented name (no probe gives an error)
    "descartado"   -> the backend knows it but EMPTIES it when written
    "derivado"     -> the server computes it: it can be read but not written
    "solo_entrada" -> it exists and is accepted in the request, but it cannot be
                      read (the `QueryInput` fields)
    "no_aceptado"  -> the backend knows it but REJECTS the value
    "no_probado"   -> the internal proto has it; its write has not been tested

=> the error reveals the name and the TYPE of the field. With an invented name there
is no error (mandatory positive control in each probe).

This catalog is the **systematic** list of what the public API lacks:
`cx_gaps` computes the gaps over the real data (and therefore only sees what
the agent has), whereas this list is computed over the complete internal
proto (and therefore sees what the agent does not have)."""
from __future__ import annotations

from typing import Any

# The MEASURED catalog (2026-11-16). Each entry:
# schema:  the public message
# fields:  the fields the internal proto has and the public one does not declare
# write:   what the backend does if they are sent (measured)
# types:   the type the parser error reveals (measured)
HUECOS_PROTO_INTERNO: list[dict[str, Any]] = [
    {
        "esquema": "FormParameter",
        "campos": ["convertPhoneticAlphabets", "description"],
        "escritura": {
            "description": "escribible",
            "convertPhoneticAlphabets": "escribible",
        },
        "tipos": {},
        "nota": "Medido en el agente de laboratorio: los dos se conservan al "
                "crear la pagina y al releerla. `description` es la "
                "descripcion del parametro de formulario (la API publica "
                "tampoco la puede leer).",
    },
    {
        "esquema": "QueryInput",
        "campos": ["alternativeLanguageCodes", "multiPartyTranscripts",
                   "partialTranscript", "speechTranscripts", "tokenizedAudio"],
        "escritura": {
            "alternativeLanguageCodes": "solo_entrada",
            "multiPartyTranscripts": "ignorado",
            "partialTranscript": "no_aceptado",
            "speechTranscripts": "solo_entrada",
            "tokenizedAudio": "ignorado",
        },
        "tipos": {
            "alternativeLanguageCodes": "repeated string",
            "partialTranscript": "PartialTranscriptInput",
            "speechTranscripts": "TranscriptInput",
        },
        "nota": "Datos de voz. `alternativeLanguageCodes` (repeated string) y "
                "`speechTranscripts` (repeated TranscriptInput) se ACEPTAN en "
                "la peticion de `detectIntent` (son de solo entrada: no se "
                "pueden leer). `partialTranscript` existe (PartialTranscriptInput) "
                "pero el backend responde 'Unknown type for QueryInput.' "
                "`multiPartyTranscripts` y `tokenizedAudio` NO los conoce: los "
                "ignora en silencio. TRAMPA: `detectIntent` exige un tipo de "
                "entrada (si no, 'Unknown type for QueryInput.' TAPA el error "
                "del campo) y los campos de voz estan en el `oneof` `input` "
                "junto al texto.",
    },
    {
        "esquema": "TransitionRoute",
        "campos": ["targetPlaybook"],
        "escritura": {"targetPlaybook": "escribible"},
        "tipos": {},
        "nota": "El destino a playbook. El proto publico declara un `oneof` "
                "con `target_page` y `target_flow`, pero no `target_playbook`: "
                "la API publica lo descarta en silencio.",
    },
    {
        "esquema": "Webhook",
        "campos": ["cloudFunction", "etag", "webhookFakeConfig"],
        "escritura": {
            "cloudFunction": "no_aceptado",
            "etag": "ignorado",
            "webhookFakeConfig": "escribible",
        },
        "tipos": {
            "cloudFunction": "Webhook.CloudFunction",
            "webhookFakeConfig": "Webhook.WebhookFakeConfig",
        },
        "nota": "`webhookFakeConfig` (una configuracion de prueba de la "
                "consola) se escribe y se relee. `etag` NO lo conoce el "
                "backend (lo ignora en silencio). `cloudFunction` SI lo "
                "conoce (el error del parser delata el tipo "
                "`Webhook.CloudFunction`, que tiene un campo `uri`), pero es "
                "IMPOSIBLE de dejar puesto: esta en el `oneof` `webhook` "
                "(junto a `genericWebService` y `serviceDirectory`) y el "
                "validador del backend exige siempre un "
                "`genericWebService.uri` (\"URI of generic web service should "
                "be specified.\"), tambien en un agente regional.",
    },
    {
        "esquema": "EventHandler",
        "campos": ["condition", "enableNonConsecutiveTriggering"],
        "escritura": {
            "condition": "escribible",
            "enableNonConsecutiveTriggering": "ignorado",
        },
        "tipos": {"condition": "string"},
        "nota": "`condition` (string) es una expresion que se guarda en el "
                "manejador de eventos del flow. `enableNonConsecutiveTriggering` "
                "NO lo conoce el backend.",
    },
    {
        "esquema": "AdvancedSettingsDtmfSettings",
        "campos": ["customRule", "disableSpeechDetection", "entityType",
                   "useSystemEntityRule"],
        "escritura": {
            "customRule": "ignorado",
            "disableSpeechDetection": "ignorado",
            "entityType": "ignorado",
            "useSystemEntityRule": "ignorado",
        },
        "tipos": {},
        "nota": "Los cuatro existen en el proto INTERNO de la consola pero el "
                "backend NO los conoce: los ignora en silencio. Medido en el "
                "`advancedSettings.dtmfSettings` del AGENTE, del FLOW y de la "
                "PAGE (y con el nombre en camelCase y en snake_case). O sea: "
                "el modelo de la consola va por delante del del backend.",
    },
    {
        "esquema": "AdvancedSettingsSpeechSettings",
        "campos": ["disableLlmEndpointing", "disableTimeoutBasedEndpointing",
                   "enableAggressiveEndpointing", "enableLlmEndpointing",
                   "enableSmartEndpointing", "fallbackModels", "fixTranscripts",
                   "geminiAsrConfig", "minDigits", "overrideRequestSpeechModel",
                   "useGeminiAsr", "useSttV2", "utteranceWaitTimeout"],
        "escritura": {
            "disableLlmEndpointing": "escribible",
            "disableTimeoutBasedEndpointing": "escribible",
            "enableAggressiveEndpointing": "escribible",
            "enableLlmEndpointing": "escribible",
            "enableSmartEndpointing": "escribible",
            "fallbackModels": "ignorado",
            "fixTranscripts": "escribible",
            "geminiAsrConfig": "escribible",
            "minDigits": "escribible",
            "overrideRequestSpeechModel": "ignorado",
            "useGeminiAsr": "escribible",
            "useSttV2": "escribible",
            "utteranceWaitTimeout": "escribible",
        },
        "tipos": {
            "disableLlmEndpointing": "bool",
            "disableTimeoutBasedEndpointing": "bool",
            "enableAggressiveEndpointing": "bool",
            "enableLlmEndpointing": "bool",
            "enableSmartEndpointing": "bool",
            "fixTranscripts": "bool",
            "geminiAsrConfig": "InputAudioConfig.GeminiAsrConfig",
            "minDigits": "int32",
            "useGeminiAsr": "bool",
            "useSttV2": "bool",
            "utteranceWaitTimeout": "google.protobuf.Duration",
        },
        "nota": "11 de los 13 se escriben y se releen (endpointing con LLM, "
                "ASR de Gemini, `useSttV2`, digitos minimos, tiempo de "
                "espera...). Los 2 que no (`fallbackModels` y "
                "`overrideRequestSpeechModel`) NO los conoce el backend: los "
                "ignora en silencio.",
    },
    {
        "esquema": "ConversationTurnUserInput",
        "campos": ["parameterScope"],
        "escritura": {"parameterScope": "escribible"},
        "tipos": {"parameterScope": "string"},
        "nota": "El ambito de los parametros inyectados en un turno: se "
                "escribe en el turno del caso de prueba y se relee.",
    },
    {
        "esquema": "IntentParameter",
        "campos": ["description"],
        "escritura": {"description": "escribible"},
        "tipos": {},
        "nota": "Medido: la descripcion de un parametro de intent se "
                "conserva. La API publica declara `IntentParameter` sin "
                "`description` (y sin `displayName`).",
    },
    {
        "esquema": "AdvancedSettingsLoggingSettings",
        "campos": ["disableLogging"],
        "escritura": {"disableLogging": "descartado"},
        "tipos": {},
        "nota": "Medido: al hacer PATCH de `advancedSettings`, el servidor "
                "VACIA el submensaje (`loggingSettings` queda en `{}`). Es el "
                "mismo comportamiento que `advancedSettings.enabled`.",
    },
    {
        "esquema": "IntentTrainingPhrasePart",
        "campos": ["auto", "multiIntentIndicator"],
        "escritura": {"auto": "derivado", "multiIntentIndicator": "ignorado"},
        "tipos": {},
        "nota": "`auto` lo calcula el servidor (marca la frase como "
                "autogenerada). `multiIntentIndicator` existe en el proto "
                "interno de la consola pero NO lo conoce el backend.",
    },
    {
        "esquema": "ConversationTurn",
        "campos": ["expectations"],
        "escritura": {"expectations": "ignorado"},
        "tipos": {},
        "nota": "Las expectativas de un turno de caso de prueba. El proto "
                "publico de `ConversationTurn` solo declara `userInput` y "
                "`virtualAgentOutput`; el backend tampoco acepta `expectations`.",
    },
    {
        "esquema": "DtmfInput",
        "campos": ["transformed"],
        "escritura": {"transformed": "escribible"},
        "tipos": {"transformed": "bool"},
        "nota": "El DTMF ya transformado (lo que se usa de verdad): se "
                "escribe en el turno del caso de prueba y se relee.",
    },
    {
        "esquema": "ResponseMessageText",
        "campos": ["ssml"],
        "escritura": {"ssml": "descartado"},
        "tipos": {},
        "nota": "Medido: al mandar `ssml` en "
                "`entryFulfillment.messages[].text` el servidor lo descarta "
                "(deja solo `text`). El proto interno lo tiene, asi que puede "
                "que viva en otro sitio (p. ej. `playAudio` o un canal "
                "concreto).",
    },
]

# The valid states (checked by the tests).
ESTADOS_ESCRITURA = {"escribible", "ignorado", "descartado", "derivado",
                      "solo_entrada", "no_aceptado", "no_probado"}


def catalogo_proto_interno() -> list[dict[str, Any]]:
    """The gap catalog of the internal proto (for the MCP)."""
    return [dict(h) for h in HUECOS_PROTO_INTERNO]


def resumen() -> dict[str, Any]:
    """Summary by write status (what is actually known)."""
    estados: dict[str, list[str]] = {}
    for h in HUECOS_PROTO_INTERNO:
        for campo, estado in (h.get("escritura") or {}).items():
            estados.setdefault(estado, []).append(f"{h['esquema']}.{campo}")
    total_campos = sum(len(h["campos"]) for h in HUECOS_PROTO_INTERNO)
    sin_medir = [f"{h['esquema']}.{c}" for h in HUECOS_PROTO_INTERNO
                 for c in h["campos"] if c not in (h.get("escritura") or {})]
    return {
        "esquemas": len(HUECOS_PROTO_INTERNO),
        "campos": total_campos,
        "medidos": total_campos - len(sin_medir),
        "sin_medir": sorted(sin_medir),
        "por_estado": {k: sorted(v) for k, v in sorted(estados.items())},
    }
