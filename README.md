# dialogflow-cx-mcp

[![Verification](docs/badges/verification.svg)](docs/VERIFICATION.md)
[![Tests](docs/badges/tests.svg)](docs/VERIFICATION.md)
[![Lint](docs/badges/lint.svg)](docs/VERIFICATION.md)
[![Server](docs/badges/server.svg)](docs/VERIFICATION.md)
[![Secrets](docs/badges/secrets.svg)](docs/VERIFICATION.md)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

> ### The Dialogflow CX API you wish you had.
>
> An [MCP](https://modelcontextprotocol.io) server that mirrors the public `v3beta1` API
> **and** reaches the fields the public API cannot express — the ones it accepts with a
> cheerful `200` and then quietly throws away.

**No project and no agent are hardcoded**: everything is configuration in a `.env`.
**No browser required**: the only credential is a `Cookie` header.

> ⚠️ **This is not a Google product.** It talks to the console's internal backend
> (`dialogflow.clients6.google.com`) using your own account's first-party authentication.
> Please read the [legal notice](#legal-notice) before using it.

---

## In 30 seconds

| | |
|---|---|
| **What it is** | An MCP server (stdio) with **31 tools** for Dialogflow CX |
| **What it mirrors** | The public `v3beta1` API: **162 methods / 32 resources**, 1:1, in any version |
| **What it adds** | The **gaps**: fields, endpoints and behaviours the public API cannot express |
| **How it talks** | Your Google session cookies + `SAPISIDHASH` (first-party), like the console does |
| **How it is configured** | One `.env`, no code changes, no hardcoded ids |
| **How it is verified** | 91 unit tests + a live measurement of the 38 internal fields ([report](docs/VERIFICATION.md)) |

**The setup in one command** (your agent does the rest):

```bash
git clone https://github.com/cristoferfuentes/dialogflow-cx-mcp.git && cd dialogflow-cx-mcp
python3 -m venv .venv && .venv/bin/pip install -e . && cp .env.example .env
```

…and then let your agent finish the job with the
**[setup prompt](docs/SETUP_PROMPT.md)** (copy & paste).

---

## The three problems it solves

### 1. 🕳️ The public API drops fields without telling you

`TransitionRoute` in the public API is a `oneof` with `target_page` and `target_flow`.
**`target_playbook` does not exist.** Send it in a `PATCH` and you get a `200 OK` — the
route is saved **without a target**, and the agent falls into `NO_MATCH`. No error, no
warning, no clue.

The console's *internal* proto knows the field. That is what this MCP talks to, and it
**verifies every write by reading it back**.

### 2. 🚪 The console does things that are in no descriptor

The console shows you the project list, the system entity types, who has access to the
agent, which generative models exist, the supported languages… **None of those endpoints
is in the public API** (the public and internal descriptors are literally the same
document: same 669,652 bytes, same md5).

`cx_endpoints_consola()` calls the **9 measured** endpoints that live in no descriptor.

### 3. 🪨 The API does not document its own traps

`force=true` to delete a referenced resource. Stale `etag` → `409`. Writes to the same
agent are **serialized** at ~1.5 ops/s. `/batch` → `404`. A `PATCH` of `advancedSettings`
empties its unknown subfields. A training phrase needs `repeatCount >= 1`. And the
environment creation wants the version of *every* flow, playbook and tool.

None of that is in the discovery doc. **All of it is in this MCP's memory**:
`cx_limites_api()` returns symptom → cause → workaround for **31 measured limits**, and
the cloning pipeline annotates every error with the fix.

---

## Public API vs. this MCP

|  | public `v3beta1` | `dialogflow-cx-mcp` |
|---|---|---|
| transition route → playbook | silently dropped | written **and verified** |
| out-of-schema fields (`speechSettings`, `dtmfPattern`, `webClientSettings`…) | ignored with a `200` | written, read back, and reported |
| console-only endpoints | `404` | reachable (`v3alpha1` included) |
| undocumented traps | you find out the hard way | catalogued in memory (`cx_limites_api()`) |
| "what can't the API do?" | nobody knows | detected live (`cx_gaps`), 23/24 collections |
| cloning an agent | by hand, slowly | one command, with a per-action benchmark |

---

## The gaps, in one paragraph

A **gap** is a field that the console's backend exposes and the public API does not
**declare**: through the public API it can be neither read nor written. There are
**38** of them, across **14 schemas** — and **19 of them are writable** through this
MCP (LLM endpointing, Gemini ASR, transcript fixing, playbook targets, DTMF patterns,
web client styling, form parameter descriptions…).

The MCP detects them **live** (`cx_gaps`): it walks your agent, compares every resource
with the public schema and reports what it finds. If Google adds a field tomorrow, it
shows up without touching the code.

👉 **The full catalogue, with the measured write state of each field, is in
[docs/HUECOS_API.md](docs/HUECOS_API.md).** You do not need it to use the MCP — it is
there for when you want the details.

---

## Install

```bash
git clone https://github.com/cristoferfuentes/dialogflow-cx-mcp.git
cd dialogflow-cx-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env      # then fill it in (see below)
```

Or, without installing the package:

```bash
.venv/bin/pip install "mcp>=1.2"
```

**Requirements**: Python ≥ 3.10 and a Google account with access to the project.

---

## Get the credentials

The MCP authenticates against the console's internal backend with **first-party**
authentication: your Google browser session cookies. No service account token needed.

### Route A — let a browser do it (automatic)

If a desktop Chrome with your Google session is available on the machine:

```bash
.venv/bin/python probes/renueva_cookies.py
```

It opens the real Chrome over CDP and writes the **complete** `.google.com` cookie jar
into your `.env`. If it says the browser has no session, open the console in that
Chrome, log in, and run it again.

### Route B — copy the header (5 minutes, works everywhere)

1. Sign in to the [Conversational Agents console](https://conversational-agents.cloud.google.com)
   with the account that owns the project.
2. `F12` → **Network** → tick **Preserve log** → reload the page.
3. Filter by `clients6.google.com` and click a request like:

   ```
   GET https://dialogflow.clients6.google.com/v3beta1/projects/.../agents/.../flows?key=AIzaSy...
   ```

4. **Request Headers** → copy the **whole** value of the `Cookie` header:

   ```
   SAPISID=XXXX; SID=YYYY; HSID=ZZZZ; SSID=WWWW; APISID=VVVV; __Secure-3PSID=...; ...
   ```

5. Paste it into the `.env`:

   ```bash
   CXP_COOKIE="SAPISID=XXXX; SID=YYYY; HSID=ZZZZ; SSID=WWWW; APISID=VVVV; ..."
   ```

> ⚠️ **Copy it WHOLE, not just the 5 "required" cookies.** Measured (2026-11-13), with
> the same binary and the same `.env`: with only `SAPISID, SID, HSID, SSID, APISID` the
> answer is **401**; with the **23** cookies of `.google.com` (the header as-is) the very
> same call answers **200**. Session cookies are revalidated, and a jar with 5 of 23
> gets locked out exactly when the session renews.
>
> The MCP **sends every** cookie it finds in `CXP_COOKIE`; the 5 are only a sanity check.
> If you get a 401, copy the header **again**, whole.

### The API key

`CXP_API_KEY` is the public key used by the Google console — the `key=` parameter of
the very same request from step 2 (`?key=AIzaSy...`). It is public and shared by every
console user, but it is **not** written in this repository: copy it from your own
request and put it in the `.env`:

```bash
CXP_API_KEY=AIzaSy...      # <- the key= of your console request
```

---

## Configuration

```bash
CXP_PROJECT=my-gcp-project
CXP_LOCATION=global
CXP_CONSOLE_ORIGIN=https://conversational-agents.cloud.google.com
CXP_API_KEY=AIzaSy...
CXP_COOKIE="SAPISID=...; SID=...; ..."
CXP_DEFAULT_AGENT=<agent-uuid>
CXP_AGENT_READWRITE=<agent-uuid>          # ONLY your own copy
CXP_AGENT_READ=<other-agent-uuid>,...      # read-only
```

The full list of variables is in [`.env.example`](.env.example). The most important ones:

| variable | required | what it is for |
|---|---|---|
| `CXP_PROJECT` | yes | Google Cloud project id (the default for calls) |
| `CXP_LOCATION` | no (`global`) | Dialogflow CX location |
| `CXP_QUOTA_PROJECT` | no | project used for the `x-goog-user-project` header; defaults to `CXP_PROJECT` |
| `CXP_CONSOLE_ORIGIN` | no | console origin; it takes part in the `SAPISIDHASH` signature |
| `CXP_API_KEY` | yes | console API key (the `key=` of the requests) |
| `CXP_COOKIE` | yes | `Cookie` header copied from the browser (**complete**, not just the 5) |
| `CXP_SAPISID`, `CXP_SID`, `CXP_HSID`, `CXP_SSID`, `CXP_APISID` | alternative | the cookies one by one (only for the signature) |
| `CXP_SESSION_FILE` | alternative | Playwright `storage_state` to extract them from |
| `CXP_API_VERSION` | no (`v3beta1`) | proto version of the internal backend |
| `CXP_DEFAULT_AGENT` | no | the agent to work on when none is given |
| **`CXP_AGENT_READ`** | — | agents allowed to **read** |
| **`CXP_AGENT_READWRITE`** | — | agents allowed to **read and write** |
| `CXP_PROJECTS` | no | (optional) restricts the allowed projects |
| `CXP_ENV_FILE` | no | path to the `.env` to load (searched first) |

The `.env` is looked up in: `CXP_ENV_FILE` → the package root → the current directory →
`~/.config/cx-playbook.env`. `CXP_*` environment variables take priority over the file
(**they do not merge**: an environment variable *replaces* the `.env` value).

### Per-agent permissions (the allowlist)

An agent **without** an entry in `CXP_AGENT_READ` nor in `CXP_AGENT_READWRITE` is **not
accessible at all** (not even for reading). Entries accept two forms:

- `<uuid>` → the permission travels with the uuid: it is valid in any project.
- `<project>/<uuid>` → matches project and agent together.

The project is deduced from the **request path**, not from `CXP_PROJECT`: that way the
same MCP can read an agent in PROD (which lives in another project) without opening write
access to it.

> **HARD RULE:** `CXP_AGENT_READWRITE` must contain **ONLY your own copy** of the agent.
> Never the reference agent nor the production one: editing a playbook that is not your
> copy would break parity with the console.

The guard compares the **resolved id** (not an alias) and is enforced in the **low
layer** (`cxp/console_api.py`), not only in the MCP layer:

- `ConsoleApi.request()` checks the allowlist against **every agent the request can
  reach** — the ones in the path *and* the ones in the query string (`?name=…`).
- A write verb whose URL mentions **no agent at all** is rejected (**fail closed**).
- The path is canonicalized before the check (`.`/`..` resolved, encoded traversal
  rejected), so the resource the guard sees is the resource the server will reach.
- `cx_batch` applies the guard **per sub-request**.
- No path (not even the generic passthrough) can bypass the allowlist.

Covered by `tests/test_guard_seguridad.py`.

---

## Register it in an MCP client

The server speaks **stdio**, so any MCP client that can launch a command works. The
`env` block is **not optional**: the MCP SDK does not inherit the environment of the
parent process, so without it the server would not find your credentials.

```json
{
  "mcpServers": {
    "dialogflow-cx-mcp": {
      "command": "/path/to/dialogflow-cx-mcp/.venv/bin/python",
      "args": ["/path/to/dialogflow-cx-mcp/server.py"],
      "env": { "CXP_ENV_FILE": "/path/to/dialogflow-cx-mcp/.env" }
    }
  }
}
```

| client | where the server is configured |
|---|---|
| Claude Desktop | `claude_desktop_config.json` (`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows) |
| Claude Code | `claude mcp add dialogflow-cx-mcp -- /path/to/.venv/bin/python /path/to/server.py` |
| Cursor | `~/.cursor/mcp.json` |
| VS Code (Copilot) | `.vscode/mcp.json` in the workspace, or your user `settings.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Any other client | the `mcpServers` block above |

> 🤖 **Do not want to do this by hand?** Paste the
> **[setup prompt](docs/SETUP_PROMPT.md)** into your agent and let it do all of it
> (install → cookies → `.env` → client → verification).

---

## Tools

31 tools. The names are the API's; the responses speak Spanish (as they always have), so
existing prompts keep working.

### The generic mirror

| tool | what it does |
|---|---|
| `cx_status` | configuration, the **permission table** and backend health (a real GET) |
| `cx_discover` | the API map, taken from the backend's **official discovery doc** (162 methods / 32 resources in v3beta1). `filter="flows"`, `method="agents.flows.train"` |
| `cx_api` | **any** method, 1:1, **in any version**: `method="agents.playbooks.patch"` or `method="GET", path="agents/{agent}/flows"`, with `version="v3alpha1"` |
| `cx_endpoints_consola` | the endpoints the console uses and that **are in no descriptor**, and that `cx_api` could not reach |
| `cx_batch` | N grouped calls (the backend's `/batch` does not exist → falls back to threads and says so) |
| `cx_net_probe` | where the time of each request goes (send / headers / body) |
| `cx_limites_api` | **the MCP's memory**: the measured limits of the API (symptom → cause → workaround) |
| `cx_clone_agent` | **cleans the destination and recreates the original 1:1**, with a per-action benchmark |

`cx_api` covers 100 % of the surface (the 162 v3beta1 methods), including the operations
that are not typed: `environments`, `deployments`, `experiments`, `testCases`,
`versions`, `export`/`import`, `operations`…

#### Paths and versions in `cx_api`

One single rule, the same for everything (it lives in `cxp/endpoint.py`):

| path you pass | what happens |
|---|---|
| `agents/{agent}/flows` | **relative**: `projects/{project}/locations/{location}/` is prepended |
| `projects/other/agent/visibility` | **absolute**: it already carries the project, it is left alone |
| `/users/me/userSettings/recentProjects` | **absolute from the root**: it does not hang off a project (it starts with `/`) |
| `v3alpha1/projects/p/...` | additionally **pins the version** of the call |

`version=` (or the path prefix) chooses the proto: `v3beta1`, `v3alpha1`, `v3`,
`v2beta1`. The backend serves all four with the same descriptor.

### The typed core

`cx_list_agents`, `cx_get_agent`, `cx_update_agent`, `cx_list_flows`, `cx_get_flow`,
`cx_update_flow`, `cx_list_pages`, `cx_get_page`, `cx_update_page`, `cx_list_playbooks`,
`cx_get_playbook`, `cx_update_playbook`, `cx_list_children` (intents / entityTypes /
tools / webhooks / transitionRouteGroups / generators / changelogs / conversations),
`cx_get_conversation`, `cx_detect_intent`, `cx_train_flow`, `cx_validate_flow`.

### The gap fillers

| tool | what it does |
|---|---|
| `cx_gaps` | **detects live** the fields the backend exposes and the public API does not declare. It walks **every** collection of the descriptor (24 in v3beta1, including the nested ones) and returns what it finds, plus the measured catalogue and the **method** gaps. With `incluir_lab=True` it sweeps the `environments*` family in a **temporary lab agent** → coverage **23/24** |
| `cx_proto_interno` | the **systematic** list of gaps: 14 schemas / 38 fields, with the write state of each one **measured** |
| `cx_set_console_field` | **writes any field** (including out-of-schema ones) by path: `transitionRoutes[0].targetPlaybook`, `form.parameters[0].description`, `entities[0].dtmfPattern`… It verifies by reading back and says what was saved and what was lost |
| `cx_set_route_playbook` | playbook target of a **page** transition route |
| `cx_set_flow_route_playbook` | playbook target of a **flow router** transition route |
| `cx_set_group_route_playbook` | playbook target of a **route group** transition route |

👉 The catalogue of what each of these writes, and how it was measured, is in
**[docs/HUECOS_API.md](docs/HUECOS_API.md)**.

---

## Recipes

Things you can ask your agent (or run yourself) once it is set up.

**📋 "Show me what my agent can do that the public API cannot."**

```
cx_gaps(agent="<uuid>", incluir_lab=True)
```

**🎯 "When the channel is WhatsApp, go to the 'ventas' playbook."**

```
cx_get_flow(agent="<uuid>", flow="<flow>")            # 1. read the routes
cx_set_flow_route_playbook(agent="<uuid>", flow="<flow>",
                            route="<route-uuid>",
                            playbook="ventas", dry_run=true)   # 2. simulate
cx_set_flow_route_playbook(..., dry_run=false)                 # 3. write + verify
```

The tool always reads first, then simulates, then writes **and verifies**: if the route
pointed to a page or a flow, that target is a `oneof` and **is erased** — the `aviso`
tells you before you write.

**🧬 "Clone this agent into that one, 1:1."**

```
cx_clone_agent(origen="<uuid-original>", destino="<uuid-copy>", mode="apply")
```

It returns the **benchmark**: `ms_total`, `ms_fases`, per-action times, `omitidos` and
`desviaciones`. Use `mode="plan"` to see what it *would* do without writing.

**🗣️ "Which generative models does my agent have?"** (an endpoint in no descriptor)

```
cx_endpoints_consola(endpoint="agents.listAvailableModels")
```

**🧪 "What did the user say last Tuesday?"**

```
cx_list_children(agent="<uuid>", collection="conversations")
cx_get_conversation(agent="<uuid>", conversation="<id>")
```

**🧠 "Remind me of the traps before I write a lot."**

```
cx_limites_api()
```

---

## Instructions for the agent using this MCP

👉 **[docs/PROMPT_AGENTE.md](docs/PROMPT_AGENTE.md)** — a ready-made block to paste into
the system prompt of the agent that consumes this MCP (when to read first, when to
simulate, when to stop and ask).

---

## Verification

This project has **no hosted CI**: the badges at the top of this file are static SVG
files (`docs/badges/*.svg`) committed to the repository, generated from a real local run.

```bash
python tools/verifica.py
```

| badge | what it proves | how it is measured |
|---|---|---|
| `verification` | the whole run passed | every check below must be green |
| `tests` | the unit suite passes and how many tests it has | `pytest` (no network, no credentials) |
| `lint` | the code is clean | `ruff check cxp server.py tests probes tools` |
| `secrets` | no credentials in the tree **nor in the git history** | regex scan over `git ls-files` and over `git log --all -p` |
| `server` | the MCP server starts and exposes its tools | `import server` and count of the `cx_*` tools |

The script exits with status `1` when a check fails, so it can be used as a pre-commit
hook. `--check` runs everything without rewriting the badges and `--only <check>` runs a
single one.

The last full verification run (unit tests, smoke test, cleanup, the internal proto
catalogue and the live measurement of all 38 fields) is in
[docs/VERIFICATION.md](docs/VERIFICATION.md).

---

## Documentation map

| document | what is in it |
|---|---|
| **[docs/HUECOS_API.md](docs/HUECOS_API.md)** | 🍩 the **gaps**: the 38 fields the public API cannot express, the 9 endpoints in no descriptor, `targetPlaybook` in depth, and how each one was measured |
| **[docs/LIMITES_API.md](docs/LIMITES_API.md)** | 🪨 the **measured limits**: symptom → cause → workaround for 31 traps, the auth headers, the versions and the network constants |
| **[docs/SETUP_PROMPT.md](docs/SETUP_PROMPT.md)** | 🤖 the **setup prompt**: paste it in your agent and it configures everything (install → cookies → `.env` → client → verification) |
| **[docs/PROMPT_AGENTE.md](docs/PROMPT_AGENTE.md)** | 🧭 the **instructions for the agent** that consumes this MCP |
| **[docs/CONVERSACIONES.md](docs/CONVERSACIONES.md)** | 💬 the **conversation history** through the API, and how to publish the logs |
| **[docs/VERIFICATION.md](docs/VERIFICATION.md)** | 📋 the report of the last full run |

---

## Development

```bash
pip install -e ".[dev]"

pytest                  # unit tests (no network)
python smoke_test.py    # end-to-end test over the MCP protocol
```

* **`tests/`** — unit tests for the gap layer and for the generic writing (no network
  nor credentials needed).
* **`smoke_test.py`** — calls the real tools over the MCP protocol. It is **generic**:
  there is no agent, flow nor playbook hardcoded; it discovers everything in the agent
  of the `.env` and only writes on **temporary** resources that it deletes at the end.
* **`probes/`** — the probes that produced the measurements: the gap detector, the
  writing matrix and the console traffic capture experiments. They are not needed to
  operate the MCP. The agent ids they use come from the environment
  (`CXP_PROBE_AGENTS`, `CXP_PROBE_TARGETS`), never hardcoded.
* **`tools/verifica.py`** — runs the checks of the [Verification](#verification) section
  and regenerates the badges.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the golden rule: **if you claim it, measure
it.**

---

## Security

* **Your browser session cookies are credentials.** Treat them like a password: never
  commit them, never share them, never paste them in an issue.
* `.env`, `.session-state.json`, browser profiles and the discovery cache are
  gitignored. `tools/verifica.py` fails if a cookie, a Google API key or a `.env`
  appears **in the tree or in the git history**.
* The MCP enforces an **allowlist** of agents it may touch: anything not in
  `CXP_AGENT_READ` / `CXP_AGENT_READWRITE` is not accessible at all. The check runs in
  the low layer, against every agent of the request, and it **fails closed** on writes
  it cannot attribute to an agent.
* The path of a request is canonicalized before it is sent, so a crafted path cannot
  make the server reach a different agent than the one that was checked.
* Write access should only be granted to **your own copy** of an agent.
* The probes that capture console traffic **redact the credentials** before writing
  them, and write their output with mode `0600`.
* If you think you have leaked a cookie, **sign out of Google** and rotate your
  session.

Report a vulnerability privately: see [SECURITY.md](SECURITY.md).

---

## Legal notice

This project **is not affiliated with Google**. It talks to an internal Google backend
that is not part of the public API and uses your own account's first-party
authentication.

* Use it only with your own accounts and projects, and only for what the console
  already lets you do by hand.
* Your browser session cookies are **credentials**: treat them like a password (do not
  push them to a repository, do not share them).
* The data this MCP writes is the same data you write in the console: the MCP does not
  invent capabilities, it only uses a path the public API does not expose.
* Google's APIs and policies may change at any time.

---

## License

[Apache-2.0](LICENSE) © Cristofer Fuentes.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
