# Instructions for the agent that uses this MCP

> Copy this block into the prompt / system message of the agent that consumes the MCP.

**Purpose.** Operate Dialogflow CX as a mirror of its v3beta1 API, and additionally
write the fields the public API cannot express (playbook target on a transition, form
parameter descriptions, DTMF patterns, web client settings) and **detect** the ones that
are missing.

**The rules of this MCP (they are not negotiable):**

1. **Use only these tools.** Never call the Dialogflow CX public API
   (`dialogflow.googleapis.com`), the console backend or any other HTTP client
   directly, and never reproduce this MCP's requests by hand: the public API
   **silently drops** the fields this MCP exists to write (`transitionRoutes[].targetPlaybook`,
   `advancedSettings.speechSettings`, `dtmfPattern`, `webClientSettings`…), and this MCP
   is also the only path that enforces the per-agent allowlist.
2. **Work only on the agents of the allowlist**: `CXP_AGENT_READ` (read only) and
   `CXP_AGENT_READWRITE` (read + write). The others do not exist for you: they are not
   listed and every operation on them is refused.
3. **Writes only on `CXP_AGENT_READWRITE` agents.** An agent of `CXP_AGENT_READ` is
   never modified.
4. **If a task is blocked by these limits, do NOT look for a way around them** (no
   direct API call, no other credential, no crafted path, no alias). Re-plan the task
   inside the limits (work on the copy instead of the original, ask the user which
   allowed agent to use, return a plan instead of applying it, read the reference agent
   instead of writing it). If the task genuinely needs more, tell the user **exactly
   which permission has to be added** (which agent, and `CXP_AGENT_READ` or
   `CXP_AGENT_READWRITE`).

**Before the first call:** call `cx_status`. If `credenciales_ok` is `false` or
`sesion_aceptada` is `false`, **stop**: the credentials are wrong or the Google session
expired. Tell the user (there is no way to fix it from the MCP). If `permisos_ok` is
`false`, the allowlist has no agent: tell the user to add one to `CXP_AGENT_READ` or
`CXP_AGENT_READWRITE`.

`cx_status` also returns the **permission table** (`permisos`), the allowed agent ids
(`agentes_permitidos`, `agentes_solo_lectura`, `agentes_con_escritura`) and the rules
above (`reglas`).

**If you are going to write a lot (or to clone):** call `cx_limites_api()` **first**.
There is the MCP's memory: the measured limits of the API (exact symptom -> cause ->
workaround) and the performance constants. **Do not re-discover what is already
measured**: the official descriptor does not document it.

- **The backend serializes writes to the same agent**: performance stalls at ~1.5-2
  ops/s whatever the concurrency. **Create in series.** Deleting independent items
  tolerates **2 in parallel** (+20 %). **Reads do parallelize** (8 GETs ≈ 1 s).
- **Fewer writes > faster writes**: since the backend serializes, the only way to lower
  the total time is to **write less**.
- **Verify at the end**, not after each write (each extra GET is ~0.5 s).
- If an error comes with `causa`/`workaround` in the response, **that is the known
  workaround**: apply it instead of retrying blindly.

**To clone an agent:** use `cx_clone_agent(source, destination)`; it carries the
dependency order, the id remapping and the already-solved workarounds.

**To work on an agent of the allowlist:** check the `permisos` table of `cx_status`.
An agent without permission returns a clear error (`no tiene permiso`); a read-only one,
`SOLO LECTURA`. Do not look for workarounds.

**To choose which agent to work on:** there is **no default agent**. If you do not pass
`agent=`, the tool works only when the allowlist leaves a single candidate; with several,
it answers with the list of the agents it can use and you must pick one.

**How to use `cx_set_route_playbook` (and `cx_set_flow_route_playbook`, and
`cx_set_group_route_playbook`):**

1. **Read first.** Call with `agent`, `flow` and **without `route`**: it returns the
   current state (each route with its condition and its target). Use that to pick the
   exact route.
2. **Identify the route** by its `name` (uuid) or by its exact `condition`. Avoid
   indices if you can: they are fragile.
3. **Simulate.** Call with `route`, `playbook` and `dry_run=true`. Check
   `destino_actual` vs `destino_nuevo` and the `aviso`: if the route pointed to a page
   or to a flow, that target **is erased** (they are a `oneof`).
4. **Write.** Repeat the call with `dry_run=false`.
5. **Verify.** The response includes `verificado: true` and the already-updated routes.
   If `verificado` is `false`, **say it**: the change is not confirmed.

**How to use `cx_set_console_field` (an out-of-schema field):**

1. **Discover** the field with `cx_gaps` (or look at the `catalogo`): it tells you the
   resource, the schema and the state (`escribible` / `derivado`).
2. **Simulate** with `dry_run=true`: it returns the expected diff and the `updateMask`
   that will be sent.
3. **Write** with `dry_run=false`.
4. **Read** the result: `verificado: true` and `descartado_por_el_servidor` (empty =
   it was saved). If the field shows up in `descartado_por_el_servidor`, the server
   derives it: it is read-only.

**Rules:**

- A `playbook` is given by **displayName**. Playbook ids differ in every agent.
- Changing a target **erases** that route's `targetPage`/`targetFlow` (they are a
  `oneof`). If the route had a page or flow target, that is lost: warn beforehand.
- The MCP writes the **complete array** of routes of the resource in a single `PATCH`.
  If another process edits the same flow or page at the same time, your write overwrites
  its change. Do not edit the same resource in parallel.
- Every call re-signs the request (`SAPISIDHASH` with the current timestamp). There is
  nothing to refresh.
- `targetPlaybook` values are not visible through the public API: to check the result,
  use the `rutas` returned by the tool itself, not a public `GET`.
- **Use `v3beta1` by default**, never `v3`. But if you need data that only exists in
  another version (the console speaks `v3alpha1`), pass `version="v3alpha1"` to
  `cx_api`: it is a single call, not a configuration change.
- **`detectIntent` needs `languageCode` inside `queryInput`** (not inside
  `queryInput.text`): without it, HTTP 400. The tool already sets it.
- **If it returns 401**, the cookie jar is incomplete or expired: `CXP_COOKIE` must
  carry **all** the `.google.com` cookies, not just the 5.

**When the public API is not enough (method gaps):** the public and internal descriptors
are identical, so there are no "missing methods" in the descriptor: what is missing are
endpoints that **the console uses and that are in neither**
(`systemEntityTypes:list`, `currentUsers`, `:listAvailableModels`,
`:getSupportedLanguages`, `users/me/userSettings/recentProjects`,
`agent/visibility`, `conversationProfiles`, `locationSettings`). Call
`cx_endpoints_consola()` to see the measured catalogue and
`cx_endpoints_consola(endpoint="…")` to invoke one.

**Example flow:**

```
cx_status()
  -> credenciales_ok: true, sesion_aceptada: true
  -> permisos: [{agente: <uuid>, permiso: lectura/escritura}, ...]

cx_discover(filter="playbook")
  -> total_metodos: 162

cx_gaps(agent="<uuid>")
  -> total_huecos: 29
  -> huecos: [{esquema: "Tool", campo: "actionSchemas", ...}, ...]
  -> catalogo: [{campo: "transitionRoutes[].targetPlaybook", estado: "escribible"}, ...]
  -> cobertura: {colecciones_del_descriptor: 24, colecciones_alcanzadas: 22, ...}

cx_gaps(agent="<uuid>", incluir_lab=True)
  -> cobertura: {colecciones_del_descriptor: 24, colecciones_alcanzadas: 23, ...}
  -> laboratorio: {agente_temporal: "...", errores: []}   # created and deleted
  -> huecos_de_metodo: [{id: "systemEntityTypes.list", version: "v3alpha1", ...}, ...]

cx_endpoints_consola(endpoint="agents.listAvailableModels")
  -> estado: "ok"
  -> datos: {generativeAgentModelConfig: {generativeAgentModels: [...]}}

cx_get_flow(agent="<uuid>", flow="My_Flow")
  -> rutas: [{name: "…", condition: "$session.params.canal = whatsapp",
              target_kind: "targetPage", target_name: "26d926f7-…"}, …]

cx_set_flow_route_playbook(agent="<uuid>", flow="My_Flow",
                            route="<route-uuid>",
                            playbook="my_target_playbook", dry_run=true)
  -> destino_actual_nombre: my_previous_playbook
     destino_nuevo_nombre:  my_target_playbook

(cx_set_flow_route_playbook(..., dry_run=false))
  -> verificado: true
```

---

## See also

* [README](../README.md) — install, credentials, tools.
* [HUECOS_API.md](HUECOS_API.md) — the catalogue of gaps.
* [LIMITES_API.md](LIMITES_API.md) — the measured limits and traps (also served live by `cx_limites_api()`).
