"""Per-agent write/read guards.

Why a separate module: the `"original"` alias can be bypassed by passing the agent's
raw UUID. The real guard compares the **resolved id**, and is applied
at the lowest point of the write route (not only in the MCP layer).

Permission model (allowlist, via environment variables):

    CXP_AGENT_READWRITE=<uuid>[,<uuid>]   read + write (ONE OR MORE agents)
    CXP_AGENT_READ=<uuid>[,<uuid>]        read-only (ONE OR MORE agents)
    CXP_PROJECTS=<project>[,...]          (optional) restricts projects

**Both lists accept as many agents as needed** (comma-separated). At least one
of the two must contain an agent: the allowlist is BOTH what the MCP works on
and the **security boundary of the harness** that consumes it (the session
cookies can reach everything the Google account can reach; the allowlist is
what keeps the agent inside the agents you chose).

An agent **without** an entry in either list is NOT accessible: not
even for reading. Entries accept two forms:

* `<uuid>` -> the permission travels with the uuid (valid in any project);
* `<project>/<uuid>` -> matches project and agent.

The project is derived from the **request path**, not from `CXP_PROJECT`: thus the
same MCP can read the PROD agent (which lives in another project) with the same
uuid in the read list.

There is **no default agent**: the allowlist IS the list of agents. A call
without an explicit agent works only when the allowlist leaves exactly one
possibility; otherwise the MCP says which agents it can work on instead of
guessing."""
from __future__ import annotations

import re
from urllib.parse import unquote

from .config import Config

LEVEL_NONE = "none"
LEVEL_READ = "read"
LEVEL_READWRITE = "read/write"

# Verbs that MODIFY state: go through `assert_writable`.
WRITE_VERBS = ("POST", "PATCH", "PUT", "DELETE")

_LEVELS = (LEVEL_NONE, LEVEL_READ, LEVEL_READWRITE)


class PermissionDeniedError(PermissionError):
    """The agent does not have permission for the requested operation."""


class ReadOnlyAgentError(PermissionDeniedError):
    """Attempted to write to a read-only agent."""


def _norm(entry: str) -> tuple[str, str]:
    """'project/uuid' -> ('project', 'uuid'); 'uuid' -> ('', 'uuid')."""
    entry = (entry or "").strip()
    if "/" in entry:
        project, _, agent = entry.partition("/")
        return project.strip(), agent.strip()
    return "", entry


def _matches(entry: str, project: str, agent_id: str) -> bool:
    """Does the input cover (project, agent)?"""
    entry_project, entry_agent = _norm(entry)
    if not entry_agent:
        return False
    if entry_agent != agent_id:
        return False
    if entry_project and entry_project != project:
        return False
    return True


def agent_level(project: str, agent_id: str, cfg: Config) -> str:
    """Permission level for (project, agent): 'read', 'read/write' or 'none'."""
    raw = (agent_id or "").strip()
    if not project and "/" in raw:
        project, _, raw = raw.partition("/")
    agent_id = raw.split("/")[-1]
    if not agent_id:
        return LEVEL_NONE
    for entry in cfg.agent_readwrite:
        if _matches(entry, project, agent_id):
            return LEVEL_READWRITE
    for entry in cfg.agent_read:
        if _matches(entry, project, agent_id):
            return LEVEL_READ
    return LEVEL_NONE


def is_read_only(agent_id: str, cfg: Config) -> bool:
    """Legacy: read-only (old list `CXP_READ_ONLY_AGENTS`)."""
    return bool(agent_id) and cfg.is_read_only(agent_id)


# --- the allowlist IS the list of agents (there is no "default agent") ------

NO_AGENTS_MSG = (
    "no allowed agent: write at least one agent in CXP_AGENT_READ (read-only) "
    "or in CXP_AGENT_READWRITE (read and write). Both variables are REQUIRED: "
    "the allowlist is what the MCP works on AND the security boundary of the "
    "harness that consumes it"
)


def _entries(cfg: Config) -> list[tuple[str, str]]:
    """(project, agent) of the allowlist, without duplicates."""
    rows: list[tuple[str, str]] = []
    for entry in cfg.agent_readwrite + cfg.agent_read:
        project, agent = _norm(entry)
        if agent and (project, agent) not in rows:
            rows.append((project, agent))
    return rows


def agents_by_level(cfg: Config, level: str) -> list[str]:
    """The allowed agent ids with a given level (`read` / `read/write`)."""
    output: list[str] = []
    for project, agent in _entries(cfg):
        if agent_level(project, agent, cfg) == level and agent not in output:
            output.append(agent)
    return output


def resolve_agent(agent: str, cfg: Config) -> str:
    """The agent to work on, resolved against the allowlist.

    - An **explicit** agent always wins (its permission is checked afterwards).
    - Without an explicit agent: the **only** allowed agent is used.
    - With several allowed agents it is an error: the caller has to say which
      one. The MCP does not guess (a guess would silently work on the wrong
      agent).
    - With no allowed agent at all the MCP is not configured: error.
    """
    raw = (agent or "").strip()
    if raw:
        return raw.split("/")[-1]
    ids: list[str] = []
    levels: dict[str, str] = {}
    for project, agent_id in _entries(cfg):
        if agent_id not in ids:
            ids.append(agent_id)
            levels[agent_id] = agent_level(project, agent_id, cfg)
    if not ids:
        raise PermissionDeniedError(NO_AGENTS_MSG)
    if len(ids) == 1:
        return ids[0]
    detail = ", ".join(f"{agent_id} ({levels[agent_id]})" for agent_id in ids)
    raise PermissionDeniedError(
        f"several allowed agents and none is specified: use agent=<id> "
        f"({detail})"
    )


def assert_readable(project: str, agent_id: str, cfg: Config) -> str:
    """Return the agent_id if it can be read; otherwise, crash."""
    agent_id = (agent_id or "").split("/")[-1]
    if cfg.projects and project and project not in cfg.projects:
        raise PermissionDeniedError(
            f"the project {project} is not in CXP_PROJECTS "
            f"({', '.join(cfg.projects)})"
        )
    level = agent_level(project, agent_id, cfg)
    if level == LEVEL_NONE:
        raise PermissionDeniedError(
            f"the agent {agent_id} (project {project or '?'}) has no permission: "
            "add it to CXP_AGENT_READ or CXP_AGENT_READWRITE"
        )
    return agent_id


def agent_from_path(path: str) -> tuple[str, str]:
    """(project, agent) that appear in a resource path/URL.

    The query string and the **method's own suffix** are dropped:
    `.../agents/{id}:export` -> the id is `{id}`, not `{id}:export`.

    Without this, any method with a colon (`:export`, `:restore`, `:validate`)
    was compared against the allowlist with the suffix attached -> the request was
    always rejected ("has no permission") even if the agent was the clone.

    It returns the **last** agent of the path; `agents_from_url()` returns
    **all** of them (path + query), and that is what the guard uses."""
    path = (path or "").split("?")[0].split("#")[0]
    parts = path.split("/")
    project = ""
    agent = ""
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            project = parts[i + 1]
        if p == "agents" and i + 1 < len(parts):
            agent = parts[i + 1]
    # `projects/p/locations/l/agents/AGENT:method` -> `AGENT`
    if ":" in agent:
        agent = agent.split(":", 1)[0]
    return project, agent


# --- extraction of EVERY agent of a URL ------------------------------------
#
# The allowlist must be checked against EVERY agent that the request can
# touch, not just one of them. Two ways of hiding an agent from a naive
# parser were found and closed:
#
#   1. the agent only in the QUERY (`?name=projects/.../agents/X/...`): the
#      old parser dropped the query, so it found no agent and the request
#      was NOT checked at all;
#   2. the agent behind `..` (`agents/OK/../../agents/X`): the server
#      resolves the `..` and lands on X, so the path has to be canonicalized
#      before it is compared (see `cxp.endpoint.resolve_path`).
#
# Both are read from the path AND from the (url-decoded) query.

_AGENT_RE = re.compile(r"(?:^|/)agents/([^/;?#]+)")
_PROJECT_RE = re.compile(r"(?:^|/)projects/([^/;?#]+)")


def _agents_in(text: str) -> list[tuple[str, str]]:
    """(project, agent) pairs of a resource path (or of a resource name)."""
    text = text or ""
    project = ""
    project_ids = _PROJECT_RE.findall(text)
    if project_ids:
        project = project_ids[0]
    output: list[tuple[str, str]] = []
    for m in _AGENT_RE.finditer(text):
        agent = m.group(1).split(":", 1)[0]
        if agent:
            output.append((project, agent))
    return output


def agents_from_url(url: str) -> list[tuple[str, str]]:
    """**Every** (project, agent) that a URL can reach, in the path and in the query.

    The query is url-decoded: the generic passthrough (`cx_api`) lets the
    caller put the whole resource name in `?name=`, and that agent is just as
    real as the one in the path."""
    url = url or ""
    path, _, query = url.partition("?")
    seen: list[tuple[str, str]] = []
    for project, agent in _agents_in(path.split("#")[0]):
        pair = (project, agent)
        if pair not in seen:
            seen.append(pair)
    for value in _query_values(query):
        for project, agent in _agents_in(value):
            pair = (project, agent)
            if pair not in seen:
                seen.append(pair)
    return seen


def _query_values(query: str) -> list[str]:
    """The url-decoded values of the query string."""
    values: list[str] = []
    for piece in (query or "").split("&"):
        if not piece:
            continue
        _, _, value = piece.partition("=")
        values.append(unquote(value))
    return values


def assert_url_allowed(url: str, verb: str, cfg: Config) -> list[tuple[str, str]]:
    """Check the allowlist against EVERY agent of `url` for `verb`.

    **Fails closed**: a write verb whose URL mentions no agent at all is
    rejected. Every CX write is per-agent (the agent is always in the
    resource name), so a write without an agent can only be a hand-crafted
    path that tries to slip the check."""
    verb = (verb or "GET").upper()
    pairs = agents_from_url(url)
    if verb in WRITE_VERBS:
        if not pairs:
            raise PermissionDeniedError(
                f"write {verb} without an identifiable agent in the path: {url!r}. "
                "Rejected: every CX write is per-agent and must be checked "
                "against the allowlist"
            )
        for project, agent in pairs:
            assert_writable(project, agent, cfg)
    else:
        for project, agent in pairs:
            assert_readable(project, agent, cfg)
    return pairs


def assert_playbook_writable(project: str, agent_id: str, cfg: Config) -> str:
    """Specific guard for WRITE a playbook.

    A playbook can only be written in a `CXP_AGENT_READWRITE` agent.
    That list must contain **only our copy** of the agent: the playbook of
    a reference agent (the original, the production one, another team's...)
    is read-only by design, and editing it would break parity with the console."""
    agent_id = (agent_id or "").split("/")[-1]
    assert_writable(project, agent_id, cfg)
    return agent_id


def assert_writable(project: str, agent_id: str, cfg: Config) -> str:
    """Return the agent_id if it is writable; otherwise, crash."""
    agent_id = (agent_id or "").split("/")[-1]
    assert_readable(project, agent_id, cfg)
    if agent_level(project, agent_id, cfg) != LEVEL_READWRITE:
        raise ReadOnlyAgentError(
            f"the agent {agent_id} (project {project or '?'}) is READ-ONLY "
            "(SOLO LECTURA): it is in CXP_AGENT_READ; to write it must be in "
            "CXP_AGENT_READWRITE"
        )
    return agent_id


def permission_table(cfg: Config) -> list[dict]:
    """View of the configured permissions (for `cx_status`).

    **One row per agent**: when the same agent is listed twice (for example
    the legacy `CXP_READ_ONLY_AGENTS` plus an explicit `<project>/<uuid>`
    entry), the entry with a project wins over the bare one, and
    read + write wins over read only."""
    rows: dict[str, dict] = {}
    for entry in cfg.agent_readwrite + cfg.agent_read:
        project, agent = _norm(entry)
        if not agent:
            continue
        level = LEVEL_READWRITE if any(
            _matches(e, project, agent) for e in cfg.agent_readwrite
        ) else LEVEL_READ
        current = rows.get(agent)
        if current is None:
            rows[agent] = {"project": project or "(any)",
                           "agent": agent, "permission": level}
            continue
        if current["project"] == "(any)" and project:
            current["project"] = project
        if current["permission"] != LEVEL_READWRITE and level == LEVEL_READWRITE:
            current["permission"] = LEVEL_READWRITE
    return list(rows.values())
