# Conversation history (through the API)

The history of what the user said and the agent answered **is in the API**, in the
`agents.conversations` resource:

```
GET https://dialogflow.clients6.google.com/v3beta1/projects/{p}/locations/{l}/agents/{a}/conversations/{id}
```

Measured 2026-11-16:

* The history ids have the shape `<prefix>-<epoch_ms>-<hash>` (e.g.
  `176789778-1794839495291-3b210e2a646addc6`): they are **`conversations`**, not CX
  UUIDs. `agents.sessions` is only the live state of a session (parameters, entity
  types) and **does not** keep history.
* The `get` brings `interactions[]` with `request` (`queryInput` + `queryParams`) and
  `response` (`queryResult` with the agent's text, `match`, `generativeInfo`,
  `traceBlocks` and `stepMetrics` with the latencies).
* The **public** `get` (`dialogflow.googleapis.com`) returns the conversation **without
  `interactions`** (metadata only): for the full log you have to go to the console's
  backend.
* `conversations.list` paginates and returns the ids + metrics; the individual `get`
  returns the detail.

## From the MCP

| tool | what it does |
|---|---|
| `cx_list_children(agent, collection="conversations")` | lists the conversations of the agent |
| `cx_get_conversation(conversation, agent)` | the full log of one conversation (`interactions[]`) |

## Extraction script (raw JSON + readable Markdown + index)

```bash
.venv/bin/python probes/exp_conversation_logs.py \
    --agent <agent-uuid> --project <project-id> \
    --session <id> --session <id> ...
```

---

## Publishing the logs on a public channel (temporary)

Measured 2026-11-16: there are **three** paths and **only one** is really public.

| Path | Really public? | Note |
|---|---|---|
| `tailscale funnel` | **NO** (measured: 520/522 from outside) | the Tailscale proxy answers inside the tailnet, but **not** from the internet: the `ts.net` subdomain does not resolve to a public edge without the Tailscale client |
| GCS / `gcloud storage sign-url` | yes | but it **uses resources of the user's GCP project** |
| **Cloudflare quick tunnel** | **YES** (measured) | `cloudflared tunnel --url` -> a public `trycloudflare.com` subdomain, **without an account** and without touching the user's resources |

Setup (static server + tunnel, both as systemd services):

```bash
# 1) server: probes/serve_logs.py -> 127.0.0.1:8099, no directory listing, no
#    dotfiles, no POST, and WITH Range support (206).
# 2) tunnel: probes/public_tunnel.sh -> cloudflared --url http://127.0.0.1:8099
```

* `sirve-logs-conversaciones.service` — the static server.
* `tunnel-logs-publico.service` — the tunnel (starts on its own, `Restart=always`).
* The live URL is stored in `logs-conversaciones/.url-publica` and read with
  `probes/public_url.sh`.

**TRAP (measured):** the quick tunnel generates a **RANDOM subdomain on every start**
=> when the service restarts the URL **changes** and has to be shared again. It
propagates in ~10 s. For a fixed URL you need a Cloudflare *named tunnel* with an
account and a domain.

**TRAP (measured):** third-party HTTP relays (allorigins, codetabs) fail with
**522/520** against `trycloudflare.com` because Cloudflare does not route
Cloudflare->Cloudflare. To verify from outside you have to use a **non-Cloudflare**
reader (e.g. `r.jina.ai`).

---

## See also

* [README](../README.md) — install, credentials, tools.
* [HUECOS_API.md](HUECOS_API.md) — the fields the public API cannot express.
