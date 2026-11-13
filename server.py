"""MCP `cx-playbook`: **espejo de la API v3beta1 de Dialogflow CX** + el
agregado que la API publica no puede expresar.

Herramientas
------------
  cx_status                   configuracion, permisos y salud del backend
  cx_discover                 el mapa de la API (sale del discovery doc oficial)
  cx_api                      passthrough generico: los 162 metodos de v3beta1, 1:1
  ...                         herramientas tipadas del nucleo (agentes, flows, pages,
                              playbooks, intents, entityTypes, tools, webhooks,
                              conversations, sessions, changelogs...)
  cx_set_route_playbook       destino a playbook de una transition route DE PAGINA
  cx_set_flow_route_playbook  destino a playbook del ROUTER DE UN FLOW  <-- el extra
  cx_set_group_route_playbook destino a playbook de una ruta de un GRUPO DE RUTAS
  cx_gaps                     los huecos de la API publica, detectados EN VIVO
  cx_set_console_field        escribir CUALQUIER campo (incluido los fuera de esquema)

El MCP es **atomico** y **sin navegador**:

Todo va por HTTP contra el backend interno de la consola
(`dialogflow.clients6.google.com/v3alpha1|v3beta1`) con autenticacion de
primera parte (las 5 cookies de sesion de Google + firma `SAPISIDHASH`).
Las credenciales se leen de un `.env`; ver README.md.

Por que el backend interno y no la API publica: es el unico que conserva
`transitionRoutes[].targetPlaybook`; en la API publica ese campo **se descarta
en silencio** (el `oneof target` solo admite `target_page` y `target_flow`).

Permisos (lista blanca por agente, en el `.env`):

    CXP_AGENT_READWRITE=<uuid>[,...]   leer + escribir
    CXP_AGENT_READ=<uuid>[,...]        solo leer
    CXP_PROJECTS=<proyecto>[,...]      (opcional) limita los proyectos

El agente de referencia y el de PROD son de solo lectura; el clon es el
unico escribible. La guarda compara el **id resuelto**, no un alias, y se
aplica en la capa baja (`cxp/console_api.py`), no solo en esta capa.

Convencion: los PARAMETROS de las herramientas van en ingles (los nombres de
la API v3beta1: `agent`, `flow`, `page`, `route`, `playbook`, `project`...);
las CLAVES de la respuesta van en espanol (como en la version anterior).
"""
from __future__ import annotations

import copy
import re
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
from cxp.console_api import ConsoleApi
from cxp.console_fields import set_field
from cxp.gaps import CATALOGO_MEDIDO, load_schema_index, scan_agent
from cxp.guard import (
    PermissionDeniedError,
    ReadOnlyAgentError,
    agent_from_path,
    agent_level,
    assert_playbook_writable,
    assert_readable,
    assert_writable,
    permission_table,
)
from cxp.httpclient import HttpStatusError, Session
from cxp.mirror import Mirror, MirrorError
from cxp.routing import find_route, summarize

mcp = _McpServer("cx-playbook")

CFG = load_config()

# ---------------------------------------------------------------------------
# UNA sola sesion HTTP para TODO el servidor (leccion 2026-11-06: "conexion
# TLS reutilizada por hilo"). Antes se creaba una `ConsoleApi` por llamada ->
# un handshake TLS (0,66 s medido) en cada peticion del MCP: 2,66 s por
# llamada frente a 0,50 s con keep-alive.
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
    return agent or CFG.default_agent


def _resolve_flow(cons: ConsoleApi, agent_id: str, flow: str, project: str = "") -> str:
    """id del flow por uuid o displayName (catalogo cacheado)."""
    if not flow:
        return ""
    if _looks_like_uuid(flow):
        return flow
    by_display, _ = cons.flow_index(agent_id, project)
    if flow in by_display:
        return by_display[flow]
    # El catalogo puede estar viejo (un flow creado hace un momento):
    # se refresca y se reintenta UNA vez.
    by_display, _ = cons.flow_index(agent_id, project, force=True)
    if flow in by_display:
        return by_display[flow]
    raise LookupError(f"flow no encontrado: {flow}")


def _resolve_page(cons: ConsoleApi, agent_id: str, flow_id: str, page: str,
                  project: str = "") -> str:
    """id de la pagina por uuid o displayName (catalogo cacheado)."""
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
    raise LookupError(f"pagina no encontrada: {page}")


def _resolve_playbook(cons: ConsoleApi, agent_id: str, playbook: str,
                      project: str = "") -> tuple[str, str]:
    """(id, displayName) del playbook destino (catalogo cacheado)."""
    by_display, by_id = cons.playbook_index(agent_id, project)
    if playbook in by_display:
        return by_display[playbook], playbook
    if _looks_like_uuid(playbook) and playbook in by_id:
        return playbook, by_id[playbook]
    # Catalogo viejo -> refresco y reintento UNA vez.
    by_display, by_id = cons.playbook_index(agent_id, project, force=True)
    if playbook in by_display:
        return by_display[playbook], playbook
    if _looks_like_uuid(playbook) and playbook in by_id:
        return playbook, by_id[playbook]
    raise LookupError(
        f"playbook no encontrado: {playbook!r}. "
        f"Disponibles: {sorted(by_display)}"
    )


def _playbook_names(cons: ConsoleApi, agent_id: str, project: str = "") -> dict[str, str]:
    """id -> displayName de los playbooks (para etiquetar los destinos)."""
    return cons.playbook_index(agent_id, project)[1]


def _playbook_resource(agent_id: str, playbook_id: str, project: str = "") -> str:
    return (f"projects/{project or CFG.project}/locations/{CFG.location}"
            f"/agents/{agent_id}/playbooks/{playbook_id}")


def _resource_name(agent_id: str, project: str, *parts: str) -> str:
    base = f"projects/{project}/locations/{CFG.location}/agents/{agent_id}"
    for p in parts:
        if p:
            base += "/" + p
    return base


def _agent_of(name: str) -> str:
    parts = (name or "").split("/")
    return parts[5] if len(parts) > 5 and parts[4] == "agents" else ""


def _destino_maps(cons: ConsoleApi, agent_id: str, flow_id: str,
                  project: str) -> tuple[dict, dict]:
    """(id->displayName de paginas, id->displayName de flows) para leer destinos."""
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


def _destino_actual(ruta: dict, pbs_inv: dict, pages: dict, flows: dict) -> dict:
    """Destino ACTUAL de una ruta: {tipo, campo, ref, nombre}."""
    for campo, etiqueta in (("targetPlaybook", "playbook"), ("targetPage", "pagina"),
                            ("targetFlow", "flow")):
        val = ruta.get(campo)
        if not val:
            continue
        ref = str(val).split("/")[-1]
        if campo == "targetPlaybook":
            nombre = pbs_inv.get(ref, ref)
        elif campo == "targetPage":
            nombre = pages.get(ref, ref)
        else:
            nombre = flows.get(ref, ref)
        return {"tipo": etiqueta, "campo": campo, "ref": ref, "nombre": nombre}
    return {"tipo": None, "campo": None, "ref": None, "nombre": None}


def _sin_credenciales() -> dict:
    return {
        "error": "credenciales incompletas",
        "faltan_cookies": CFG.missing_cookies(),
        "tiene_api_key": bool(CFG.api_key),
        "tiene_proyecto": bool(CFG.project),
        "fichero_env": str(CFG.env_file) if CFG.env_file else None,
        "pista": "ver README.md -> 'Como obtener las credenciales'",
    }


def _err(exc: Exception) -> dict:
    if isinstance(exc, HttpStatusError):
        return {"error": f"HTTP {exc.status}", "detalle": exc.body[:400]}
    if isinstance(exc, (PermissionDeniedError, ReadOnlyAgentError)):
        return {"error": str(exc)}
    if isinstance(exc, LookupError):
        return {"error": str(exc)}
    return {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}


def _agent_from_path(path: str) -> tuple[str, str]:
    """(proyecto, agente) de un path/URL (delega en `cxp.guard`)."""
    return agent_from_path(path)


def _guard_path(path: str, verb: str) -> None:
    """Aplica el permiso del agente que aparece en el path de la peticion."""
    project, agent = _agent_from_path(path)
    if not agent:
        return
    if verb.upper() in WRITE_VERBS:
        assert_writable(project, agent, CFG)
    else:
        assert_readable(project, agent, CFG)


def _resolve_path(path: str, name: str, parent: str, params: dict | None,
                  query: dict | None, project: str, agent: str) -> tuple[str, dict]:
    """(path relativo a la version, query) de una peticion.

    Es la mitad "path" de `_build_url`, aparte, para poder reutilizarla en el
    endpoint `/batch` (que lleva sus propias URLs dentro del multipart).
    """
    values: dict[str, Any] = dict(params or {})
    if name:
        values["name"] = name
    if parent:
        values["parent"] = parent
    if agent and "agent" not in values:
        values["agent"] = agent
    p = path
    if not p.startswith("projects/"):
        p = f"projects/{project}/locations/{CFG.location}/{p}"
    if "{agent}" in p and not values.get("agent"):
        values["agent"] = agent or CFG.default_agent
    for k, v in values.items():
        p = p.replace("{" + k + "}", str(v))
    leftovers = re.findall(r"\{[A-Za-z0-9_.]+\}", p)
    if leftovers:
        raise MirrorError(f"faltan parametros para el path: {sorted(set(leftovers))}")
    q = {k: v for k, v in (query or {}).items() if v is not None}
    return p, q


def _build_url(path: str, name: str, parent: str, params: dict | None,
               query: dict | None, project: str, agent: str) -> str:
    """URL a partir de una plantilla de path relativa o absoluta."""
    from urllib.parse import urlencode

    p, q = _resolve_path(path, name, parent, params, query, project, agent)
    q = {"key": CFG.api_key, **q}
    return f"https://dialogflow.clients6.google.com/{CFG.api_version}/{p}?{urlencode(q)}"


# ------------------------------------------------------------------- tools
@mcp.tool()
def cx_limites_api(filtro: str = "") -> dict:
    """**Memoria del MCP**: los limites MEDIDOS de la API de Dialogflow CX.

    Cada limite trae el sintoma exacto que devuelve el servidor, por que
    ocurre y como esquivarlo. Sin argumentos: todos. `filtro`: texto libre
    ("force", "playbook", "batch", "referencia"...).

    Esto es lo que la API **no** documenta: el descriptor oficial dice que
    `referencedPlaybooks` es de solo lectura y no dice nada de `force`, del
    `/batch`, de los bucles entre playbooks ni de la latencia real.
    """
    from cxp.limits import CONSTANTES, buscar

    limites = buscar(filtro)
    return {
        "filtro": filtro or None,
        "total": len(limites),
        "constantes": CONSTANTES,
        "limites": limites,
    }


@mcp.tool()
def cx_status() -> dict:
    """Estado del MCP: configuracion, permisos por agente y salud del backend.

    Devuelve de donde salio la configuracion, si estan las 5 cookies
    obligatorias, la version del proto en uso, la **tabla de permisos** de cada
    agente (lectura / lectura-escritura / sin acceso) y si el backend interno
    ACEPTA la sesion (GET real).
    """
    out: dict[str, Any] = {
        "proyecto": CFG.project or None,
        "location": CFG.location,
        "api_version": CFG.api_version,
        "console_origin": CFG.console_origin,
        "backend": f"{CFG.console_backend}/{CFG.api_version}",
        "fichero_env": str(CFG.env_file) if CFG.env_file else None,
        "api_key": "puesta" if CFG.api_key else "FALTA",
        "cookies_requeridas": list(("SAPISID", "SID", "HSID", "SSID", "APISID")),
        "cookies_faltantes": CFG.missing_cookies(),
        "agente_por_defecto": CFG.default_agent or None,
        "agentes_solo_lectura": CFG.read_only_agents,
        "proyectos_permitidos": CFG.projects or "(sin restriccion)",
        "permisos": permission_table(CFG),
    }
    try:
        from cxp.limits import CONSTANTES, LIMITES
        out["constantes_rendimiento"] = CONSTANTES
        out["limites_api"] = [f"{x['id']}: {x['titulo']}" for x in LIMITES]
        out["ver_limites"] = "cx_limites_api()"
    except Exception:  # pragma: no cover
        pass
    if not CFG.credentials_ok():
        out["credenciales_ok"] = False
        out.update(_sin_credenciales())
        return out

    try:
        cons = _api()
        agentes = cons.list_agents()
        out["credenciales_ok"] = True
        out["sesion_aceptada"] = True
        out["agentes_visibles"] = [
            {"id": a["name"].split("/")[-1],
             "displayName": a.get("displayName"),
             "proyecto": a["name"].split("/")[1],
             "permiso": agent_level(a["name"].split("/")[1],
                                    a["name"].split("/")[-1], CFG)}
            for a in agentes
        ]
    except HttpStatusError as exc:
        out["credenciales_ok"] = False
        out["sesion_aceptada"] = False
        if exc.status in (401, 403):
            out["error"] = (f"HTTP {exc.status}: el backend NO acepta la sesion. "
                            "Las cookies-caducan o estan mal copiadas; vuelve a copiarlas "
                            "segun README.md.")
        else:
            out["error"] = f"HTTP {exc.status}: {exc.body[:300]}"
    except Exception as exc:
        out["credenciales_ok"] = False
        out["sesion_aceptada"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return out


@mcp.tool()
def cx_discover(filter: str = "", method: str = "", project: str = "") -> dict:
    """El mapa de la API que refleja este MCP (discovery doc de v3beta1).

    Sin argumentos: todos los recursos con sus metodos (162 en v3beta1).
    `filter`: subcadena que filtra por recurso o metodo ("flows", "playbook"...).
    `method`: detalle de UN metodo (id del discovery, p. ej. "agents.flows.train").
    `project`: si se da, muestra que agentes hay en ese proyecto.
    """
    mir = _mirror()
    try:
        if method:
            return {"metodo": mir.describe(method)}

        idx = mir.index()
        rows: list[dict] = []
        for mid, spec in sorted(idx.items()):
            if filter and filter.lower() not in mid.lower():
                continue
            rows.append({
                "id": mid,
                "http": spec.get("httpMethod"),
                "path": spec.get("path"),
                "params_obligatorios": sorted(
                    k for k, v in (spec.get("parameters") or {}).items()
                    if v.get("required")
                ),
            })
        out = {
            "api_version": CFG.api_version,
            "total_metodos": len(idx),
            "filtro": filter or None,
            "metodos": rows,
        }
        if project:
            try:
                cons = _api()
                out["agentes_del_proyecto"] = [
                    {"id": a["name"].split("/")[-1], "displayName": a.get("displayName")}
                    for a in cons.list_agents(project)
                ]
            except Exception as exc:
                out["agentes_del_proyecto_error"] = str(exc)[:200]
        return out
    except MirrorError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_api(method: str = "", path: str = "", name: str = "", parent: str = "",
           params: dict | None = None, body: dict | None = None,
           query: dict | None = None, project: str = "", agent: str = "") -> dict:
    """Llamada GENERICA a la API: cualquier metodo de v3beta1, 1:1.

    `method`: id del discovery ("agents.flows.list", "agents.playbooks.patch",
    "agents.sessions.detectIntent"...) o un verbo HTTP ("GET", "POST"...).
    `path`: plantilla de path ("agents/{agent}/flows/{flow}") cuando `method` es
    un verbo HTTP. Admite `{name}`, `{parent}` y `{...}` sueltos.
    `name`/`parent`: nombres completos de recurso
    ("projects/p/locations/l/agents/a/flows/f"). Con `agent` y `project` se
    puede abreviar `parent`.
    `params`: valores de los huecos de la plantilla.
    `body`: cuerpo JSON (POST/PATCH).
    `query`: parametros de query extra (updateMask, languageCode, pageSize...).

    Respuesta: el JSON crudo del backend.
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    mir = _mirror()
    proj = project or CFG.project
    try:
        verb = ""
        if method and method.upper() in HTTP_VERBS:
            verb = method.upper()
            if not path:
                return {"error": "con un verbo HTTP hace falta `path`"}
            url = _build_url(path, name, parent, params, query, proj, agent)
        elif method:
            spec = mir.spec(method)
            verb = spec.get("httpMethod") or "GET"
            p = dict(params or {})
            if name:
                p["name"] = name
            if parent:
                p["parent"] = parent
            if not p.get("parent"):
                p["parent"] = _resource_name(agent or _agent_of(p.get("name") or ""), proj)
            url, verb = mir.url(method, params=p)
        else:
            return {"error": "falta `method`"}

        _guard_path(url, verb)
        cons = _api()
        if verb in WRITE_VERBS:
            data = cons.request(verb, url, body=body or {}, query=query)
        else:
            data = cons.request(verb, url, body=body or None, query=query)
        return {"metodo": method or path, "http": verb, "url": url.split("?")[0],
                "datos": data}
    except MirrorError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_batch(requests: list[dict] | None = None, project: str = "") -> dict:
    """Ejecuta N llamadas a la API en UNA sola peticion HTTP (endpoint `/batch`).

    Cada elemento de `requests`:
        {"method": "POST", "path": "agents/{agent}/flows", "body": {...},
         "query": {...}, "agent": "<uuid>", "params": {...}}

    `method` admite un verbo HTTP o un id del discovery ("agents.flows.create").
    La **guarda se aplica sub-peticion a sub-peticion**: un batch que mezcle
    agentes de lectura y escritura se evalua cada uno por separado (nadie se
    cuela por el batch).

    Devuelve los resultados en el MISMO orden de entrada, con su codigo HTTP,
    y el tiempo total del viaje. Es la via rapida para el trabajo en bloque:
    en vez de N viajes de ida y vuelta (cada uno ~0,5 s de latencia), uno.
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    reqs = list(requests or [])
    if not reqs:
        return {"error": "falta `requests`"}
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
            # id del discovery: se resuelve a su path y se reutiliza el verbo
            values = dict(item.get("params") or {})
            if item.get("name"):
                values["name"] = item["name"]
            if item.get("parent"):
                values["parent"] = item["parent"]
            if not values.get("parent"):
                values["parent"] = _resource_name(agent or CFG.default_agent, proj)
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
            modo = "batch"
        except HttpStatusError as exc:
            if exc.status != 404:
                raise
            # El backend interno NO implementa `/batch` (404). Se cae a las
            # mismas sub-peticiones en HILOS: es lo que el batch pretendia.
            out = cons.batch_secundario(items, project=proj)
            modo = "hilos (/batch no existe en este backend)"
        ms = (time.perf_counter() - t0) * 1000
        results = []
        for i, r in enumerate(out):
            results.append({
                "indice": i,
                "http": r.get("status"),
                "ok": 200 <= int(r.get("status") or 0) < 300,
                "datos": r.get("data"),
            })
        return {
            "n": len(items),
            "modo": modo,
            "ms_total": round(ms, 1),
            "ms_por_subpeticion": round(ms / max(len(items), 1), 1),
            "errores": sum(1 for r in results if not r["ok"]),
            "respuestas": results,
        }
    except MirrorError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_net_probe(repeticiones: int = 5, path: str = "agents/{agent}",
                 agent: str = "", project: str = "", paralelo: int = 1) -> dict:
    """Diagnostico de red: **donde se va el tiempo** de cada peticion.

    Hace `repeticiones` GET del mismo recurso y devuelve, peticion a peticion,
    el reparto entre envio, espera de cabeceras y cuerpo, y si la conexion se
    reutilizo. Con `paralelo` > 1 las peticiones salen a la vez (hilos distintos
    -> una conexion TLS cada uno).

    Sirve para distinguir "tarda la red" de "tarda el backend" y para medir el
    efecto de cualquier cambio en el transporte.
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    cons = _api()
    agent_id = _resolve_agent(agent)
    proj = project or CFG.project
    try:
        url = cons._url(f"{cons._base(proj)}/{path.replace('{agent}', agent_id)}")
    except Exception:
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}")
    _HTTP.take_stats()
    t0 = time.perf_counter()
    if paralelo > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=paralelo) as pool:
            list(pool.map(lambda _i: cons.http.json("GET", url, cons._headers()), range(repeticiones)))
    else:
        for _ in range(repeticiones):
            cons.http.json("GET", url, cons._headers())
    ms = (time.perf_counter() - t0) * 1000
    filas = _HTTP.take_stats()
    return {
        "url": url.split("?")[0],
        "repeticiones": repeticiones,
        "paralelo": paralelo,
        "ms_total": round(ms, 1),
        "ms_por_peticion": round(ms / max(repeticiones, 1), 1),
        "peticiones": filas,
    }


@mcp.tool()
def cx_clone_agent(origen: str = "", destino: str = "", project: str = "",
                   lecturas: int = 8, escrituras: int = 2,
                   verificar: bool = True, modo: str = "aplicar") -> dict:
    """Clona un agente 1:1: **limpia el destino** y **recrea el original** dentro.

    `origen` = agente de solo lectura (la fuente). `destino` = agente escribible
    (nuestra copia). La guarda se aplica en la capa baja: es imposible tocar
    el original con esta herramienta.

    Fases (con su tiempo):
      1. `lectura`  - snapshot del original (lecturas EN PARALELO).
      2. `borrado`  - se borran todos los elementos del destino. Los borrados
                      independientes van `escrituras` en paralelo (2 = optimo
                      medido); los playbooks y los intents, en serie, porque su
                      orden lo impone el servidor.
      3. `creacion` - se recrea todo en orden de dependencias, remapeando los
                      ids (los recursos del original tienen ids distintos). Va
                      **en serie**: el backend serializa las escrituras del
                      mismo agente.
      4. `verificacion` - se relee el destino y se compara con el original.

    `modo="plan"` no escribe nada: devuelve lo que se borraria y lo que se
    crearia. `lecturas` = hilos de lectura en paralelo (8 = optimo medido).
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    from cxp.clone import Cloner

    cons = _api()
    src = origen or CFG.default_agent
    dst = destino or CFG.default_agent
    proj = project or CFG.project
    if src == dst:
        return {"error": "origen y destino son el mismo agente"}
    try:
        clonador = Cloner(cons, src, dst, project=proj,
                          lecturas=lecturas, escrituras=escrituras)
        if modo == "plan":
            snap = clonador.snapshot()
            estado = clonador._estado(dst)
            return {
                "modo": "plan",
                "origen": src, "destino": dst,
                "original": {k: len(v) for k, v in snap.items()
                             if isinstance(v, list)},
                # OJO: la clave del id del destino es "destino"; los conteos
                # van aparte (si se llaman igual, el id se pierde).
                "destino_conteos": {k: len(v) for k, v in estado.items()},
                "paginas_original": sum(len(v) for v in (snap.get("pages") or {}).values()),
                "ms": round(clonador._t_fases.get("lectura", 0.0), 1),
            }
        if modo == "limpiar":
            # Solo la limpieza del destino (sin tocar el original).
            t0 = time.perf_counter()
            limpio = clonador.limpiar()
            return {"modo": "limpiar", "origen": src, "destino": dst,
                    "ms_total": round((time.perf_counter() - t0) * 1000, 1),
                    "limpiar": limpio,
                    "acciones": clonador.acciones,
                    "errores": clonador.errores}
        informe = clonador.run(verificar=verificar)
        return {"modo": "aplicar", **informe}
    except PermissionError as exc:
        return {"error": f"permiso: {exc}"}
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------- tipadas
@mcp.tool()
def cx_list_agents(project: str = "") -> dict:
    """Lista los agentes del proyecto (los que la sesion puede ver)."""
    try:
        cons = _api()
        agents = cons.list_agents(project)
        rows = []
        for a in agents:
            parts = a["name"].split("/")
            rows.append({
                "id": parts[-1],
                "displayName": a.get("displayName"),
                "proyecto": parts[1],
                "permiso": agent_level(parts[1], parts[-1], CFG),
                "startFlow": (a.get("startFlow") or "").split("/")[-1],
                "startPlaybook": (a.get("startPlaybook") or "").split("/")[-1],
            })
        return {"proyecto": project or CFG.project, "total": len(rows), "agentes": rows}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_agent(agent: str = "", project: str = "") -> dict:
    """GET de un agente (v3beta1)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        data = cons.request("GET", cons._url(f"{cons._base(proj)}/agents/{agent_id}"))
        return {"agente": agent_id, "proyecto": proj, "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_update_agent(agent: str = "", body: dict | None = None,
                    update_mask: str = "", project: str = "") -> dict:
    """PATCH de un agente (v3beta1). Requiere permiso de escritura."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        assert_writable(proj, agent_id, CFG)
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}", alt="json")
        data = cons.request("PATCH", url, body=body or {},
                            query={"updateMask": update_mask} if update_mask else None)
        return {"agente": agent_id, "proyecto": proj,
                "updateMask": update_mask or "(derivado del cuerpo)", "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_list_flows(agent: str = "", project: str = "") -> dict:
    """Lista los flows de un agente."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        flows = cons.list_flows(agent_id, proj)
        return {"agente": agent_id, "proyecto": proj, "total": len(flows),
                "flows": [{"id": f["name"].split("/")[-1],
                           "displayName": f.get("displayName"),
                           "rutas": len(f.get("transitionRoutes") or [])}
                          for f in flows]}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_flow(flow: str = "", agent: str = "", project: str = "") -> dict:
    """GET de un flow (v3beta1), con su router (rutas y destinos)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        data = cons.get_flow(agent_id, flow_id, project=proj)
        return {"agente": agent_id, "proyecto": proj, "flow": flow_id,
                "displayName": data.get("displayName"),
                "rutas": summarize(data.get("transitionRoutes") or [],
                                   _playbook_names(cons, agent_id, proj)),
                "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_update_flow(flow: str = "", body: dict | None = None, update_mask: str = "",
                   agent: str = "", project: str = "") -> dict:
    """PATCH de un flow (v3beta1). Requiere permiso de escritura."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        data = cons.patch_flow(agent_id, flow_id, body or {},
                                update_mask=update_mask or None, project=proj)
        return {"agente": agent_id, "proyecto": proj, "flow": flow_id,
                "updateMask": update_mask or "(derivado del cuerpo)", "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_list_pages(flow: str = "", agent: str = "", project: str = "") -> dict:
    """Lista las paginas de un flow."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        pages = cons.list_pages(agent_id, flow_id, proj)
        return {"agente": agent_id, "proyecto": proj, "flow": flow_id,
                "total": len(pages),
                "pages": [{"id": p["name"].split("/")[-1],
                           "displayName": p.get("displayName"),
                           "rutas": len(p.get("transitionRoutes") or [])} for p in pages]}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_page(flow: str = "", page: str = "", agent: str = "",
                project: str = "") -> dict:
    """GET de una pagina (v3beta1), con sus rutas y destinos."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        page_id = _resolve_page(cons, agent_id, flow_id, page, proj)
        data = cons.get_page(agent_id, flow_id, page_id, project=proj)
        return {"agente": agent_id, "proyecto": proj, "flow": flow_id, "page": page_id,
                "displayName": data.get("displayName"),
                "rutas": summarize(data.get("transitionRoutes") or [],
                                   _playbook_names(cons, agent_id, proj)),
                "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_update_page(flow: str = "", page: str = "", body: dict | None = None,
                   update_mask: str = "", agent: str = "", project: str = "") -> dict:
    """PATCH de una pagina (v3beta1). Requiere permiso de escritura."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        page_id = _resolve_page(cons, agent_id, flow_id, page, proj)
        data = cons.patch_page(agent_id, flow_id, page_id, body or {},
                                update_mask=update_mask or None, project=proj)
        return {"agente": agent_id, "proyecto": proj, "flow": flow_id, "page": page_id,
                "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_list_playbooks(agent: str = "", project: str = "") -> dict:
    """Lista los playbooks de un agente."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        pbs = cons.list_playbooks(agent_id, proj)
        return {"agente": agent_id, "proyecto": proj, "total": len(pbs),
                "playbooks": [{"id": p["name"].split("/")[-1],
                               "displayName": p.get("displayName"),
                               "goal": (p.get("goal") or "")[:120],
                               "model": (p.get("llmModelSettings") or {}).get("model"),
                               "tokens": p.get("tokenCount")} for p in pbs]}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_playbook(playbook: str = "", agent: str = "", project: str = "") -> dict:
    """GET de un playbook (v3beta1)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        pb_id, pb_name = _resolve_playbook(cons, agent_id, playbook, proj)
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}/playbooks/{pb_id}")
        data = cons.request("GET", url)
        return {"agente": agent_id, "proyecto": proj, "playbook": pb_id,
                "displayName": pb_name, "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_update_playbook(playbook: str = "", body: dict | None = None,
                       update_mask: str = "", agent: str = "",
                       project: str = "") -> dict:
    """PATCH de un playbook (v3beta1). Requiere permiso de escritura.

    OJO: CX revalida el playbook entero -> sin `goal` responde
    "Playbook goal cannot be empty". Manda el cuerpo completo.
    """
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        # Un playbook solo se escribe en un agente de CXP_AGENT_READWRITE
        # (nuestra copia). Los agentes de referencia son de solo lectura.
        assert_playbook_writable(proj, agent_id, CFG)
        pb_id, pb_name = _resolve_playbook(cons, agent_id, playbook, proj)
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}/playbooks/{pb_id}",
                        alt="json")
        data = cons.request("PATCH", url, body=body or {},
                            query={"updateMask": update_mask} if update_mask else None)
        return {"agente": agent_id, "proyecto": proj, "playbook": pb_id,
                "displayName": pb_name, "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_list_children(agent: str = "", collection: str = "intents",
                     project: str = "") -> dict:
    """Lista una coleccion hija del agente.

    `collection`: intents | entityTypes | tools | webhooks | transitionRouteGroups |
    generators | changelogs | conversations
    """
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
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
            return {"error": f"coleccion desconocida: {collection!r}",
                    "colecciones": ["intents", "entityTypes", "tools", "webhooks",
                                     "transitionRouteGroups", "generators",
                                     "changelogs", "conversations"]}
        items = fn(agent_id, proj)
        return {"agente": agent_id, "proyecto": proj, "tipo": collection,
                "total": len(items),
                "items": [{"id": i.get("name", "").split("/")[-1],
                           "displayName": i.get("displayName")} for i in items]}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_get_conversation(conversation: str = "", agent: str = "",
                        project: str = "") -> dict:
    """GET de una conversacion (v3beta1): la traza real del agente."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        data = cons.get_conversation(agent_id, conversation, project=proj)
        return {"agente": agent_id, "proyecto": proj,
                "conversacion": conversation, "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_detect_intent(text: str = "", session_id: str = "", agent: str = "",
                     project: str = "", params: dict | None = None) -> dict:
    """sessions.detectIntent (v3beta1) contra un agente.

    `text`: lo que dice el usuario. `session_id`: id de sesion (si no, se
    genera). `params`: parametros de sesion (van en el primer turno).

    OJO (leccion 2026-11-05): contra algunos agentes el turno **vence en
    ~241 s** (el deadline es del servidor, HTTP 408) cuando el backend que
    consume las tools esta frio. Aqui el presupuesto es de 300 s: subirlo mas
    no arregla un 408. Si falla, **reintenta EN SERIE y con pausa (~90 s)**,
    nunca en paralelo.
    """
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        assert_readable(proj, agent_id, CFG)
        sid = session_id or f"mcp-{abs(hash(text)) % (10 ** 12)}"
        url = cons._url(f"{cons._base(proj)}/agents/{agent_id}/sessions/{sid}"
                        f":detectIntent", alt="json")
        body: dict[str, Any] = {"queryInput": {"text": {"text": text}}}
        if params:
            body["queryParams"] = {"parameters": params}
        data = cons.request("POST", url, body=body, timeout=300.0)
        return {"agente": agent_id, "proyecto": proj, "session": sid, "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_train_flow(flow: str = "", agent: str = "", project: str = "") -> dict:
    """Entrena un flow (flows:{flow}:train)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        assert_writable(proj, agent_id, CFG)
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        name = _resource_name(agent_id, proj, "flows", flow_id)
        url = cons._url(f"{name}:train", alt="json")
        data = cons.request("POST", url, body={}, timeout=300.0)
        return {"agente": agent_id, "proyecto": proj, "flow": flow_id, "datos": data}
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cx_validate_flow(flow: str = "", agent: str = "", project: str = "") -> dict:
    """Valida un flow (flows:{flow}:validate)."""
    try:
        cons = _api()
        agent_id = _resolve_agent(agent)
        proj = project or CFG.project
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        name = _resource_name(agent_id, proj, "flows", flow_id)
        url = cons._url(f"{name}:validate", alt="json")
        data = cons.request("POST", url, body={}, timeout=300.0)
        return {"agente": agent_id, "proyecto": proj, "flow": flow_id, "datos": data}
    except Exception as exc:
        return _err(exc)


# ------------------------------------------------- EL AGREGADO: playbook destino
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
    """Pone un PLAYBOOK como destino de UNA transition route DE PAGINA.

    Es lo que la API publica NO puede hacer: ahi `TransitionRoute.target` solo
    admite pagina o flow, y un `targetPlaybook` se descarta en silencio. Este
    MCP escribe contra el backend interno de la consola (mismo proto que usa la
    UI), en ~1 s. **Medido: el runtime ejecuta el playbook.**

    - agent:   id del agente (por defecto, `CXP_DEFAULT_AGENT`).
    - flow:    id o displayName del flow (por defecto, el primero del agente).
    - page:    id o displayName de la pagina (por defecto, la primera).
    - route:   uuid de la ruta, su condicion EXACTA, o su indice (0-based).
               **Si se omite, la herramienta solo LEE** y devuelve el estado
               actual de la pagina.
    - playbook: displayName o id del playbook destino (por defecto, NO cambia).
    - dry_run: True (por defecto) = no escribe; devuelve el plan.

    OJO: cambiar el destino **borra** `targetPage`/`targetFlow` de esa ruta
    (son un `oneof`): si la ruta apuntaba a una pagina o a un flow, eso se
    pierde. El `dry_run` devuelve el `aviso` con el destino que se va a perder.
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "falta el agente",
                "pista": "pasa agent=... o pon CXP_DEFAULT_AGENT en el .env"}
    cons = _api()
    proj = project or CFG.project
    try:
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        if not flow_id:
            flows = cons.list_flows(agent_id, proj)
            if not flows:
                return {"error": "el agente no tiene flows", "agente": agent_id}
            flow_id = flows[0]["name"].split("/")[-1]
        page_id = _resolve_page(cons, agent_id, flow_id, page, proj)
        if not page_id:
            pages = cons.list_pages(agent_id, flow_id, proj)
            if not pages:
                return {"error": "el flow no tiene paginas", "agente": agent_id,
                        "flow": flow_id}
            page_id = pages[0]["name"].split("/")[-1]
        pg = cons.get_page(agent_id, flow_id, page_id, project=proj)
        rutas = pg.get("transitionRoutes") or []
        pbs_inv = _playbook_names(cons, agent_id, proj)
        estado = {
            "agente": agent_id, "proyecto": proj, "flow": flow_id, "pagina": page_id,
            "nivel": "page",
            "displayName": pg.get("displayName"),
            "rutas": summarize(rutas, pbs_inv),
        }
    except Exception as exc:
        return _err(exc)

    # Solo lectura: se devuelve el estado actual.
    if not route:
        return {"modo": "lectura", **estado,
                "pista": "pasa route=... y playbook=... para cambiar el destino"}

    try:
        index, actual = find_route(rutas, route)
    except LookupError as exc:
        return {"error": str(exc), **estado}

    if not playbook:
        return {"modo": "lectura",
                "ruta": {"indice": index, "name": actual.get("name"),
                         "condition": actual.get("condition"),
                         "destino_actual": actual.get("targetPlaybook")},
                **estado, "pista": "pasa playbook=... para poner el destino"}

    try:
        pb_id, pb_nombre = _resolve_playbook(cons, agent_id, playbook, proj)
    except LookupError as exc:
        return {"error": str(exc), **estado}

    destino_actual = actual.get("targetPlaybook")
    destino_nuevo = _playbook_resource(agent_id, pb_id, proj)
    pages_map, flows_map = _destino_maps(cons, agent_id, flow_id, proj)
    actual_info = _destino_actual(actual, pbs_inv, pages_map, flows_map)
    aviso = None
    if actual_info["campo"] in ("targetPage", "targetFlow"):
        aviso = (f"la ruta apuntaba a {actual_info['tipo']} "
                 f"'{actual_info['nombre']}': al poner el playbook ese destino SE BORRA "
                 f"(targetPage/targetFlow son un 'oneof' con targetPlaybook)")

    if dry_run:
        return {"modo": "dry-run",
                "ruta": {"indice": index, "name": actual.get("name"),
                         "condition": actual.get("condition"),
                         "destino_actual": destino_actual,
                         "destino_actual_tipo": actual_info["tipo"],
                         "destino_actual_nombre": actual_info["nombre"],
                         "destino_nuevo": destino_nuevo,
                         "destino_nuevo_nombre": pb_nombre},
                "aviso": aviso,
                **estado}

    try:
        assert_writable(proj, agent_id, CFG)
    except ReadOnlyAgentError as exc:
        return {"error": str(exc), "agente": agent_id}

    try:
        res = cons.set_route_playbook(agent_id, flow_id, page_id, route, destino_nuevo,
                                       project=proj)
    except Exception as exc:
        return _err(exc)

    despues = cons.get_page(agent_id, flow_id, page_id, project=proj)
    res["rutas"] = summarize(despues.get("transitionRoutes") or [], pbs_inv)
    res["displayName"] = despues.get("displayName")
    res["modo"] = "escrito"
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
    """Pone un PLAYBOOK como destino de una transition route DEL ROUTER DE UN FLOW.

    **El agregado**: la API publica de Dialogflow CX no puede expresar esto.
    En el proto publico `TransitionRoute.target` es un `oneof` con
    `target_page` y `target_flow`: `target_playbook` NO existe y un PATCH que
    lo envie se descarta en silencio (la ruta queda sin destino y el agente cae
    en NO_MATCH). El backend interno de la consola SI lo conserva: el MCP
    escribe ahi, en ~1 s, y verifica releyendo.
    **Medido: el runtime EJECUTA el playbook cuando el destino esta aqui**
    (prueba con un playbook de marca propia: `matchType=PLAYBOOK`,
    `currentPlaybooks=[el playbook nuevo]` y la respuesta fue su texto).

    - agent:   id del agente (por defecto, `CXP_DEFAULT_AGENT`).
    - flow:    id o displayName del flow (por defecto, el primero del agente).
    - route:   uuid de la ruta, su condicion EXACTA, o su indice (0-based).
               **Si se omite, la herramienta solo LEE** y devuelve el router
               del flow con el destino de cada ruta.
    - playbook: displayName o id del playbook destino (por defecto, NO cambia).
    - dry_run: True (por defecto) = no escribe; devuelve el plan.

    OJO: cambiar el destino **borra** `targetPage`/`targetFlow` de esa ruta
    (son un `oneof`): si la ruta apuntaba a una pagina o a un flow, eso se
    pierde. El MCP avisa antes de escribir (dry_run).
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "falta el agente",
                "pista": "pasa agent=... o pon CXP_DEFAULT_AGENT en el .env"}
    cons = _api()
    proj = project or CFG.project
    try:
        flow_id = _resolve_flow(cons, agent_id, flow, proj)
        if not flow_id:
            flows = cons.list_flows(agent_id, proj)
            if not flows:
                return {"error": "el agente no tiene flows", "agente": agent_id}
            flow_id = flows[0]["name"].split("/")[-1]
        fl = cons.get_flow(agent_id, flow_id, project=proj)
        rutas = fl.get("transitionRoutes") or []
        pbs_inv = _playbook_names(cons, agent_id, proj)
        estado = {
            "agente": agent_id, "proyecto": proj, "flow": flow_id,
            "nivel": "flow (router del flow)",
            "displayName": fl.get("displayName"),
            "rutas": summarize(rutas, pbs_inv),
        }
    except Exception as exc:
        return _err(exc)

    if not route:
        return {"modo": "lectura", **estado,
                "pista": "pasa route=... y playbook=... para cambiar el destino"}

    try:
        index, actual = find_route(rutas, route)
    except LookupError as exc:
        return {"error": str(exc), **estado}

    if not playbook:
        return {"modo": "lectura",
                "ruta": {"indice": index, "name": actual.get("name"),
                         "condition": actual.get("condition"),
                         "destino_actual": actual.get("targetPlaybook")},
                **estado, "pista": "pasa playbook=... para poner el destino"}

    try:
        pb_id, pb_nombre = _resolve_playbook(cons, agent_id, playbook, proj)
    except LookupError as exc:
        return {"error": str(exc), **estado}

    destino_actual = actual.get("targetPlaybook")
    destino_nuevo = _playbook_resource(agent_id, pb_id, proj)
    pages_map, flows_map = _destino_maps(cons, agent_id, flow_id, proj)
    actual_info = _destino_actual(actual, pbs_inv, pages_map, flows_map)
    aviso = None
    if actual_info["campo"] in ("targetPage", "targetFlow"):
        aviso = (f"la ruta apuntaba a {actual_info['tipo']} "
                 f"'{actual_info['nombre']}': al poner el playbook ese destino SE BORRA "
                 f"(targetPage/targetFlow son un 'oneof' con targetPlaybook)")

    if dry_run:
        return {"modo": "dry-run",
                "ruta": {"indice": index, "name": actual.get("name"),
                         "condition": actual.get("condition"),
                         "destino_actual": destino_actual,
                         "destino_actual_tipo": actual_info["tipo"],
                         "destino_actual_nombre": actual_info["nombre"],
                         "destino_nuevo": destino_nuevo,
                         "destino_nuevo_nombre": pb_nombre},
                "aviso": aviso,
                **estado}

    try:
        assert_writable(proj, agent_id, CFG)
    except ReadOnlyAgentError as exc:
        return {"error": str(exc), "agente": agent_id}

    try:
        res = cons.set_flow_route_playbook(agent_id, flow_id, route, destino_nuevo,
                                            project=proj)
    except Exception as exc:
        return _err(exc)

    despues = cons.get_flow(agent_id, flow_id, project=proj)
    res["rutas"] = summarize(despues.get("transitionRoutes") or [], pbs_inv)
    res["displayName"] = despues.get("displayName")
    res["modo"] = "escrito"
    return res


# ===========================================================================
# LOS HUECOS DE LA API PUBLICA
# ===========================================================================
# La API publica v3beta1 y el backend interno de la consola sirven el MISMO
# discovery doc, pero no el MISMO proto: el backend de la consola tiene campos
# que la API publica ni siquiera DECLARA (y por tanto no puede leer ni
# escribir). Eso son agujeros, y son la razón de existir de este MCP.
#
# `cx_gaps` los DETECTA en vivo (compara el JSON real con el esquema publico),
# `cx_set_console_field` los ESCRIBE, y las herramientas `cx_set_*_playbook`
# son el caso mas usado.

_TIPOS_RECURSO = ("flow", "page", "playbook", "tool", "intent", "entityType",
                  "webhook", "generator", "transitionRouteGroup")


def _resolve_resource_name(recurso: str, agent_id: str, proj: str) -> str:
    """Convierte un selector de recurso en su `name` completo.

    Acepta:
      * el `name` completo que devuelve la API
      * `flow/<id>`, `page/<flowId>/<pageId>`, `playbook/<id>`, `tool/<id>`,
        `intent/<id>`, `entityType/<id>`, `transitionRouteGroup/<id>`, ...
      * `agent` (el propio agente)
    """
    recurso = (recurso or "").strip()
    if not recurso:
        return ""
    if recurso == "agent":
        return f"projects/{proj}/locations/{CFG.location}/agents/{agent_id}"
    if "/" not in recurso:
        return recurso  # ya es un nombre completo
    tipo, _, resto = recurso.partition("/")
    if tipo not in _TIPOS_RECURSO:
        return recurso
    base = f"projects/{proj}/locations/{CFG.location}/agents/{agent_id}"
    if tipo == "page":
        flow_id, _, page_id = resto.partition("/")
        return f"{base}/flows/{flow_id}/pages/{page_id}"
    return f"{base}/{tipo}/{resto}"


@mcp.tool()
def cx_gaps(agent: str = "", scope: str = "", project: str = "",
            incluir_paginas: bool = True,
            incluir_catalogo: bool = True) -> dict:
    """Los HUECOS de la API publica v3beta1, detectados EN VIVO.

    Recorre el agente y compara cada recurso que devuelve el backend con el
    **esquema publico** de v3beta1 (el discovery doc oficial). Todo campo que
    el backend expone y la API publica **no declara** es un agujero: por la
    API publica no se puede ni leer ni escribir.

    No es una lista escrita a mano: se calcula con los datos reales, asi que
    si la consola anade un campo nuevo, este MCP lo ve sin tocar el codigo.

    - agent:   id del agente (por defecto, `CXP_DEFAULT_AGENT`).
    - scope:   filtra los huecos por esquema o por nombre de campo
               (por ejemplo `"Tool"` o `"targetPlaybook"`). Vacio = todos.
    - project: proyecto (por defecto, `CXP_PROJECT`).
    - incluir_paginas: si False, no baja a las paginas de cada flow (mas rapido).
    - incluir_catalogo: añade el catalogo MEDIDO (que huecos se pueden
               escribir y cuales son solo lectura). Ver `cx_limites_api`.

    Devuelve `huecos` (lo detectado ahora), `catalogo` (lo medido) y
    `recursos_escaneados`.
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "falta el agente",
                "pista": "pasa agent=... o pon CXP_DEFAULT_AGENT en el .env"}
    cons = _api()
    proj = project or CFG.project
    try:
        idx = load_schema_index(CFG.api_version)
    except Exception as exc:
        return _err(exc)
    try:
        escaneo = scan_agent(cons, agent_id, proj, idx=idx,
                             incluir_paginas=incluir_paginas)
    except PermissionDeniedError as exc:
        return {"error": str(exc), "agente": agent_id}
    except Exception as exc:
        return _err(exc)

    huecos = escaneo.get("huecos") or []
    if scope:
        s = scope.lower()
        huecos = [h for h in huecos
                  if s in (h.get("esquema") or "").lower() or s in h["campo"].lower()]

    salida: dict[str, Any] = {
        "agente": agent_id,
        "proyecto": proj,
        "version_esquema": CFG.api_version,
        "recursos_escaneados": escaneo.get("recursos_escaneados"),
        "total_huecos": len(huecos),
        "huecos": huecos,
        "nota": ("Un hueco es un campo que el backend de la consola expone y "
                 "la API publica NO declara: por la API publica no se puede "
                 "ni leer ni escribir. Los que se pueden ESCRIBIR estan "
                 "marcados como 'escribible' en el catalogo."),
    }
    if incluir_catalogo:
        cat = CATALOGO_MEDIDO
        if scope:
            s = scope.lower()
            cat = [c for c in cat if s in c["campo"].lower()
                   or any(s in r.lower() for r in c["recursos"])]
        salida["catalogo"] = cat
    return salida


@mcp.tool()
def cx_set_console_field(recurso: str = "", campo: str = "", valor: Any = None,
                         dry_run: bool = True, project: str = "",
                         agent: str = "") -> dict:
    """Escribe UN campo de un recurso, incluso los que la API publica no declara.

    Es la version **generica** de "poner un playbook como destino": en vez de
    un metodo por operacion, hay UNA:

        GET  recurso -> se aplica el valor en la ruta -> PATCH -> GET y compara.

    La comparacion final es la garantia: el servidor puede descartar un campo
    en silencio, y la respuesta dice que se guardo y que se perdio.

    - recurso: el recurso. Acepta el `name` completo que devuelve la API
                (`projects/.../agents/.../flows/.../pages/...`) o una forma
                corta: `flow/<id>`, `page/<flowId>/<pageId>`,
                `playbook/<id>`, `tool/<id>`, `intent/<id>`,
                `entityType/<id>`, `transitionRouteGroup/<id>`, `agent`.
    - campo:   ruta del campo. Admite indices: `transitionRoutes[0].targetPlaybook`,
                `form.parameters[0].description`, `entities[0].dtmfPattern`,
                `webClientSettings.boxBorderRadius`.
                Para arrays completos: `transitionRoutes` con el array entero
                como valor.
    - valor:   el valor (JSON). Para borrar un campo, `null`.
    - dry_run: True (por defecto) = no escribe; devuelve el plan y el diff.
    - agent:   agente (por defecto, `CXP_DEFAULT_AGENT`); solo hace falta para
                las formas cortas.

    OJO: para poner el destino a un playbook, `targetPlaybook` es un `oneof`
    con `targetPage`/`targetFlow`: hay que quitar esos dos. Lo hace
    `cx_set_route_playbook` por ti.
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "falta el agente",
                "pista": "pasa agent=... o pon CXP_DEFAULT_AGENT en el .env"}
    if not recurso or not campo:
        return {"error": "faltan recurso y/o campo",
                "pista": "recurso=... y campo=... (pista: cx_gaps)"}
    proj = project or CFG.project
    nombre = _resolve_resource_name(recurso, agent_id, proj)
    if not nombre:
        return {"error": "no se pudo resolver el recurso", "recurso": recurso}

    try:
        res = set_field(_api(), nombre, campo, valor, dry_run=dry_run,
                        project=proj)
    except PermissionDeniedError as exc:
        return {"error": str(exc), "recurso": nombre}
    except Exception as exc:
        return _err(exc)

    salida = res.as_dict()
    salida["modo"] = "dry-run" if dry_run else "escrito"
    if dry_run:
        salida["pista"] = ("esto NO ha escrito nada; pon dry_run=False para "
                           "aplicarlo")
    return salida


@mcp.tool()
def cx_set_group_route_playbook(agent: str = "", group: str = "", route: str = "",
                                playbook: str = "", dry_run: bool = True,
                                project: str = "") -> dict:
    """Pone un PLAYBOOK como destino de una transition route de un GRUPO DE RUTAS.

    El tercer miembro de la familia del "agregado": una `transitionRouteGroup`
    es un recurso propio (`.../agents/{A}/transitionRouteGroups/{G}`) y sus
    rutas son `TransitionRoute` como las de una pagina o las de un flow. La
    API publica declara ese mismo `TransitionRoute` **sin** `targetPlaybook`:
    por API publica no se puede expresar.

    - agent:   id del agente (por defecto, `CXP_DEFAULT_AGENT`).
    - group:   id o displayName del grupo de rutas.
    - route:   uuid de la ruta, su condicion EXACTA, o su indice (0-based).
               **Si se omite, la herramienta solo LEE** y devuelve las rutas
               del grupo con su destino actual.
    - playbook: displayName o id del playbook destino (por defecto, NO cambia).
    - dry_run: True (por defecto) = no escribe; devuelve el plan.

    OJO: los grupos de rutas se COMPARTEN entre paginas y flows: tocar uno
    cambia el comportamiento de todas las paginas que lo usen.
    """
    if not CFG.credentials_ok():
        return _sin_credenciales()
    agent_id = _resolve_agent(agent)
    if not agent_id:
        return {"error": "falta el agente",
                "pista": "pasa agent=... o pon CXP_DEFAULT_AGENT en el .env"}
    cons = _api()
    proj = project or CFG.project
    try:
        grupos = cons.list_transition_route_groups(agent_id, proj)
    except Exception as exc:
        return _err(exc)
    if not grupos:
        return {"error": "el agente no tiene grupos de rutas", "agente": agent_id}

    elegido = None
    if group:
        for g in grupos:
            if group in ((g.get("displayName") or ""), (g.get("name") or "").rsplit("/", 1)[-1],
                         g.get("name") or ""):
                elegido = g
                break
        if elegido is None:
            return {"error": f"grupo de rutas no encontrado: {group!r}",
                    "grupos": [g.get("displayName") for g in grupos]}
    else:
        elegido = grupos[0]

    group_name = elegido.get("name") or ""
    group_id = group_name.rsplit("/", 1)[-1]
    try:
        grupo = cons.request("GET", cons._url("/" + group_name))
    except Exception as exc:
        return _err(exc)

    rutas = grupo.get("transitionRoutes") or []
    pbs_inv = _playbook_names(cons, agent_id, proj)
    estado = {
        "agente": agent_id, "proyecto": proj,
        "grupo": group_id, "displayName": elegido.get("displayName"),
        "nivel": "grupo de rutas",
        "rutas": summarize(rutas, pbs_inv),
    }

    if not route:
        return {"modo": "lectura", **estado,
                "pista": "pasa route=... y playbook=... para cambiar el destino"}

    try:
        index, actual = find_route(rutas, route)
    except LookupError as exc:
        return {"error": str(exc), **estado}

    if not playbook:
        return {"modo": "lectura",
                "ruta": {"indice": index, "name": actual.get("name"),
                         "condition": actual.get("condition"),
                         "destino_actual": actual.get("targetPlaybook")},
                **estado, "pista": "pasa playbook=... para poner el destino"}

    try:
        pb_id, pb_nombre = _resolve_playbook(cons, agent_id, playbook, proj)
    except LookupError as exc:
        return {"error": str(exc), **estado}

    destino_nuevo = _playbook_resource(agent_id, pb_id, proj)
    aviso = None
    if actual.get("targetPage") or actual.get("targetFlow"):
        aviso = ("la ruta apuntaba a una pagina o a un flow: al poner el "
                 "playbook ese destino SE BORRA (targetPage/targetFlow son "
                 "un 'oneof' con targetPlaybook)")

    if dry_run:
        return {"modo": "dry-run",
                "ruta": {"indice": index, "name": actual.get("name"),
                         "condition": actual.get("condition"),
                         "campo": f"transitionRoutes[{index}].targetPlaybook",
                         "destino_actual": actual.get("targetPlaybook"),
                         "destino_nuevo": destino_nuevo,
                         "destino_nuevo_nombre": pb_nombre},
                "aviso": aviso, **estado}

    try:
        assert_writable(proj, agent_id, CFG)
    except ReadOnlyAgentError as exc:
        return {"error": str(exc), "agente": agent_id}

    # Se quita el `oneof` (targetPage/targetFlow) del elemento de la ruta.
    try:
        grupo_actual = cons.request("GET", cons._url("/" + group_name))
        rutas_actuales = grupo_actual.get("transitionRoutes") or []
        copia = copy.deepcopy(rutas_actuales)
        copia[index].pop("targetPage", None)
        copia[index].pop("targetFlow", None)
        cuerpo = {k: v for k, v in grupo_actual.items()
                  if k not in ("etag", "createTime", "updateTime")}
        cuerpo["transitionRoutes"] = copia
        cons.patch_resource(group_name, cuerpo, update_mask="transitionRoutes",
                            project=proj)
    except Exception as exc:
        return _err(exc)

    despues = cons.request("GET", cons._url("/" + group_name))
    rutas_despues = despues.get("transitionRoutes") or []
    verificado = any(
        r.get("name") == actual.get("name")
        and r.get("targetPlaybook") == destino_nuevo
        for r in rutas_despues
    )
    return {
        "modo": "escrito",
        "agente": agent_id, "proyecto": proj,
        "grupo": group_id, "displayName": elegido.get("displayName"),
        "nivel": "grupo de rutas",
        "ruta": {"indice": index, "name": actual.get("name"),
                 "condition": actual.get("condition"),
                 "destino": destino_nuevo, "destino_nombre": pb_nombre},
        "rutas_totales": len(rutas_despues),
        "rutas": summarize(rutas_despues, pbs_inv),
        "verificado": verificado,
        "aviso": aviso,
    }


if __name__ == "__main__":
    mcp.run()
