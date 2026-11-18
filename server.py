"""MCP `cx-playbook`: **mirror of Dialogflow CX API v3beta1** + the
addition that the public API cannot express.

Tools
------------
  cx_status                   backend configuration, permissions and health
  cx_discover                 the API map (comes from the official discovery doc)
  cx_api                      generic passthrough: the 162 v3beta1 methods, 1:1
  ...                         typed core tools (agents, flows, pages,
                              playbooks, intents, entityTypes, tools, webhooks,
                              conversations, sessions, changelogs...)
  cx_set_route_playbook       playbook target of a transition route OF PAGE
  cx_set_flow_route_playbook  playbook target of the ROUTER OF A FLOW  <-- the extra
  cx_set_group_route_playbook playbook target of a route of a GROUP OF ROUTES
  cx_gaps                     the gaps of the public API, detected LIVE
  cx_set_console_field        write ANY field (including those outside the schema)

The MCP is **atomic** and **browserless**:

Everything goes through HTTP against the console's internal backend
(`dialogflow.clients6.google.com/v3alpha1|v3beta1`) with first-party authentication (the 5 Google session cookies + `SAPISIDHASH`
signature).
Credentials are read from an `.env`; see README.md.

Why the internal backend and not the public API: it is the only one that keeps
`transitionRoutes[].targetPlaybook`; in the public API that field **is silently
dropped** (the `oneof target` only accepts `target_page` and `target_flow`).

Permissions (allowlist per agent, in the `.env`):

    CXP_AGENT_READWRITE=<uuid>[,...]   read + write
    CXP_AGENT_READ=<uuid>[,...]        read-only
    CXP_PROJECTS=<project>[,...]      (optional) limits the projects

The reference agent and the PROD one are read-only; the clone is the
only writable one. The guard compares the **resolved id**, not an alias, and it
is applied at the low layer (`cxp/console_api.py`), not only at this layer.

Convention: the tool PARAMETERS are in English (the names of the
API v3beta1: `agent`, `flow`, `page`, `route`, `playbook`, `project`...);
the response KEYS are in English too."""
from __future__ import annotations

import copy
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _McpServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

from cxp.config import load_config
from cxp.console_api import KNOWN_LOCATIONS, ConsoleApi
from cxp.console_fields import set_field
from cxp.gaps import MEASURED_CATALOGUE, load_schema_index, scan_agent
from cxp.guard import (
    PermissionDeniedError,
    ReadOnlyAgentError,
    agent_from_path,
    agent_level,
    agents_by_level,
    assert_playbook_writable,
    assert_readable,
    assert_writable,
    permission_table,
    resolve_agent,
)
from cxp.httpclient import HttpStatusError, Session
from cxp.mirror import Mirror, MirrorError
from cxp.routing import find_route, summarize

# ---------------------------------------------------------------------------
# The rules the agent that consumes this MCP MUST follow.
#
# They are sent as the **instructions of the MCP server** (the client shows
# them to the model) and they are repeated in `cx_status` (the first call any
# agent should make), so that the boundaries of the MCP are known before the
# first tool call and not after the first refusal.
# ---------------------------------------------------------------------------
RULES = """\
1) USE ONLY THESE TOOLS. Never call the Dialogflow CX public API
(dialogflow.googleapis.com), the console backend or any other HTTP client
directly, and never reproduce this MCP's requests by hand: the public API
silently DROPS the fields this MCP exists to write (transitionRoutes[].\
targetPlaybook, advancedSettings.speechSettings, dtmfPattern, webClientSettings,\
...), and this MCP is also the only path that enforces the per-agent allowlist.

2) WORK ONLY ON THE AGENTS OF THE ALLOWLIST: the agents of CXP_AGENT_READ (read\
-only) and CXP_AGENT_READWRITE (read + write). Each list accepts as many agents\
as needed, and AT LEAST ONE of the two lists has to have an agent (not\
necessarily both). The others do not exist for you: they are not listed and\
every operation on them is refused. Call cx_status to see the table. The\
allowlist is the security boundary of your harness: the session cookies can\
reach everything the Google account can reach, and the allowlist is what keeps\
you inside the agents the user chose.

3) WRITES ONLY ON CXP_AGENT_READWRITE AGENTS. An agent of CXP_AGENT_READ is\
never modified. If a tool needs an agent and you did not pass one, it works only\
when the allowlist leaves a single candidate; otherwise it tells you which agents\
are available and you must choose.

4) IF A TASK IS BLOCKED BY THESE LIMITS, DO NOT LOOK FOR A WAY AROUND THEM: no\
direct API call, no other credential, no crafted path, no alias. Re-plan the task\
inside the limits (work on the copy instead of the original, ask the user which\
allowed agent to use, return a plan instead of applying it, read the reference\
agent instead of writing it). If the task genuinely needs more, tell the user\
exactly which permission has to be added (which agent, and CXP_AGENT_READ or\
CXP_AGENT_READWRITE)."""

mcp = _McpServer("cx-playbook", instructions=RULES)

CFG = load_config()

# ---------------------------------------------------------------------------
# A single HTTP session for ALL the server (lesson 2026-11-06: "one reused
# TLS connection per thread"). Previously one `ConsoleApi` was created per call ->
# one TLS handshake (0.66 s measured) on every MCP request: 2.66 s per
# call versus 0.50 s with keep-alive.
# ---------------------------------------------------------------------------
_HTTP = Session(timeout=CFG.socket_timeout, maxseg=CFG.tcp_maxseg,
                retries=CFG.http_retries, connect_timeout=CFG.connect_timeout)
_API = ConsoleApi(CFG, session=_HTTP)
_MIRROR = Mirror(CFG, session=_HTTP)

WRITE_VERBS = ("POST", "PATCH", "PUT", "DELETE")
HTTP_VERBS = ("GET", "POST", "PATCH", "PUT", "DELETE")


# --------------------------------------------------------------------- utils
def _api() -> ConsoleApi:
    return _API


def _mirror() -> Mirror:
    return _MIRROR


def _looks_like_uuid(value: str) -> bool:
    return bool(value) and len(value) == 36 and value.count("-") == 4


def _resolve_agent(agent: str) -> str:
    """The agent to work on.

    There is NO default agent: the allowlist is the list of agents.
    Without an explicit agent it works only when the allowlist leaves a single
    candidate; otherwise it says which agents are available."""
    return resolve_agent(agent, CFG)


def _resolve_flow(cons: ConsoleApi, agent_id: str, flow: str, project: str = "") -> str:
    """flow id by uuid or displayName (cached catalog)."""
    if not flow:
        return ""
    if _looks_like_uuid(flow):
        return flow
    by_display, _ = cons.flow_index(agent_id, project)
    if flow in by_display:
        return by_display[flow]
    # The catalog may be stale (a flow created a moment ago):
    # it is refreshed and retried ONCE.
    by_display, _ = cons.flow_index(agent_id, project, force=True)
    if flow in by_display:
        return by_display[flow]
    raise LookupError(f"flow not found: {flow}")


def _resolve_page(cons: ConsoleApi, agent_id: str, flow_id: str, page: str,
                  project: str = "") -> str:
    """Page id by uuid or displayName (cached catalog)."""
    if not page:
        return ""
    if _looks_like_uuid(page):
        return page
    by_display, _ = cons.page_index(agent_id, flow_id, project)
    if page in by_display:
        return by_display[page]
    by_display, _ = cons.page_index(agent_id, flow_id, project, force=True)
    if page in by_display:
        return by_display[page]
    raise LookupError(f"page not found: {page}")


def _resolve_playbook(cons: ConsoleApi, agent_id: str, playbook: str,
                      project: str = "") -> tuple[str, str]:
    """(id, displayName) of the target playbook (cached catalog)."""
    by_display, by_id = cons.playbook_index(agent_id, project)
    if playbook in by_display:
        return by_display[playbook], playbook
    if _looks_like_uuid(playbook) and playbook in by_id:
        return playbook, by_id[playbook]
    # Old catalog -> refresh and retry ONCE.
    by_display, by_id = cons.playbook_index(agent_id, project, force=True)
    if playbook in by_display:
        return by_display[playbook], playbook
    if _looks_like_uuid(playbook) and playbook in by_id:
        return playbook, by_id[playbook]
    raise LookupError(
        f"playbook not found: {playbook!r}. "
        f"Available: {sorted(by_display)}"
    )


def _playbook_names(cons: ConsoleApi, agent_id: str, project: str = "") -> dict[str, str]:
    """id -> displayName of the playbooks (to label the targets)."""
    return cons.playbook_index(agent_id, project)[1]


def _playbook_resource(agent_id: str, playbook_id: str, project: str = "") -> str:
    return (f"projects/{project or CFG.project}/locations/{CFG.location}"
            f"/agents/{agent_id}/playbooks/{playbook_id}")


def _resource_name(agent_id: str, project: str, *parts: str,
                  location: str = "") -> str:
    loc = location or CFG.location
    base = f"projects/{project}/locations/{loc}/agents/{agent_id}"
    for p in parts:
        if p:
            base += "/" + p
    return base


def _agent_of(name: str) -> str:
    parts = (name or "").split("/")
    return parts[5] if len(parts) > 5 and parts[4] == "agents" else ""


def _destination_maps(cons: ConsoleApi, agent_id: str, flow_id: str,
                      project: str) -> tuple[dict, dict]:
    """(id->displayName of pages, id->displayName of flows) to read targets."""
    pages: dict[str, str] = {}
    flows: dict[str, str] = {}
    try:
        pages = cons.page_index(agent_id, flow_id, project)[1]
    except Exception:
        pass
    try:
        flows = cons.flow_index(agent_id, project)[1]
    except Exception:
        pass
    return pages, flows


def _destination_current(route: dict, pbs_inv: dict, pages: dict,
                         flows: dict) -> dict:
    """Current target of a route: {type, field, reference, name}."""
    for field, label in (("targetPlaybook", "playbook"), ("targetPage", "page"),
                         ("targetFlow", "flow")):
        val = route.get(field)
        if not val:
            continue
        ref = str(val).split("/")[-1]
        if field == "targetPlaybook":
            name = pbs_inv.get(ref, ref)
        elif field == "targetPage":
            name = pages.get(ref, ref)
        else:
            name = flows.get(ref, ref)
        return {"type": label, "field": field, "reference": ref, "name": name}
    return {"type": None, "field": None, "reference": None, "name": None}


def _missing_credentials() -> dict:
    return {
        "error": "incomplete credentials",
        "missing_cookies": CFG.missing_cookies(),
        "has_api_key": bool(CFG.api_key),
        "has_project": bool(CFG.project),
        "env_file": str(CFG.env_file) if CFG.env_file else None,
        "hint": "see README.md -> 'How to get the credentials'",
    }


def _err(exc: Exception) -> dict:
    if isinstance(exc, HttpStatusError):
        return {"error": f"HTTP {exc.status}", "detail": exc.body[:400]}
    if isinstance(exc, (PermissionDeniedError, ReadOnlyAgentError)):
        return {"error": str(exc)}
    if isinstance(exc, LookupError):
        return {"error": str(exc)}
    return {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}


def _agent_from_path(path: str) -> tuple[str, str]:
    """(project, agent) of a path/URL (delegates to `cxp.guard`)."""
    return agent_from_path(path)


def _guard_path(path: str, verb: str) -> None:
    """Apply the agent permission that appears in the request path."""
    project, agent = _agent_from_path(path)
    if not agent:
        return
    if verb.upper() in WRITE_VERBS:
        assert_writable(project, agent, CFG)
    else:
        assert_readable(project, agent, CFG)


def _agent_for_the_path(path: str, agent: str,
                        params: dict | None = None) -> str:
    """The agent to fill the `{agent}` placeholder of a path template.

    It is resolved ONLY when the template needs it and nobody provided it: a
    path without an agent (e.g. `GET /projects`) is legitimate and must not
    fail because the allowlist has several agents."""
    if agent:
        return agent
    if params and params.get("agent"):
        return str(params["agent"])
    if "{agent}" in (path or ""):
        return resolve_agent("", CFG)
    return ""


def _project_of_agent(agent_id: str) -> str:
    """The project of an allowlist entry written as `<project>/<uuid>` ('' if none).

    An entry with a project means "this agent, in that project": the MCP works
    on it there instead of on `CXP_PROJECT` (the agent may live in another
    project, e.g. the PROD one)."""
    target = (agent_id or "").strip()
    if not target:
        return ""
    for entry in CFG.agent_readwrite + CFG.agent_read:
        if "/" not in entry:
            continue
        project, _, agent = entry.partition("/")
        if agent.strip() == target:
            return project.strip()
    return ""


def _single_agent(candidates: list[str], what: str) -> str:
    """The only candidate of a list (or '' when there is none or more than one).

    It is used to deduce the source/destination of a clone from the
    allowlist: with several candidates the MCP does not guess."""
    if len(candidates) == 1:
        return candidates[0]
    return ""


def _resolve_path(path: str, name: str, parent: str, params: dict | None,
                  query: dict | None, project: str, agent: str,
                  version: str = "", location: str = "") -> tuple[str, dict]:
    """(path relative to the version, query) of a request.

    It is the "path" half of `_build_url`, kept separate so it can be reused in the
    `/batch` endpoint (which carries its own URLs inside the multipart).

    Route resolution (absolute, relative, with version) lives in
    `cxp.endpoint`: it is the same rule used by `cx_api` and the batch."""
    from cxp.endpoint import PathError, resolve_path
    try:
        p, _version_final, extra = resolve_path(
            path, version=version, project=project,
            location=location or CFG.location,
            name=name, parent=parent, params=params,
            agent=_agent_for_the_path(path, agent, params),
        )
    except PathError as exc:
        raise MirrorError(str(exc)) from exc
    q = {k: v for k, v in (query or {}).items() if v is not None}
    q.update({k: v for k, v in extra.items() if v is not None})
    return p, q


def _build_url(path: str, name: str, parent: str, params: dict | None,
               query: dict | None, project: str, agent: str,
               version: str = "", location: str = "") -> str:
    """URL from a relative or absolute path template."""
    from urllib.parse import urlencode

    from cxp.endpoint import PathError, resolve_path

    try:
        p, version_final, extra = resolve_path(
            path, version=version, project=project,
            location=location or CFG.location,
            name=name, parent=parent, params=params,
            agent=_agent_for_the_path(path, agent, params),
        )
    except PathError as exc:
        raise MirrorError(str(exc)) from exc
    q = {"key": CFG.api_key,
         **{k: v for k, v in (query or {}).items() if v is not None},
         **{k: v for k, v in extra.items() if v is not None}}
    # MEASURED (2026-11-15): the host depends on the path location (for
    # `global` the global host; for the rest, the regional host).
    from cxp.console_api import host_from_path
    return (f"https://{host_from_path(p)}/{version_final}/{p}?{urlencode(q)}")


# ------------------------------------------------------------------- tools
@mcp.tool()
def cx_limites_api(filter: str = "") -> dict:
    """**MCP memory**: the MEASURED limits of the Dialogflow CX API.

    Each limit comes with the exact symptom the server returns, why it
    occurs and how to work around it. No arguments: all. `filter`: free text
    ("force", "playbook", "batch", "reference"...).

    This is what the API does **not** document: the official discovery doc says
    `referencedPlaybooks` is read-only and says nothing about `force`, the
    `/batch`, the loops between playbooks or the real latency."""
    from cxp.limits import CONSTANTES, search

    limits = search(filter)
    return {
        "filter": filter or None,
        "total": len(limits),
        "constants": CONSTANTES,
        "limits": limits,
    }


@mcp.tool()
def cx_status() -> dict:
    """Status of MCP: configuration, per-agent permissions and backend health.

    Returns where the configuration came from, whether the 5 mandatory
    cookies are present, the proto version in use, the **permission table** of each
    agent (read / read-write / no access), the **rules** the agent that
    consumes this MCP must follow, and whether the internal backend ACCEPTS the
    session (real GET).

    **Call it first**: it is the fastest way to know which agents exist for
    this MCP and what can be done with them."""
    out: dict[str, Any] = {
        "project": CFG.project or None,
        "location": CFG.location,
        "api_version": CFG.api_version,
        "console_origin": CFG.console_origin,
        "backend": f"{CFG.console_backend}/{CFG.api_version}",
        "env_file": str(CFG.env_file) if CFG.env_file else None,
        "api_key": "set" if CFG.api_key else "MISSING",
        "required_cookies": list(("SAPISID", "SID", "HSID", "SSID", "APISID")),
        "missing_cookies": CFG.missing_cookies(),
        "allowed_agents": CFG.allowed_agent_ids(),
        "read_only_agents": agents_by_level(CFG, "read"),
        "writable_agents": agents_by_level(CFG, "read/write"),
        "allowed_projects": CFG.projects or "(no restriction)",
        "permissions": permission_table(CFG),
        "rules": RULES,
    }
    permissions_error = CFG.permissions_error()
    if permissions_error:
        # Without an agent in the allowlist the MCP has nothing to work on.
        out["permissions_ok"] = False
        out["permissions_error"] = permissions_error
        out["credentials_ok"] = False
        out.update(_missing_credentials())
        out["error"] = permissions_error
        return out
    out["permissions_ok"] = True
    try:
        from cxp.limits import CONSTANTES, LIMITES
        out["performance_constants"] = CONSTANTES
        out["api_limits"] = [f"{x['id']}: {x['title']}" for x in LIMITES]
        out["see_limits"] = "cx_limites_api()"
    except Exception:  # pragma: no cover
        pass
    if not CFG.credentials_ok():
        out["credentials_ok"] = False
        out.update(_missing_credentials())
        return out

    try:
        cons = _api()
        agents = cons.list_agents()
        out["credentials_ok"] = True
        out["session_accepted"] = True
        # ONLY the agents of the allowlist are visible: the rest does not
        # exist for this MCP (and it could not work on them either).
        out["visible_agents"] = [
            {"id": a["name"].split("/")[-1],
             "displayName": a.get("displayName"),
             "project": a["name"].split("/")[1],
             "permission": agent_level(a["name"].split("/")[1],
                                       a["name"].split("/")[-1], CFG)}
            for a in agents
            if agent_level(a["name"].split("/")[1],
                           a["name"].split("/")[-1], CFG) != "none"
        ]
        out["hidden_agents"] = len(agents) - len(out["visible_agents"])
        # Are the allowed agents really there? An entry of the allowlist may
        # point to an agent that does not exist any more (deleted, or in a
        # project this session does not see): the MCP says it instead of
        # failing later with a 404.
        out["checked_agents"] = _check_agents(cons)
    except HttpStatusError as exc:
        out["credentials_ok"] = False
        out["session_accepted"] = False
        if exc.status in (401, 403):
            out["error"] = (f"HTTP {exc.status}: the backend does NOT accept the "
                            "session. The cookies expired or were copied badly; "
                            "copy them again following README.md.")
        else:
            out["error"] = f"HTTP {exc.status}: {exc.body[:300]}"
    except Exception as exc:
        out["credentials_ok"] = False
        out["session_accepted"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return out


@mcp.tool()
def cx_discover(filter: str = "", method: str = "", project: str = "",
                version: str = "") -> dict:
    """The API map that this MCP reflects (v3beta1 discovery doc).

    No arguments: all resources with their methods (162 in v3beta1).
    `filter`: substring that filters by resource or method ("flows", "playbook"...).
    `method`: detail of ONE method (discovery id, e.g. "agents.flows.train").
    `project`: if given, shows which agents there are in that project.
    `version`: proto version ("v3beta1", "v3alpha1", "v3", "v2beta1"). The
        backend serves all four with the same descriptor: v3beta1 is the
        superset (162 methods), v3alpha1 has only 8."""
    mir = _mirror()
    try:
        if method:
            return {"method": mir.describe(method, version)}

        idx = mir.index(version)
        rows: list[dict] = []
        for mid, spec in sorted(idx.items()):
            if filter and filter.lower() not in mid.lower():
                continue
            rows.append({
                "id": mid,
                "http": spec.get("httpMethod"),
                "path": spec.get("path"),
                "required_params": sorted(
                    k for k, v in (spec.get("parameters") or {}).items()
                    if v.get("required")
                ),
            })
        out = {
            "api_version": version or CFG.api_version,
            "total_methods": len(idx),
            "filter": filter or None,
            "methods": rows,
        }
        if project:
            try:
                cons = _api()
                out["project_agents"] = [
                    {"id": a["name"].split("/")[-1], "displayName": a.get("displayName")}
                    for a in cons.list_agents(project)
                ]
            except Exception as exc:
                out["project_agents_error"] = str(exc)[:200]
        return out
    except MirrorError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_api(method: str = "", path: str = "", name: str = "", parent: str = "",
           params: dict | None = None, body: dict | None = None,
           query: dict | None = None, project: str = "", agent: str = "",
           version: str = "", location: str = "") -> dict:
    """Call GENERIC to the API: any method, 1:1, in any version.

    `method`: discovery id ("agents.flows.list", "agents.playbooks.patch",
    "agents.sessions.detectIntent"...) or an HTTP verb ("GET", "POST"...).
    `path`: path template ("agents/{agent}/flows/{flow}") when `method` is
    an HTTP verb. It accepts `{name}`, `{parent}` and loose `{...}`.
    `name`/`parent`: full resource names
    ("projects/p/locations/l/agents/a/flows/f"). With `agent` and `project`,
    `parent` can be abbreviated.
    `params`: values for the holes of the template.
    `body`: JSON body (POST/PATCH).
    `query`: extra query parameters (updateMask, languageCode, pageSize...).
    `version`: proto version ("v3beta1", "v3alpha1", "v3", "v2beta1").
        By default, the one of `CXP_API_VERSION`. **It can also be put in the
        path itself**: "v3alpha1/projects/p/...".
    `location`: Dialogflow CX location ("global", "us-central1"...). By
        default, `CXP_LOCATION`. **Measured (2026-11-15): every location has its
        own host** (`{location}-dialogflow.clients6.google.com`, and
        `dialogflow.clients6.google.com` for `global`), and the global host does
        NOT serve another location (400 *"to find the correct endpoint"*).

    Paths (one single rule, the same for all):
      * "agents/{agent}/flows"      -> RELATIVE: `projects/{project}/locations/{location}/`
                                       is prepended
      * "projects/p/agent/visibility" -> ABSOLUTE: it already carries the project
      * "/users/me/userSettings/recentProjects" -> ABSOLUTE FROM THE ROOT
        (this is how endpoints that do not hang off a project are reached)
      * "v3alpha1/..." -> it also pins the version of the call

    Response: the backend's raw JSON."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    mir = _mirror()
    # The project of an allowlist entry ('<project>/<uuid>') wins over
    # `CXP_PROJECT`: the agent may live in another project.
    api_agent = (agent or (params or {}).get("agent")
                 or _agent_of(name or "") or _agent_of(parent or ""))
    proj = project or _project_of_agent(api_agent) or CFG.project
    try:
        verb = ""
        if method and method.upper() in HTTP_VERBS:
            verb = method.upper()
            if not path:
                return {"error": "with an HTTP verb, `path` is required"}
            url = _build_url(path, name, parent, params, query, proj, agent,
                             version=version, location=location)
        elif method:
            spec = mir.spec(method, version)
            verb = spec.get("httpMethod") or "GET"
            p = dict(params or {})
            if name:
                p["name"] = name
            if parent:
                p["parent"] = parent
            if not p.get("parent"):
                p["parent"] = _resource_name(
                    agent or _agent_of(p.get("name") or ""), proj,
                    location=location)
            url, verb = mir.url(method, params=p, version=version)
        else:
            return {"error": "`method` is missing"}

        _guard_path(url, verb)
        cons = _api()
        if verb in WRITE_VERBS:
            data = cons.request(verb, url, body=body or {}, query=query)
        else:
            data = cons.request(verb, url, body=body or None, query=query)
        return {"method": method or path, "http": verb, "url": url.split("?")[0],
                "data": data}
    except MirrorError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_batch(requests: list[dict] | None = None, project: str = "") -> dict:
    """Run N calls to API in a SINGLE HTTP request (endpoint `/batch`).

    Each element of `requests`:
        {"method": "POST", "path": "agents/{agent}/flows", "body": {...},
         "query": {...}, "agent": "<uuid>", "params": {...}}

    `method` accepts an HTTP verb or a discovery id ("agents.flows.create").
    The **guard is applied sub-request to sub-request**: a batch mixing
    read and write agents is evaluated one by one (nobody slips
    through via the batch).

    Returns the results in the SAME input order, with their HTTP code,
    and the total round-trip time. It is the fast path for bulk work:
    instead of N round trips (each ~0.5 s of latency), one."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    reqs = list(requests or [])
    if not reqs:
        return {"error": "`requests` is missing"}
    # Each sub-request carries its own agent, so the batch has ONE project.
    proj = project or CFG.project
    t0 = time.perf_counter()
    try:
        mir = _mirror()
        items: list[dict] = []
        for item in reqs:
            method = str(item.get("method") or "GET").upper()
            agent = item.get("agent") or ""
            if method in HTTP_VERBS:
                p, q = _resolve_path(item.get("path") or "", item.get("name") or "",
                                     item.get("parent") or "", item.get("params"),
                                     item.get("query"), proj, agent)
                items.append({"method": method, "path": p, "query": q,
                              "body": item.get("body")})
                continue
            # discovery id: resolves to its path and the verb is reused
            values = dict(item.get("params") or {})
            if item.get("name"):
                values["name"] = item["name"]
            if item.get("parent"):
                values["parent"] = item["parent"]
            if not values.get("parent"):
                values["parent"] = _resource_name(_resolve_agent(agent), proj)
            url, verb = mir.url(method, params=values)
            from urllib.parse import parse_qsl, urlsplit
            sp = urlsplit(url)
            rel = sp.path.split(f"/{CFG.api_version}/", 1)[-1]
            q = {k: v for k, v in parse_qsl(sp.query) if k != "key"}
            items.append({"method": verb, "path": rel, "query": q,
                          "body": item.get("body")})
        cons = _api()
        try:
            out = cons.batch(items, project=proj)
            mode = "batch"
        except HttpStatusError as exc:
            if exc.status != 404:
                raise
            # The internal backend does NOT implement `/batch` (404). It falls back to the
            # same sub-requests in THREADS: this is what the batch intended.
            out = cons.batch_secondary(items, project=proj)
            mode = "threads (/batch does not exist on this backend)"
        ms = (time.perf_counter() - t0) * 1000
        results = []
        for i, r in enumerate(out):
            results.append({
                "index": i,
                "http": r.get("status"),
                "ok": 200 <= int(r.get("status") or 0) < 300,
                "data": r.get("data"),
            })
        return {
            "n": len(items),
            "mode": mode,
            "ms_total": round(ms, 1),
            "ms_per_subrequest": round(ms / max(len(items), 1), 1),
            "errors": sum(1 for r in results if not r["ok"]),
            "responses": results,
        }
    except MirrorError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_net_probe(repetitions: int = 5, path: str = "agents/{agent}",
                 agent: str = "", project: str = "", parallel: int = 1) -> dict:
    """Network diagnostics: **where the time goes** for each request.

    Makes `repetitions` GET of the same resource and returns, request by request,
    the split between send, header wait and body, and whether the connection was
    reused. With `parallel` > 1 the requests go out at once (separate threads
    -> one TLS connection each).

    Useful to tell "the network is slow" from "the backend is slow" and to measure the
    effect of any change in the transport."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    cons = _api()
    agent_id = _resolve_agent(agent)
    proj = project or _project_of_agent(agent_id) or CFG.project
    try:
        url = cons._url(f"{cons._base(proj)}/{path.replace('{agent}', agent_id)}")
    except Exception:
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}")
    _HTTP.take_stats()
    t0 = time.perf_counter()
    if parallel > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            list(pool.map(lambda _i: cons.http.json("GET", url, cons._headers()),
                          range(repetitions)))
    else:
        for _ in range(repetitions):
            cons.http.json("GET", url, cons._headers())
    ms = (time.perf_counter() - t0) * 1000
    rows = _HTTP.take_stats()
    return {
        "url": url.split("?")[0],
        "repetitions": repetitions,
        "parallel": parallel,
        "ms_total": round(ms, 1),
        "ms_per_request": round(ms / max(repetitions, 1), 1),
        "requests": rows,
    }


@mcp.tool()
def cx_clone_agent(source: str = "", destination: str = "", project: str = "",
                   reads: int = 8, writes: int = 2,
                   verify: bool = True, mode: str = "apply") -> dict:
    """Clone an agent 1:1: **clean the target** and **recreate the original** inside it.

    `source` = read-only agent (the source). `destination` = writable agent
    (our copy). The guard is enforced at the low layer: it is impossible to touch
    the original with this tool.

    Phases (with their timing):
      1. `read`  - snapshot of the original (reads IN PARALLEL).
      2. `delete`  - all elements of the target are deleted. Independent
                      deletions run `writes` in parallel (2 = measured optimum);
                      playbooks and intents, in series, because their
                      order is imposed by the server.
      3. `create` - everything is recreated in dependency order, remapping
                      the ids (the original's resources have different ids). It runs
                      **in series**: the backend serializes writes to the
                      same agent.
      4. `verify` - the target is re-read and compared with the original.

    `mode="plan"` writes nothing: it returns what would be deleted and what would be
    created. `reads` = parallel read threads (8 = measured optimum)."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    from cxp.clone import Cloner

    cons = _api()
    # The clone works with ONE project for the source and the target (the
    # ids are remapped inside it): the project is explicit or `CXP_PROJECT`.
    proj = project or CFG.project
    # There is no default agent: the source is the readable agent (the
    # read-only one of the allowlist) and the target the writable one.
    src = source or _single_agent(agents_by_level(CFG, "read"), "source")
    dst = destination or _single_agent(agents_by_level(CFG, "read/write"),
                                       "destination")
    if not src or not dst:
        return {
            "error": "cannot deduce the source/destination from the allowlist",
            "readable_allowed": agents_by_level(CFG, "read"),
            "writable_allowed": agents_by_level(CFG, "read/write"),
            "hint": "pass source=<uuid> and destination=<uuid>",
        }
    if src == dst:
        return {"error": "the source and the destination are the same agent"}
    try:
        cloner = Cloner(cons, src, dst, project=proj,
                        reads=reads, writes=writes)
        if mode == "plan":
            snap = cloner.snapshot()
            state = cloner._state(dst)
            return {
                "mode": "plan",
                "source": src, "destination": dst,
                "original": {k: len(v) for k, v in snap.items()
                             if isinstance(v, list)},
                # NOTE: the target id key is "destination"; the counts
                # go separately (if they share a name, the id is lost).
                "destination_counts": {k: len(v) for k, v in state.items()},
                "original_pages": sum(len(v) for v in (snap.get("pages") or {}).values()),
                "ms": round(cloner._t_phases.get("read", 0.0), 1),
            }
        if mode == "clean":
            # Only the target cleanup (without touching the original).
            t0 = time.perf_counter()
            cleaned = cloner.clean()
            return {"mode": "clean", "source": src, "destination": dst,
                    "ms_total": round((time.perf_counter() - t0) * 1000, 1),
                    "clean": cleaned,
                    "actions": cloner.actions,
                    "errors": cloner.errors}
        report = cloner.run(verify=verify)
        return {"mode": "apply", **report}
    except PermissionError as exc:
        return {"error": f"permission: {exc}"}
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------- typed
@mcp.tool()
def cx_list_agents(project: str = "", location: str = "",
                   all_locations: bool = False) -> dict:
    """List the agents this MCP can work on (the ones of the allowlist).

    The session may see more agents in the project, but **they are not
    listed**: an agent that is not in `CXP_AGENT_READ` nor in
    `CXP_AGENT_READWRITE` does not exist for this MCP (it could not be read
    nor written either). `hidden_by_permissions` says how many were filtered out.

    - project: project (default, `CXP_PROJECT`).
    - location: location (default, `CXP_LOCATION`).
    - all_locations: also list in the remaining locations, as the
      console does (which calls each one's regional host). MEASURED
      (2026-11-15): each location has its own host
      (`{location}-dialogflow.clients6.google.com`); the global host does NOT work
      for another location."""
    try:
        cons = _api()
        locations = [location or CFG.location]
        if all_locations:
            locations = KNOWN_LOCATIONS
        rows: list[dict] = []
        errors: list[dict] = []
        hidden = 0
        for loc in locations:
            try:
                agents = cons.list_agents(project, location=loc)
            except Exception as exc:
                errors.append({"location": loc, "error": str(exc)[:200]})
                continue
            for a in agents:
                parts = a["name"].split("/")
                permission = agent_level(parts[1], parts[-1], CFG)
                if permission == "none":
                    # Not on the allowlist: it does not exist for this MCP.
                    hidden += 1
                    continue
                rows.append({
                    "id": parts[-1],
                    "displayName": a.get("displayName"),
                    "project": parts[1],
                    "location": parts[3] if len(parts) > 3 else loc,
                    "permission": permission,
                    "in_the_project": True,
                    "startFlow": (a.get("startFlow") or "").split("/")[-1],
                    "startPlaybook": (a.get("startPlaybook") or "").split("/")[-1],
                })
        output = {"project": project or CFG.project,
                  "locations": locations,
                  "total": len(rows), "agents": rows,
                  "filtered_by_permissions": True,
                  "hidden_by_permissions": hidden}
        # The agents of the allowlist that do NOT live in the listed project
        # are added too: the list is "the agents this MCP can work on", not
        # "the agents of a project".
        missing = _allowed_agents_outside(rows)
        if missing:
            output["agents"].extend(missing)
            output["total"] = len(output["agents"])
        if errors:
            output["errors"] = errors
        return output
    except Exception as exc:
        return _err(exc)


def _check_agents(cons: ConsoleApi) -> list[dict]:
    """A real GET on every allowed agent: does it still exist?

    An entry of the allowlist can point to an agent that is not there any
    more (deleted, or in a project this session does not see). The MCP says
    so instead of failing later with a 404."""
    output: list[dict] = []
    for agent_id in CFG.allowed_agent_ids():
        project = _project_of_agent(agent_id) or CFG.project
        url = cons._url(f"{cons._base(project)}/agents/{agent_id}")
        row = {"agent": agent_id, "project": project}
        try:
            cons.request("GET", url)
            row["accessible"] = True
        except HttpStatusError as exc:
            row["accessible"] = False
            row["http"] = exc.status
        except Exception as exc:  # pragma: no cover
            row["accessible"] = False
            row["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        output.append(row)
    return output


def _allowed_agents_outside(rows: list[dict]) -> list[dict]:
    """The allowed agents that the project listing did not return.

    An allowlist entry may point to an agent of **another project** (the PROD
    one, for example): it is allowed, so it has to be visible."""
    seen = {r.get("id") for r in rows}
    outside: list[dict] = []
    for entry in CFG.agent_readwrite + CFG.agent_read:
        if "/" in entry:
            project, _, agent = entry.partition("/")
        else:
            project, agent = "", entry
        project, agent = project.strip(), agent.strip()
        if not agent or agent in seen:
            continue
        level = agent_level(project, agent, CFG)
        if level == "none":
            continue
        outside.append({
            "id": agent,
            "displayName": None,
            "project": project or "(another project)",
            "location": None,
            "permission": level,
            "in_the_project": False,
        })
        seen.add(agent)
    return outside


@mcp.tool()
def cx_get_agent(agent: str = "", project: str = "") -> dict:
    """GET of an agent (v3beta1)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        data = cons.request("GET", cons._url(f"{cons._base(proj)}/agents/{agent_id}"))
        return {"agent": agent_id, "project": proj, "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_update_agent(agent: str = "", body: dict | None = None,
                    update_mask: str = "", project: str = "") -> dict:
    """PATCH of an agent (v3beta1). Requires write permission."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        assert_writable(proj, agent_id, CFG)
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}", alt="json")
        data = cons.request("PATCH", url, body=body or {},
                            query={"updateMask": update_mask} if update_mask else None)
        return {"agent": agent_id, "project": proj,
                "updateMask": update_mask or "(derived from the body)", "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_list_flows(agent: str = "", project: str = "") -> dict:
    """List the flows of an agent."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        flows = cons.list_flows(agent_id, proj)
        return {"agent": agent_id, "project": proj, "total": len(flows),
                "flows": [{"id": f["name"].split("/")[-1],
                           "displayName": f.get("displayName"),
                           "routes": len(f.get("transitionRoutes") or [])}
                          for f in flows]}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_flow(flow: str = "", agent: str = "", project: str = "") -> dict:
    """GET of a flow (v3beta1), with its router (routes and targets)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        data = cons.get_flow(agent_id, flow_id, project=proj)
        return {"agent": agent_id, "project": proj, "flow": flow_id,
                "displayName": data.get("displayName"),
                "routes": summarize(data.get("transitionRoutes") or [],
                                    _playbook_names(cons, agent_id, proj)),
                "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_update_flow(flow: str = "", body: dict | None = None, update_mask: str = "",
                   agent: str = "", project: str = "") -> dict:
    """PATCH of a flow (v3beta1). Requires write permission."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        # The permission is checked BEFORE touching the resource: a read-only
        # agent says "SOLO LECTURA", not "flow not found".
        assert_writable(proj, agent_id, CFG)
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        data = cons.patch_flow(agent_id, flow_id, body or {},
                                update_mask=update_mask or None, project=proj)
        return {"agent": agent_id, "project": proj, "flow": flow_id,
                "updateMask": update_mask or "(derived from the body)", "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_list_pages(flow: str = "", agent: str = "", project: str = "") -> dict:
    """List the pages of a flow."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        pages = cons.list_pages(agent_id, flow_id, proj)
        return {"agent": agent_id, "project": proj, "flow": flow_id,
                "total": len(pages),
                "pages": [{"id": p["name"].split("/")[-1],
                           "displayName": p.get("displayName"),
                           "routes": len(p.get("transitionRoutes") or [])} for p in pages]}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_page(flow: str = "", page: str = "", agent: str = "",
                project: str = "") -> dict:
    """GET of a page (v3beta1), with its routes and targets."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        page_id = _resolve_page(cons, agent_id, flow_id, page, proj)
        data = cons.get_page(agent_id, flow_id, page_id, project=proj)
        return {"agent": agent_id, "project": proj, "flow": flow_id, "page": page_id,
                "displayName": data.get("displayName"),
                "routes": summarize(data.get("transitionRoutes") or [],
                                    _playbook_names(cons, agent_id, proj)),
                "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_update_page(flow: str = "", page: str = "", body: dict | None = None,
                   update_mask: str = "", agent: str = "", project: str = "") -> dict:
    """PATCH of a page (v3beta1). Requires write permission."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        # The permission is checked BEFORE touching the resource: a read-only
        # agent says "SOLO LECTURA", not "flow not found".
        assert_writable(proj, agent_id, CFG)
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        page_id = _resolve_page(cons, agent_id, flow_id, page, proj)
        data = cons.patch_page(agent_id, flow_id, page_id, body or {},
                                update_mask=update_mask or None, project=proj)
        return {"agent": agent_id, "project": proj, "flow": flow_id, "page": page_id,
                "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_list_playbooks(agent: str = "", project: str = "") -> dict:
    """List an agent's playbooks."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        pbs = cons.list_playbooks(agent_id, proj)
        return {"agent": agent_id, "project": proj, "total": len(pbs),
                "playbooks": [{"id": p["name"].split("/")[-1],
                               "displayName": p.get("displayName"),
                               "goal": (p.get("goal") or "")[:120],
                               "model": (p.get("llmModelSettings") or {}).get("model"),
                               "tokens": p.get("tokenCount")} for p in pbs]}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_playbook(playbook: str = "", agent: str = "", project: str = "") -> dict:
    """GET of a playbook (v3beta1)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        pb_id, pb_name = _resolve_playbook(cons, agent_id, playbook, proj)
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}/playbooks/{pb_id}")
        data = cons.request("GET", url)
        return {"agent": agent_id, "project": proj, "playbook": pb_id,
                "displayName": pb_name, "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_update_playbook(playbook: str = "", body: dict | None = None,
                       update_mask: str = "", agent: str = "",
                       project: str = "") -> dict:
    """PATCH of a playbook (v3beta1). Requires write permission.

    NOTE: CX revalidates the entire playbook -> without `goal` it responds
    "Playbook goal cannot be empty". Sends the full body."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        # A playbook is only written on an CXP_AGENT_READWRITE agent
        # (our copy). Reference agents are read-only.
        assert_playbook_writable(proj, agent_id, CFG)
        pb_id, pb_name = _resolve_playbook(cons, agent_id, playbook, proj)
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}/playbooks/{pb_id}",
                        alt="json")
        data = cons.request("PATCH", url, body=body or {},
                            query={"updateMask": update_mask} if update_mask else None)
        return {"agent": agent_id, "project": proj, "playbook": pb_id,
                "displayName": pb_name, "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_list_children(agent: str = "", collection: str = "intents",
                     project: str = "") -> dict:
    """List a child collection of the agent.

    `collection`: intents | entityTypes | tools | webhooks | transitionRouteGroups |
    generators | changelogs | conversations"""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        fn = {
            "intents": cons.list_intents,
            "entitytypes": cons.list_entity_types,
            "tools": cons.list_tools,
            "webhooks": cons.list_webhooks,
            "transitionroutegroups": cons.list_transition_route_groups,
            "generators": cons.list_generators,
            "changelogs": cons.list_changelogs,
            "conversations": cons.list_conversations,
        }.get((collection or "").replace("_", "").lower())
        if fn is None:
            return {"error": f"unknown collection: {collection!r}",
                    "collections": ["intents", "entityTypes", "tools", "webhooks",
                                    "transitionRouteGroups", "generators",
                                    "changelogs", "conversations"]}
        items = fn(agent_id, proj)
        return {"agent": agent_id, "project": proj, "type": collection,
                "total": len(items),
                "items": [{"id": i.get("name", "").split("/")[-1],
                           "displayName": i.get("displayName")} for i in items]}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_conversation(conversation: str = "", agent: str = "",
                        project: str = "") -> dict:
    """GET of a conversation (v3beta1): the agent's actual trace."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        data = cons.get_conversation(agent_id, conversation, project=proj)
        return {"agent": agent_id, "project": proj,
                "conversation": conversation, "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_detect_intent(text: str = "", session_id: str = "", agent: str = "",
                     project: str = "", params: dict | None = None,
                     language_code: str = "es") -> dict:
    """sessions.detectIntent (v3beta1) against an agent.

    `text`: what the user says. `session_id`: session id (if absent, it is
    generated). `params`: session parameters (sent in the first turn).
    `language_code`: turn language (default, `es`).

    NOTE (measured 2026-11-13): the backend **requires `queryInput.languageCode`**
    ("Field 'query_input.language_code' is missing" -> HTTP 400). The language does NOT
    go inside `queryInput.text.languageCode` (that form is silently dropped
    and the server responds 400).

    NOTE (lesson 2026-11-05): against some agents the turn **expires at
    ~241 s** (the deadline is the server's, HTTP 408) when the backend that
    consumes the tools is cold. Here the budget is 300 s: raising it further
    does not fix a 408. If it fails, **retry IN SERIES and with a pause (~90 s)**,
    never in parallel."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        assert_readable(proj, agent_id, CFG)
        sid = session_id or f"mcp-{abs(hash(text)) % (10 ** 12)}"
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}/sessions/{sid}"
                        f":detectIntent", alt="json")
        body: dict[str, Any] = {
            "queryInput": {"text": {"text": text}, "languageCode": language_code},
        }
        if params:
            body["queryParams"] = {"parameters": params}
        data = cons.request("POST", url, body=body, timeout=300.0)
        return {"agent": agent_id, "project": proj, "session": sid, "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_train_flow(flow: str = "", agent: str = "", project: str = "") -> dict:
    """Train a flow (flows:{flow}:train)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        assert_writable(proj, agent_id, CFG)
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        name = _resource_name(agent_id, proj, "flows", flow_id)
        url = cons._url(f"{name}:train", alt="json")
        data = cons.request("POST", url, body={}, timeout=300.0)
        return {"agent": agent_id, "project": proj, "flow": flow_id, "data": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_validate_flow(flow: str = "", agent: str = "", project: str = "") -> dict:
    """Validates a flow (flows:{flow}:validate)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or _project_of_agent(agent_id) or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        name = _resource_name(agent_id, proj, "flows", flow_id)
        url = cons._url(f"{name}:validate", alt="json")
        data = cons.request("POST", url, body={}, timeout=300.0)
        return {"agent": agent_id, "project": proj, "flow": flow_id, "data": data}
    except Exception as exc:
        return _err(exc)


# ------------------------------------------------- THE AGGREGATE: target playbook
@mcp.tool()
def cx_set_route_playbook(
    agent: str = "",
    flow: str = "",
    page: str = "",
    route: str = "",
    playbook: str = "",
    dry_run: bool = True,
    project: str = "",
) -> dict:
    """Sets a PLAYBOOK as the target of ONE transition route OF PAGE.

    It is what the API publishes CANNOT do: there `TransitionRoute.target` only
    accepts page or flow, and a `targetPlaybook` is dropped silently. This
    MCP writes against the console's internal backend (same proto the
    UI uses), in ~1 s. **Measured: the runtime executes the playbook.**

    - agent:   agent id (optional: it is inferred when the allowlist leaves
               a single candidate; with several, the tool lists them).
    - flow:    flow id or displayName (default, the agent's first one).
    - page:    page id or displayName (default, the first one).
    - route:   route uuid, its EXACT condition, or its index (0-based).
               **If omitted, the tool only READS** and returns the current
               state of the page.
    - playbook: displayName or id of the target playbook (default, does NOT change).
    - dry_run: True (default) = does not write; returns the plan.

    NOTE: changing the target **erases** `targetPage`/`targetFlow` from that route
    (they are a `oneof`): if the route pointed to a page or a flow, that is
    lost. The `dry_run` returns the `warning` with the target that will be lost."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "the agent is missing",
                "hint": "pass agent=<id> (the allowed agents are in cx_status)"}
    cons = _api()
    proj = project or _project_of_agent(agent_id) or CFG.project
    try:
        # A write needs write permission, and it is checked BEFORE touching the
        # resource: a read-only agent says "SOLO LECTURA", not "flow not found".
        # A `dry_run` only reads, so it works on a read-only agent.
        if not dry_run:
            assert_writable(proj, agent_id, CFG)
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        if not flow_id:
            flows = cons.list_flows(agent_id, proj)
            if not flows:
                return {"error": "the agent has no flows", "agent": agent_id}
            flow_id = flows[0]["name"].split("/")[-1]
        page_id = _resolve_page(cons, agent_id, flow_id, page, proj)
        if not page_id:
            pages = cons.list_pages(agent_id, flow_id, proj)
            if not pages:
                return {"error": "the flow has no pages", "agent": agent_id,
                        "flow": flow_id}
            page_id = pages[0]["name"].split("/")[-1]
        pg = cons.get_page(agent_id, flow_id, page_id, project=proj)
        routes = pg.get("transitionRoutes") or []
        pbs_inv = _playbook_names(cons, agent_id, proj)
        state = {
            "agent": agent_id, "project": proj, "flow": flow_id, "page": page_id,
            "level": "page",
            "displayName": pg.get("displayName"),
            "routes": summarize(routes, pbs_inv),
        }
    except Exception as exc:
        return _err(exc)

    # Read-only: the current state is returned.
    if not route:
        return {"mode": "read", **state,
                "hint": "pass route=... and playbook=... to change the destination"}

    try:
        index, current = find_route(routes, route)
    except LookupError as exc:
        return {"error": str(exc), **state}

    if not playbook:
        return {"mode": "read",
                "route": {"index": index, "name": current.get("name"),
                          "condition": current.get("condition"),
                          "current_destination": current.get("targetPlaybook")},
                **state, "hint": "pass playbook=... to set the destination"}

    try:
        pb_id, pb_name = _resolve_playbook(cons, agent_id, playbook, proj)
    except LookupError as exc:
        return {"error": str(exc), **state}

    current_destination = current.get("targetPlaybook")
    new_destination = _playbook_resource(agent_id, pb_id, proj)
    pages_map, flows_map = _destination_maps(cons, agent_id, flow_id, proj)
    current_info = _destination_current(current, pbs_inv, pages_map, flows_map)
    warning = None
    if current_info["field"] in ("targetPage", "targetFlow"):
        warning = (f"the route pointed to {current_info['type']} "
                   f"'{current_info['name']}': when the playbook is set that "
                   f"destination is DELETED (targetPage/targetFlow are a 'oneof' "
                   f"with targetPlaybook)")

    if dry_run:
        return {"mode": "dry-run",
                "route": {"index": index, "name": current.get("name"),
                          "condition": current.get("condition"),
                          "current_destination": current_destination,
                          "current_destination_type": current_info["type"],
                          "current_destination_name": current_info["name"],
                          "new_destination": new_destination,
                          "new_destination_name": pb_name},
                "warning": warning,
                **state}

    try:
        assert_writable(proj, agent_id, CFG)
    except ReadOnlyAgentError as exc:
        return {"error": str(exc), "agent": agent_id}

    try:
        res = cons.set_route_playbook(agent_id, flow_id, page_id, route,
                                      new_destination, project=proj)
    except Exception as exc:
        return _err(exc)

    afterwards = cons.get_page(agent_id, flow_id, page_id, project=proj)
    res["routes"] = summarize(afterwards.get("transitionRoutes") or [], pbs_inv)
    res["displayName"] = afterwards.get("displayName")
    res["mode"] = "written"
    return res


@mcp.tool()
def cx_set_flow_route_playbook(
    agent: str = "",
    flow: str = "",
    route: str = "",
    playbook: str = "",
    dry_run: bool = True,
    project: str = "",
) -> dict:
    """Sets a PLAYBOOK as the target of a transition route of the ROUTER OF A FLOW.

    **The aggregate**: the public API of Dialogflow CX cannot express this.
    In the public proto `TransitionRoute.target` is a `oneof` with
    `target_page` and `target_flow`: `target_playbook` does NOT exist and a PATCH that
    sends it is silently dropped (the route is left without a target and the agent falls
    into NO_MATCH). The console's internal backend DOES keep it: the MCP
    writes there, in ~1 s, and verifies by re-reading.
    **Measured: the runtime EXECUTES the playbook when the target is here**
    (test with a custom-brand playbook: `matchType=PLAYBOOK`,
    `currentPlaybooks=[the new playbook]` and the response was its text).

    - agent:   agent id (optional: it is inferred when the allowlist leaves
               a single candidate; with several, the tool lists them).
    - flow:    flow id or displayName (default, the agent's first one).
    - route:   route uuid, its EXACT condition, or its index (0-based).
               **If omitted, the tool only READS** and returns the flow's
               router with each route's target.
    - playbook: displayName or target playbook id (default, does NOT change).
    - dry_run: True (default) = does not write; returns the plan.

    NOTE: changing the target **erases** `targetPage`/`targetFlow` from that route
    (they are a `oneof`): if the route pointed to a page or a flow, that is
    lost. The MCP warns before writing (dry_run)."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "the agent is missing",
                "hint": "pass agent=<id> (the allowed agents are in cx_status)"}
    cons = _api()
    proj = project or _project_of_agent(agent_id) or CFG.project
    try:
        # A write needs write permission, and it is checked BEFORE touching the
        # resource: a read-only agent says "SOLO LECTURA", not "flow not found".
        # A `dry_run` only reads, so it works on a read-only agent.
        if not dry_run:
            assert_writable(proj, agent_id, CFG)
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        if not flow_id:
            flows = cons.list_flows(agent_id, proj)
            if not flows:
                return {"error": "the agent has no flows", "agent": agent_id}
            flow_id = flows[0]["name"].split("/")[-1]
        fl = cons.get_flow(agent_id, flow_id, project=proj)
        routes = fl.get("transitionRoutes") or []
        pbs_inv = _playbook_names(cons, agent_id, proj)
        state = {
            "agent": agent_id, "project": proj, "flow": flow_id,
            "level": "flow (flow router)",
            "displayName": fl.get("displayName"),
            "routes": summarize(routes, pbs_inv),
        }
    except Exception as exc:
        return _err(exc)

    if not route:
        return {"mode": "read", **state,
                "hint": "pass route=... and playbook=... to change the destination"}

    try:
        index, current = find_route(routes, route)
    except LookupError as exc:
        return {"error": str(exc), **state}

    if not playbook:
        return {"mode": "read",
                "route": {"index": index, "name": current.get("name"),
                          "condition": current.get("condition"),
                          "current_destination": current.get("targetPlaybook")},
                **state, "hint": "pass playbook=... to set the destination"}

    try:
        pb_id, pb_name = _resolve_playbook(cons, agent_id, playbook, proj)
    except LookupError as exc:
        return {"error": str(exc), **state}

    current_destination = current.get("targetPlaybook")
    new_destination = _playbook_resource(agent_id, pb_id, proj)
    pages_map, flows_map = _destination_maps(cons, agent_id, flow_id, proj)
    current_info = _destination_current(current, pbs_inv, pages_map, flows_map)
    warning = None
    if current_info["field"] in ("targetPage", "targetFlow"):
        warning = (f"the route pointed to {current_info['type']} "
                   f"'{current_info['name']}': when the playbook is set that "
                   f"destination is DELETED (targetPage/targetFlow are a 'oneof' "
                   f"with targetPlaybook)")

    if dry_run:
        return {"mode": "dry-run",
                "route": {"index": index, "name": current.get("name"),
                          "condition": current.get("condition"),
                          "current_destination": current_destination,
                          "current_destination_type": current_info["type"],
                          "current_destination_name": current_info["name"],
                          "new_destination": new_destination,
                          "new_destination_name": pb_name},
                "warning": warning,
                **state}

    try:
        assert_writable(proj, agent_id, CFG)
    except ReadOnlyAgentError as exc:
        return {"error": str(exc), "agent": agent_id}

    try:
        res = cons.set_flow_route_playbook(agent_id, flow_id, route,
                                            new_destination, project=proj)
    except Exception as exc:
        return _err(exc)

    afterwards = cons.get_flow(agent_id, flow_id, project=proj)
    res["routes"] = summarize(afterwards.get("transitionRoutes") or [], pbs_inv)
    res["displayName"] = afterwards.get("displayName")
    res["mode"] = "written"
    return res


# ===========================================================================
# THE GAPS OF THE PUBLIC API
# ===========================================================================
# The public API and the console's internal backend serve the SAME
# discovery doc, but not the SAME proto: the console backend has fields
# that the public API does not even DECLARE (and therefore cannot read or
# write). Those are gaps, and they are the reason this MCP exists:
# `cx_gaps` DETECTS them live (compares the real JSON with the public schema),
# `cx_set_console_field` WRITES them, and the `cx_set_*_playbook` tools
# are the most used case.

_RESOURCE_TYPES = ("flow", "page", "playbook", "tool", "intent", "entityType",
                   "webhook", "generator", "transitionRouteGroup")


def _resolve_resource_name(resource: str, agent_id: str, proj: str) -> str:
    """Converts a resource selector into its full `name`.

    Accepts:
      * the full `name` returned by the API
      * `flow/<id>`, `page/<flowId>/<pageId>`, `playbook/<id>`, `tool/<id>`,
        `intent/<id>`, `entityType/<id>`, `transitionRouteGroup/<id>`, ...
      * `agent` (the agent itself)"""
    resource = (resource or "").strip()
    if not resource:
        return ""
    if resource == "agent":
        return f"projects/{proj}/locations/{CFG.location}/agents/{agent_id}"
    if "/" not in resource:
        return resource  # it is already a full name
    type_, _, rest = resource.partition("/")
    if type_ not in _RESOURCE_TYPES:
        return resource
    base = f"projects/{proj}/locations/{CFG.location}/agents/{agent_id}"
    if type_ == "page":
        flow_id, _, page_id = rest.partition("/")
        return f"{base}/flows/{flow_id}/pages/{page_id}"
    return f"{base}/{type_}/{rest}"


@mcp.tool()
def cx_gaps(agent: str = "", scope: str = "", project: str = "",
            include_pages: bool = True,
            include_catalogue: bool = True,
            include_methods: bool = True,
            include_lab: bool = False,
            max_parents: int = 5) -> dict:
    """The GAPS of the public API v3beta1, detected LIVE.

    Walks the agent and compares each resource the backend returns with the
    **public schema** of v3beta1 (the official discovery doc). Any field the
    backend exposes and the public API **does not declare** is a gap: through
    the public API it can neither be read nor written.

    It is not a hand-written list: it is computed from real data, so
    if the console adds a new field, this MCP sees it without touching the code.

    The walk is **generic**: it lists ALL the collections the discovery doc
    declares (24 in v3beta1), including nested ones (`flows.pages`,
    `tools.versions`, `playbooks.examples`, `environments.deployments`,
    `testCases.results`...). For nested ones the ancestor must be resolved
    (`flows.pages` needs a flow): up to `max_parents` candidates are used,
    so a flow without pages does not mask one that has them.

    - agent:   agent id (optional: it is inferred when the allowlist leaves
               a single candidate; with several, the tool lists them).
    - scope:   filters the gaps by schema or by field name
               (for example `"Tool"` or `"targetPlaybook"`). Empty = all.
    - project: project (default, `CXP_PROJECT`).
    - include_pages: if False, does not descend into each flow's pages (faster).
    - include_catalogue: adds the MEASURED catalogue (which gaps can be
               written and which are read-only). See `cx_limites_api`.
    - include_methods: adds the catalogue of **METHOD** gaps (endpoints
               the console uses and that are not in any discovery doc).
    - include_lab: **creates a temporary lab agent** and sweeps with it the
               `environments*` family (`environments`, `.deployments`,
               `.experiments`, `.continuousTestResults`,
               `.sessions.entityTypes`), which on a normal agent cannot be
               reached (an environment must be created, and the environment requires the
               version of all the flows, playbooks and tools in the graph). The
               lab agent is DELETED at the end. With this the
               coverage reaches **24/24** collections of the discovery doc.
    - max_parents: how many ancestors are tried as parent of a nested
               collection (more = more coverage, more calls).

    Returns `gaps` (what is detected now), `catalogue` (what is measured),
    `scanned_resources`, `unreachable` (the collections that agent
    does not have: it is not a failure of the MCP, the resource simply does not exist) and
    `coverage`."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "the agent is missing",
                "hint": "pass agent=<id> (the allowed agents are in cx_status)"}
    cons = _api()
    proj = project or _project_of_agent(agent_id) or CFG.project
    try:
        idx = load_schema_index(CFG.api_version)
    except Exception as exc:
        return _err(exc)
    try:
        scan = scan_agent(cons, agent_id, proj, idx=idx,
                          include_pages=include_pages,
                          max_parents=max_parents)
    except PermissionDeniedError as exc:
        return {"error": str(exc), "agent": agent_id}
    except Exception as exc:
        return _err(exc)

    gaps = scan.get("gaps") or []
    if scope:
        s = scope.lower()
        gaps = [h for h in gaps
                if s in (h.get("schema") or "").lower() or s in h["field"].lower()]

    with_data = scan.get("collections_scanned") or []
    reachable = scan.get("reachable_collections") or []
    total_col = scan.get("collections") or 0
    unreachable = scan.get("unreachable") or []
    output: dict[str, Any] = {
        "agent": agent_id,
        "project": proj,
        "schema_version": CFG.api_version,
        "scanned_resources": scan.get("scanned_resources"),
        "total_gaps": len(gaps),
        "gaps": gaps,
        "coverage": {
            "descriptor_collections": total_col,
            "reachable_collections": len(reachable),
            "collections_with_data": len(with_data),
            "unreachable_collections": len(unreachable),
            "reachable_detail": reachable,
            "with_data_detail": with_data,
        },
        "unreachable": unreachable,
        "note": ("A gap is a field that the console backend exposes and the "
                 "public API does NOT declare: through the public API it can "
                 "neither be read nor written. The ones that CAN be WRITTEN are "
                 "marked as 'writable' in the catalogue."),
    }
    if include_catalogue:
        cat = MEASURED_CATALOGUE
        if scope:
            s = scope.lower()
            cat = [c for c in cat if s in c["field"].lower()
                   or any(s in r.lower() for r in c["resources"])]
        output["catalogue"] = cat
    if include_methods:
        from cxp.console_endpoints import METHODS_BY_VERSION, method_catalogue
        output["method_gaps"] = method_catalogue()
        output["methods_per_version"] = METHODS_BY_VERSION

    if include_lab:
        # The `environments*` family is only reachable with an environment, and an
        # environment cannot be created in the work agent (its graph uses a
        # system tool that cannot be versioned). It is swept in a temporary
        # lab agent, which is deleted when finished.
        from cxp.lab import sweep_environments
        lab = sweep_environments(cons, CFG, idx, proj)
        lab_gaps: list[dict[str, Any]] = []
        lab_reachable: list[str] = []
        for key, measurement in (lab.get("measurements") or {}).items():
            if measurement.get("status") != "ok":
                continue
            lab_reachable.append(key)
            for h in ((measurement.get("wrapper") or [])
                      + (measurement.get("items") or [])):
                row = dict(h)
                row["source"] = "lab"
                lab_gaps.append(row)
        if scope:
            s = scope.lower()
            lab_gaps = [h for h in lab_gaps
                        if s in (h.get("schema") or "").lower()
                        or s in h["field"].lower()]
        gaps.extend(lab_gaps)
        reachable.extend(k for k in lab_reachable if k not in reachable)
        reachable = sorted(set(reachable))
        unreachable = [x for x in unreachable
                       if x.get("collection") not in lab_reachable]
        output["total_gaps"] = len(gaps)
        output["gaps"] = gaps
        output["coverage"].update({
            "reachable_collections": len(reachable),
            "unreachable_collections": len(unreachable),
            "reachable_detail": reachable,
        })
        output["unreachable"] = unreachable
        output["lab"] = {
            "temporary_agent": lab.get("agent"),
            "measurements": lab.get("measurements"),
            "errors": lab.get("errors"),
            "note": ("The lab agent is created and DELETED within this call: "
                     "nothing is left behind."),
        }
    return output


@mcp.tool()
def cx_proto_interno(scope: str = "") -> dict:
    """The gaps of FIELD in the console's **internal proto** (not declared
    by the public API v3beta1).

    `cx_gaps` computes the gaps over the agent's real data
    (and therefore only sees what that agent has). This tool gives the list
    **systematically**: it is extracted from the internal proto carried by the console
    bundle and cross-checked against the public discovery doc, so it also sees
    what the agent does NOT have.

    Each field's state says whether the backend **accepts** it:
    "writable" (measured: write and read back), "ignored" (the backend does NOT
    know it: it ignores it silently), "dropped" (it knows it but empties it),
    "derived" (the server computes it), "input_only" (it is accepted in the
    request but cannot be read), "not_accepted" (the backend knows it but rejects
    the value) or "not_tested" (the internal proto has it, its write has not been
    tested).

    - scope: filters by schema or by field name ("FormParameter",
              "ssml"...). Empty = all.
    """
    from cxp.proto_interno import internal_proto_catalogue, summary
    cat = internal_proto_catalogue()
    if scope:
        s = scope.lower()
        cat = [c for c in cat
               if s in c["schema"].lower()
               or any(s in field.lower() for field in c["fields"])]
    output = {"total_schemas": len(cat), "gaps": cat}
    if not scope:
        output["summary"] = summary()
    output["note"] = (
        "The list comes from the internal proto carried by the console bundle "
        "(1536 classes) cross-checked against the public v3beta1 discovery doc "
        "(709 schemas). The states are ALL MEASURED by writing and re-reading "
        "in a temporary lab agent: the backend fails with the TYPE when the "
        "field exists ('Invalid value at ... (TYPE_BOOL)') and silently ignores "
        "the names it does not know.")
    return output


@mcp.tool()
def cx_endpoints_consola(endpoint: str = "", agent: str = "",
                          project: str = "", version: str = "",
                          query: dict | None = None) -> dict:
    """The endpoints used by the CONSOLE and that are **not in any descriptor**.

    The public discovery doc and the internal backend one are **the same
    document** (measured: 669 652 bytes each, same canonical md5
    `29adf74f...`; the only thing that changes is the order of the JSON keys). That is
    to say: no method is "missing" from the descriptor. What is missing are the
    endpoints that the UI uses and that are not part of the public API: the
    system entity types, who else is using the agent, which models are
    available, which projects you have opened, the conversation profiles of the
    old model...

    Without arguments: the MEASURED catalogue of those endpoints (with their real
    response). With `endpoint=<id>`: **it is called** and the response is returned.

    - endpoint: catalogue id ("systemEntityTypes.list", "agents.currentUsers",
               "users.recentProjects", "agents.listAvailableModels",
               "agents.getSupportedLanguages", "agent.visibility",
               "conversationProfiles.list", "locationSettings.get").
    - agent:   agent (for those that need it). It is inferred when the
               allowlist leaves a single candidate.
    - project: project (by default, `CXP_PROJECT`).
    - version: if given, forces the version (otherwise, the catalogue one).
    - query:   extra query parameters.

    Examples:
      cx_endpoints_consola()                                     -> the catalogue
      cx_endpoints_consola(endpoint="systemEntityTypes.list")    -> the call
      cx_endpoints_consola(endpoint="agents.listAvailableModels") -> models
    """
    from cxp.console_endpoints import METHOD_GAPS
    catalogue = [dict(h) for h in METHOD_GAPS]
    if not endpoint:
        return {"total": len(catalogue), "endpoints": catalogue,
                "note": ("The endpoints are reached with `cx_api` by setting the "
                         "version and the route; this tool calls them "
                         "directly.")}
    if not CFG.credentials_ok():
        return _missing_credentials()

    chosen = next((h for h in catalogue if h["id"] == endpoint), None)
    if chosen is None:
        return {"error": f"unknown endpoint: {endpoint!r}",
                "available": [h["id"] for h in catalogue]}

    # The agent is only resolved when the endpoint needs one: the project list
    # or the recent projects do not hang off an agent, and they must not fail
    # because the allowlist has several agents.
    agent_id = _resolve_agent(agent) if "{agent}" in (chosen.get("path") or "") else (agent or "")
    proj = project or _project_of_agent(agent_id) or CFG.project
    cons = _api()
    ver = version or chosen.get("version") or "v3alpha1"
    route = (chosen.get("path") or "")
    route = (route.replace("{project}", proj)
             .replace("{location}", CFG.location)
             .replace("{agent}", agent_id))
    # Routes that do not hang off `projects/` are absolute from the root.
    if not route.startswith("projects/"):
        route = "/" + route
    url = (f"https://dialogflow.clients6.google.com/{ver}/{route.lstrip('/')}"
           f"?key={CFG.api_key}")
    try:
        data = cons.request("GET", url, query=query)
    except Exception as exc:
        return {"endpoint": endpoint, "version": ver, "url": url,
                "status": chosen.get("status"), "error": str(exc)[:400]}
    return {"endpoint": endpoint, "version": ver, "url": url,
            "status": "ok", "data": data}


@mcp.tool()
def cx_set_console_field(resource: str = "", field: str = "", value: Any = None,
                         dry_run: bool = True, project: str = "",
                         agent: str = "") -> dict:
    """Write ONE field of a resource, even those that the API publishes does not declare.

    It is the **generic** version of "set a playbook as the target": instead of
    one method per operation, there is ONE:

        GET  resource -> the value is applied on the route -> PATCH -> GET and compares.

    The final comparison is the guarantee: the server can drop a field
    silently, and the response says what was saved and what was lost.

    - resource: the resource. Accepts the full `name` returned by the API
                (`projects/.../agents/.../flows/.../pages/...`) or a short
                form: `flow/<id>`, `page/<flowId>/<pageId>`,
                `playbook/<id>`, `tool/<id>`, `intent/<id>`,
                `entityType/<id>`, `transitionRouteGroup/<id>`, `agent`.
    - field:   field route. Accepts indices: `transitionRoutes[0].targetPlaybook`,
                `form.parameters[0].description`, `entities[0].dtmfPattern`,
                `webClientSettings.boxBorderRadius`.
                For whole arrays: `transitionRoutes` with the entire array
                as the value.
    - value:   the value (JSON). To delete a field, `null`.
    - dry_run: True (default) = does not write; returns the plan and the diff.
    - agent:   agent (only needed for the short forms); it is inferred when
                the allowlist leaves a single candidate.

    NOTE: to set the target to a playbook, `targetPlaybook` is a `oneof`
    with `targetPage`/`targetFlow`: those two must be removed. `cx_set_route_playbook` does it
    for you."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "the agent is missing",
                "hint": "pass agent=<id> (the allowed agents are in cx_status)"}
    if not resource or not field:
        return {"error": "resource and/or field are missing",
                "hint": "resource=... and field=... (hint: cx_gaps)"}
    proj = project or _project_of_agent(agent_id) or CFG.project
    name = _resolve_resource_name(resource, agent_id, proj)
    if not name:
        return {"error": "the resource could not be resolved", "resource": resource}

    try:
        # A write needs write permission, and it is checked BEFORE touching the
        # resource. A `dry_run` only reads, so it works on a read-only agent.
        if not dry_run:
            assert_writable(proj, agent_id, CFG)
        res = set_field(_api(), name, field, value, dry_run=dry_run,
                        project=proj)
    except PermissionDeniedError as exc:
        return {"error": str(exc), "resource": name}
    except Exception as exc:
        return _err(exc)

    output = res.as_dict()
    output["mode"] = "dry-run" if dry_run else "written"
    if dry_run:
        output["hint"] = ("this has NOT written anything; set dry_run=False to "
                           "apply it")
    return output


@mcp.tool()
def cx_set_group_route_playbook(agent: str = "", group: str = "", route: str = "",
                                playbook: str = "", dry_run: bool = True,
                                project: str = "") -> dict:
    """Sets a PLAYBOOK as the target of a transition route of a GROUP OF ROUTES.

    The third member of the "aggregate" family: a `transitionRouteGroup`
    is its own resource (`.../agents/{A}/transitionRouteGroups/{G}`) and its
    routes are `TransitionRoute` like those of a page or a flow. The
    public API declares that same `TransitionRoute` **without** `targetPlaybook`:
    it cannot be expressed via public API.

    - agent:   agent id (optional: it is inferred when the allowlist leaves
               a single candidate; with several, the tool lists them).
    - group:   id or displayName of the route group.
    - route:   route uuid, its EXACT condition, or its index (0-based).
               **If omitted, the tool only READS** and returns the routes
               of the group with their current target.
    - playbook: displayName or id of the target playbook (default, does NOT change).
    - dry_run: True (default) = does not write; returns the plan.

    NOTE: route groups are SHARED between pages and flows: touching one
    changes the behavior of all pages that use it."""
    if not CFG.credentials_ok():
        return _missing_credentials()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "the agent is missing",
                "hint": "pass agent=<id> (the allowed agents are in cx_status)"}
    cons = _api()
    proj = project or _project_of_agent(agent_id) or CFG.project
    try:
        # A write needs write permission, and it is checked BEFORE touching the
        # resource: a read-only agent says "SOLO LECTURA", not "group not found".
        # A `dry_run` only reads, so it works on a read-only agent.
        if not dry_run:
            assert_writable(proj, agent_id, CFG)
        groups = cons.list_transition_route_groups(agent_id, proj)
    except Exception as exc:
        return _err(exc)
    if not groups:
        return {"error": "the agent has no route groups", "agent": agent_id}

    chosen = None
    if group:
        for g in groups:
            if group in ((g.get("displayName") or ""), (g.get("name") or "").rsplit("/", 1)[-1],
                         g.get("name") or ""):
                chosen = g
                break
        if chosen is None:
            return {"error": f"route group not found: {group!r}",
                    "groups": [g.get("displayName") for g in groups]}
    else:
        chosen = groups[0]

    group_name = chosen.get("name") or ""
    group_id = group_name.rsplit("/", 1)[-1]
    try:
        group = cons.request("GET", cons._url("/" + group_name))
    except Exception as exc:
        return _err(exc)

    routes = group.get("transitionRoutes") or []
    pbs_inv = _playbook_names(cons, agent_id, proj)
    state = {
        "agent": agent_id, "project": proj,
        "group": group_id, "displayName": chosen.get("displayName"),
        "level": "route group",
        "routes": summarize(routes, pbs_inv),
    }

    if not route:
        return {"mode": "read", **state,
                "hint": "pass route=... and playbook=... to change the destination"}

    try:
        index, current = find_route(routes, route)
    except LookupError as exc:
        return {"error": str(exc), **state}

    if not playbook:
        return {"mode": "read",
                "route": {"index": index, "name": current.get("name"),
                          "condition": current.get("condition"),
                          "current_destination": current.get("targetPlaybook")},
                **state, "hint": "pass playbook=... to set the destination"}

    try:
        pb_id, pb_name = _resolve_playbook(cons, agent_id, playbook, proj)
    except LookupError as exc:
        return {"error": str(exc), **state}

    new_destination = _playbook_resource(agent_id, pb_id, proj)
    warning = None
    if current.get("targetPage") or current.get("targetFlow"):
        warning = ("the route pointed to a page or to a flow: when the "
                   "playbook is set that destination is DELETED (targetPage/"
                   "targetFlow are a 'oneof' with targetPlaybook)")

    if dry_run:
        return {"mode": "dry-run",
                "route": {"index": index, "name": current.get("name"),
                          "condition": current.get("condition"),
                          "field": f"transitionRoutes[{index}].targetPlaybook",
                          "current_destination": current.get("targetPlaybook"),
                          "new_destination": new_destination,
                          "new_destination_name": pb_name},
                "warning": warning, **state}

    try:
        assert_writable(proj, agent_id, CFG)
    except ReadOnlyAgentError as exc:
        return {"error": str(exc), "agent": agent_id}

    # The `oneof` (targetPage/targetFlow) is removed from the route element.
    try:
        current_group = cons.request("GET", cons._url("/" + group_name))
        current_routes = current_group.get("transitionRoutes") or []
        copy_routes = copy.deepcopy(current_routes)
        copy_routes[index].pop("targetPage", None)
        copy_routes[index].pop("targetFlow", None)
        body = {k: v for k, v in current_group.items()
                if k not in ("etag", "createTime", "updateTime")}
        body["transitionRoutes"] = copy_routes
        cons.patch_resource(group_name, body, update_mask="transitionRoutes",
                            project=proj)
    except Exception as exc:
        return _err(exc)

    afterwards = cons.request("GET", cons._url("/" + group_name))
    routes_afterwards = afterwards.get("transitionRoutes") or []
    verified = any(
        r.get("name") == current.get("name")
        and r.get("targetPlaybook") == new_destination
        for r in routes_afterwards
    )
    return {
        "mode": "written",
        "agent": agent_id, "project": proj,
        "group": group_id, "displayName": chosen.get("displayName"),
        "level": "route group",
        "route": {"index": index, "name": current.get("name"),
                  "condition": current.get("condition"),
                  "destination": new_destination, "destination_name": pb_name},
        "total_routes": len(routes_afterwards),
        "routes": summarize(routes_afterwards, pbs_inv),
        "verified": verified,
        "warning": warning,
    }


if __name__ == "__main__":
    mcp.run()
