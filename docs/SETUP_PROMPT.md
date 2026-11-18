# Setup prompt: let your agent configure the MCP for you

Copy the block below, paste it into your agent (Claude Code, Claude Desktop, Cursor,
Windsurf, VS Code, OpenCode, anything that can run shell commands), and let it do the
whole setup: install, credentials, `.env`, MCP client registration and verification.

> **What the agent will need from you:** your Google Cloud **project id**, the
> **location** (`global` unless you know otherwise) and the **agent id** you want to
> write on. Nothing else. Everything else it can do by itself — except the cookies,
> and only if it cannot read them from a browser session on that machine (see step 3).

---

## The prompt

```text
Install and configure the Dialogflow CX MCP server (dialogflow-cx-mcp) on this
machine, end to end. I will only give you the project, the location and the agent;
you do the rest. Do NOT ask me to edit files by hand: do it yourself and show me the
real output of every command you run.

REPO: https://github.com/cristoferfuentes/dialogflow-cx-mcp.git
Docs: README.md, docs/SETUP_PROMPT.md, docs/HUECOS_API.md, docs/LIMITES_API.md

1) ASK ME (only what you cannot find out yourself):
   - the Google Cloud project id  -> CXP_PROJECT
   - the location                -> CXP_LOCATION (default: global)
   - the agent id you may WRITE  -> CXP_AGENT_READWRITE
   - (optional) agent ids you may only READ -> CXP_AGENT_READ
   If I do not know the agent ids, keep going with the rest and list the agents you
   can see once the session works, so I can pick one.
   NOTE: at least ONE of the two agent lists must have an agent: they ARE the
   list of agents of the MCP (there is no "default agent" variable).

2) INSTALL
   git clone https://github.com/cristoferfuentes/dialogflow-cx-mcp.git
   cd dialogflow-cx-mcp
   python3 -m venv .venv            # needs Python >= 3.10
   .venv/bin/pip install -e .
   If `python3` does not exist, look for python3.11/3.12/pyenv and use it.

3) CREDENTIALS (the delicate step)
   The MCP authenticates like the console does: with MY Google session cookies
   (first-party auth). Use the first option that works:
   a) If a desktop Chrome with my Google session exists on this machine, run:
        .venv/bin/python probes/renueva_cookies.py
      It opens the real Chrome over CDP and writes the COMPLETE .google.com cookie
      jar into the .env. If it says the browser has no session, open the console
      (https://conversational-agents.cloud.google.com) in that Chrome, log in, and
      run the command again.
   b) Otherwise ask me for the Cookie header, WHOLE, and tell me exactly how:
      open the Conversational Agents console (logged in) -> F12 -> Network ->
      tick "Preserve log" -> reload -> filter by "clients6.google.com" -> click the
      request -> Request Headers -> copy the COMPLETE value of the `Cookie` header
      (it looks like "SAPISID=…; SID=…; HSID=…; __Secure-3PSID=…; …").
      IMPORTANT: copy it WHOLE. The 5 "required" cookies alone give a 401.
      The SAME request URL gives you the API key: it is the `key=` parameter
      (?key=AIzaSy...).
   Put it in the .env as CXP_COOKIE="…" (one line, double quotes).
   c) The API key is needed in BOTH cases: it is the `key=` parameter of any console
      request URL (?key=AIzaSy...). It is public and the same for every console user.
      If you do not have it, open the console, copy it from the URL of a request to
      clients6.google.com and continue.

4) WRITE THE .env
   cp .env.example .env   and fill in:
     CXP_PROJECT=<project id>
     CXP_LOCATION=<location>
     CXP_CONSOLE_ORIGIN=https://conversational-agents.cloud.google.com
     CXP_API_KEY=<the key= parameter of that same console request>
     CXP_COOKIE="…"
     CXP_AGENT_READWRITE=<the agent I may write>
     CXP_AGENT_READ=<the agents I may only read, comma separated>
   Then: chmod 600 .env
   - CXP_AGENT_READWRITE MUST contain ONLY my own copy of the agent. Never the
     production one and never the reference agent.
   - These two lists ARE the agents of the MCP: it works on them and on nothing
     else (the others are not even listed), and at least one of the two must
     have an agent.
   - An entry can be `<uuid>` (valid in any project) or `<project>/<uuid>` (the
     MCP works on that agent in that project). Use the second form for an agent
     that lives in another project (the PROD one, for example).
   - .env is already gitignored: never commit it, never paste the cookies into the
     chat, an issue or a commit message.

5) REGISTER IT IN MY MCP CLIENT
   Find out which client I use (Claude Desktop / Claude Code / Cursor / VS Code /
   Windsurf) and add this block (with ABSOLUTE paths):
     {
       "mcpServers": {
         "dialogflow-cx-mcp": {
           "command": "<abs>/dialogflow-cx-mcp/.venv/bin/python",
           "args": ["<abs>/dialogflow-cx-mcp/server.py"],
           "env": { "CXP_ENV_FILE": "<abs>/dialogflow-cx-mcp/.env" }
         }
       }
     }
   The `env` block is NOT optional: the MCP SDK does not inherit the environment of
   the parent process.
   Paths: Claude Desktop -> ~/Library/Application Support/Claude/claude_desktop_config.json
   (macOS) or %APPDATA%\Claude\claude_desktop_config.json (Windows);
   Claude Code -> `claude mcp add dialogflow-cx-mcp -- <python> <server.py>`;
   Cursor -> ~/.cursor/mcp.json; VS Code -> .vscode/mcp.json (or user settings.json);
   Windsurf -> ~/.codeium/windsurf/mcp_config.json.
   Back up the file before editing it and keep its other servers intact.

6) VERIFY (do not declare success before this passes)
   cd <repo> && .venv/bin/python -c "import sys; sys.path.insert(0,'.'); import server; r=server.cx_status(); print(r.get('credenciales_ok'), r.get('sesion_aceptada')); print(r.get('permisos')); print(r.get('error'))"
   It must print True True and the permission table.
   - If it prints 401: the cookie jar is incomplete -> repeat step 3 with the WHOLE
     header.
   - If it prints False for the session: the Google session expired.
   Then restart my MCP client so it picks up the new server.

7) REPORT BACK (short)
   - where the repo and the .env ended up,
   - which client you registered and which file you touched,
   - which agents ended up with read and with write permission,
   - the exact output of the verification command,
   - the first thing I can now say to my agent, e.g. "list my flows".
   If something failed, show me the real error output; do not invent a workaround.
```

---
## Why step 3 is the delicate one

The MCP talks to the console's **internal backend** with **first-party authentication**:
the session cookies of your own browser. Measured (2026-11-13), with the same binary and
the same `.env`:

| cookie jar | answer |
|---|---|
| only the 5 "required" cookies (`SAPISID`, `SID`, `HSID`, `SSID`, `APISID`) | **401** *"Request had invalid authentication credentials"* |
| the **23** cookies of `.google.com`, the header as-is | **200** |

Session cookies are revalidated: a jar with only 5 of them gets locked out exactly when
the session renews. That is why the prompt insists on the **whole** header, and why
`probes/renueva_cookies.py` writes the whole jar.

## What the agent cannot do for you

* It cannot log into your Google account for you: the login (and the reCAPTCHA score) is
  a human step. Everything after it is automatable.
* It cannot decide which agent you may write on: that is a business decision, not a
  technical one. That is why the prompt asks you for it explicitly.

---

## See also

* [README](../README.md) — the manual version of all of this.
* [HUECOS_API.md](HUECOS_API.md) — what the MCP adds on top of the public API.
* [LIMITES_API.md](LIMITES_API.md) — the traps the setup may hit (401, 409, `/batch`…).
