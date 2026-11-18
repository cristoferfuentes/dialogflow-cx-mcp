# dialogflow-cx-mcp

[![Verification](docs/badges/verification.svg)](docs/VERIFICATION.md)
[![Tests](docs/badges/tests.svg)](docs/VERIFICATION.md)
[![Lint](docs/badges/lint.svg)](docs/VERIFICATION.md)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io)

> An [MCP](https://modelcontextprotocol.io) server (stdio) for **Dialogflow CX** with **31 tools**.
> It mirrors the public `v3beta1` API 1:1 **and** reaches the fields the public API silently drops
> (`targetPlaybook`, `speechSettings`, `dtmfPattern`, `webClientSettings`…), plus the console
> endpoints that exist in no descriptor.

> ⚠️ **Not a Google product.** It talks to the console's internal backend with your own account's
> first-party authentication. Read the [legal notice](#legal-notice).

---

## Install

**The default way is to let your agent do it.** Copy this line into your agent
(Claude Code, Claude Desktop, Cursor, Windsurf, VS Code, anything that can run shell commands):

```text
Execute the instructions from this source https://raw.githubusercontent.com/cristoferfuentes/dialogflow-cx-mcp/refs/heads/main/docs/SETUP_PROMPT.md
```

The agent installs the package, gets the credentials, writes the `.env`, registers the server in
your MCP client and verifies it. It will only ask you for your project id, the location and the
agents you may read and write.

> **If the credentials cannot be obtained automatically, the agent says so.** The cookies live
> in a Google session of a real browser: on a headless server, in a container or over SSH there
> is nothing to read them from. The installer probes the machine first and, when the automatic
> route is impossible, it tells you **why**, hands you the manual recipe (copy the **whole**
> `Cookie` header from DevTools) and finishes the rest of the setup while you paste it back.
> It never invents a credential and never reports the setup as finished with a missing one.

<details>
<summary>Manual install</summary>

```bash
git clone https://github.com/cristoferfuentes/dialogflow-cx-mcp.git
cd dialogflow-cx-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env      # then fill it in (below)
```

**Requirements**: Python ≥ 3.10 and a Google account with access to the project.

</details>

---

## Configure

The credentials are your **browser session cookies**: the MCP authenticates against the console's
internal backend the same way the console does (no service account needed).

```bash
CXP_PROJECT=my-gcp-project
CXP_LOCATION=global
CXP_API_KEY=AIzaSy...                    # the key= of a console request
CXP_COOKIE="SAPISID=...; SID=...; ..."  # the WHOLE Cookie header
CXP_AGENT_READWRITE=<uuid>[,<uuid>...]  # read + write: ONLY your own copies
CXP_AGENT_READ=<uuid>[,<uuid>...]       # read-only
```

| how to get it | |
|---|---|
| **the API key** | DevTools → Network → a request to `dialogflow.clients6.google.com` → the `key=` parameter |
| **the cookie** | same request → **Request Headers** → copy the **whole** `Cookie` header (not only the 5 "required" cookies: with 5 of 23 the answer is **401**, with the header as-is it is **200**) |
| **automatically** | `.venv/bin/python probes/renueva_cookies.py` (real Chrome over CDP, writes the full jar into your `.env`) |

### The two agent lists are REQUIRED

`CXP_AGENT_READ` and `CXP_AGENT_READWRITE` are the allowlist, and **each one accepts as many
agents as you need** (comma-separated).

* **They are what makes the MCP work.** With no agent in either list there is nothing to operate
  on: the server starts, but every tool refuses (`no allowed agent`).
* **They are the security boundary of the harness.** The cookies can reach **everything** your
  Google account can reach; the allowlist is what keeps the agent inside the agents you chose. It
  is enforced in the low layer, on every agent of every request (path **and** query), with the
  path canonicalized before the check, and it **fails closed**.
* An agent that is not on the lists is not even listed: `cx_list_agents` does not show it and
  every operation on it is refused.
* `CXP_AGENT_READWRITE` must contain **only your own copies**, never a reference or production one.

`cx_status` returns the permission table (the level of each agent: `read` / `read/write`).

Full list of variables: [`.env.example`](.env.example) · Details: [docs/SETUP_PROMPT.md](docs/SETUP_PROMPT.md)

---

## Register it in an MCP client

The `env` block is **not optional** (the SDK does not inherit the parent environment, so without it
the server would not find your credentials):

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

| client | where |
|---|---|
| Claude Desktop | `claude_desktop_config.json` |
| Claude Code | `claude mcp add dialogflow-cx-mcp -- /path/to/.venv/bin/python /path/to/server.py` |
| Cursor | `~/.cursor/mcp.json` |
| VS Code (Copilot) | `.vscode/mcp.json` or your user `settings.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |

---

## Tools

31 tools. The responses speak Spanish.

| group | tools |
|---|---|
| **generic mirror** | `cx_status` (call it first) · `cx_discover` · `cx_api` (any method, any version) · `cx_endpoints_consola` · `cx_batch` · `cx_net_probe` · `cx_limites_api` · `cx_clone_agent` |
| **typed core** | `cx_list_agents` · `cx_get/update_agent` · `cx_list/get/update_flows` · `cx_list/get/update_pages` · `cx_list/get/update_playbooks` · `cx_list_children` · `cx_get_conversation` · `cx_detect_intent` · `cx_train_flow` · `cx_validate_flow` |
| **gap fillers** | `cx_gaps` (detects them live) · `cx_proto_interno` · `cx_set_console_field` · `cx_set_route_playbook` · `cx_set_flow_route_playbook` · `cx_set_group_route_playbook` |

`cx_api` covers 100 % of the public surface (162 methods / 32 resources in `v3beta1`), including
the untyped ones (`environments`, `deployments`, `experiments`, `testCases`, `versions`,
`export`/`import`, `operations`).

### Recipes

```
cx_gaps(agent="<uuid>", include_lab=True)                  # what the API cannot express
cx_set_flow_route_playbook(agent=…, flow=…, route=…,
                            playbook="ventas", dry_run=true) # simulate, then apply
cx_clone_agent(source="<uuid>", destination="<uuid>", mode="apply")
cx_endpoints_consola(endpoint="agents.listAvailableModels")
cx_limites_api()                                            # the measured traps
```

The write tools always **read first → simulate → write → verify**.

---

## The rules of the agent that uses this MCP

They ship with the handshake (the `instructions` of the MCP server) and `cx_status` returns them
again:

1. **Use only these tools.** Never call the public API (`dialogflow.googleapis.com`), the console
   backend or any other HTTP client directly: the public API **silently drops** the fields this MCP
   exists to write, and the MCP is the only path that enforces the allowlist.
2. **Work only on the agents of the allowlist.** The others do not exist for you.
3. **Write only on `CXP_AGENT_READWRITE`.** A read-only agent is never modified.
4. **If a task is blocked, re-plan it inside the limits** and tell the user exactly which
   permission to add. Never look for a way around them.

👉 Ready-made block for the system prompt: [docs/PROMPT_AGENTE.md](docs/PROMPT_AGENTE.md)

---

## Documentation

| document | what is in it |
|---|---|
| **[docs/SETUP_PROMPT.md](docs/SETUP_PROMPT.md)** | 🤖 the setup prompt (the default install) |
| **[docs/HUECOS_API.md](docs/HUECOS_API.md)** | 🍩 the gaps: the 38 fields the public API cannot express, the 9 endpoints in no descriptor |
| **[docs/LIMITES_API.md](docs/LIMITES_API.md)** | 🪨 the measured limits: symptom → cause → workaround for 31 traps |
| **[docs/PROMPT_AGENTE.md](docs/PROMPT_AGENTE.md)** | 🧭 the instructions for the agent that consumes this MCP |
| **[docs/CONVERSATIONS.md](docs/CONVERSATIONS.md)** | 💬 conversation history through the API |
| **[docs/VERIFICATION.md](docs/VERIFICATION.md)** | 📋 the report of the last full run |

## Development

```bash
pip install -e ".[dev]"
pytest                  # unit tests (no network, no credentials)
python smoke_test.py    # end-to-end over the MCP protocol
python tools/verifica.py   # regenerates the badges of this README
```

## Security

* **Your browser session cookies are credentials**: never commit them, never share them.
* `.env`, `.session-state.json` and browser profiles are gitignored, and `tools/verifica.py` fails
  if a cookie, a Google API key or a `.env` appears in the tree **or in the git history**.

Report a vulnerability privately: see [SECURITY.md](SECURITY.md).

## Legal notice

This project **is not affiliated with Google**. It talks to an internal Google backend that is not
part of the public API, using your own account's first-party authentication.

* Use it only with your own accounts and projects, and only for what the console already lets you
  do by hand.
* Google's APIs and policies may change at any time.

## License

[Apache-2.0](LICENSE) © Cristofer Fuentes. Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
