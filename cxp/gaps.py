"""La capa que tapa los agujeros de la API publica de Dialogflow CX.

El problema (medido, no supuesto)
---------------------------------
El backend interno de la consola (`dialogflow.clients6.google.com`) y la API
publica (`dialogflow.googleapis.com`) sirven **el mismo discovery doc**, asi que
comparar los descriptores NO encuentra nada. Lo que difiere es **que campos
acepta cada uno**:

* La API publica declara en su proto `TransitionRoute{target_page,target_flow}`
  y **descarta en silencio** cualquier otro campo.
* El backend de la consola tiene un proto MAS RICO: conoce campos que la API
  publica **ni siquiera declara** (`TransitionRoute.targetPlaybook`,
  `Tool.actionSchemas`, `FormParameter.description`...).

Esos campos son **agujeros**: se pueden leer y (a veces) escribir contra el
backend de la consola, pero no se pueden pedir por la API publica.

Este modulo trae las piezas:

1. `SchemaIndex`  -> el esquema PUBLICO (el discovery doc oficial), con el
   esquema EXACTO que usa cada recurso (el doc mezcla variantes `V3` y
   `V3beta1` del mismo nombre: hay que elegir la correcta, no fusionarlas).
2. `find_gaps()`  -> recorre un recurso real y devuelve los campos que el
   backend expone y la API publica **no declara**. Se calcula **en vivo**: no
   es una lista escrita a mano, asi que envejece bien.
3. `apply_path()` / `read_path()` -> las utilidades de ruta (`a.b[0].c`) que
   permiten tocar un campo anidado sin saber nada de la forma del recurso.

Y `cxp.console_api.ConsoleApi.patch_resource()` hace el PATCH generico por
`name` de recurso, que es lo unico que hace falta para escribir en cualquier
recurso del agente sin escribir un metodo por recurso.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DISCOVERY_HOST = "dialogflow.clients6.google.com"
DISCOVERY_URL = "https://{host}/$discovery/rest?version={version}"

# Prefijos de los nombres de esquema del discovery doc. El mismo concepto
# aparece con prefijos distintos segun la version (`V3beta1`, `V3`, `V2beta1`).
SCHEMA_PREFIXES = (
    "GoogleCloudDialogflowCxV3beta1",
    "GoogleCloudDialogflowCxV3alpha1",
    "GoogleCloudDialogflowCxV3",
    "GoogleCloudDialogflowV2beta1",
    "GoogleCloudDialogflowV2",
)

# Que prefijo corresponde a cada version de la API.
PREFIX_DE_VERSION = {
    "v3beta1": "GoogleCloudDialogflowCxV3beta1",
    "v3alpha1": "GoogleCloudDialogflowCxV3alpha1",
    "v3": "GoogleCloudDialogflowCxV3",
}

# Campos que el servidor deriva y que no se pueden (ni se deben) mandar al
# escribir. Medido: mandarlos hace fallar el PATCH o no cambia nada.
DERIVED_FIELDS = frozenset({
    "etag", "createTime", "updateTime", "parent", "session", "environment",
    "derivedTransitions", "nluSettingsList", "projectNumber", "modelIdentifier",
    "referencedFlows", "referencedPlaybooks", "inlineActions", "tokenCount",
    "numTrainingPhrases", "defaultLanguageCode",
})

# Expresion de ruta: `campo`, `campo.sub`, `campo[0]`, `campo[0].sub[2]`.
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def norm_schema(name: str) -> str:
    """'GoogleCloudDialogflowCxV3beta1TransitionRoute' -> 'TransitionRoute'."""
    for pref in SCHEMA_PREFIXES:
        if name.startswith(pref):
            return name[len(pref):]
    return name


def parse_path(path: str) -> list[tuple[str, int | None]]:
    """`'a.b[0].c'` -> `[('a', None), ('b', 0), ('c', None)]`."""
    out: list[tuple[str, int | None]] = []
    for nombre, indice in _PATH_TOKEN.findall(path or ""):
        if nombre:
            out.append((nombre, None))
        elif indice != "":
            out.append(("", int(indice)))
    return out


def read_path(obj: Any, path: str) -> Any:
    """Lee el valor de una ruta. Devuelve `None` si no existe (o si la ruta
    esta vacia: una ruta vacia no apunta a ningun campo)."""
    if not path:
        return None
    cur = obj
    for nombre, indice in parse_path(path):
        if indice is not None:
            if not isinstance(cur, list) or indice >= len(cur):
                return None
            cur = cur[indice]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(nombre)
    return cur


def apply_path(obj: dict, path: str, value: Any) -> None:
    """Escribe `value` en la ruta `path` de `obj` (in-place).

    Crea los contenedores intermedios que falten. Si el ultimo paso es un
    indice y el elemento no existe, lo crea como diccionario.
    """
    partes = parse_path(path)
    if not partes:
        raise ValueError("ruta vacia")
    cur: Any = obj
    for i, (nombre, indice) in enumerate(partes):
        ultimo = (i == len(partes) - 1)
        if indice is not None:
            if not isinstance(cur, list):
                raise ValueError(f"'{path}': se esperaba una lista en {nombre!r}")
            while len(cur) <= indice:
                cur.append({})
            if ultimo:
                cur[indice] = value
            else:
                if not isinstance(cur[indice], (dict, list)):
                    cur[indice] = {}
                cur = cur[indice]
        else:
            if not isinstance(cur, dict):
                raise ValueError(f"'{path}': se esperaba un objeto en {nombre!r}")
            if ultimo:
                cur[nombre] = value
            else:
                siguiente = partes[i + 1]
                vacio: Any = [] if siguiente[1] is not None else {}
                if not isinstance(cur.get(nombre), (dict, list)):
                    cur[nombre] = vacio
                cur = cur[nombre]


def top_level_keys(path: str) -> list[str]:
    """La mascara de actualizacion que hay que mandar para tocar `path`.

    `'transitionRoutes[0].targetPlaybook'` -> `['transitionRoutes']`
    (un PATCH con `updateMask=transitionRoutes` reescribe el array entero).
    """
    partes = parse_path(path)
    return [partes[0][0]] if partes else []


def strip_derived(obj: dict, extra: Iterable[str] = ()) -> dict:
    """Copia del objeto sin los campos que el servidor deriva."""
    fuera = DERIVED_FIELDS | set(extra)
    return {k: v for k, v in obj.items() if k not in fuera}


@dataclass
class SchemaIndex:
    """El esquema PUBLICO de la API, resuelto por recurso.

    El discovery doc contiene el mismo concepto con prefijos distintos
    (`V3beta1TransitionRoute` y `V3TransitionRoute`). Fusionarlos daria falsos
    negativos (un campo declarado solo en `V3` pareceria publico). Por eso se
    guarda el nombre COMPLETO de cada esquema y se elige el que corresponde a
    la version de la API en uso, con el `$ref` del descriptor como fuente de
    verdad para saber cual usa cada recurso.
    """

    version: str = "v3beta1"
    props: dict[str, dict[str, Any]] = field(default_factory=dict)
    canonical: dict[str, str] = field(default_factory=dict)
    colecciones: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- construccion
    @classmethod
    def from_doc(cls, doc: dict[str, Any], version: str = "v3beta1") -> SchemaIndex:
        props: dict[str, dict[str, Any]] = {}
        for full, body in (doc.get("schemas") or {}).items():
            props[full] = body.get("properties") or {}

        por_corto: dict[str, list[str]] = {}
        for full in props:
            por_corto.setdefault(norm_schema(full), []).append(full)

        prefijo = PREFIX_DE_VERSION.get(version, "")
        canonical: dict[str, str] = {}
        for corto, fulls in por_corto.items():
            elegido = next((f for f in fulls if f.startswith(prefijo)), None)
            canonical[corto] = elegido or fulls[0]

        idx = cls(version=version, props=props, canonical=canonical, raw=doc)
        idx.colecciones = idx._resolver_colecciones()
        return idx

    @classmethod
    def load(cls, path: str | Path, version: str = "v3beta1") -> SchemaIndex:
        return cls.from_doc(json.loads(Path(path).read_text(encoding="utf-8")),
                            version=version)

    def _resolver_colecciones(self) -> dict[str, str]:
        """Coleccion -> nombre COMPLETO del esquema que usa la API.

        Se saca del descriptor: el `get` de cada coleccion declara en su
        `response` el `$ref` del tipo que devuelve.
        """
        out: dict[str, str] = {}

        def _camina(node: dict, camino: str = "") -> None:
            for nombre, res in (node or {}).items():
                ruta = f"{camino}.{nombre}" if camino else nombre
                metodos = res.get("methods") or {}
                ref = (metodos.get("get", {}).get("response") or {}).get("$ref")
                if ref and ".agents." in ruta:
                    out[ruta.split(".agents.")[1]] = ref
                elif ref and ruta.endswith(".agents"):
                    out["agents"] = ref
                _camina(res.get("resources") or {}, ruta)

        _camina(self.raw.get("resources") or {})
        return out

    # ----------------------------------------------------------------- consulta
    def esquema_de(self, coleccion: str) -> str:
        """El nombre completo del esquema de una coleccion ('tools' -> '...Tool')."""
        ref = self.colecciones.get(coleccion)
        if ref:
            return ref
        return self.canonical.get(coleccion, "")

    def props_de(self, esquema: str) -> dict[str, Any]:
        if esquema in self.props:
            return self.props[esquema]
        corto = norm_schema(esquema)
        lleno = self.canonical.get(corto, "")
        return self.props.get(lleno, {})

    def declared(self, esquema: str) -> set[str]:
        """Campos que la API publica declara para ese esquema."""
        return set(self.props_de(esquema))

    def sub_schema(self, esquema: str, campo: str) -> str:
        """El nombre COMPLETO del esquema del valor de un campo ('' si no lo hay)."""
        pv = self.props_de(esquema).get(campo)
        if not pv:
            return ""
        ref = pv.get("$ref") or (pv.get("items") or {}).get("$ref")
        if ref:
            return ref.split("/")[-1]
        return ""


@dataclass
class Gap:
    """Un campo que el backend expone y la API publica no declara."""

    schema: str          # esquema donde vive ("" si no se pudo resolver)
    campo: str           # nombre del campo
    ruta: str            # ruta completa dentro del recurso ("advancedSettings.enabled")
    valor: Any = None    # valor real (recortado)
    solo_lectura: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = {"esquema": norm_schema(self.schema) if self.schema else "",
             "campo": self.campo, "ruta": self.ruta}
        if self.valor is not None:
            d["valor"] = self.valor
        if self.solo_lectura:
            d["solo_lectura"] = True
        return d


def _recortar(valor: Any, limite: int = 200) -> Any:
    try:
        texto = json.dumps(valor, ensure_ascii=False)
    except (TypeError, ValueError):
        texto = str(valor)
    if len(texto) > limite:
        return texto[:limite] + "..."
    return valor


def find_gaps(obj: Any, esquema: str, idx: SchemaIndex,
              prefijo: str = "") -> list[Gap]:
    """Campos de `obj` que la API publica NO declara.

    Se recorre el JSON real contra el esquema publico: en cuanto un campo no
    esta declarado, se anota y **no se baja mas** (su forma interna es
    desconocida para el esquema publico).
    """
    salida: list[Gap] = []
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            salida.extend(find_gaps(item, esquema, idx, f"{prefijo}[{i}]"))
        return salida
    if not isinstance(obj, dict):
        return salida
    declarados = idx.declared(esquema)
    for campo, valor in obj.items():
        ruta = f"{prefijo}.{campo}" if prefijo else campo
        if campo not in declarados:
            salida.append(Gap(schema=esquema, campo=campo, ruta=ruta,
                              valor=_recortar(valor),
                              solo_lectura=campo in DERIVED_FIELDS))
            continue
        sub = idx.sub_schema(esquema, campo)
        if sub:
            salida.extend(find_gaps(valor, sub, idx, ruta))
    return salida


def agrupar_gaps(gaps: Iterable[Gap]) -> list[dict[str, Any]]:
    """Agrupa los huecos por (esquema, campo) y cuenta donde aparece cada uno."""
    cuenta: dict[tuple[str, str], dict[str, Any]] = {}
    for g in gaps:
        clave = (norm_schema(g.schema) if g.schema else "?", g.campo)
        if clave not in cuenta:
            cuenta[clave] = {"esquema": clave[0], "campo": g.campo,
                             "ejemplo": g.ruta, "apariciones": 0,
                             "solo_lectura": g.solo_lectura}
        cuenta[clave]["apariciones"] += 1
    return sorted(cuenta.values(), key=lambda d: (d["esquema"], d["campo"]))


# --- El catalogo MEDIDO ----------------------------------------------------
# Lo que se comprobo a fuerza de PATCH y relectura contra el backend interno.
# `estado`:
#   "escribible"  -> el servidor lo CONSERVA (verificado releyendo)
#   "derivado"    -> el servidor lo CALCULA: se lee pero no se puede escribir
#   "solo_lectura"-> el backend lo expone y la API publica ni lo declara
CATALOGO_MEDIDO: list[dict[str, Any]] = [
    {
        "campo": "transitionRoutes[].targetPlaybook",
        "esquema": "TransitionRoute",
        "recursos": ["Page", "Flow", "TransitionRouteGroup"],
        "estado": "escribible",
        "nota": "El `target` publico es un `oneof` con `target_page` y "
                "`target_flow`: `target_playbook` NO existe en el proto "
                "publico y un PATCH por la API publica se descarta en "
                "silencio (la ruta queda sin destino). El backend de la "
                "consola si lo conserva.",
        "herramienta": "cx_set_route_playbook, cx_set_flow_route_playbook, "
                       "cx_set_group_route_playbook",
    },
    {
        "campo": "form.parameters[].description",
        "esquema": "FormParameter",
        "recursos": ["Page"],
        "estado": "escribible",
        "nota": "La API publica declara `FormParameter` sin `description`: la "
                "descripcion de un parametro de formulario no se puede poner "
                "ni leer por API publica.",
        "herramienta": "cx_set_console_field",
    },
    {
        "campo": "entities[].dtmfPattern",
        "esquema": "EntityTypeEntity",
        "recursos": ["EntityType"],
        "estado": "escribible",
        "nota": "El patron DTMF de una entidad no existe en el proto publico. "
                "Si se escribe, el servidor lo conserva (medido).",
        "herramienta": "cx_set_console_field",
    },
    {
        "campo": "webClientSettings",
        "esquema": "Agent",
        "recursos": ["Agent"],
        "estado": "escribible",
        "nota": "Ajustes del cliente web de la consola (por ejemplo "
                "`boxBorderRadius`, que es un STRING). No estan en el proto "
                "publico.",
        "herramienta": "cx_set_console_field",
    },
    {
        "campo": "actionSchemas",
        "esquema": "Tool",
        "recursos": ["Tool"],
        "estado": "derivado",
        "nota": "Los esquemas de accion del tool (hasta 23 en un tool real) "
                "existen solo en la consola. El servidor los DERIVA del "
                "`openApiSpec` o del `dataStoreSpec`: escribir el array no "
                "cambia nada (medido).",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "advancedSettings.enabled / .playbackInterruptionSettings "
                 "/ .speechAdaptationSettings",
        "esquema": "AdvancedSettings",
        "recursos": ["Agent", "Page", "Flow"],
        "estado": "derivado",
        "nota": "Se leen, pero al escribir el mensaje el servidor vacia los "
                "subcampos desconocidos: `enabled` desaparece y "
                "`playbackInterruptionSettings` queda en `{}` (medido).",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "speakerIdSettings",
        "esquema": "Agent",
        "recursos": ["Agent", "Page"],
        "estado": "derivado",
        "nota": "El servidor lo devuelve como `{}` y descarta lo que se le "
                "escriba dentro.",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "modelIdentifier / nluSettingsList / nluSettings.customConfigKey",
        "esquema": "Flow",
        "recursos": ["Flow"],
        "estado": "derivado",
        "nota": "El backend los deriva y los regenera: escribir "
                "`customConfigKey` devuelve una clave nueva (medido).",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "trainingPhrases[].parts[].auto",
        "esquema": "IntentTrainingPhrasePart",
        "recursos": ["Intent"],
        "estado": "derivado",
        "nota": "Marca la frase como autogenerada. Se lee; escribirlo no "
                "cambia nada (medido).",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "dataStoreSpec.models / .groundingSettings",
        "esquema": "ToolDataStoreTool",
        "recursos": ["Tool"],
        "estado": "derivado",
        "nota": "El modelo de resumido del tool de data store y sus ajustes "
                "de grounding solo existen en la consola.",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "projectNumber",
        "esquema": "Agent",
        "recursos": ["Agent"],
        "estado": "solo_lectura",
        "nota": "El numero de proyecto que ve la consola. No esta en el proto "
                "publico.",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "speechAdaptationSettings",
        "esquema": "Playbook",
        "recursos": ["Playbook"],
        "estado": "derivado",
        "nota": "Ajustes de adaptacion de voz del playbook: solo en la "
                "consola.",
        "herramienta": "cx_gaps (lectura)",
    },
]


def catalogo() -> list[dict[str, Any]]:
    """El catalogo medido, como lista de dicts (para el MCP)."""
    return [dict(c) for c in CATALOGO_MEDIDO]


# Colecciones que se listan con su metodo propio en `ConsoleApi`.
COLECCIONES = ("intents", "entityTypes", "tools", "webhooks", "generators",
               "transitionRouteGroups", "playbooks")

# Coleccion -> metodo de `ConsoleApi` que la lista.
METODO_DE_COLECCION = {
    "intents": "list_intents",
    "entityTypes": "list_entity_types",
    "tools": "list_tools",
    "webhooks": "list_webhooks",
    "generators": "list_generators",
    "transitionRouteGroups": "list_transition_route_groups",
    "playbooks": "list_playbooks",
}


def load_schema_index(version: str = "v3beta1", cache_dir: Path | None = None,
                      timeout: float = 30.0) -> SchemaIndex:
    """El esquema PUBLICO de la API, del discovery doc oficial.

    Se descarga una vez y se cachea en disco. **No hace falta autenticacion**
    para leerlo: el descriptor es publico.
    """
    cache_dir = cache_dir or (Path(__file__).resolve().parent.parent / ".discovery")
    cache = Path(cache_dir) / f"{version}.json"
    if cache.exists():
        try:
            return SchemaIndex.from_doc(
                json.loads(cache.read_text(encoding="utf-8")), version=version)
        except (OSError, ValueError):
            pass
    import urllib.request
    url = DISCOVERY_URL.format(host=DISCOVERY_HOST, version=version)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        texto = resp.read().decode("utf-8")
    doc = json.loads(texto)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(texto, encoding="utf-8")
    except OSError:
        pass
    return SchemaIndex.from_doc(doc, version=version)


def recurso_kind(name: str) -> str:
    """El tipo de recurso deducido de su `name` ('.../flows/{f}' -> 'flow')."""
    partes = (name or "").split("/")
    for i, p in enumerate(partes):
        if p == "agents" and i + 1 < len(partes):
            for q in partes[i + 2:]:
                if q in COLECCIONES:
                    return q
    return ""


def scan_agent(cons, agent_id: str, project: str = "",
               idx: SchemaIndex | None = None,
               incluir_paginas: bool = True,
               max_recursos: int = 25) -> dict[str, Any]:
    """Recorre un agente y devuelve los huecos que ve, EN VIVO.

    No usa ninguna lista escrita a mano: compara el JSON que devuelve el
    backend con el esquema publico v3beta1, recurso a recurso. Asi, si Google
    anade un campo nuevo a la consola, el MCP lo detecta sin tocar el codigo.

    Devuelve:
        {"agente":..., "recursos": {tipo: n}, "huecos": [...], "por_recurso": {...}}
    """
    idx = idx or load_schema_index(cons.cfg.api_version)
    base = f"/projects/{project or cons.cfg.project}/locations/{cons.cfg.location}"
    huecos: list[Gap] = []
    recursos: dict[str, int] = {}
    por_recurso: list[dict[str, Any]] = []

    def _anota(tipo: str, nombre: str, obj: Any, coleccion: str) -> None:
        nonlocal huecos
        recursos[tipo] = recursos.get(tipo, 0) + 1
        esquema = idx.esquema_de(coleccion)
        g = find_gaps(obj, esquema, idx)
        if g:
            por_recurso.append({"tipo": tipo, "recurso": nombre,
                                "esquema": norm_schema(esquema), "huecos": len(g)})
        huecos.extend(g)

    # --- agente ---
    ag = cons.request("GET", cons._url(f"{base}/agents/{agent_id}"))
    _anota("agent", agent_id, ag, "agents")

    # --- flows (y sus paginas) ---
    for f in cons.list_flows(agent_id, project)[:max_recursos]:
        fid = (f.get("name") or "").rsplit("/", 1)[-1]
        fl = cons.get_flow(agent_id, fid, project)
        _anota("flow", fid, fl, "flows")
        if not incluir_paginas:
            continue
        for pg in cons.list_pages(agent_id, fid, project)[:max_recursos]:
            pid = (pg.get("name") or "").rsplit("/", 1)[-1]
            _anota("page", pid, cons.get_page(agent_id, fid, pid, project), "flows.pages")

    # --- colecciones planas ---
    for coll in COLECCIONES:
        try:
            getter = getattr(cons, METODO_DE_COLECCION.get(coll, ""), None)
            if getter is None:
                continue
            items = getter(agent_id, project)[:max_recursos]
        except Exception:
            continue
        for item in items:
            _anota(coll, (item.get("displayName") or item.get("name") or ""),
                   item, coll)

    return {
        "agente": agent_id,
        "proyecto": project or cons.cfg.project,
        "recursos_escaneados": recursos,
        "huecos": agrupar_gaps(huecos),
        "por_recurso": por_recurso,
    }
