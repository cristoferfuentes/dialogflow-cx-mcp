# Security Policy

## Supported versions

Only the latest released version of `dialogflow-cx-mcp` receives security fixes.

| Version | Supported |
|---------|-----------|
| latest release | ✅ |
| older versions | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/cristoferfuentes/dialogflow-cx-mcp/security/advisories/new)
(select "Report a vulnerability"), or by email to the maintainer.

Please include:

* what the issue is and which tool or code path is involved;
* the steps to reproduce it;
* the impact you believe it has.

You can expect an acknowledgement within a few days. If the report is accepted, a fix
will be prepared and released, and you will be credited in the advisory unless you
prefer otherwise.

## How this project handles credentials

This MCP authenticates with **first-party session cookies** from your own Google
browser session. That has an important consequence:

> **Your cookies are the credential. Treat them exactly like a password.**

The project therefore:

* keeps `.env`, `.session-state.json`, browser profiles and the discovery cache out of
  git (see `.gitignore`);
* never asks for a service account key, an OAuth client secret or an API key of your
  own project;
* ships only the console's public API key as an example, never a personal one;
* enforces an **allowlist** of agents the server may read or write
  (`CXP_AGENT_READ` / `CXP_AGENT_READWRITE`), so a misconfigured server cannot touch
  agents you did not list;
* re-signs every request with a fresh `SAPISIDHASH` timestamp and never logs the
  `Cookie` or `Authorization` headers;
* has a CI job that fails the build if a session cookie, a Google API key or a `.env`
  appears **in the tree or in the git history**.

## How the allowlist is enforced

The permission check is deliberately **not** a filter in the MCP layer: it lives in the
lowest layer (`cxp/console_api.py`), so that no tool — not even the generic `cx_api`
passthrough — can reach an agent that is not on the list.

| rule | why |
|---|---|
| the check compares the **resolved id**, not an alias | the `"original"` alias can be bypassed by passing the agent's raw UUID |
| it looks at **every** agent of the request, in the path **and** in the query string | `cx_api` can put the whole resource name in `?name=…`; an agent that is only in the query is just as real |
| a write whose URL mentions **no agent** is rejected (**fail closed**) | every write in CX is per-agent, so a write without an agent can only be a crafted path |
| the path is **canonicalized** before it is sent (`.`/`..` resolved, `%2e`/`%2f` rejected) | the server resolves `..`, so `agents/OK/../../agents/FORBIDDEN` would land on `FORBIDDEN` while the guard read `OK` |
| `cx_batch` checks **each sub-request** | a batch must not be a way around the allowlist |

The behaviour is pinned by `tests/test_guard_seguridad.py`.

If you ever believe you leaked your cookies: **sign out of Google** to invalidate the
session, then paste a fresh header into your `.env`.

## Scope

In scope:

* the MCP server, its HTTP client and its guard logic;
* the probes shipped in `probes/`.

Out of scope:

* the Dialogflow CX service itself and any Google backend;
* your own Google account or the security of your project;
* use of this tool against accounts, projects or agents you are not authorised to touch.
