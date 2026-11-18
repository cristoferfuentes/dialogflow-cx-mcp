# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The installer now probes the machine and says what it cannot do.** The setup
  prompt no longer assumes that the cookie can be obtained: it runs a probe
  (Chrome binary, `DISPLAY`, Xvfb/noVNC, browser profile), picks the route that
  is really possible and, when the automatic one is impossible (headless server,
  container, SSH box, no Chrome), it tells the user **explicitly** that the cookie
  cannot be obtained on this machine, explains the reason in one sentence, gives
  the manual recipe (copy the **whole** `Cookie` header from DevTools) and
  finishes the rest of the setup while it waits. It never invents a credential
  and the verification step cannot be reported as passed with a credential
  missing. `probes/renueva_cookies.py` prints the same three options (A: desktop
  browser, B: start a virtual display with Xvfb, C: copy it manually) and exits
  with code 2 when it cannot reach a browser.
- **The allowlist accepts as many agents as needed in each list.** `CXP_AGENT_READ` and
  `CXP_AGENT_READWRITE` are comma-separated lists and each one can carry several agents
  (the code already parsed them; the documentation implied a single agent per list).
  Every agent keeps its own level and `cx_status` returns one row per agent.
- The documentation now states **explicitly that those two variables are REQUIRED**:
  they are what makes the MCP work (without an agent in either list there is nothing to
  operate on and every tool refuses) **and they are the security boundary of the
  harness** that consumes the MCP: the session cookies can reach everything the Google
  account can reach, and the allowlist is what keeps the agent inside the agents the
  user chose. Said in the README, `.env.example`, `docs/SETUP_PROMPT.md`,
  `docs/PROMPT_AGENTE.md` and in the `instructions` of the MCP server.
- The **rules of the MCP** are now sent to the agent that consumes it: the MCP
  server declares them in its `instructions` (they arrive with the handshake) and
  `cx_status` returns them again in `reglas`. They say: use only these tools (never
  the public API directly — it silently drops the fields this MCP exists to write),
  work only on the agents of the allowlist, write only on the read/write ones, and
  when a task is blocked, **re-plan it inside the limits** or tell the user exactly
  which permission has to be added.
- `tests/test_agent_permissions.py`: the tests of the new agent model (allowlist as
  the only source of agents, resolution of an omitted `agent=`, filtering of the
  listings, refusal of a write on a read-only agent, the rules themselves).
- `docs/SETUP_PROMPT.md`: a **setup prompt** you can paste into any agent so it configures
  the MCP end to end (clone + install → cookies → `.env` → MCP client registration →
  verification with `cx_status`). It asks you for only three things: the project, the
  location and the agent you may write on.
- `docs/HUECOS_API.md`: the **catalogue of the gaps** the public API cannot express (38
  fields / 14 schemas with their measured write state, the 9 endpoints in no descriptor,
  `targetPlaybook` in depth and how each one was measured).
- `docs/LIMITES_API.md`: the **measured limits** of the API (symptom → cause →
  workaround for 31 traps) plus the first-party auth headers, the versions and the
  network constants.
- `docs/PROMPT_AGENTE.md`: the **instructions for the agent** that consumes this MCP.
- `docs/CONVERSATIONS.md`: the **conversation history** through the API and how to
  publish the logs.
- `tools/verifica.py`: local verification runner. It runs the unit tests, the linter,
  the server start-up check and the secret scan (working tree **and** git history),
  and regenerates the badges in `docs/badges/`.

### Changed

- **The whole repository is now in English.** The code, the documentation, the
  catalogues, the probes, the tests and the MCP output were in Spanish; they are
  now in English. This includes:
  - the **output keys** of the tools (`permisos` → `permissions`, `agentes` →
    `agents`, `huecos` → `gaps`, `rutas` → `routes`, `estado` → `status`,
    `datos` → `data`, `verificado` → `verified`, `reglas` → `rules`, …), so the
    contract the agent sees changed;
  - the **tool parameters** (`incluir_lab` → `include_lab`,
    `todas_las_locations` → `all_locations`, `recurso` → `resource`,
    `campo` → `field`, `valor` → `value`, `origen` → `source`,
    `destino` → `destination`, `max_padres` → `max_parents`);
  - the **catalogue states** (`escribible` → `writable`, `derivado` → `derived`,
    `ignorado` → `ignored`, `descartado` → `dropped`, `solo_entrada` →
    `input_only`, `no_aceptado` → `not_accepted`, `solo_lectura` → `read_only`,
    `no_probado` → `not_tested`), the **ids** and the **tags** of the limits, and
    the whole prose of the 31 limits and the 38 gaps;
  - the **file names**: `docs/CONVERSACIONES.md` → `docs/CONVERSATIONS.md`, the
    test modules (`test_permisos_agente.py` → `test_agent_permissions.py`, …)
    and 22 probes (`verificacion_final.py` → `final_verification.py`,
    `paridad_consola.py` → `console_parity.py`, …);
  - the **CLI flags** of the probes (`--agente` → `--agent`, `--proyecto` →
    `--project`, `--campo` → `--field`, `--raiz` → `--root`, `--puerto` →
    `--port`, `--rapido` → `--fast`);
  - the **setup prompt** of the README: `Ejecuta las instrucciones de esta fuente…`
    → `Execute the instructions from this source…`.
- The permission levels are `none` / `read` / `read/write` (were `ninguno` /
  `lectura` / `lectura-escritura`). The English read-only refusal keeps the
  token `SOLO LECTURA` inside it, so anything that matched it still matches.


- **The messages the agent reads are now in English**: the refusals of the guard
  (`has no permission`, `READ-ONLY (SOLO LECTURA)`, `no allowed agent`, `several allowed
  agents and none is specified`), the permission levels of the table (`read` /
  `read/write` instead of `lectura` / `lectura/escritura`) and the clone errors. The
  `SOLO LECTURA` token is kept inside the English message so anything that matched it
  still matches.
- The README is now a **short onboarding document**: the setup prompt is the default
  way to install (the manual install is a fallback), the obvious statements (that no
  project or agent is hardcoded, that the server speaks stdio) are gone and the depth
  lives in `docs/`.
- **There is no default agent any more.** `CXP_DEFAULT_AGENT` is gone: the agents
  come from `CXP_AGENT_READ` (read only) and `CXP_AGENT_READWRITE` (read + write),
  and **at least one of the two must have an agent** (otherwise the MCP says it has
  nothing to work on instead of failing later with a 403).
- A tool called **without `agent=`** works when the allowlist leaves a single
  candidate; with several, it answers with the list of the agents it can use instead
  of guessing one.
- `cx_list_agents` and `cx_status` list **only the agents of the allowlist**: the
  ones the session can see but that are not on the list do not appear
  (`ocultos_por_permisos` / `agentes_ocultos` say how many were filtered out). The
  allowed agents that live in **another project** are added to the list too.
- An allowlist entry written as `<project>/<uuid>` now also decides **the project**
  the MCP works on for that agent (it does not need `CXP_PROJECT` to be its
  project), and the permission table shows **one row per agent**.
- `cx_status` checks the allowed agents with a **real GET** (`agentes_comprobados`):
  an entry that points to an agent that no longer exists is reported instead of
  failing later with a 404.
- The permission of a write is checked **before** touching the resource: a read-only
  agent answers "READ-ONLY (SOLO LECTURA)" and not "flow not found".
- `cx_clone_agent` deduces the source and the target from the allowlist (read-only
  agent → readable one, read/write agent → writable one) instead of a default agent.
- The README is now an **onboarding document**: it explains what the MCP is for, the
  three problems it solves, how to install, configure and use it, and it links to the
  detailed documents in `docs/` instead of inlining them.

### Removed

- `CXP_DEFAULT_AGENT` (and the `Config.default_agent` field): the allowlist is the
  list of agents.
- The GitHub Actions workflow: the repository ships the source code only, with no
  hosted CI. The badges of the README are static SVG files generated from a real
  local run.
- The `github-actions` ecosystem of `dependabot.yml` (there are no workflows left
  to update).

## [0.3.0] — 2026-11-16

### Added

- `cx_proto_interno`: the **systematic** list of gaps — the 14 schemas / 38 fields that
  the console's internal proto has and the public discovery doc does not declare, with
  the write state of each one measured (19 writable, 12 ignored by the backend, 2
  dropped, 2 input-only, 2 not accepted, 1 derived).
- `cx_endpoints_consola`: the **measured** catalogue of the 9 endpoints the console
  calls and that are in no descriptor (`GET /projects`, `systemEntityTypes:list`,
  `currentUsers`, `:listAvailableModels`, `:getSupportedLanguages`,
  `users/me/userSettings/recentProjects`, `agent/visibility`, `conversationProfiles`,
  `locationSettings`), plus the call to any of them.
- `cx_gaps(include_lab=True)`: sweeps the `environments*` and `testCases*` families in
  a temporary **lab agent** that is created and deleted automatically, taking coverage
  from 18/24 to **23/24**.
- `cxp.lab`: the temporary lab agent used for the sweep.
- `cxp.endpoint`: one single rule for resolving relative, absolute and version-prefixed
  paths in `cx_api`.
- `cx_net_probe`: per-request network breakdown (send / headers / body).

### Changed

- `cx_api` now accepts `version=` and version-prefixed paths: the console speaks
  `v3alpha1`, and there are endpoints and data that only come out in another version.
- The gap sweep is fully generic: it is derived from the descriptor
  (`colecciones_listables()`) instead of a hand-written list, and it now resolves the
  ancestor of nested collections (up to `max_parents` candidates).
- The gap detector resolves both the wrapper schema and the item schema of a list, so
  a listing no longer reports false positives.
- Every region now has its own host: `{location}-dialogflow.clients6.google.com`, with
  the `referer` derived from the host of the URL.

### Fixed

- `agents/{id}:export` was rejected by the permission guard (the `:method` suffix was
  not split off).
- The traffic analyser no longer reports console-only endpoints as being in v3beta1
  (the `{+name}` expansion was swallowing the `:method` suffix).

### Documentation

- The measured limits of the API are now returned by the MCP itself (`cx_limites_api`),
  so an agent does not have to rediscover them.

## [0.2.0] — 2026-11-13

### Added

- `cx_set_console_field`: generic writing of any field, including the ones outside the
  public schema, with verification by reading back.
- `cx_set_route_playbook`, `cx_set_flow_route_playbook` and
  `cx_set_group_route_playbook`: the `targetPlaybook` target at page, flow and route
  group level.
- The per-agent allowlist (`CXP_AGENT_READ` / `CXP_AGENT_READWRITE`), enforced in the
  low layer so that no path can bypass it.

### Fixed

- The complete `.google.com` cookie jar is now sent, not just the 5 mandatory
  cookies (measured: with 5 cookies the backend answers 401 once the session renews).

## [0.1.0] — 2026-11-06

### Added

- First public shape of the mirror: the generic `cx_api` passthrough over the whole
  `v3beta1` surface (162 methods), the typed core tools, and the first version of the
  gap detector.

[Unreleased]: https://github.com/cristoferfuentes/dialogflow-cx-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/cristoferfuentes/dialogflow-cx-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/cristoferfuentes/dialogflow-cx-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/cristoferfuentes/dialogflow-cx-mcp/releases/tag/v0.1.0
