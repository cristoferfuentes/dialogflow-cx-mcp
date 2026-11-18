# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
- `docs/CONVERSACIONES.md`: the **conversation history** through the API and how to
  publish the logs.
- `tools/verifica.py`: local verification runner. It runs the unit tests, the linter,
  the server start-up check and the secret scan (working tree **and** git history),
  and regenerates the badges in `docs/badges/`.

### Changed

- The README is now an **onboarding document**: it explains what the MCP is for, the
  three problems it solves, how to install, configure and use it, and it links to the
  detailed documents in `docs/` instead of inlining them.

### Removed

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
- `cx_gaps(incluir_lab=True)`: sweeps the `environments*` and `testCases*` families in
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
  ancestor of nested collections (up to `max_padres` candidates).
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
