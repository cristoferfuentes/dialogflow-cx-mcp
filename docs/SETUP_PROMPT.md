# Setup prompt: let your agent configure the MCP for you

Copy the block below, paste it into your agent (Claude Code, Claude Desktop, Cursor,
Windsurf, VS Code, OpenCode, anything that can run shell commands), and let it do the
whole setup: install, credentials, `.env`, MCP client registration and verification.

> **What the agent will need from you:** your Google Cloud **project id**, the
> **location** (`global` unless you know otherwise) and the **agent ids** you want to
> read and to write on (one or more in each list). Nothing else. Everything else it
> can do by itself — **except the cookies**, and only if this machine has a browser
> with your Google session. If it does not, the agent cannot obtain them and it will
> tell you so, with the manual recipe (see step 3).

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
   - the agent ids I may WRITE  -> CXP_AGENT_READWRITE (comma-separated, one or more)
   - (optional) the agent ids I may only READ -> CXP_AGENT_READ (comma-separated)
   If I do not know the agent ids, keep going with the rest and list the agents you
   can see once the session works, so I can pick one.
   NOTE: these two variables are REQUIRED: they are the allowlist of agents, the
   MCP works on them and on nothing else (there is no "default agent" variable),
   and they are the security boundary of the harness (the cookies can reach
   everything my Google account can reach; these lists are what keep the agent
   inside the agents I choose).

2) INSTALL
   git clone https://github.com/cristoferfuentes/dialogflow-cx-mcp.git
   cd dialogflow-cx-mcp
   python3 -m venv .venv            # needs Python >= 3.10
   .venv/bin/pip install -e .
   If `python3` does not exist, look for python3.11/3.12/pyenv and use it.

3) CREDENTIALS (the delicate step)
   The MCP authenticates like the console does: with MY Google session cookies
   (first-party auth) plus the console's public API key.
   Do NOT assume a route works: PROBE the machine first, then take the route that
   is really possible. Never invent a credential, and never continue as if you had
   one that you do not have.

   3.1 PROBE (run these and show me the real output):
        command -v google-chrome google-chrome-stable chromium chromium-browser chrome
        echo "DISPLAY=${DISPLAY:-<none>}"
        command -v Xvfb x11vnc websockify novnc_proxy
        ls -d ~/.config/google-chrome ~/.config/chromium 2>/dev/null
        (only if a Chrome binary AND a display exist)
        .venv/bin/python probes/renueva_cookies.py --check

   3.2 ROUTE A — a real Chrome with my Google session exists ON THIS MACHINE
       (a desktop browser with a display):
        .venv/bin/python probes/renueva_cookies.py
       It opens that Chrome over CDP and writes the COMPLETE .google.com cookie
       jar into the .env.
       - If it prints "the browser has no session": open
         https://conversational-agents.cloud.google.com in THAT Chrome, log in
         yourself, and run the command again. (The login and its reCAPTCHA are a
         human step: you cannot log in for me.)
       - If it cannot reach the browser: it needs a DISPLAY -> route A2 or B.

   3.3 ROUTE A2 — no desktop display, but this machine can run a VIRTUAL one
       (Xvfb is installed): the cookie is still obtainable, but I need to log in
       through the virtual browser:
       - Start the virtual display on :99 (this repo expects `CXP_DISPLAY=:99`)
         and, if noVNC/websockify are installed, expose it so I can open it in
         my browser (the repo has `probes/public_tunnel.sh` and
         `probes/public_url.sh` for that).
       - Give me the URL and wait until I tell you I am logged in.
       - Then run `probes/renueva_cookies.py` and check the result.
       - If no VNC tunnel is possible here, say so and continue with route B.

   3.4 ROUTE B — NO usable browser here (headless server with no virtual display,
       container, SSH box, no Chrome, or Chrome cannot be launched):
       You CANNOT obtain the cookie on this machine. Do not keep trying and do
       not fake it. Instead:
       - Say it plainly, in one sentence, with the reason: "the cookie cannot be
         obtained on this machine because <no Chrome / no display / no session>".
       - Give me the exact manual recipe and WAIT for me to paste it back:
         (1) on ANY machine where I am logged in, open
             https://conversational-agents.cloud.google.com
         (2) F12 -> Network -> tick "Preserve log" -> reload the page
         (3) filter by "clients6.google.com" and click a request like
             GET .../v3beta1/projects/.../agents/...?key=AIzaSy...
         (4) Request Headers -> copy the COMPLETE value of the `Cookie` header
             (it looks like "SAPISID=…; SID=…; HSID=…; __Secure-3PSID=…; …")
         (5) paste it back here, or write it yourself into <abs>/.env as
             CXP_COOKIE="…" (one line, double quotes)
       - IMPORTANT: copy it WHOLE. The 5 "required" cookies alone give a 401.
       - The SAME request URL gives the API key: the `key=` parameter
         (?key=AIzaSy...).
       - Continue with every other step (install, the rest of the .env, the
         client registration) while you wait, and finish as soon as I paste it.
       - Never ask me for a password, never ask me to log in for you, never
         invent a cookie, and never go to step 6 as if the setup were complete.

   3.5 API KEY: needed in BOTH routes. It is the `key=` parameter of any console
       request URL (?key=AIzaSy...). It is public and the same for every console
       user. If you cannot read it from a request, ask me for it with the same
       recipe as in 3.4 (step 3.4 gives you the URL that carries it).

   3.6 If ANY credential could not be obtained automatically, keep a list of
       what is missing and why. You will report it in step 7 and step 6 will
       not pass until it is fixed.

4) WRITE THE .env
   cp .env.example .env   and fill in:
     CXP_PROJECT=<project id>
     CXP_LOCATION=<location>
     CXP_CONSOLE_ORIGIN=https://conversational-agents.cloud.google.com
     CXP_API_KEY=<the key= parameter of that same console request>
     CXP_COOKIE="…"
     CXP_AGENT_READWRITE=<the agents I may write, comma separated>
     CXP_AGENT_READ=<the agents I may only read, comma separated>
   Then: chmod 600 .env
   - BOTH agent lists accept as many agents as needed (comma-separated). At least
     one of the two must have an agent: without them the MCP has nothing to work on.
   - These two lists ARE the agents of the MCP: it works on them and on nothing
     else (the others are not even listed), and they are the security boundary of
     your harness: the cookies can reach everything my Google account can reach,
     and these lists are what keep you inside the agents I chose.
   - CXP_AGENT_READWRITE MUST contain ONLY my own copies. Never the
     production one and never the reference agent.
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
   cd <repo> && .venv/bin/python -c "import sys; sys.path.insert(0,'.'); import server; r=server.cx_status(); print(r.get('credentials_ok'), r.get('session_accepted')); print(r.get('permissions')); print(r.get('error'))"
   It must print True True and the permission table.
   - If it prints 401: the cookie jar is incomplete -> repeat step 3 with the
     WHOLE header.
   - If it prints False for the session: the Google session expired.
   - If a credential was never obtained (route B), this CANNOT pass yet: report
     the setup as PENDING, say exactly which credential is missing and why, and
     repeat the manual recipe of step 3.3. Do not report success.
   Then restart my MCP client so it picks up the new server.

7) REPORT BACK (short)
   - where the repo and the .env ended up,
   - which client you registered and which file you touched,
   - which agents ended up with read and with write permission,
   - which credentials you obtained automatically and which ones you could NOT,
     with the reason and the exact manual recipe for each one you could not,
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
* **It cannot obtain the cookie where there is no browser.** The cookies live in a
  Google session of a real browser: on a headless server, in a container, over SSH or
  in any machine without a display and without Chrome, there is nothing to read them
  from. The prompt forces the agent to **probe the machine first** and to say so
  plainly when the automatic route is impossible, instead of failing later with a
  mysterious 401. In that case it hands you the manual recipe (open the console on any
  machine where you are logged in → DevTools → Network → a request to
  `dialogflow.clients6.google.com` → copy the **whole** `Cookie` header) and it
  finishes the rest of the setup while it waits for you to paste it back.
---

## The three routes of step 3

| the machine | what the agent does |
|---|---|
| **a desktop browser with your Google session** | it launches that Chrome over CDP, reads the **whole** `.google.com` cookie jar and writes it into the `.env` (`probes/renueva_cookies.py`). If the browser has no session, it opens the console there and **you** log in |
| **no desktop, but a virtual display is possible** (Xvfb) | it starts the virtual display (`:99`), exposes it with noVNC, **you** log in through that URL and it then reads the cookie jar from it |
| **no usable browser** (no Chrome, no display and no virtual one, container, SSH box) | it tells you **explicitly** that it cannot obtain the cookie on this machine, explains why in one sentence, and gives you the manual recipe. It never invents a credential and never reports the setup as finished |

---

## See also

* [README](../README.md) — the manual version of all of this.
* [HUECOS_API.md](HUECOS_API.md) — what the MCP adds on top of the public API.
* [LIMITES_API.md](LIMITES_API.md) — the traps the setup may hit (401, 409, `/batch`…).
