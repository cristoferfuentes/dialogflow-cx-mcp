# Contributing

Thanks for wanting to help. This is a small MCP: its value is that **everything it
claims is measured**, not assumed.

## Golden rule: if you claim it, measure it

Every claim about the API's behaviour (a specific error, a limit, a field that "cannot
be written") must come from **a real execution**, not from documentation or memory. If
you did not measure it, do not write it.

## How to measure

The probes in `probes/` are the instrument. The ones that produced the published
measurements are:

| probe | what it measures |
|---|---|
| `probes/probe_schema_gaps.py` | which fields the backend returns that the public schema does not declare |
| `probes/probe_console_only_writable.py` | which out-of-schema fields the server **keeps** when written |
| `probes/probe_console_only_writable3.py` | the final matrix (uses production code: `cxp.console_fields.set_field`) |
| `probes/test_new_tools.py` | the new tools, end to end |

Agent ids are **never hardcoded**: they come from the environment.

```bash
export CXP_PROBE_AGENTS="default=<uuid>"
export CXP_PROBE_TARGETS="default=<agent>,<flow>,<page>"
.venv/bin/python probes/probe_schema_gaps.py
```

**Hard rule when measuring:** write only to an agent in `CXP_AGENT_READWRITE` (your own
copy). Never to a reference agent nor to the production one: it would break parity with
the console. Create temporary resources and delete them.

## Before opening a PR

```bash
pytest                  # unit tests (no network)
python smoke_test.py    # end-to-end test (needs credentials)
ruff check cxp server.py tests probes tools

python tools/verifica.py    # all of the above + secret scan, and regenerates the badges
```

The repository has **no hosted CI**: the badges of the README are static SVG files
produced by `tools/verifica.py` on the machine that runs it. If you change the code,
run it and commit the regenerated badges.

* The **unit tests** must not touch the network. If your change needs to verify
  something about the backend, it goes in `probes/`, not in `tests/`.
* The **smoke test** must stay generic: no hardcoded agent, flow nor playbook ids, and
  no fixed counts.
* If you touch a layer, the smoke test has to **call every tool** that uses that layer.

## Style

* Code, comments and docstrings are in **English**.
* The MCP tool docstrings are its **API**: explain what it does, which parameters it
  takes and which errors it can raise.
* No new dependencies without a reason: the MCP works with `mcp` alone.

## When adding a limit or a gap

1. Measure it (with a probe) and note the **exact symptom** the server returns.
2. Add it to `cxp/limits.py` (limit) or to `cxp/gaps.py` (gap), with its cause and its
   workaround.
3. If it is a gap, state **whether it can be written** (state `writable` /
   `derived` / `read_only`): that is the information with the most value.

## Security

* **Never** push a `.env`, cookies, a `storage_state` or a real agent id to a PR.
* Session cookies are credentials: treat them like a password.
* If you find a security problem, do not open a public issue: see
  [SECURITY.md](SECURITY.md).

## Translations

The project documentation lives in English. If you want to help with another language,
open an issue first so we can agree on where the translated files live (the repository
keeps a single source of truth for now).
