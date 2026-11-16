# Verification report — `dialogflow-cx-mcp`

Generated 2026-11-16 11:45 -03 · project `my-project` · location `global`

Full verification of the MCP: tests, smoke test, cleanup, internal proto catalogue,
**live measurement** of the 31 fields that were still missing, and a real write of an
internal field with the MCP's own tool.

## 1. Unit tests

- ✅ `pytest tests/ -q` → 76 passed in 0.17s
- total tests: 76

## 2. Smoke test

- ✅ `smoke_test.py` → ALL OK

## 3. Cleanup (temporary resources and lab agents)

- ✅ no leftovers: 0 environments, 0 test cases, 0 flow versions (12 flows), 0
  playbook/tool versions
- ✅ no orphaned lab agent in the known locations

## 4. Internal proto catalogue

- ✅ 38/38 fields MEASURED (14 schemas)
- ✅ every state is valid
- ✅ none of the 38 fields is declared by the public v3beta1 discovery doc (they are
  still gaps)

## 5. Live measurement of the fields (lab agent)

- live measurement: 31 fields in 188 s
- ✅ the 31 states measured live MATCH the catalogue

| Field | Measured type | Catalogue | Live | |
|---|---|---|---|---|
| `QueryInput.alternativeLanguageCodes` | TYPE_STRING | solo_entrada | solo_entrada | ✅ |
| `QueryInput.multiPartyTranscripts` | — | ignorado | ignorado | ✅ |
| `QueryInput.partialTranscript` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.PartialTranscriptInput | no_aceptado | no_aceptado | ✅ |
| `QueryInput.speechTranscripts` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.TranscriptInput | solo_entrada | solo_entrada | ✅ |
| `QueryInput.tokenizedAudio` | — | ignorado | ignorado | ✅ |
| `Webhook.cloudFunction` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.Webhook.CloudFunction | no_aceptado | no_aceptado | ✅ |
| `Webhook.etag` | — | ignorado | ignorado | ✅ |
| `Webhook.webhookFakeConfig` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.Webhook.WebhookFakeConfig | escribible | escribible | ✅ |
| `EventHandler.condition` | TYPE_STRING | escribible | escribible | ✅ |
| `EventHandler.enableNonConsecutiveTriggering` | — | ignorado | ignorado | ✅ |
| `AdvancedSettingsDtmfSettings.customRule` | — | ignorado | ignorado | ✅ |
| `AdvancedSettingsDtmfSettings.disableSpeechDetection` | — | ignorado | ignorado | ✅ |
| `AdvancedSettingsDtmfSettings.entityType` | — | ignorado | ignorado | ✅ |
| `AdvancedSettingsDtmfSettings.useSystemEntityRule` | — | ignorado | ignorado | ✅ |
| `AdvancedSettingsSpeechSettings.disableLlmEndpointing` | TYPE_BOOL | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.disableTimeoutBasedEndpointing` | TYPE_BOOL | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.enableAggressiveEndpointing` | TYPE_BOOL | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.enableLlmEndpointing` | TYPE_BOOL | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.enableSmartEndpointing` | TYPE_BOOL | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.fallbackModels` | — | ignorado | ignorado | ✅ |
| `AdvancedSettingsSpeechSettings.fixTranscripts` | TYPE_BOOL | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.geminiAsrConfig` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.InputAudioConfig.GeminiAsrConfig | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.minDigits` | TYPE_INT32 | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.overrideRequestSpeechModel` | — | ignorado | ignorado | ✅ |
| `AdvancedSettingsSpeechSettings.useGeminiAsr` | TYPE_BOOL | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.useSttV2` | TYPE_BOOL | escribible | escribible | ✅ |
| `AdvancedSettingsSpeechSettings.utteranceWaitTimeout` | type.googleapis.com/google.protobuf.Duration | escribible | escribible | ✅ |
| `ConversationTurnUserInput.parameterScope` | TYPE_STRING | escribible | escribible | ✅ |
| `IntentTrainingPhrasePart.multiIntentIndicator` | — | ignorado | ignorado | ✅ |
| `ConversationTurn.expectations` | — | ignorado | ignorado | ✅ |
| `DtmfInput.transformed` | TYPE_BOOL | escribible | escribible | ✅ |

## 6. Real write of an internal field (`cx_set_console_field`)

- ✅ 3 internal fields written with the MCP's tool and verified by reading back

| Field | Written | Read back | |
|---|---|---|---|
| `advancedSettings.speechSettings.enableLlmEndpointing` | true | true | ✅ |
| `advancedSettings.speechSettings.utteranceWaitTimeout` | "2s" | "2s" | ✅ |
| `advancedSettings.speechSettings.minDigits` | 4 | 4 | ✅ |

- lab agent deleted

## Verdict

**ALL OK** — every check passes.

_(end of the verification, 11:50:13)_

> Reproduce it yourself: `python probes/verificacion_final.py` (it needs credentials
> and a writable agent of your own).
