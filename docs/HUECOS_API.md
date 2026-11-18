# What the public API cannot do

The long version of the "holes" that `dialogflow-cx-mcp` fills in.

> **TL;DR**
> The Dialogflow CX **public API** (`dialogflow.googleapis.com/v3beta1`) and the
> **console's internal backend** (`dialogflow.clients6.google.com/v3beta1`) serve the
> *same discovery doc* — but **not the same proto**. The console's proto knows fields
> the public descriptor does not even declare. Through the public API those fields are
> **silently dropped**: the call returns `200` and nothing is saved.
>
> This document is the catalogue of those holes, measured against a real agent.
> The README explains *what the MCP is for*; this explains *the holes themselves*.

**How to read this document**

| if you want to… | go to |
|---|---|
| know which fields you can **write** | [Writable gaps](#1-what-can-be-written) |
| know which fields are **read-only** | [Read-only gaps](#2-what-can-only-be-read) |
| see the **systematic** list (38 fields, 14 schemas) | [The console's internal proto](#3-the-systematic-list-the-consoles-internal-proto) |
| point a **transition route at a playbook** | [`targetPlaybook`](#4-the-big-one-targetplaybook) |
| call an **endpoint that is in no descriptor** | [Method gaps](#5-method-gaps-endpoints-in-no-descriptor) |
| understand **how it was measured** | [The type-error trick](#6-how-it-is-measured-the-type-error-trick) |

---

## 0. Why the internal backend

The public proto of `TransitionRoute` is a `oneof` with `target_page` and
`target_flow`. **`target_playbook` does not exist.** A `PATCH` that sends it is
**silently dropped**: the route is left without a target and the agent falls into
`NO_MATCH`.

The console's internal proto is richer: it knows fields the public API does not even
declare in its descriptor.

That is why comparing the two descriptors finds nothing (they are identical): what
differs is **which fields each proto accepts**. And that is why this MCP:

* talks to the console's backend (first-party authentication: your Google cookies plus
  a `SAPISIDHASH` signature), and
* carries a layer that **compares the real JSON against the public schema** to detect the
  fields the public API cannot express.

Fine print: **use `v3beta1`, never `v3`**. In `v3`, fields such as
`llmModelSettings.parameters` do not exist and are silently dropped.

---

## 1. What can be WRITTEN

Measured: written **and read back**.

| Resource | Field | Tool |
|---|---|---|
| `Page`, `Flow`, `TransitionRouteGroup` | `transitionRoutes[].targetPlaybook` | `cx_set_route_playbook`, `cx_set_flow_route_playbook`, `cx_set_group_route_playbook` |
| `Page` | `form.parameters[].description` | `cx_set_console_field` |
| `Page` | `form.parameters[].convertPhoneticAlphabets` | `cx_set_console_field` |
| `Intent` | `parameters[].description` | `cx_set_console_field` |
| `EntityType` | `entities[].dtmfPattern` | `cx_set_console_field` |
| `Agent` | `webClientSettings` (e.g. `boxBorderRadius`) | `cx_set_console_field` |

### The 11 speech settings that the public API cannot touch

The most useful block of writable gaps is `AdvancedSettingsSpeechSettings`: **11 of its
13 fields** can be written only through the console's backend. Without them the public
API cannot enable LLM endpointing, Gemini ASR or transcript correction.

| field | what it turns on |
|---|---|
| `enableLlmEndpointing` | endpointing driven by the LLM instead of the classic one |
| `enableAggressiveEndpointing` | cut the user's silence earlier |
| `disableTimeoutBasedEndpointing` | never end the turn on a timer |
| `disableLlmEndpointing` | turn LLM endpointing back off |
| `enableSmartEndpointing` | smart endpointing |
| `useGeminiAsr` | Gemini as the ASR engine |
| `geminiAsrConfig` | its configuration (`InputAudioConfig.GeminiAsrConfig`) |
| `fixTranscripts` | fix the transcripts (`bool`) |
| `useSttV2` | the v2 speech-to-text |
| `minDigits` | minimum digits before the turn is closed (`int32`) |
| `utteranceWaitTimeout` | how long to wait (`Duration`) |

The two that **cannot** be written are `fallbackModels` and
`overrideRequestSpeechModel` (the backend does not know them).

Writing one of them (with `cx_set_console_field`, which verifies by reading back):

```jsonc
// cx_set_console_field(resource="agent",
//                      field="advancedSettings.speechSettings.enableLlmEndpointing",
//                      value=true, dry_run=false)
// -> {"mode": "written", "saved": true, ...}
```

---

## 2. What can only be READ

The server derives them: you can read them, but not write them.

| Resource | Field | Why |
|---|---|---|
| `Tool` | `actionSchemas`, `sharedSchemas` | the server derives them from `openApiSpec`/`dataStoreSpec`; writing them changes nothing |
| `Agent`/`Page`/`Flow` | `advancedSettings.enabled`, `.playbackInterruptionSettings`, `.speechAdaptationSettings` | when the message is written, the server empties the unknown subfields |
| `Agent`/`Page`/`Flow` | `advancedSettings.loggingSettings.disableLogging` | on `PATCH` of `advancedSettings`, the server empties the submessage (`loggingSettings` stays `{}`) |
| `Agent` | `speakerIdSettings` | the server returns it as `{}` and drops whatever is written to it |
| `Flow` | `modelIdentifier`, `nluSettingsList`, `nluSettings.customConfigKey` | the server regenerates them |
| `Intent` | `trainingPhrases[].parts[].auto` | marker for an auto-generated phrase |
| `Page` | `entryFulfillment.messages[].text.ssml` | the internal proto has it, but the server drops it when written there |
| `Tool` | `dataStoreSpec.models`, `.groundingSettings` | summary model and grounding of the data store tool |
| `Agent` | `projectNumber` | the project number the console sees |
| `Playbook` | `speechAdaptationSettings` | speech adaptation settings |
| `Environment` | `etag` | the server computes it |
| `Tool` | `dataStoreSpec.models`, `.groundingSettings` | summary model and grounding of the data store tool |

---

## 3. The systematic list: the console's internal proto

`cx_gaps` sees the gaps **live**, on the agent's real data: if the agent has no
`Environment`, it does not see its gaps. For that there is `cx_proto_interno`: the
console's **internal** proto (the one it really uses) carries **more fields** than the
public descriptor, and it is recovered from the **console bundle** (its client-side code
ships the proto minified: the class names are minified, but the **field names are not**).

Method: 1536 classes from the bundle × 709 schemas from the discovery doc, paired by
"the class contains all the public fields of the schema **and** contributes extra
fields" (a tie is discarded as ambiguous).

| Schema | Internal fields the public API does not declare | Measured state |
|---|---|---|
| `FormParameter` | `convertPhoneticAlphabets`, `description` | writable |
| `QueryInput` | `alternativeLanguageCodes`, `multiPartyTranscripts`, `partialTranscript`, `speechTranscripts`, `tokenizedAudio` | 2 input-only, 1 not accepted, 2 ignored |
| `TransitionRoute` | `targetPlaybook` | writable |
| `Webhook` | `cloudFunction`, `etag`, `webhookFakeConfig` | 1 writable, 1 not accepted, 1 ignored |
| `EventHandler` | `condition`, `enableNonConsecutiveTriggering` | 1 writable, 1 ignored |
| `AdvancedSettingsDtmfSettings` | `customRule`, `disableSpeechDetection`, `entityType`, `useSystemEntityRule` | all 4 ignored |
| `AdvancedSettingsSpeechSettings` | 13 fields (LLM endpointing, Gemini ASR, fallback models…) | 11 writable, 2 ignored |
| `ConversationTurnUserInput` | `parameterScope` | writable |
| `IntentParameter` | `description` | writable |
| `AdvancedSettingsLoggingSettings` | `disableLogging` | dropped |
| `IntentTrainingPhrasePart` | `auto`, `multiIntentIndicator` | 1 derived, 1 ignored |
| `ConversationTurn` | `expectations` | ignored |
| `DtmfInput` | `transformed` | writable |
| `ResponseMessageText` | `ssml` | dropped |

**All 38 fields are measured** (none is left untested), by writing and reading back in a
temporary lab agent (`probes/exp_verify_internal_fields.py`).

| State | What it means |
|---|---|
| `writable` | the backend **keeps it**: it is written and read back |
| `ignored` | the backend **does not know it**: it is silently ignored, like an invented name |
| `dropped` | it knows it, but **empties** it when written |
| `derived` | the server **computes** it (readable, not writable) |
| `input_only` | it exists and is accepted in the request, but **cannot be read** |
| `not_accepted` | it knows it, but **rejects** the value |

### The result: 12 fields the backend does not know

`fallbackModels`, `overrideRequestSpeechModel`, `customRule`,
`disableSpeechDetection`, `entityType`, `useSystemEntityRule`,
`multiPartyTranscripts`, `tokenizedAudio`, `etag`,
`enableNonConsecutiveTriggering`, `expectations`, `multiIntentIndicator`.

This is not a failure of the public API: **the console's model runs ahead of the
backend's**. Those fields exist in the proto the console ships, but the backend does not
have them in its descriptor and silently ignores them (measured in the agent's, the
flow's and the page's `advancedSettings.dtmfSettings`, and with the name in both
camelCase and snake_case).

---

## 4. The big one: `targetPlaybook`

The public v3beta1 API has **three** ways of reaching a playbook, and none of them is
equivalent to "one transition -> one playbook":

| public route | exists? | equivalent to "route -> playbook"? |
|---|---|---|
| `EventHandler.targetPlaybook` | ✅ | ❌ the `EventHandler` has **no `condition`**: it fires on an event, not on session parameters |
| `Agent.startPlaybook` | ✅ | ❌ it is an entry point of the **agent**, not of a flow or a route |
| `Playbook.referencedPlaybooks` / `inlineActions` | ✅ but `readOnly: true` | ❌ they cannot be written |

And what **does not exist**: `TransitionRoute.targetPlaybook`. Since the
`TransitionRoute` is **the same message** in the three places it lives (the page, the
flow's router and the route group), the gap repeats in all three, and the MCP covers all
three:

| level | covers | what it enables |
|---|---|---|
| **page** | `cx_set_route_playbook` | "given a condition on THIS page -> playbook X" |
| **flow** | `cx_set_flow_route_playbook` | "**on entering** this flow -> playbook X" (impossible via the public API: a flow is only entered with `targetFlow` or `Agent.startFlow`) |
| **route group** | `cx_set_group_route_playbook` | one route shared by several pages -> playbook X |

Measured behavioural differences:

* **Scope**: flow-level routes are **inherited by every page of the flow** (page-level
  routes take priority over them). Only flow routes **with an intent** are inherited;
  condition-only ones apply at flow entry.
* **Runtime**: it runs at both levels (measured with a playbook with its own marker:
  `matchType=PLAYBOOK`, `currentPlaybooks=[the new playbook]`, and the response was its
  exact text).
* **Writes**: 1 (flow, with intent) versus N (one per page).

⚠️ **`verified: true` is an API check, not a runtime check.** The CX runtime runs
**behind** the API: after reverting a route and deleting a playbook, the `GET` showed a
clean state while the runtime kept executing the old route (`HTTP 404 "Action with id
<deleted playbook> not found"`) until it caught up. Practical consequence: **do not
delete a playbook that a route points at without removing the route first.**

---

## 5. Method gaps: endpoints in no descriptor

The public *discovery doc* and the internal backend's one are **the same document**: both
669,652 bytes and the **same canonical md5**
`29adf74f6e0f0e4dede7fefeb5de774e` (the only difference is the **order of the JSON
keys**). The same 162 methods in v3beta1. In other words: **no method is "missing"**
from the descriptor. What is missing are the **endpoints the console uses and that are in
neither of them**. They were found by capturing the console's real traffic (444
requests) and replaying each one by hand:

| endpoint | version | what it is for | state |
|---|---|---|---|
| `GET /projects` | `v3alpha1` | the project list (the console's project picker); it calls it with `filter=id="…"` and `view=FULL` | ✅ 200 |
| `systemEntityTypes:list` | `v3alpha1` | the system entity types (`sys.date`, `sys.date-period`…) | ✅ 200 |
| `agents/{a}/currentUsers` | `v3alpha1` | who currently has access to the agent | ✅ 200 |
| `users/me/userSettings/recentProjects` | `v3alpha1` | the projects you opened recently (**absolute path from the root**) | ✅ 200 |
| `agents/{a}:listAvailableModels` | `v3alpha1` | the available generative models and their limits | ✅ 200 |
| `agents/{a}:getSupportedLanguages` | `v3alpha1` | the languages the agent supports | ✅ 200 |
| `projects/{p}/agent/visibility` | `v2beta1` | agent visibility (**without `locations`**) | ✅ 200 (empty) |
| `projects/{p}/locations/{l}/conversationProfiles` | `v2beta1` | the conversation profiles of the old CX model | ✅ 200 |
| `projects/{p}/locations/{l}/locationSettings` | `v3` | settings at location level | ⚠️ 404 if the project does not have them |

They are reachable with `cx_api` (with `version=` and absolute paths) or directly with
`cx_endpoints_consola(endpoint="…")`.

> **The console speaks `v3alpha1`**, not v3beta1. That is why `cx_api` accepts
> `version=`: there is data (and endpoints) that only come out in another version.

---

## 6. How it is measured (the type-error trick)

The backend *knowing* a field does not mean it *accepts* it. To find out you do not need
to capture the console writing: the backend **fails with the type** when the field
exists and the value does not fit, and **silently ignores** names it does not know:

```
Invalid value at 'agent.advanced_settings.speech_settings.fix_transcripts' (TYPE_BOOL), "x"
Invalid value at 'query_input.speech_transcripts' (type.googleapis.com/…TranscriptInput), "x"
Invalid JSON payload received. Unknown name "uri" at 'webhook.cloud_function': Proto field is not repeating, cannot start list.
```

=> the error reveals the **name and the type** of the field. An invented name produces no
error (a positive control is mandatory in every probe: without it, "ignored" could be a
false negative). **Seven values** are sent (object, string, integer, boolean and three
lists) and the type is deduced from the error path; the `[0]` index betrays a *repeated*
field.

Step 2 is to write a **valid** value of the measured type and read the resource back: if
the value is not there, the backend dropped it.

Traps on this path (all measured):

* `detectIntent` requires an input type: without text it answers *"Unknown type for
  QueryInput."* and that error **masks** the field's error. And the voice fields live in
  the `input` `oneof` **next to the text**: sending them with text gives *"oneof field
  'input' is already set"*.
* The `PATCH` of `eventHandlers` **replaces** the array: the default handlers must be
  sent back or the server answers *"You cannot delete event handler for
  'sys.no-input-default' in the flow"*.
* A training phrase requires `repeatCount >= 1`.
* `cloudFunction` is in the `webhook` `oneof` and the backend's validator **always**
  requires a `genericWebService.uri` => it is impossible to set.
* `serviceDirectory` (public) only works on **regional agents** (*"Service Directory
  webhook is only supported for regional agents"*).

---

## 7. Coverage: what the sweep reaches (and what it does not)

`cx_gaps` walks the agent, compares every resource with the public schema and returns
what it finds. The walk is **generic**: it comes from the descriptor
(`colecciones_listables()`), not from a hand-written list. It covers the 24 collections
v3beta1 declares — flat (`intents`, `tools`, `playbooks`, `webhooks`, `generators`,
`entityTypes`, `changelogs`, `conversations`…) and **nested** (`flows.pages`,
`tools.versions`, `playbooks.versions`, `playbooks.examples`, `flows.versions`,
`flows.transitionRouteGroups`, `testCases`, `testCases.results`, `environments`,
`environments.deployments`, `environments.experiments`,
`environments.continuousTestResults`, `environments.sessions.entityTypes`,
`sessions.entityTypes`)—. For the nested ones the ancestor has to be resolved
(`flows.pages` needs a flow): up to `max_parents` candidates are tried, so that a flow
without pages does not mask one that has them.

The output says **what was covered and what was not** (`coverage` and `unreachable`):
an agent not having `environments` is not an MCP failure, it means the resource does not
exist there.

### Full coverage (`include_lab=True`)

Six collections hang off resources a normal agent does not have, and the generic sweep
cannot invent them:

| collection | why it is not reachable in the working agent |
|---|---|
| `environments`, `.deployments`, `.experiments`, `.continuousTestResults`, `.sessions.entityTypes` | they hang off an **environment**, and one has to be created |
| `testCases`, `testCases.results` | they hang off a **test case**, and one has to be created and run |

`cx_gaps(include_lab=True)` creates a **temporary lab agent** (one flow, one page, no
system tools), sweeps it **completely** and **deletes** it when finished.

**The three rules for creating an environment** (measured 2026-11-15; the public
descriptor does not state them, which is why the `create` fails):

1. You must include the **version of every flow** of the agent, and also the one of the
   *start flow*: `"Version must be provided for start flow resource"`.
2. And the **version of every playbook** the graph references (transitive closure):
   `"Playbook 'P' is referenced in version 'V' of flow 'F', but not included in the
   environment"`.
3. And the **version of the tools** those playbooks reference.

The trick (and the finding): **`versionConfigs[].version` is not just a flow**. It is a
**generic** resource name: it also accepts `.../playbooks/{id}/versions/{v}` and
`.../tools/{id}/versions/{v}`. With that, the server's error **advances to the next
level** and so the whole chain is discovered.

> ⚠️ **The working agent CANNOT have an environment**: its graph references
> `df-code-interpreter-tool` (a system tool), which cannot be versioned
> (`POST tools/…/versions` -> 404 *"does not exist in the agent"*), has no versions,
> and its name does not match the version template. That is why the `environments*`
> sweep is done in the lab agent.

With `include_lab=True` coverage is **23/24**: the remaining one (`sessions.entityTypes`
at agent level) **does not exist in the internal proto** — its real name is
`projects/*/…/environments/*/sessions/*/entityTypes/*`, and at agent level it gives a
404. The sweep marks it with its environment-level equivalent.

What shows up once those six collections are covered (and **was not** visible before):

| gap | why it matters |
|---|---|
| `Environment.etag` | the public `Environment` proto does not declare it (the server computes it) |
| `TestRunDifference.actualValue` / `.expectedValue` / `.assertionType` / `.warning` | the public proto only declares `type` and `description`: through the public API **you cannot know what differs** in a failed test case |

---

## 8. How to use the gaps from the MCP

| tool | what it does |
|---|---|
| `cx_gaps` | **detects live** the fields the backend exposes and the public API does not declare. It walks **every** collection of the descriptor (24 in v3beta1, including the nested ones), and also returns the measured catalogue and the **method** gaps. With `include_lab=True` it also sweeps the `environments*` family (and `testCases*`) in a **temporary lab agent** -> coverage **23/24** |
| `cx_proto_interno` | the **systematic** list of gaps: 14 schemas / 38 fields, with the write state of each one **measured** |
| `cx_endpoints_consola` | the **measured** catalogue of the endpoints the console calls and that **are in no descriptor**, plus the call to any of them |
| `cx_set_console_field` | **writes any field** (including out-of-schema ones) by path: `transitionRoutes[0].targetPlaybook`, `form.parameters[0].description`, `entities[0].dtmfPattern`… It verifies by reading back and says what was saved and what was lost |
| `cx_set_route_playbook` | playbook target of a **page** transition route |
| `cx_set_flow_route_playbook` | playbook target of a **flow router** transition route |
| `cx_set_group_route_playbook` | playbook target of a **route group** transition route |

`cx_gaps` is the part that **does not depend on that list**: it walks the agent, compares
every resource with the public schema and returns what it finds. If Google adds a new
field to the console tomorrow, it shows up in `cx_gaps` without touching the code.

### The writing recipe (4 steps, always the same)

```
GET resource  ->  the value is applied on the path  ->  PATCH  ->  GET and compare
```

The final comparison is the guarantee: the server may drop a field silently (that is
what the public API does), and the response says so (`verified`,
`dropped_by_the_server`, `changes`).

1. **Discover** the field with `cx_gaps` (or look at the `catalogue`): it tells you the
   resource, the schema and the state (`writable` / `derived`).
2. **Simulate** with `dry_run=true`: it returns the expected diff and the `updateMask`
   that will be sent.
3. **Write** with `dry_run=false`.
4. **Read** the result: `verified: true` and `dropped_by_the_server` (empty =
   it was saved). If the field shows up in `dropped_by_the_server`, the server
   derives it: it is read-only.

---

## See also

* [README](../README.md) — what the MCP is for, install, credentials, tools.
* [LIMITES_API.md](LIMITES_API.md) — the measured limits and traps of the API.
* [VERIFICATION.md](VERIFICATION.md) — the report of the run that measured all of this.
