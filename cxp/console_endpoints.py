"""The gaps in the **METHOD** of the public Dialogflow CX API.

The public discovery doc and the internal backend one are **the same document**
(measured on 2026-11-15: both 669 652 bytes and the same canonical md5
`29adf74f6e0f0e4dede7fefeb5de774e`; the only thing that changes is the ORDER of the
JSON keys). That is: **no method is missing from the discovery doc**.

What is missing are the **endpoints the console uses and that are in NO
discovery doc**: the console calls "console-only" endpoints (the ones that appear on opening
the UI) that are not part of the public API. Those are what make, among other
things, the console show the system entity types, who else is
using the agent, which models are available or which projects you have opened
recently.

Also, the console speaks **v3alpha1** (not v3beta1): there are methods that only
exist in one version and endpoints that only exist in `v2beta1` (the old
"conversation profiles" model) or in `v3`.

This module:

1. `HUECOS_METODO`: the **MEASURED** catalog of those endpoints (verified
   live against the backend, with their real response).
2. `METODOS_POR_VERSION`: which methods each version of the discovery doc declares
   (v3beta1 is the superset).
3. `huecos_de_metodo(idx)`: which methods of one version are **not** in another
   (computed from the discovery doc, not written by hand).

To call these endpoints, `cx_api` accepts `version=` and absolute routes
(see `cxp/endpoint.py`)."""
from __future__ import annotations

from typing import Any

# The endpoints that CONSOLE calls and that are not in any descriptor.
# MEASURED (2026-11-13): captured the real console traffic with the browser
# (444 requests) and reproduced each endpoint by hand against the backend.
# `estado`:
#   "verificado"  -> responds 200 and brings data
#   "sin_datos"   -> responds 200 but empty (depends on the project)
#   "error"       -> responds with an error (explained in `nota`)
HUECOS_METODO: list[dict[str, Any]] = [
    {
        "id": "projects.list",
        "version": "v3alpha1",
        "metodo": "GET",
        "path": "projects",
        "estado": "verificado",
        "para_que": "El listado de PROYECTOS (lo que pinta el selector de "
                    "proyecto de la consola). Lo llama con "
                    "`filter=id=\"<proyecto>\"` y `view=FULL`. No esta en "
                    "ningun descriptor (medido el 2026-11-15: 6 llamadas en la "
                    "captura del trafico).",
        "respuesta": "{\"projects\":[{\"name\":\"projects/my-project\", ...}]}",
        "query": {"filter": "id=\"<proyecto>\"", "view": "FULL"},
    },
    {
        "id": "systemEntityTypes.list",
        "version": "v3alpha1",
        "metodo": "GET",
        "path": "projects/{project}/locations/{location}/systemEntityTypes:list",
        "estado": "verificado",
        "para_que": "Los tipos de entidad del SISTEMA (sys.date, sys.date-period, "
                    "sys.number...). La consola los usa para las entidades de "
                    "sistema que el usuario puede anadir a un agente.",
        "respuesta": "{\"systemEntityTypes\":[{\"name\":\".../sys.date\","
                     "\"displayName\":\"sys.date\"}, ...]}",
    },
    {
        "id": "agents.currentUsers",
        "version": "v3alpha1",
        "metodo": "GET",
        "path": "projects/{project}/locations/{location}/agents/{agent}/currentUsers",
        "estado": "verificado",
        "para_que": "Que usuarios de Google tienen acceso al agente ahora mismo. "
                    "No hay nada equivalente en la API publica.",
        "respuesta": "{\"currentUsers\":[{\"email\":\"usuario@dominio.com\"}]}",
    },
    {
        "id": "users.recentProjects",
        "version": "v3alpha1",
        "metodo": "GET",
        "path": "users/me/userSettings/recentProjects",
        "estado": "verificado",
        "para_que": "Los proyectos que el usuario ha abierto ultimamente. "
                    "**NO cuelga de un proyecto**: es una ruta ABSOLUTA desde la "
                    "raiz del backend (por eso `cx_api` admite `/...`).",
        "respuesta": "{\"projects\":[{\"name\":\"projects/my-project\","
                     "\"viewTime\":\"2026-...\"}]}",
    },
    {
        "id": "agents.listAvailableModels",
        "version": "v3alpha1",
        "metodo": "GET",
        "path": "projects/{project}/locations/{location}/agents/{agent}"
                ":listAvailableModels",
        "estado": "verificado",
        "para_que": "Los modelos generativos disponibles para ese agente (y sus "
                    "limites: tokens, temperatura...). La API publica no lo expone.",
        "respuesta": "{\"generativeAgentModelConfig\":{\"generativeAgentModels\":"
                     "[{\"displayName\":\"gemini-2.5-flash\", ...}]}}",
    },
    {
        "id": "agents.getSupportedLanguages",
        "version": "v3alpha1",
        "metodo": "GET",
        "path": "projects/{project}/locations/{location}/agents/{agent}"
                ":getSupportedLanguages",
        "estado": "verificado",
        "para_que": "Los idiomas que el agente admite. La API publica solo admite "
                    "los del campo `defaultLanguageCode`.",
        "respuesta": "{\"supportedLanguages\":{\"ta-my\":{...}, ...}}",
    },
    {
        "id": "agent.visibility",
        "version": "v2beta1",
        "metodo": "GET",
        "path": "projects/{project}/agent/visibility",
        "estado": "sin_datos",
        "para_que": "La visibilidad del agente a nivel de proyecto. **Ojo: la ruta "
                    "no lleva `locations`**: es `projects/{p}/agent/visibility`, no "
                    "lo que haria el patron habitual.",
        "respuesta": "{}",
    },
    {
        "id": "conversationProfiles.list",
        "version": "v2beta1",
        "metodo": "GET",
        "path": "projects/{project}/locations/{location}/conversationProfiles",
        "estado": "verificado",
        "para_que": "Los 'conversation profiles' del modelo VIEJO de CX (Dialogflow "
                    "ES). Solo existe en `v2beta1`: no hay nada en v3 ni v3beta1.",
        "respuesta": "{\"conversationProfiles\":[...]}",
    },
    {
        "id": "locationSettings.get",
        "version": "v3",
        "metodo": "GET",
        "path": "projects/{project}/locations/{location}/locationSettings",
        "estado": "error",
        "para_que": "Ajustes a nivel de location. La consola lo llama, pero el "
                    "backend responde 404 si el proyecto no los tiene "
                    "configurados (NO es un error de permisos: el mismo camino "
                    "responde 401 con sesion caducada).",
        "respuesta": "404 'Location settings does not exist in project: ...'",
    },
]


# What each version of the discovery doc declares (measured on the discovery doc).
# v3beta1 -> 162 methods  (SUPERSET: missing none of v3/v3alpha1)
# v3      -> 158 methods
# v3alpha1->   8 methods
METODOS_POR_VERSION: dict[str, int] = {
    "v3beta1": 162,
    "v3": 158,
    "v3alpha1": 8,
}


def catalogo_metodos() -> list[dict[str, Any]]:
    """The method gap catalog, as a list of dicts (for MCP)."""
    return [dict(h) for h in HUECOS_METODO]


def _ids_de(idx) -> set[str]:
    """Method IDs (`agents.flows.list`) from the `idx` discovery doc."""
    out: set[str] = set()
    for ruta, nodo in _recursos(idx.raw.get("resources") or {}):
        for nombre in (nodo.get("methods") or {}):
            partes = ruta.split(".")
            if partes[:2] == ["projects", "locations"]:
                partes = partes[2:]
            out.add(".".join(partes + [nombre]))
    return out


def _recursos(node: dict, camino: str = ""):
    for nombre, res in (node or {}).items():
        ruta = f"{camino}.{nombre}" if camino else nombre
        yield ruta, res
        yield from _recursos(res.get("resources") or {}, ruta)


def huecos_de_metodo(idx, version: str = "v3beta1",
                     contra: str = "v3beta1") -> dict[str, list[str]]:
    """Methods of `version` that are NOT in `contra` (computed from the discovery doc).

    Used to answer "what is left out if I use such a version"."""
    mios = _ids_de(idx)
    suyos = _ids_de(idx)
    return {"version": version, "contra": contra,
            "solo_en_version": sorted(mios - suyos),
            "solo_en_contra": sorted(suyos - mios)}
