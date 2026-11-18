# Measured limits of the API

Everything the official descriptor **does not** tell you, measured against the real
backend. The MCP carries this list in memory: call `cx_limites_api()` and it comes back
with the exact symptom, the cause and the workaround.

* [The limits, one by one](#the-limits-one-by-one)
* [Performance constants](#performance-constants)
* [First-party authentication](#first-party-authentication)
* [Every location has its host](#every-location-has-its-host)
* [Versions](#versions)
* [Network](#network)
* [Traps at a glance](#traps-at-a-glance)

---

## The limits, one by one

| id | what happens | workaround |
|---|---|---|
| `delete_force` | `DELETE` of a referenced resource -> 400 *"Please set 'force' to true"* | `force=true` on flows/tools/webhooks/entityTypes/generators/transitionRouteGroups (**not** on playbooks nor intents) -> delete in dependency order |
| `escrituras_serializadas` | performance stalls: POST 1->1.57 · 2->1.01 · 3->1.03 · 4->1.34 · 6->1.14 · 8->1.12 ops/s | **create in series**; **deletion** does tolerate **2 in parallel** (+20 %) |
| `batch_404` | `/v3beta1/batch` -> **404** on the internal backend | `cx_batch` falls back to **threads**; never count on `/batch` |
| `latencia_por_tamano` | +50 KB of body ≈ +600 ms | do not duplicate round-trips: verify **at the end**, not after each write |
| `flow_ignora_pages` | `POST flow` ignores the `pages` in the body | one POST per page |
| `destinos_validados` | 400 *"Target page … does not exist in the flow"* / *"Referenced resource X does not exist"* | create in dependency order; for cycles, create without routes and patch afterwards |
| `ciclos_paginas` | in a page cycle the route cannot be validated when created | order the pages to break the cycles: **1 extra PATCH per page in the cycle** |
| `rutas_en_el_post` | a flow created without routes and patched later = 2 writes | if **all** its routes are already resolvable, put them in the flow's `POST` |
| `end_flow` | the end-of-flow target is `.../flows/{flow}/pages/END_FLOW` | remap **every** `resource/id` pair of the name |
| `playbook_loop` | 400 *"Playbook X caused loop in playbook routes"* | a self-referencing playbook cannot be created or updated through the API |
| `macros_validadas` | the `${PLAYBOOK:x}`/`${FLOW:x}`/`${TOOL:x}` macros in the text are validated | create the playbooks in topological order |
| `campos_solo_lectura` | `referencedFlows`/`referencedPlaybooks`/`inlineActions` are derived; `defaultLanguageCode` cannot be updated | do not send them when writing |
| `campos_fuera_de_esquema` | a PATCH of a field the public API does not declare returns 200 and **the field is not there** | write against the internal backend and **verify by reading back** (`cx_set_console_field`) |
| `patch_etag_obsoleto` | PATCH with the old `etag` -> **409** *"Others may have made recent changes…"* | remove the `etag` from the body (the console does not send it either) |
| `ruta_sin_intent_ni_condition` | 400 *"At least one of intent or condition in transition route should be specified"* | always put `condition` or `intent` in the route |
| `ruta_sin_destino` | 400 *"At least one of trigger fulfillment, target page, target flow, or target playbook…"* (in route groups) | give the group a target when creating it |
| `subcampos_desconocidos` | writing `advancedSettings` empties its unknown subfields | they are read-only (`estado: derivado` in the catalogue) |
| `action_schemas_derivadas` | writing `Tool.actionSchemas` changes nothing | read-only: change the `openApiSpec`, not the schemas |
| `tipos_no_declarados_se_validan` | 400 *"Invalid value at 'agent.web_client_settings' (TYPE_STRING)"* | copy the value the resource already has and only change the type |
| `tool_builtin` | `df-*` tools cannot be created or deleted | skip them (they exist in every agent) |
| `tool_openapi_vacio` | 400 *"Null or empty definition"* (duplicated tools without `textSchema`) | recreate them **without** `openApiSpec` (documented deviation) |
| `tool_datastore_borrado` | 400 *"Failed to connect datastores … do not exist"* | recreate **without** `dataStoreSpec` (documented deviation) |
| `start_flow_protegido` | the `Default Start Flow` cannot be deleted nor emptied of its handlers | do not delete it: PATCH `transitionRoutes` and nothing else |
| `guard_metodo_propio` | `agents/{id}:export` was rejected ("no tiene permiso") | fixed in `cxp/guard.py` (the `:method` suffix is split off) |
| `cookies_jar_incompleto` | with **only the 5 cookies** -> **401** *"Request had invalid authentication credentials"* | send the **complete** `.google.com` jar (23 cookies): `CXP_COOKIE` as-is |
| `page_size_maximo_100` | `pageSize=200` -> **400** *"Page size should be within [0, 100]."* (on several endpoints; on `flows` 200 is fine) | ask for <=100: `cxp.gaps._listar()` retries only with 50, 20 and 5 |
| `detect_intent_exige_language_code` | **400** *"Field 'query_input.language_code' is missing."* | the language goes in **`queryInput.languageCode`**, not in `queryInput.text.languageCode` (that form gives 400) |
| `entorno_exige_todos_los_flows` | creating an environment -> 400 *"Flow 'X' … is not included in the environment"* (then *"Playbook 'P' … is not included"*) | publish a version of **each** flow and put them all in `versionConfigs` |
| `test_cases_sin_delete` | `DELETE agents/{a}/testCases/{id}` -> **404** (the method does not exist) | `POST agents/{a}/testCases:batchDelete` with `{"names": [...]}` |
| `endpoints_fuera_del_descriptor` | the UI shows data the public API **does not expose** (system entity types, who has access, which models exist…) | `cx_endpoints_consola` calls them; `cx_api` with `version=` and absolute paths reaches them |

---

## Performance constants

| constant | value |
|---|---|
| writes on the same agent | serialized: **~1.5-2 ops/s** whatever the concurrency |
| deletions of independent items | tolerate **2 in parallel** (+20 %) |
| reads | they **do** parallelize (8 GETs ≈ 1 s) |
| latency by body size | +50 KB ≈ +600 ms |
| typical call (`cx_get_flow`) | 2.663 s -> **0.402 s** (keep-alive + catalogue cache) |

**Fewer writes > faster writes**: since the backend serializes, the only way to lower
the total time is to **write less**.

---

## First-party authentication

The minimum headers the backend accepts (verified by capture, not from memory):

```
authorization: SAPISIDHASH {ts}_{h} SAPISID1PHASH {ts}_{h} SAPISID3PHASH {ts}_{h}
cookie:        the COMPLETE jar of .google.com session cookies (not just 5)
x-origin:      <CXP_CONSOLE_ORIGIN>
x-referer:     <CXP_CONSOLE_ORIGIN>
x-goog-authuser: 0
referer:       https://dialogflow.clients6.google.com/static/proxy.html?usegapi=1
```

- `h = sha1(f"{ts} {value_of_SAPISID} {console_origin}")`, `ts` = current epoch. The
  three hashes are equal (the console uses `SAPISID` for all three). **It is re-signed
  on every call** (measured delta: 0 s).
- **Do not send an `Origin` header**: the server requires `Origin` and `Referer` to
  match -> HTTP 400 *"Origin and Referer header don't match"*.
- With `Bearer` (service account token) the backend answers **404 "Method not found"**:
  it requires first-party.
- HTTP/1.1 is fine: h2 is not needed.
- Only cookies from the `.google.com` domain travel: a cookie from **another** domain
  invalidates the credential (401).
- **The complete jar, not just the 5**: measured, with 5 cookies -> 401 and with the 23
  from `.google.com` -> 200, with the same binary and the same `.env`. Session cookies
  are revalidated; if you only send 5 of 23 you get locked out when the session renews.

**Writing.** `PATCH .../flows/{FLOW}?updateMask=transitionRoutes` with the **complete**
array of `transitionRoutes` in the body (same as the console). The `etag` is
deliberately omitted: the internal `GET` does not return it and a stale etag makes the
`PATCH` fail.

---

## Every location has its host

(and the `referer` goes with it)

Measured (2026-11-15): the console calls **17 regional hosts**
(`{location}-dialogflow.clients6.google.com`) to list the agents of every location, and
the global host **does not** serve the others:

```
GET https://dialogflow.clients6.google.com/v3beta1/projects/p/locations/us-central1/agents
  -> 400 "Please refer to .../region#avail to find the correct endpoint"
```

The headers **do not change with the host**, except the `referer`:

| header | value | why |
|---|---|---|
| `authorization` (SAPISIDHASH) | signed over the **console origin** | the same for every host |
| `x-origin`, `x-referer` | the **console origin** | otherwise -> 401 *"invalid authentication credentials"* |
| `referer` | `{HOST}/static/proxy.html?usegapi=1` | otherwise -> 400 *"Origin doesn't match Host for XD3"* |

The MCP applies it on its own: the `referer` is derived from the host of the URL. To
talk to another location, `cx_api(location="us-central1")` (or
`cx_list_agents(todas_las_locations=True)`, which is what the console does).

---

## Versions

`dialogflow.clients6.google.com` answers on `/v3alpha1`, `/v3beta1`, `/v3` and
`/v2beta1`, and serves its **discovery doc** on each one (`$discovery/rest?version=…`).
That is why the mirror can be "v3beta1" without hand-writing the 162 methods: the map
comes from the descriptor.

Methods each version declares (counted on the real descriptor):

| version | methods | relation |
|---|---|---|
| `v3beta1` | **162** | **superset**: none of `v3`'s nor `v3alpha1`'s is missing from it |
| `v3` | 158 | 4 of them are missing from `v3beta1` |
| `v3alpha1` | 8 | all 8 are also in `v3beta1` |
| `v2beta1` | — | no descriptor; only `agent/visibility` and `conversationProfiles` |

> **The public descriptor and the internal backend's one are THE SAME DOCUMENT**
> (measured: 669,652 bytes each and the same canonical md5
> `29adf74f6e0f0e4dede7fefeb5de774e`; only the order of the JSON keys changes). That is
> why looking for "which method is missing" by comparing descriptors **finds nothing**:
> what is missing are endpoints the console uses that are in **neither** of them (see
> [HUECOS_API.md](HUECOS_API.md#5-method-gaps-endpoints-in-no-descriptor)).

---

## Network

| risk | mitigation | where |
|---|---|---|
| TLS handshake on every request (0.66 s) | **a single `Session` for the whole server** -> keep-alive per thread | `server.py` (`_HTTP`/`_API`/`_MIRROR`) |
| Black-holed IP among the ones in `/etc/hosts` | **8 s connect timeout** (not 45) -> it falls through to the next IP quickly | `cxp/httpclient.py` |
| Huge payload (`list_playbooks` can be hundreds of KB) | **catalogue cache** (id->displayName) with a 120 s TTL and self-renewal | `cxp/console_api.py` |
| Transient network error | 1 retry, **network only** (an HTTP 4xx/5xx is not retried) | `cxp/httpclient.py` |
| Read timeout | 45 s (300 s for `detectIntent`/`train`/`validate`) | `cxp/config.py` |
| MTU / offloads / system DNS | **not touched**: the MCP does not modify system state | — |

---

## Traps at a glance

| symptom | cause |
|---|---|
| HTTP 404 "Method not found" | `Bearer` was used instead of first-party |
| HTTP 401 "invalid authentication credentials" | cookies are missing, cookies from other domains were sent, **or only the 5 were sent instead of the complete jar** |
| HTTP 400 "Origin and Referer don't match" | the `Origin` header was sent |
| The change does not appear | the `PATCH` does not include `targetPlaybook` (public proto) or the flow is not the one you thought |
| `no tiene permiso` | the agent is not in `CXP_AGENT_READ` / `CXP_AGENT_READWRITE` |
| The MCP subprocess does not see your variables | the MCP SDK does not inherit the environment: pass them in `env` |

---

## See also

* [README](../README.md) — install, credentials, tools.
* [HUECOS_API.md](HUECOS_API.md) — the fields the public API cannot express.
* [PROMPT_AGENTE.md](PROMPT_AGENTE.md) — the instructions to paste into the agent that uses this MCP.
