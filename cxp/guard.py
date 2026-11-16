"""Per-agent escritura/lectura guards.

Why a separate module: the `"original"` alias can be bypassed by passing the agent's
raw UUID. The real guard compares the **resolved id**, and is applied
at the lowest point of the write route (not only in the MCP layer).

Permission model (allowlist, via environment variables):

    CXP_AGENT_READWRITE=<uuid>[,<uuid>]   read + write
    CXP_AGENT_READ=<uuid>[,<uuid>]        read-only
    CXP_PROJECTS=<project>[,...]          (optional) restricts projects

An agent **without** an entry in either list is NOT accessible: not
even for reading. Entries accept two forms:

* `<uuid>` -> the permission travels with the uuid (valid in any project);
* `<proyecto>/<uuid>` -> matches project and agent.

The project is derived from the **request path**, not from `CXP_PROJECT`: thus the
same MCP can read the PROD agent (which lives in another project) with the same
uuid in the read list."""
from __future__ import annotations

from .config import Config

LEVEL_NONE = "ninguno"
LEVEL_READ = "lectura"
LEVEL_READWRITE = "lectura/escritura"

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
    """Permission level for (project, agent): 'lectura', 'lectura/escritura' or 'ninguno'."""
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


def assert_readable(project: str, agent_id: str, cfg: Config) -> str:
    """Return the agent_id if it can be read; otherwise, crash."""
    agent_id = (agent_id or "").split("/")[-1]
    if cfg.projects and project and project not in cfg.projects:
        raise PermissionDeniedError(
            f"el proyecto {project} no esta en CXP_PROJECTS "
            f"({', '.join(cfg.projects)})"
        )
    nivel = agent_level(project, agent_id, cfg)
    if nivel == LEVEL_NONE:
        raise PermissionDeniedError(
            f"el agente {agent_id} (proyecto {project or '?'}) no tiene permiso: "
            "agregalo a CXP_AGENT_READ o CXP_AGENT_READWRITE"
        )
    return agent_id


def agent_from_path(path: str) -> tuple[str, str]:
    """(project, agent) that appear in a resource path/URL.

    The query string and the **method's own suffix** are dropped:
    `.../agents/{id}:export` -> the id is `{id}`, not `{id}:export`.

    Without this, any method with a colon (`:export`, `:restore`, `:validate`)
    was compared against the allowlist with the suffix attached -> the request was
    always rejected ("no tiene permiso") even if the agent was the clone."""
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
            f"el agente {agent_id} (proyecto {project or '?'}) es SOLO LECTURA: "
            "esta en CXP_AGENT_READ; para escribir debe estar en CXP_AGENT_READWRITE"
        )
    return agent_id


def permission_table(cfg: Config) -> list[dict]:
    """View of the configured permissions (for `cx_status`)."""
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in cfg.agent_readwrite + cfg.agent_read:
        project, agent = _norm(entry)
        key = (project, agent)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "proyecto": project or "(cualquiera)",
            "agente": agent,
            "permiso": LEVEL_READWRITE if any(
                _matches(e, project, agent) for e in cfg.agent_readwrite
            ) else LEVEL_READ,
        })
    return rows
