"""LIMITS MEASURED of the internal API of Dialogflow CX (2026-11-08).

This module is the **memory of the MCP**: everything that was measured the hard way (the
exact error the API returns, why it returns it and how to work around it) lives
here as DATA, so that the next agent using the MCP does not stumble
on the same thing again or have to rediscover it through HTTP 400s.

It is queried with the `cx_limites_api` tool and is automatically appended to
the errors of the cloning pipeline (`cause` + `workaround`).

The limits of the official discovery doc (`discovery`) do NOT cover this: the public
discovery doc says that `referencedPlaybooks` is read-only and says nothing about
`force`, the `/batch`, playbook loops or latency."""
from __future__ import annotations

from typing import Any

# --- Performance constants (measured on this host, 2026-11-08) ------------
CONSTANTES: dict[str, Any] = {
    "ms_get": 450,
    "ms_post": 560,
    "ms_patch": 470,
    "ms_manual_tls": 660,
    "optimal_parallel_reads": 8,
    "parallel_writes": False,
    "optimal_parallel_delete": 2,
    "concurrency_curve_ops_per_s": {
        # median of 3 repetitions with warm-up (probe_write_concurrency2)
        "POST": {"1": 1.57, "2": 1.01, "3": 1.03, "4": 1.34, "6": 1.14, "8": 1.12},
        "DELETE": {"1": 1.75, "2": 2.10, "3": 1.50, "4": 1.34, "6": 1.21, "8": 1.14},
    },
    "note": (
        "The time goes into WAITING FOR HEADERS (~450 ms), not into the network: 8 "
        "parallel GETs take ~1 s (121 ms each). The backend SERIALIZES the writes "
        "of the same agent: THROUGHPUT plateaus at ~1.5-2 ops/s whatever the "
        "concurrency is (8 parallel POSTs = 899 ms each, WORSE than 500 ms in "
        "series). CREATION runs in series; DELETE allows 2 in parallel (2.10 ops/s "
        "against 1.75: +20%). With N>=3 the backend blocks and throughput DROPS. "
        "The cost scales with the body size (+50 KB = +600 ms). The /batch endpoint "
        "does NOT exist (404)."
    ),
}

# --- API limits (symptom -> cause -> how to work around it) ----------------
LIMITES: list[dict[str, Any]] = [
    {
        "id": "delete_force",
        "title": "DELETE with `force=true` (only on some resources)",
        "symptom": "HTTP 400 'X are referencing the resource. Please set "
                   "'force' to true or remove these references first'",
        "cause": "The DELETE validates the live references. `force` is in the "
                 "descriptor for flows, tools, webhooks, entityTypes, "
                 "generators and transitionRouteGroups, but NOT for playbooks "
                 "nor intents (even with `force` the playbook refuses).",
        "workaround": "Delete in dependency order: flows (with force) -> "
                      "playbooks (REVERSE dependency order) -> intents -> "
                      "entityTypes -> tools -> webhooks -> generators. The "
                      "flows take their pages with them, and with them the "
                      "references to playbooks.",
        "tags": ["delete", "force", "references"],
    },
    {
        "id": "batch_404",
        "title": "The `/batch` endpoint does not exist in the internal backend",
        "symptom": "HTTP 404 (Google error page) on POST to "
                   "/v3beta1/batch",
        "cause": "The descriptor declares `batchPath: batch`, but the internal "
                 "backend of the console (clients6) does not implement it.",
        "workaround": "`cx_batch` detects it and **falls back to threads** (the "
                      "same N sub-requests in parallel; it says so in `mode`). To "
                      "group calls by hand: threads in the MCP server "
                      "(parallel reads) — never /batch.",
        "tags": ["batch", "performance", "404"],
    },
    {
        "id": "serialized_writes",
        "title": "The backend serializes the writes of the same agent",
        "symptom": "8 parallel POSTs: 7.2 s (899 ms each) against "
                   "~4 s in series (500 ms each). Measured curve (median of "
                   "3 repetitions, ops/s): POST 1->1.57 | 2->1.01 | 3->1.03 | "
                   "4->1.34 | 6->1.14 | 8->1.12. DELETE 1->1.75 | 2->2.10 | "
                   "3->1.50 | 4->1.34 | 8->1.14.",
        "cause": "Write lock per agent (it recompiles the agent after every "
                 "change) + wait backoff. Throughput plateaus: parallelizing "
                 "more writes does NOT give more writes per second, only more "
                 "latency per write.",
        "workaround": "CREATION runs IN SERIES. DELETE allows 2 in parallel "
                      "(the items of the same group are independent): +20% "
                      "ops/s. Playbooks and intents are deleted IN SERIES (the "
                      "server imposes their order). Parallelism only helps READS "
                      "(8 GETs in ~1 s).",
        "tags": ["performance", "write", "concurrency"],
    },
    {
        "id": "page_cycles",
        "title": "Cyclic pages need an extra PATCH",
        "symptom": "A page whose route points to a page that does not exist "
                   "yet -> HTTP 400 'Target page ... does not exist'",
        "cause": "The API validates the destination at write time. In a CYCLE "
                 "of the page graph (measured: a cycle of 11 pages, one of 3 "
                 "and a self-reference) the page cannot be created with its "
                 "route: it has to be created without routes and patched "
                 "afterwards (1 extra PATCH per page of the cycle).",
        "workaround": "Order the pages to minimize the broken cycles: local "
                      "search over the cost 'pages with an unresolved "
                      "destination' (measured: 7 -> 5 PATCHes in a real agent "
                      "with 3 cycles). And note: `END_FLOW` and "
                      "`END_FLOW_WITH_HUMAN_ESCALATION` are NOT pages: they are "
                      "valid at any time (they do not count as a cycle).",
        "tags": ["creation", "pages", "topology", "cycles"],
    },
    {
        "id": "routes_in_the_post",
        "title": "The routes of a flow can go in its own POST",
        "symptom": "A flow created without routes and patched afterwards costs "
                   "2 writes (POST + PATCH) instead of 1.",
        "cause": "The API validates the destination on write, but if ALL the "
                 "routes of the flow point to things that already exist, they "
                 "can be sent in the POST (measured: `Flujo_Emision` and "
                 "`Flujo_limpiar_autenticacion` -> their routes go in the POST).",
        "workaround": "Create the flows in topological order (a flow AFTER "
                      "the flows its routes point to) and put in the POST the "
                      "routes that are already resolvable. If the destination is "
                      "a page or a playbook (they do not exist yet), the flow "
                      "needs the PATCH anyway. The default flow always needs a "
                      "PATCH (it already exists: it cannot be created).",
        "tags": ["creation", "flows", "routes", "performance"],
    },
    {
        "id": "latency_by_size",
        "title": "Write latency scales with the body size",
        "symptom": "minimal flow 559 ms; flow with 50 KB of description 1163 ms",
        "cause": "The backend parses and validates the full JSON on every "
                 "write (it is not network time).",
        "workaround": "There is not much to do: the content has to be sent. "
                      "But do not duplicate trips (no GET-verify after every "
                      "write: verify at the end).",
        "tags": ["performance", "write", "payload"],
    },
    {
        "id": "flow_ignores_pages",
        "title": "`POST flow` ignores the `pages` of the body",
        "symptom": "The flow is created but its pages are not (empty list)",
        "cause": "The backend does not create nested pages in the flow POST.",
        "workaround": "One POST per page, in topological order.",
        "tags": ["creation", "pages"],
    },
    {
        "id": "validated_destinations",
        "title": "The API validates ALL the destinations on write",
        "symptom": "HTTP 400 'Target page ... does not exist in the flow' / "
                   "'Referenced resource X does not exist' / 'Flow X does not "
                   "exist under the agent'",
        "cause": "targetPage, targetFlow, targetPlaybook, intent, "
                 "referencedTools, referencedFlows, referencedPlaybooks and the "
                 "${PLAYBOOK:}/${FLOW:}/${TOOL:} macros of the text are "
                 "checked against the state of the agent.",
        "workaround": "Create in dependency order: leaves (entityTypes, "
                      "intents, webhooks, tools) -> flows -> playbooks -> "
                      "generators -> pages (topological) -> route patches. For "
                      "cycles: create the page without routes and patch it "
                      "afterwards.",
        "tags": ["creation", "references", "topology"],
    },
    {
        "id": "end_flow",
        "title": "The end-of-flow destination is `.../flows/{flow}/pages/END_FLOW`",
        "symptom": "When cloning, the route keeps pointing to the flow of the "
                   "ORIGINAL",
        "cause": "It is a NESTED name: the flow INSIDE the name and the inner "
                 "page have to be remapped. If only the first pair is remapped, "
                 "the inner page keeps the old id.",
        "workaround": "Remap EVERY `resource/id` pair of the route (not just the "
                      "first one). `END_FLOW` is not a page: it does not count as "
                      "a pending destination.",
        "tags": ["creation", "remapping", "pages"],
    },
    {
        "id": "playbook_loop",
        "title": "A playbook that references itself is IMPOSSIBLE",
        "symptom": "HTTP 400 'Playbook X caused loop in playbook routes.'",
        "cause": "The API rejects the loops in the playbook graph. The "
                 "orchestrator of a real agent mentions itself "
                 "(${PLAYBOOK:<its own id>}) in a text that FORBIDS invoking "
                 "itself.",
        "workaround": "Neutralize ONLY that macro (inert token) and remove the "
                      "`referencedPlaybooks` entry. The rest of the macros are "
                      "left untouched. It is an unavoidable API deviation.",
        "tags": ["creation", "playbooks", "bucles"],
    },
    {
        "id": "validated_macros",
        "title": "The macros of the text are validated on create/update",
        "symptom": "HTTP 400 when creating a playbook with ${PLAYBOOK:x} if x "
                   "does not exist yet (or if x is the playbook itself)",
        "cause": "The backend resolves the macros of the instruction/codeBlock "
                 "against the agent.",
        "workaround": "Create the playbooks in topological order (their "
                      "references and those of other playbooks, first) and "
                      "neutralize only the self-reference.",
        "tags": ["creation", "playbooks", "macros"],
    },
    {
        "id": "read_only_fields",
        "title": "`referencedFlows`, `referencedPlaybooks` and `inlineActions` "
                  "are READ-ONLY (they are derived)",
        "symptom": "The POST ignores them (they come back null) and the PATCH "
                   "fails if they are sent: 'Agent default language code cannot "
                   "be updated' / 'Flow X does not exist'",
        "cause": "The backend computes them from the macros of the text and "
                 "from the flow/page references.",
        "workaround": "Do not send them on write: let the backend derive them. "
                      "`defaultLanguageCode` CANNOT be updated.",
        "tags": ["creation", "fields", "playbooks"],
    },
    {
        "id": "tool_builtin",
        "title": "Builtin tools cannot be created nor deleted",
        "symptom": "POST: 'Builtin tools are provided by Dialogflow and "
                   "should not be created manually'. DELETE: 404 'Tool "
                   "df-code-interpreter-tool does not exist'",
        "cause": "They belong to Dialogflow (id `df-*`) and already exist in any "
                 "agent.",
        "workaround": "Skip them: do not create, do not delete, and remap their "
                      "name to itself.",
        "tags": ["tools", "builtin"],
    },
    {
        "id": "tool_openapi_empty",
        "title": "A tool with an empty OpenAPI cannot be recreated",
        "symptom": "HTTP 400 'OpenApiSchemaParser$BadParseException: Found "
                   "errors in the Open API specification [Null or empty "
                   "definition]'",
        "cause": "In the original there are duplicated tools with `openApiSpec` "
                 "without `textSchema` (dated copies).",
        "workaround": "Create them WITHOUT `openApiSpec`/`dataStoreSpec`: the "
                      "item exists and the playbooks that reference it no "
                      "longer fail. It remains as a documented deviation.",
        "tags": ["tools", "fallback"],
    },
    {
        "id": "tool_datastore_deleted",
        "title": "A tool whose data store no longer exists cannot be recreated",
        "symptom": "HTTP 400 'Failed to connect datastores ... Data store(s) "
                   "... do not exist'",
        "cause": "The data store the tool points to was deleted: the original "
                 "references it and it no longer validates.",
        "workaround": "Fallback: create the tool WITHOUT `dataStoreSpec` "
                      "(same name and description) and note it as a deviation.",
        "tags": ["tools", "fallback", "datastore"],
    },
    {
        "id": "protected_start_flow",
        "title": "The default flow and the default intents are protected",
        "symptom": "DELETE flow: 'Cannot delete the start flow 00000000-...'. "
                   "PATCH of eventHandlers: 'You cannot delete event handler "
                   "for sys.no-match-default'",
        "cause": "The backend creates the `Default Start Flow` "
                 "(00000000-0000-0000-0000-000000000000) and the intents "
                 "00000000-...-0000/0001 in every agent.",
        "workaround": "Do not delete them: they are PATCHed so that their "
                      "content matches the original (and the default flow only "
                      "with `transitionRoutes`, without touching its handlers).",
        "tags": ["delete", "flows", "intents"],
    },
    {
        "id": "verify_at_the_end",
        "title": "Verifying after every write duplicates the traffic",
        "symptom": "A clone with a GET-verify per write takes ~2x",
        "cause": "Every write plus its verification GET is 2 trips of "
                 "~0.5 s.",
        "workaround": "Write without verifying and compare ONCE at the end "
                      "(counts + routing signature per page).",
        "tags": ["performance", "verificacion"],
    },
    {
        "id": "own_method_guard",
        "title": "The agent id arrives with the `:method` suffix in the own "
                  "methods",
        "symptom": "'the agent X:export has no permission' although X is in "
                   "the allowlist",
        "cause": "`agent_from_path` parsed `.../agents/{id}:export` and "
                 "compared `{id}:export` with the list.",
        "workaround": "Already fixed in `cxp/guard.py`: the `:method` suffix is "
                      "cut before comparing.",
        "tags": ["guards", "permissions"],
    },
    # ------------------------------------------------------------------
    # The limits of WRITE FIELDS OUTSIDE OF SCHEMA (measured with the
    # probe `probes/probe_console_only_writable3.py`).
    # ------------------------------------------------------------------
    {
        "id": "fields_outside_the_schema",
        "title": "The published API silently drops the fields it does not "
                  "declare; the console backend keeps some of them",
        "symptom": "A PATCH of a field the published API does not declare "
                   "returns HTTP 200 and the field is NOT there: it is lost "
                   "without any error. Through the published API it cannot be "
                   "READ either.",
        "cause": "The public v3beta1 proto has no `TransitionRoute."
                 "target_playbook`, `FormParameter.description`, "
                 "`EntityTypeEntity.dtmf_pattern` nor `Agent.web_client_"
                 "settings`. The proto of the console backend is richer.",
        "workaround": "Write against the internal backend (this MCP does it) "
                      "and ALWAYS verify by re-reading: `cx_set_console_field` "
                      "compares before/after and says what was saved and what "
                      "was lost. `cx_gaps` lists the gaps, measured live.",
        "tags": ["schema", "fields", "gaps"],
    },
    {
        "id": "stale_etag_patch",
        "title": "A PATCH with the old `etag` returns 409",
        "symptom": "HTTP 409 FAILED_PRECONDITION 'Others may have made recent "
                   "changes to the X. Please refresh your CX console to load "
                   "the updated resource.'",
        "cause": "The GET of the backend returns an `etag` and if it is sent "
                 "back in the PATCH, the server compares it with its own and "
                 "rejects it.",
        "workaround": "Remove the `etag` from the body before PATCHing (the "
                      "console does not send it either). `strip_derived()` in "
                      "`cxp/gaps.py` does it.",
        "tags": ["patch", "etag", "concurrency"],
    },
    {
        "id": "route_without_intent_nor_condition",
        "title": "A transition route needs `intent` or `condition`",
        "symptom": "HTTP 400 'At least one of intent or condition in "
                   "transition route should be specified.'",
        "cause": "The server validates the route when writing it, even though "
                 "the field is in the descriptor.",
        "workaround": "Always put `condition` (or `intent`) in the route. If a "
                      "new route is created with `cx_set_console_field`, "
                      "include it in the object.",
        "tags": ["transitions", "validation"],
    },
    {
        "id": "route_without_target",
        "title": "A transition route of a ROUTE GROUP needs a destination",
        "symptom": "HTTP 400 'At least one of trigger fulfillment, target "
                   "page, target flow, or target playbook in transition route "
                   "should be specified.'",
        "cause": "In `transitionRouteGroups` the route cannot be left "
                 "pending: unlike a page, there is no post-process that "
                 "completes it.",
        "workaround": "Give it a destination when creating the group (for "
                      "example `targetPlaybook`, which the published API cannot "
                      "express: see `cx_set_group_route_playbook`).",
        "tags": ["transitions", "route groups", "validation"],
    },
    {
        "id": "unknown_subfields",
        "title": "When writing a message, the server empties the subfields it "
                  "does not know",
        "symptom": "Writing `advancedSettings` leaves `enabled` missing and "
                   "`playbackInterruptionSettings` as `{}` (measured).",
        "cause": "The console carries those subfields on its own: the backend "
                 "derives them and drops whatever is sent to it.",
        "workaround": "Do not try to write them: they are read-only "
                      "(`status: derived` in the `cx_gaps` catalog).",
        "tags": ["fields", "derived"],
    },
    {
        "id": "derived_action_schemas",
        "title": "The `actionSchemas` of a tool are derived by the server",
        "symptom": "Writing `Tool.actionSchemas` returns HTTP 200 and changes "
                   "nothing: the previous ones remain.",
        "cause": "The server generates them from the `openApiSpec` or the "
                 "`dataStoreSpec` of the tool.",
        "workaround": "Read-only. To change the contract of a tool you have to "
                      "change its spec, not its actionSchemas.",
        "tags": ["tools", "derived"],
    },
    {
        "id": "undeclared_types_are_validated",
        "title": "The fields the published API does not declare are validated "
                  "too (and with types that are not visible)",
        "symptom": "HTTP 400 \"Invalid value at 'agent.web_client_settings' "
                   "(type ...Agent.WebClientSettings), \\\"8\\\"\" when sending "
                   "a number where a string goes.",
        "cause": "`boxBorderRadius` is a STRING in the backend proto, but it "
                 "does not appear in the public descriptor, so it cannot be "
                 "known in advance.",
        "workaround": "Copy the value the resource already has (the GET "
                      "returns it) and change only the type.",
        "tags": ["types", "fields", "validation"],
    },
    {
        "id": "incomplete_cookie_jar",
        "title": "The 5 cookies are NOT enough: the backend asks for the full jar",
        "symptom": "HTTP 401 'Request had invalid authentication credentials. "
                   "Expected OAuth 2 access token, login cookie or other valid "
                   "authentication credential.'",
        "cause": "The Google session cookies (`__Secure-*PSIDTS`, "
                 "`__Host-GAPS`, `SIDCC`, `OSID`...) are revalidated too. "
                 "MEASURED (2026-11-13) with the same binary and the same "
                 "`:env`: with only `SAPISID, SID, HSID, SSID, APISID` -> 401; "
                 "with the 23 cookies of `.google.com` -> 200.",
        "workaround": "`CXP_COOKIE` must carry the COMPLETE `.google.com` jar "
                      "(copied from the browser). The 5 are still the minimum "
                      "that is checked (`missing_cookies()`), but not the set "
                      "that is sent. `probes/renueva_cookies.py` renews them "
                      "from the real Chrome.",
        "tags": ["auth", "cookies", "401"],
    },
    {
        "id": "max_page_size_100",
        "title": "The internal backend caps `pageSize` at 100 (the descriptor "
                  "does not)",
        "symptom": "HTTP 400 'Page size should be within [0, 100].' when asking "
                   "for `pageSize=200`.",
        "cause": "The public descriptor declares `pageSize` without a maximum, "
                 "but the console backend applies a cap. MEASURED: "
                 "`agents/{a}/environments` with `pageSize=200` -> 400; with "
                 "100, 50, 20 or 5 -> 200. Note: `agents/{a}/flows` with 200 "
                 "DOES work: the cap is per endpoint, not global.",
        "workaround": "Ask for 100 (or less). `cxp.gaps._list()` retries on "
                      "its own with 50, 20 and 5 if the server rejects it.",
        "tags": ["listado", "pageSize", "400"],
    },
    {
        "id": "detect_intent_requires_language_code",
        "title": "`detectIntent` requires `queryInput.languageCode`",
        "symptom": "HTTP 400 \"Field 'query_input.language_code' is missing.\"",
        "cause": "The language goes in `queryInput.languageCode`, NOT inside "
                 "`queryInput.text.languageCode` (that form is dropped and the "
                 "server answers 400). MEASURED (2026-11-13) with the three "
                 "forms: `queryInput.languageCode` -> 200; "
                 "`queryInput.text.languageCode` -> 400; `languageCode` at the "
                 "root -> 400.",
        "workaround": "Body: `{\"queryInput\": {\"text\": {\"text\": ...}, "
                      "\"languageCode\": \"es\"}}`. The route is "
                      "`agents/{a}/sessions/{sessionId}:detectIntent` "
                      "(NOT `sessions:detectIntent`: that one gives 404).",
        "tags": ["detectIntent", "session", "400"],
    },
    {
        "id": "environment_requires_all_flows",
        "title": "Creating an environment requires versionConfigs with ALL the "
                  "flows",
        "symptom": "HTTP 400 \"Flow 'X' is referenced in version 'Y' of flow "
                   "'Z', but not included in the environment.\" and, on the "
                   "next round, \"Playbook 'P' is referenced in version "
                   "'Y' of flow 'Z', but not included in the environment.\"",
        "cause": "The published version drags references to other flows **and "
                 "to playbooks**, and the environment has to include all of "
                 "them in `versionConfigs`. The public proto declares no field "
                 "to put a playbook in the `versionConfig`: the gap shows up "
                 "exactly when creating the environment.",
        "workaround": "Publish a version of EVERY flow of the agent and put them "
                      "all in `versionConfigs`. If it also fails because of a "
                      "playbook, the playbook versions have to be published "
                      "too.",
        "tags": ["entornos", "versiones", "400"],
    },
    {
        "id": "test_cases_without_delete",
        "title": "`testCases` has no DELETE: they are deleted in batch",
        "symptom": "DELETE to `agents/{a}/testCases/{id}` -> **404** (it is not "
                   "a permissions problem: the method does not exist).",
        "cause": "The descriptor only declares `batchDelete` "
                 "(`POST agents/{a}/testCases:batchDelete`) and `patch`. "
                 "Deleting a single case does not exist in the API.",
        "workaround": "`POST agents/{a}/testCases:batchDelete` with "
                      "`{\"names\": [\"projects/.../testCases/{id}\"]}` (measured: "
                      "it deletes the 6 at once and the list ends up empty).",
        "tags": ["testCases", "delete", "404"],
    },
    {
        "id": "endpoints_outside_the_descriptor",
        "title": "The console calls endpoints that are NOT in the descriptor",
        "symptom": "The UI shows data (system entity types, users with access, "
                   "available models, recent projects) that the public API does "
                   "not expose: there is no method to ask for.",
        "cause": "The public discovery doc and the one of the internal backend "
                 "are THE SAME DOCUMENT (measured: 669 652 bytes each, the same "
                 "canonical md5 29adf74f..., the same 162 "
                 "methods in v3beta1): what is missing is not a method, it is "
                 "endpoints that the console uses and that are in NO "
                 "descriptor (`systemEntityTypes:list`, `currentUsers`, "
                 "`:listAvailableModels`, `:getSupportedLanguages`, "
                 "`users/me/userSettings/recentProjects`, `agent/visibility`, "
                 "`conversationProfiles`, `locationSettings`).",
        "workaround": "`cx_api` with `version=` and absolute routes reaches "
                      "all of them: see the catalog in `cxp/console_endpoints.py` "
                      "(`METHOD_GAPS`) and the tool "
                      "`cx_endpoints_consola`.",
        "tags": ["methods", "descriptor", "console"],
    },
    {
        "id": "login_in_chromium_is_invalidated",
        "title": "A Google login made in Chromium or in a 'Chrome for Testing' "
                 "build is invalidated",
        "symptom": "the sign-in appears to work and the session dies on its own: "
                   "the console answers 401 and `/v3/signin/rejected` appears, "
                   "with the very same `.env` that worked minutes before.",
        "cause": "Google does not keep the sessions of the browsers it does not "
                 "ship: Chromium and the 'Chrome for Testing' builds are "
                 "flagged at the login, and the session made there is revoked "
                 "afterwards. Only the official Google Chrome keeps it.",
        "workaround": "sign in ONLY in the official Google Chrome (install it "
                      "with `wget -q -O - "
                      "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb "
                      "| sudo apt-get install -y -`); the MCP refuses to open a "
                      "login window in any other browser. Reading an existing "
                      "session from another browser is fine; making a login in "
                      "it is not.",
        "tags": ["auth", "login", "browser", "cookies"],
    },
]


def search(filter_text: str = "") -> list[dict[str, Any]]:
    """Limits filtered by text (id, title, symptom, cause, workaround)."""
    if not filter_text:
        return LIMITES
    needle = filter_text.lower()
    output = []
    for limit in LIMITES:
        text = " ".join(str(limit.get(k, "")) for k in
                        ("id", "title", "symptom", "cause", "workaround",
                         "tags")).lower()
        if needle in text:
            output.append(limit)
    return output


def cause_of(message: str) -> dict[str, Any] | None:
    """Deduces the known limit that explains an error from the API.

    Used to automatically annotate pipeline errors: the agent that
    reads the report sees, next to the HTTP 400, the cause and the workaround."""
    m = (message or "").lower()
    if not m:
        return None
    signatures = [
        ("caused loop in playbook routes", "playbook_loop"),
        ("null or empty definition", "tool_openapi_empty"),
        ("failed to connect datastores", "tool_datastore_deleted"),
        ("builtin tools are provided by dialogflow", "tool_builtin"),
        ("cannot delete the start flow", "protected_start_flow"),
        ("you cannot delete event handler", "protected_start_flow"),
        ("default language code cannot be updated", "read_only_fields"),
        ("please set 'force' to true", "delete_force"),
        ("are referencing the", "delete_force"),
        ("does not exist in the flow", "validated_destinations"),
        ("referenced resource", "validated_destinations"),
        ("does not exist under the agent", "validated_destinations"),
    ]
    for signature, limit_id in signatures:
        if signature in m:
            for limit in LIMITES:
                if limit["id"] == limit_id:
                    return limit
    return None
