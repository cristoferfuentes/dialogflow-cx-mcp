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
| `QueryInput.alternativeLanguageCodes` | TYPE_STRING | input_only | input_only | ✅ |
| `QueryInput.multiPartyTranscripts` | — | ignored | ignored | ✅ |
| `QueryInput.partialTranscript` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.PartialTranscriptInput | not_accepted | not_accepted | ✅ |
| `QueryInput.speechTranscripts` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.TranscriptInput | input_only | input_only | ✅ |
| `QueryInput.tokenizedAudio` | — | ignored | ignored | ✅ |
| `Webhook.cloudFunction` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.Webhook.CloudFunction | not_accepted | not_accepted | ✅ |
| `Webhook.etag` | — | ignored | ignored | ✅ |
| `Webhook.webhookFakeConfig` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.Webhook.WebhookFakeConfig | writable | writable | ✅ |
| `EventHandler.condition` | TYPE_STRING | writable | writable | ✅ |
| `EventHandler.enableNonConsecutiveTriggering` | — | ignored | ignored | ✅ |
| `AdvancedSettingsDtmfSettings.customRule` | — | ignored | ignored | ✅ |
| `AdvancedSettingsDtmfSettings.disableSpeechDetection` | — | ignored | ignored | ✅ |
| `AdvancedSettingsDtmfSettings.entityType` | — | ignored | ignored | ✅ |
| `AdvancedSettingsDtmfSettings.useSystemEntityRule` | — | ignored | ignored | ✅ |
| `AdvancedSettingsSpeechSettings.disableLlmEndpointing` | TYPE_BOOL | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.disableTimeoutBasedEndpointing` | TYPE_BOOL | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.enableAggressiveEndpointing` | TYPE_BOOL | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.enableLlmEndpointing` | TYPE_BOOL | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.enableSmartEndpointing` | TYPE_BOOL | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.fallbackModels` | — | ignored | ignored | ✅ |
| `AdvancedSettingsSpeechSettings.fixTranscripts` | TYPE_BOOL | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.geminiAsrConfig` | type.googleapis.com/google.cloud.dialogflow.cx.v3beta1.InputAudioConfig.GeminiAsrConfig | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.minDigits` | TYPE_INT32 | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.overrideRequestSpeechModel` | — | ignored | ignored | ✅ |
| `AdvancedSettingsSpeechSettings.useGeminiAsr` | TYPE_BOOL | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.useSttV2` | TYPE_BOOL | writable | writable | ✅ |
| `AdvancedSettingsSpeechSettings.utteranceWaitTimeout` | type.googleapis.com/google.protobuf.Duration | writable | writable | ✅ |
| `ConversationTurnUserInput.parameterScope` | TYPE_STRING | writable | writable | ✅ |
| `IntentTrainingPhrasePart.multiIntentIndicator` | — | ignored | ignored | ✅ |
| `ConversationTurn.expectations` | — | ignored | ignored | ✅ |
| `DtmfInput.transformed` | TYPE_BOOL | writable | writable | ✅ |

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

> Reproduce it yourself: `python probes/final_verification.py` (it needs credentials
> and a writable agent of your own).
