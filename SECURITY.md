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
  `Cookie` or `Authorization` headers.

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
