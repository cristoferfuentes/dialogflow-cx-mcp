"""Guardas de escritura/lectura por agente.

Por que un modulo aparte: el alias `"original"` se puede esquivar pasando el
UUID crudo del agente. La guarda real compara el **id resuelto**, y se aplica
en el punto mas bajo de la ruta de escritura (no solo en la capa MCP).

Modelo de permisos (lista blanca, por variables de entorno):

    CXP_AGENT_READWRITE=<uuid>[,<uuid>]   leer + escribir
    CXP_AGENT_READ=<uuid>[,<uuid>]        solo leer
    CXP_PROJECTS=<proyecto>[,...]          (opcional) limita los proyectos

Un agente **sin** entrada en ninguna de las dos listas NO es accesible: ni
siquiera para leer. Las entradas admiten dos formas:

* `<uuid>` -> el permiso viaja con el uuid (vale en cualquier proyecto);
* `<proyecto>/<uuid>` -> empareja proyecto y agente.

El proyecto se deduce del **path de la peticion**, no de `CXP_PROJECT`: asi el
mismo MCP puede leer el agente de PROD (que vive en otro proyecto) con el mismo
uuid en la lista de lectura.
"""
from __future__ import annotations

from .config import Config

LEVEL_NONE = "ninguno"
LEVEL_READ = "lectura"
LEVEL_READWRITE = "lectura/escritura"

# Verbos que MODIFICAN estado: pasan por `assert_writable`.
WRITE_VERBS = ("POST", "PATCH", "PUT", "DELETE")

_LEVELS = (LEVEL_NONE, LEVEL_READ, LEVEL_READWRITE)


class PermissionDeniedError(PermissionError):
    """El agente no tiene permiso para la operacion solicitada."""


class ReadOnlyAgentError(PermissionDeniedError):
    """Se intento escribir en un agente de solo lectura."""


def _norm(entry: str) -> tuple[str, str]:
    """'proyecto/uuid' -> ('proyecto', 'uuid'); 'uuid' -> ('', 'uuid')."""
    entry = (entry or "").strip()
    if "/" in entry:
        project, _, agent = entry.partition("/")
        return project.strip(), agent.strip()
    return "", entry


def _matches(entry: str, project: str, agent_id: str) -> bool:
    """La entrada cubre a (project, agent)?"""
    entry_project, entry_agent = _norm(entry)
    if not entry_agent:
        return False
    if entry_agent != agent_id:
        return False
    if entry_project and entry_project != project:
        return False
    return True


def agent_level(project: str, agent_id: str, cfg: Config) -> str:
    """Nivel de permiso de (project, agent): 'lectura', 'lectura/escritura' o 'ninguno'."""
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
    """Legacy: solo lectura (lista antigua `CXP_READ_ONLY_AGENTS`)."""
    return bool(agent_id) and cfg.is_read_only(agent_id)


def assert_readable(project: str, agent_id: str, cfg: Config) -> str:
    """Devuelve el agent_id si se puede leer; si no, revienta."""
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
    """(proyecto, agente) que aparecen en un path/URL de recurso.

    Se descartan la query string y el **sufijo de metodo propio**:
    `.../agents/{id}:export` -> el id es `{id}`, no `{id}:export`.

    Sin esto, cualquier metodo con colon (`:export`, `:restore`, `:validate`)
    se comparaba contra la lista blanca con el sufijo pegado -> la peticion se
    rechazaba siempre ("no tiene permiso") aunque el agente fuera el clon.
    """
    path = (path or "").split("?")[0].split("#")[0]
    parts = path.split("/")
    project = ""
    agent = ""
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            project = parts[i + 1]
        if p == "agents" and i + 1 < len(parts):
            agent = parts[i + 1]
    # `projects/p/locations/l/agents/AGENTE:metodo` -> `AGENTE`
    if ":" in agent:
        agent = agent.split(":", 1)[0]
    return project, agent


def assert_playbook_writable(project: str, agent_id: str, cfg: Config) -> str:
    """Guarda especifica para ESCRIBIR un playbook.

    Un playbook solo se puede escribir en un agente de `CXP_AGENT_READWRITE`.
    Esa lista debe contener **solo nuestra copia** del agente: el playbook de
    un agente de referencia (el original, el de produccion, el de otro
    equipo...) es de solo lectura por diseno, y editarlo romperia la paridad
    con la consola.
    """
    agent_id = (agent_id or "").split("/")[-1]
    assert_writable(project, agent_id, cfg)
    return agent_id


def assert_writable(project: str, agent_id: str, cfg: Config) -> str:
    """Devuelve el agent_id si se puede escribir; si no, revienta."""
    agent_id = (agent_id or "").split("/")[-1]
    assert_readable(project, agent_id, cfg)
    if agent_level(project, agent_id, cfg) != LEVEL_READWRITE:
        raise ReadOnlyAgentError(
            f"el agente {agent_id} (proyecto {project or '?'}) es SOLO LECTURA: "
            "esta en CXP_AGENT_READ; para escribir debe estar en CXP_AGENT_READWRITE"
        )
    return agent_id


def permission_table(cfg: Config) -> list[dict]:
    """Vista de los permisos configurados (para `cx_status`)."""
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
