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

The method (`probes/exp_internal_proto_bundle.py`):
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
gap is measured **by writing and re-reading** (in a temporary lab agent, `probes/exp_verify_internal_fields.py`):

    "writable"    -> the backend KEEPS it (verified by reading it back)
    "ignored"     -> the backend does NOT know it: it ignores it silently, just
                      like an invented name (no probe gives an error)
    "dropped"     -> the backend knows it but EMPTIES it when written
    "derived"     -> the server computes it: it can be read but not written
    "input_only"  -> it exists and is accepted in the request, but it cannot be
                      read (the `QueryInput` fields)
    "not_accepted" -> the backend knows it but REJECTS the value
    "not_tested"  -> the internal proto has it; its write has not been tested

=> the error reveals the name and the TYPE of the field. With an invented name there
is no error (mandatory positive control in each probe).

This catalogue is the **systematic** list of what the public API lacks:
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
INTERNAL_PROTO_GAPS: list[dict[str, Any]] = [
    {
        "schema": "FormParameter",
        "fields": ["convertPhoneticAlphabets", "description"],
        "write": {
            "description": "writable",
            "convertPhoneticAlphabets": "writable",
        },
        "types": {},
        "note": "Measured in the lab agent: both are kept when creating the "
                "page and when reading it back. `description` is the "
                "description of the form parameter (the public API cannot "
                "read it either).",
    },
    {
        "schema": "QueryInput",
        "fields": ["alternativeLanguageCodes", "multiPartyTranscripts",
                   "partialTranscript", "speechTranscripts", "tokenizedAudio"],
        "write": {
            "alternativeLanguageCodes": "input_only",
            "multiPartyTranscripts": "ignored",
            "partialTranscript": "not_accepted",
            "speechTranscripts": "input_only",
            "tokenizedAudio": "ignored",
        },
        "types": {
            "alternativeLanguageCodes": "repeated string",
            "partialTranscript": "PartialTranscriptInput",
            "speechTranscripts": "TranscriptInput",
        },
        "note": "Voice data. `alternativeLanguageCodes` (repeated string) and "
                "`speechTranscripts` (repeated TranscriptInput) are ACCEPTED in "
                "the `detectIntent` request (they are input-only: they cannot "
                "be read). `partialTranscript` exists (PartialTranscriptInput) "
                "but the backend answers 'Unknown type for QueryInput.' "
                "`multiPartyTranscripts` and `tokenizedAudio` are NOT known to "
                "it: it ignores them silently. TRAP: `detectIntent` requires "
                "an input type (otherwise 'Unknown type for QueryInput.' "
                "HIDES the error of the field) and the voice fields live in the "
                "`oneof` `input` next to the text.",
    },
    {
        "schema": "TransitionRoute",
        "fields": ["targetPlaybook"],
        "write": {"targetPlaybook": "writable"},
        "types": {},
        "note": "The playbook destination. The public proto declares a `oneof` "
                "with `target_page` and `target_flow`, but not `target_playbook`: "
                "the public API drops it silently.",
    },
    {
        "schema": "Webhook",
        "fields": ["cloudFunction", "etag", "webhookFakeConfig"],
        "write": {
            "cloudFunction": "not_accepted",
            "etag": "ignored",
            "webhookFakeConfig": "writable",
        },
        "types": {
            "cloudFunction": "Webhook.CloudFunction",
            "webhookFakeConfig": "Webhook.WebhookFakeConfig",
        },
        "note": "`webhookFakeConfig` (a console test configuration) is written "
                "and read back. The backend does NOT know `etag` (it ignores "
                "it silently). It DOES know `cloudFunction` (the parser error "
                "gives away the type `Webhook.CloudFunction`, which has a "
                "`uri` field), but it is IMPOSSIBLE to set: it is in the "
                "`oneof` `webhook` (next to `genericWebService` and "
                "`serviceDirectory`) and the backend validator always requires "
                "a `genericWebService.uri` (\"URI of generic web service should "
                "be specified.\"), also in a regional agent.",
    },
    {
        "schema": "EventHandler",
        "fields": ["condition", "enableNonConsecutiveTriggering"],
        "write": {
            "condition": "writable",
            "enableNonConsecutiveTriggering": "ignored",
        },
        "types": {"condition": "string"},
        "note": "`condition` (string) is an expression that is stored in the "
                "flow's event handler. The backend does NOT know "
                "`enableNonConsecutiveTriggering`.",
    },
    {
        "schema": "AdvancedSettingsDtmfSettings",
        "fields": ["customRule", "disableSpeechDetection", "entityType",
                   "useSystemEntityRule"],
        "write": {
            "customRule": "ignored",
            "disableSpeechDetection": "ignored",
            "entityType": "ignored",
            "useSystemEntityRule": "ignored",
        },
        "types": {},
        "note": "The four exist in the INTERNAL proto of the console but the "
                "backend does NOT know them: it ignores them silently. "
                "Measured in the `advancedSettings.dtmfSettings` of the AGENT, "
                "of the FLOW and of the PAGE (and with the name in camelCase "
                "and in snake_case). In other words: the console model runs "
                "ahead of the backend model.",
    },
    {
        "schema": "AdvancedSettingsSpeechSettings",
        "fields": ["disableLlmEndpointing", "disableTimeoutBasedEndpointing",
                   "enableAggressiveEndpointing", "enableLlmEndpointing",
                   "enableSmartEndpointing", "fallbackModels", "fixTranscripts",
                   "geminiAsrConfig", "minDigits", "overrideRequestSpeechModel",
                   "useGeminiAsr", "useSttV2", "utteranceWaitTimeout"],
        "write": {
            "disableLlmEndpointing": "writable",
            "disableTimeoutBasedEndpointing": "writable",
            "enableAggressiveEndpointing": "writable",
            "enableLlmEndpointing": "writable",
            "enableSmartEndpointing": "writable",
            "fallbackModels": "ignored",
            "fixTranscripts": "writable",
            "geminiAsrConfig": "writable",
            "minDigits": "writable",
            "overrideRequestSpeechModel": "ignored",
            "useGeminiAsr": "writable",
            "useSttV2": "writable",
            "utteranceWaitTimeout": "writable",
        },
        "types": {
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
        "note": "11 of the 13 are written and read back (LLM endpointing, "
                "Gemini ASR, `useSttV2`, minimum digits, wait time...). The 2 "
                "that are not (`fallbackModels` and "
                "`overrideRequestSpeechModel`) are NOT known to the backend: it "
                "ignores them silently.",
    },
    {
        "schema": "ConversationTurnUserInput",
        "fields": ["parameterScope"],
        "write": {"parameterScope": "writable"},
        "types": {"parameterScope": "string"},
        "note": "The scope of the parameters injected in a turn: it is written "
                "in the turn of the test case and read back.",
    },
    {
        "schema": "IntentParameter",
        "fields": ["description"],
        "write": {"description": "writable"},
        "types": {},
        "note": "Measured: the description of an intent parameter is kept. The "
                "public API declares `IntentParameter` without `description` "
                "(and without `displayName`).",
    },
    {
        "schema": "AdvancedSettingsLoggingSettings",
        "fields": ["disableLogging"],
        "write": {"disableLogging": "dropped"},
        "types": {},
        "note": "Measured: when PATCHing `advancedSettings`, the server EMPTIES "
                "the submessage (`loggingSettings` ends up as `{}`). It is the "
                "same behaviour as `advancedSettings.enabled`.",
    },
    {
        "schema": "IntentTrainingPhrasePart",
        "fields": ["auto", "multiIntentIndicator"],
        "write": {"auto": "derived", "multiIntentIndicator": "ignored"},
        "types": {},
        "note": "The server computes `auto` (it marks the phrase as "
                "auto-generated). `multiIntentIndicator` exists in the internal "
                "proto of the console but the backend does NOT know it.",
    },
    {
        "schema": "ConversationTurn",
        "fields": ["expectations"],
        "write": {"expectations": "ignored"},
        "types": {},
        "note": "The expectations of a test case turn. The public proto of "
                "`ConversationTurn` only declares `userInput` and "
                "`virtualAgentOutput`; the backend does not accept `expectations` "
                "either.",
    },
    {
        "schema": "DtmfInput",
        "fields": ["transformed"],
        "write": {"transformed": "writable"},
        "types": {"transformed": "bool"},
        "note": "The already transformed DTMF (what is actually used): it is "
                "written in the turn of the test case and read back.",
    },
    {
        "schema": "ResponseMessageText",
        "fields": ["ssml"],
        "write": {"ssml": "dropped"},
        "types": {},
        "note": "Measured: when `ssml` is sent in "
                "`entryFulfillment.messages[].text` the server drops it "
                "(only `text` remains). The internal proto has it, so it may "
                "live somewhere else (e.g. `playAudio` or a specific "
                "channel).",
    },
]

# The valid states (checked by the tests).
WRITE_STATES = {"writable", "ignored", "dropped", "derived",
                 "input_only", "not_accepted", "not_tested"}


def internal_proto_catalogue() -> list[dict[str, Any]]:
    """The gap catalogue of the internal proto (for the MCP)."""
    return [dict(h) for h in INTERNAL_PROTO_GAPS]


def summary() -> dict[str, Any]:
    """Summary by write status (what is actually known)."""
    states: dict[str, list[str]] = {}
    for h in INTERNAL_PROTO_GAPS:
        for field_name, state in (h.get("write") or {}).items():
            states.setdefault(state, []).append(f"{h['schema']}.{field_name}")
    total_fields = sum(len(h["fields"]) for h in INTERNAL_PROTO_GAPS)
    unmeasured = [f"{h['schema']}.{c}" for h in INTERNAL_PROTO_GAPS
                  for c in h["fields"] if c not in (h.get("write") or {})]
    return {
        "schemas": len(INTERNAL_PROTO_GAPS),
        "fields": total_fields,
        "measured": total_fields - len(unmeasured),
        "unmeasured": sorted(unmeasured),
        "by_status": {k: sorted(v) for k, v in sorted(states.items())},
    }
