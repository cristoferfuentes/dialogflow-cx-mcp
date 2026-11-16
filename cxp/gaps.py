"""The layer that covers the gaps of the public API of Dialogflow CX.

The problem (measured, not assumed)
-----------------------------------
The internal backend of the console (`dialogflow.clients6.google.com`) and the public API
(`dialogflow.googleapis.com`) serve **the same discovery doc**, so
comparing the descriptors finds NOTHING. What differs is **which fields
each one accepts**:

* The public API declares in its proto `TransitionRoute{target_page,target_flow}`
  and **silently drops** any other field.
* The console backend has a RICHER proto: it knows fields that the public API
  **does not even declare** (`TransitionRoute.targetPlaybook`,
  `Tool.actionSchemas`, `FormParameter.description`...).

Those fields are **gaps**: they can be read and (sometimes) written against the
console backend, but they cannot be requested through the public API.

This module brings the pieces:

1. `SchemaIndex`  -> the PUBLIC schema (the official discovery doc), with the
   EXACT schema that each resource uses (the doc mixes `V3` and
   `V3beta1` variants of the same name: the correct one must be chosen, not merged).
2. `find_gaps()`  -> walks a real resource and returns the fields that the
   backend exposes and the public API **does not declare**. It is computed **live**: it is not
   a hand-written list, so it ages well.
3. `apply_path()` / `read_path()` -> the route utilities (`a.b[0].c`) that
   allow touching a nested field without knowing anything about the resource's shape.

And `cxp.console_api.ConsoleApi.patch_resource()` does the generic PATCH per
`name` of resource, which is all that is needed to write to any
resource of the agent without writing one method per resource."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DISCOVERY_HOST = "dialogflow.clients6.google.com"
DISCOVERY_URL = "https://{host}/$discovery/rest?version={version}"

# Schema name prefixes in the discovery doc. The same concept
# appears with different prefixes depending on the version (`V3beta1`, `V3`, `V2beta1`).
SCHEMA_PREFIXES = (
    "GoogleCloudDialogflowCxV3beta1",
    "GoogleCloudDialogflowCxV3alpha1",
    "GoogleCloudDialogflowCxV3",
    "GoogleCloudDialogflowV2beta1",
    "GoogleCloudDialogflowV2",
)

# Which prefix corresponds to each version of API.
PREFIX_DE_VERSION = {
    "v3beta1": "GoogleCloudDialogflowCxV3beta1",
    "v3alpha1": "GoogleCloudDialogflowCxV3alpha1",
    "v3": "GoogleCloudDialogflowCxV3",
}

# Fields the server derives that cannot (and should not) be sent on
# write. Measured: sending them makes PATCH fail or changes nothing.
DERIVED_FIELDS = frozenset({
    "etag", "createTime", "updateTime", "parent", "session", "environment",
    "derivedTransitions", "nluSettingsList", "projectNumber", "modelIdentifier",
    "referencedFlows", "referencedPlaybooks", "inlineActions", "tokenCount",
    "numTrainingPhrases", "defaultLanguageCode",
})

# Route expression: `campo`, `campo.sub`, `campo[0]`, `campo[0].sub[2]`.
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
    """Read the value of a route. Returns `None` if it does not exist (or if the route
    is empty: an empty route does not point to any field)."""
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
    """Write `value` to route `path` of `obj` (in-place).

    Create any missing intermediate containers. If the last step is an
    index and the element does not exist, create it as a dictionary."""
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
    """The update mask to send to touch `path`.

    `'transitionRoutes[0].targetPlaybook'` -> `['transitionRoutes']`
    (a PATCH with `updateMask=transitionRoutes` rewrites the entire array)."""
    partes = parse_path(path)
    return [partes[0][0]] if partes else []


def strip_derived(obj: dict, extra: Iterable[str] = ()) -> dict:
    """Copy of the object without the fields the server derives."""
    fuera = DERIVED_FIELDS | set(extra)
    return {k: v for k, v in obj.items() if k not in fuera}


@dataclass
class SchemaIndex:
    """The PUBLIC schema of the API, resolved per resource.

    The discovery doc contains the same concept with different prefixes
    (`V3beta1TransitionRoute` and `V3TransitionRoute`). Merging them would give false
    negatives (a field declared only in `V3` would look public). That is why
    the COMPLETE name of each schema is kept and the one matching
    the version of the API in use is chosen, with the `$ref` of the discovery doc as the source of
    truth for knowing which one each resource uses."""

    version: str = "v3beta1"
    props: dict[str, dict[str, Any]] = field(default_factory=dict)
    canonical: dict[str, str] = field(default_factory=dict)
    colecciones: dict[str, str] = field(default_factory=dict)
    listas: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- construction
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
        idx.colecciones, idx.listas = idx._resolver_colecciones()
        return idx

    @classmethod
    def load(cls, path: str | Path, version: str = "v3beta1") -> SchemaIndex:
        return cls.from_doc(json.loads(Path(path).read_text(encoding="utf-8")),
                            version=version)

    def _resolver_colecciones(self) -> tuple[dict[str, str], dict[str, str]]:
        """(item, list) -> COMPLETE name of each collection's schema.

        Taken from the discovery doc, which declares TWO distinct types:

        * each collection's `get` -> the **item** schema
          (`testCases.results` -> `TestCaseResult`);
        * the `list` -> the **WRAPPER** schema
          (`testCases.results` -> `ListTestCaseResultsResponse`, which has
          `testCaseResults: [TestCaseResult]`).

        MEASURED (2026-11-15): sweeping the wrapper with the item schema (or vice
        versa) produces **false positives**: `testCaseResults` comes out as
        "campo no declarado" if the wrapper is compared against `TestCaseResult`.
        That is why the two are stored separately."""
        items: dict[str, str] = {}
        listas: dict[str, str] = {}

        def _camina(node: dict, camino: str = "") -> None:
            for nombre, res in (node or {}).items():
                ruta = f"{camino}.{nombre}" if camino else nombre
                metodos = res.get("methods") or {}
                ref_item = (metodos.get("get", {}).get("response") or {}).get("$ref")
                ref_lista = (metodos.get("list", {}).get("response") or {}).get("$ref")
                clave = ""
                if ".agents." in ruta:
                    clave = ruta.split(".agents.")[1]
                elif ruta.endswith(".agents"):
                    clave = "agents"
                if clave:
                    if ref_item:
                        items[clave] = ref_item
                    if ref_lista:
                        listas[clave] = ref_lista
                _camina(res.get("resources") or {}, ruta)

        _camina(self.raw.get("resources") or {})
        return items, listas

    # ----------------------------------------------------------------- query
    def esquema_de(self, coleccion: str) -> str:
        """The full schema name of the ITEM of a collection.

        ('tools' -> '...Tool'; 'testCases.results' -> '...TestCaseResult')"""
        ref = self.colecciones.get(coleccion)
        if ref:
            return ref
        return self.canonical.get(coleccion, "")

    def esquema_lista_de(self, coleccion: str) -> str:
        """The full schema name of the WRAPPER of a listing.

        ('tools' -> '...ListToolsResponse'; '' if the discovery doc does not declare it)."""
        return self.listas.get(coleccion, "")

    def item_de_lista(self, esquema_lista: str) -> str:
        """The ITEM schema inside a listing wrapper ('' if none)."""
        pv = self.props_de(esquema_lista)
        for _campo, spec in pv.items():
            ref = (spec.get("items") or {}).get("$ref")
            if ref:
                return ref.split("/")[-1]
        return ""

    def props_de(self, esquema: str) -> dict[str, Any]:
        if esquema in self.props:
            return self.props[esquema]
        corto = norm_schema(esquema)
        lleno = self.canonical.get(corto, "")
        return self.props.get(lleno, {})

    def declared(self, esquema: str) -> set[str]:
        """Fields that the published API declares for that schema."""
        return set(self.props_de(esquema))

    def sub_schema(self, esquema: str, campo: str) -> str:
        """The schema name COMPLETE of a field's value ('' if absent)."""
        pv = self.props_de(esquema).get(campo)
        if not pv:
            return ""
        ref = pv.get("$ref") or (pv.get("items") or {}).get("$ref")
        if ref:
            return ref.split("/")[-1]
        return ""


@dataclass
class Gap:
    """A field the backend exposes and API publishes does not declare."""

    schema: str          # schema where it lives ("" if it could not be resolved)
    campo: str           # field name
    ruta: str            # full route within the resource ("advancedSettings.enabled")
    valor: Any = None    # actual value (trimmed)
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
    """Fields of `obj` that the API publishes but does NOT declare.

    The real JSON is traversed against the public schema: as soon as a field is
    not declared, it is noted and **no further descent** is made (its internal
    shape is unknown to the public schema)."""
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
    """Group the gaps by (schema, field) and count where each one appears."""
    cuenta: dict[tuple[str, str], dict[str, Any]] = {}
    for g in gaps:
        clave = (norm_schema(g.schema) if g.schema else "?", g.campo)
        if clave not in cuenta:
            cuenta[clave] = {"esquema": clave[0], "campo": g.campo,
                             "ejemplo": g.ruta, "apariciones": 0,
                             "solo_lectura": g.solo_lectura}
        cuenta[clave]["apariciones"] += 1
    return sorted(cuenta.values(), key=lambda d: (d["esquema"], d["campo"]))


def scan_coleccion(datos: Any, idx: SchemaIndex, coleccion: str) -> dict[str, Any]:
    """Sweeps what a listing OR a loose resource returns, with its schema.

    MEASURED (2026-11-15): a listing returns a WRAPPER
    (`{"testCaseResults": [...]}`) whose schema differs from that of the ITEM
    (`TestCaseResult`). Sweeping the wrapper with the item schema gives false
    positives; sweeping it with the wrapper schema **only descends** to the items
    (because the array field declares its `$ref`), so a single traversal
    covers both.

    And the reverse: sweeping a LOOSE resource (a `GET {name}`) with the wrapper
    schema also gives false positives (all its fields come out "no
    declarados"). That is why it **auto-detects**
    whether the response carries the wrapper's array field: if it does, it is a listing; if not, it is an item.

    Returns {"envoltorio": [...], "items": [...], "n_items": n}."""
    env_ref = idx.esquema_lista_de(coleccion)
    item_ref = idx.esquema_de(coleccion)

    # Is the response a LISTING or a loose resource?
    es_lista = False
    if env_ref and isinstance(datos, dict):
        for campo, spec in idx.props_de(env_ref).items():
            if (spec.get("items") or {}).get("$ref"):
                es_lista = campo in datos
                break

    if es_lista:
        huecos = find_gaps(datos, env_ref, idx)
        env: list[Gap] = []
        items: list[Gap] = []
        for g in huecos:
            (items if g.schema and g.schema == item_ref else env).append(g)
        return {"envoltorio": agrupar_gaps(env), "items": agrupar_gaps(items),
                "n_items": _cuenta_items(datos, env_ref, idx),
                "esquema_envoltorio": norm_schema(env_ref),
                "esquema_item": norm_schema(item_ref)}

    # Loose resource (or an already unwrapped list of resources).
    lista = datos if isinstance(datos, list) else [datos]
    huecos = []
    for it in lista:
        huecos.extend(find_gaps(it, item_ref, idx))
    return {"envoltorio": [], "items": agrupar_gaps(huecos),
            "n_items": len(lista),
            "esquema_envoltorio": "", "esquema_item": norm_schema(item_ref)}


def _cuenta_items(datos: Any, env_ref: str, idx: SchemaIndex) -> int:
    """How many items the wrapper carries (0 if it is not a listing)."""
    if not isinstance(datos, dict):
        return 0
    for _campo, spec in idx.props_de(env_ref).items():
        if (spec.get("items") or {}).get("$ref"):
            v = datos.get(_campo)
            return len(v) if isinstance(v, list) else 0
    return 0


# --- The MEASURED catalog ----------------------------------------------------
# What was verified by dint of PATCH and re-read against the internal backend.
# `estado`:
#   "escribible"  -> the server KEEPS it (verified by reading it back)
#   "derivado"    -> the server COMPUTES it: it can be read but not written
#   "solo_lectura"-> the backend exposes it and the public API does not even declare it
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
        "campo": "form.parameters[].convertPhoneticAlphabets",
        "esquema": "FormParameter",
        "recursos": ["Page"],
        "estado": "escribible",
        "nota": "MEDIDO (2026-11-15, agente de laboratorio): el flag de "
                "conversion de alfabetos foneticos del parametro de "
                "formulario se CONSERVA al escribir y releer, y no esta "
                "declarado en ningun descriptor.",
        "herramienta": "cx_set_console_field",
    },
    {
        "campo": "intents[].parameters[].description",
        "esquema": "IntentParameter",
        "recursos": ["Intent"],
        "estado": "escribible",
        "nota": "MEDIDO (2026-11-15, agente de laboratorio): la descripcion de "
                "un parametro de intent se conserva al escribir y releer. El "
                "proto publico declara `IntentParameter` sin `description`.",
        "herramienta": "cx_set_console_field",
    },
    {
        "campo": "advancedSettings.loggingSettings.disableLogging",
        "esquema": "AdvancedSettingsLoggingSettings",
        "recursos": ["Agent", "Page"],
        "estado": "descartado",
        "nota": "MEDIDO (2026-11-15): al hacer PATCH de `advancedSettings` el "
                "servidor VACIA el submensaje (`loggingSettings` queda en "
                "`{}`). Es el mismo comportamiento que `advancedSettings.enabled`.",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "entryFulfillment.messages[].text.ssml",
        "esquema": "ResponseMessageText",
        "recursos": ["Page"],
        "estado": "descartado",
        "nota": "MEDIDO (2026-11-15): el proto interno tiene `ssml` en "
                "`ResponseMessageText`, pero al mandarlo en "
                "`entryFulfillment.messages[].text` el servidor lo descarta "
                "(deja solo `text`).",
        "herramienta": "cx_gaps (lectura)",
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
    {
        "campo": "etag",
        "esquema": "Environment",
        "recursos": ["Environment"],
        "estado": "derivado",
        "nota": "El `etag` del entorno lo calcula el servidor (medido en el "
                "agente de laboratorio: el `GET` lo devuelve y el proto "
                "publico de `Environment` no lo declara).",
        "herramienta": "cx_gaps (lectura)",
    },
    {
        "campo": "actualValue / expectedValue / assertionType / warning",
        "esquema": "TestRunDifference",
        "recursos": ["TestCaseResult"],
        "estado": "derivado",
        "nota": "El proto publico de `TestRunDifference` solo declara `type` "
                "y `description`: por API publica NO se puede saber **que** "
                "difiere en un test case fallido (ni el valor esperado ni el "
                "obtenido ni el aviso). Solo sale al ejecutar el caso, y "
                "solo si hay diferencias.",
        "herramienta": "cx_gaps (lectura, agente de laboratorio)",
    },
]

# The THREE RULES to create an environment (measured on 2026-11-15).
# There is no new field: the public API says that `versionConfigs` is a
# list of `{version}`, and `version` is a GENERIC resource name. What it
# does NOT say (and that is why it fails) is that you must include the ALL version of the graph.
REGLAS_ENTORNO: list[dict[str, Any]] = [
    {
        "regla": "versionConfigs[].version",
        "que": "La version de CADA flow del agente (y tambien la del 'start "
               "flow').",
        "error": "Flow 'X' is referenced in version 'V' of flow 'F', but not "
                 "included in the environment.",
    },
    {
        "regla": "versionConfigs[].version",
        "que": "La version de CADA playbook que referencien las rutas (cierre "
               "transitivo). El nombre de un playbook es "
               "'.../playbooks/{id}/versions/{v}'.",
        "error": "Playbook 'P' is referenced in version 'V' of flow 'F', but "
                 "not included in the environment.",
    },
    {
        "regla": "versionConfigs[].version",
        "que": "La version de CADA tool que referencien esos playbooks. El "
               "nombre de un tool es '.../tools/{id}/versions/{v}'.",
        "error": "Tool 'T' is referenced in version 'V' of playbook 'P', but "
                 "not included in the environment.",
    },
]


def catalogo() -> list[dict[str, Any]]:
    """The measured catalog, as a list of dicts (for the MCP)."""
    return [dict(c) for c in CATALOGO_MEDIDO]


# Collections that, in the INTERNAL proto, exist only nested under an
# environment. MEASURED (2026-11-15): `GET agents/{a}/sessions/{sid}/entityTypes`
# responds **404**: the internal name template is
# `projects/*/locations/*/agents/*/environments/*/sessions/*/entityTypes/*`.
# The public discovery doc declares the route at agent level, but it does not exist there.
COLECCIONES_SOLO_EN_ENTORNO = frozenset({"sessions.entityTypes"})

# Collections listed with their own method in `ConsoleApi`.
COLECCIONES = ("intents", "entityTypes", "tools", "webhooks", "generators",
               "transitionRouteGroups", "playbooks")

# Collection -> `ConsoleApi` method that lists it.
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
    """The PUBLIC schema of the API, from the official discovery doc.

    It is downloaded once and cached on disk. **No authentication is needed**
    to read it: the discovery doc is public."""
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
    """The resource type inferred from its `name` ('.../flows/{f}' -> 'flow')."""
    partes = (name or "").split("/")
    for i, p in enumerate(partes):
        if p == "agents" and i + 1 < len(partes):
            for q in partes[i + 2:]:
                if q in COLECCIONES:
                    return q
    return ""


def colecciones_listables(idx: SchemaIndex) -> dict[str, str]:
    """Collection -> path suffix of `list`, as declared by the discovery doc.

    It is **computed** from the discovery doc (not a hand-written list): the
    resources are traversed and those having a `list` with `{+parent}` and hanging
    off an agent are kept. Thus, if Google adds a new collection, the sweep sees it
    without touching the code.

    Example: `flows.pages` -> `pages` (the list hangs off a flow)."""
    out: dict[str, str] = {}

    def _camina(node: dict, camino: str = "") -> None:
        for nombre, res in (node or {}).items():
            ruta = f"{camino}.{nombre}" if camino else nombre
            path = ((res.get("methods") or {}).get("list") or {}).get("path") or ""
            if ".agents." in ruta and "/{+parent}/" in path:
                sufijo = path.split("/{+parent}/", 1)[1].split("/")[0]
                out[ruta.split(".agents.", 1)[1]] = sufijo
            _camina(res.get("resources") or {}, ruta)

    _camina(idx.raw.get("resources") or {})
    return out


def _listar(cons, parent: str, sufijo: str,
            page_size: str = "100") -> list[dict]:
    """`GET {parent}/{sufijo}` -> the list of items (empty if there are none).

    MEASURED: the console's internal backend **caps `pageSize` at 100** in
    several endpoints ("Page size should be within [0, 100]" -> HTTP 400), while the public discovery doc says
    nothing. If the server rejects it, it is retried with fewer (50, 20)
    before giving up."""
    path = f"{parent}/{sufijo}"
    if not path.startswith("/"):
        path = "/" + path
    ultimo: Exception | None = None
    for size in (page_size, "50", "20", "5"):
        url = cons._url(path, pageSize=size)
        try:
            data = cons.request("GET", url)
        except Exception as exc:
            ultimo = exc
            if "Page size" not in str(exc):
                raise
            continue
        if not isinstance(data, dict):
            return []
        items = data.get(sufijo)
        if items is None:
            for valor in data.values():
                if isinstance(valor, list):
                    items = valor
                    break
        return items if isinstance(items, list) else []
    raise ultimo or RuntimeError(f"no se pudo listar {path}")


def _nota_de_coleccion(clave: str) -> dict[str, str]:
    """Explain why a collection cannot be reached at the agent level."""
    if clave in COLECCIONES_SOLO_EN_ENTORNO:
        return {"nota": "en el proto INTERNO esta coleccion solo existe anidada "
                       "bajo un entorno (environments/{id}/sessions/{sid}/...): "
                       "a nivel de agente la ruta da 404. Se barre en el "
                       "agente de laboratorio (environments.sessions.entityTypes).",
                "equivalente_en_entorno": "environments.sessions.entityTypes"}
    return {}


def scan_agent(cons, agent_id: str, project: str = "",
               idx: SchemaIndex | None = None,
               incluir_paginas: bool = True,
               max_recursos: int = 25,
               max_padres: int = 3) -> dict[str, Any]:
    """Walk an agent and return the gaps it sees, LIVE.

    It uses no hand-written list: it compares the JSON returned by the
    backend with the public v3beta1 schema, resource by resource. Thus, if Google
    adds a new field to the console, the MCP detects it without touching the code.

    The walk is **generic**: it lists ALL the collections the
    descriptor declares under an agent (`colecciones_listables()`), including
    the nested ones (`flows.pages`, `tools.versions`, `playbooks.examples`,
    `environments.deployments`, `testCases.results`...). For the nested ones the ancestor must be
    resolved (`flows.pages` needs a flow): the first one that exists is used and
    where it comes from is noted.

    Returns:
        {"agente":..., "recursos": {tipo: n}, "huecos": [...], "por_recurso": {...}}"""
    idx = idx or load_schema_index(cons.cfg.api_version)
    project = project or cons.cfg.project
    base = f"/projects/{project}/locations/{cons.cfg.location}"
    agent_name = f"{base}/agents/{agent_id}"
    huecos: list[Gap] = []
    recursos: dict[str, int] = {}
    por_recurso: list[dict[str, Any]] = []
    sin_alcanzar: list[dict[str, str]] = []
    escaneadas: list[str] = []      # collections with SOMETHING inside
    alcanzables: list[str] = []      # collections that could be listed

    def _anota(tipo: str, nombre: str, obj: Any, coleccion: str) -> None:
        nonlocal huecos
        if coleccion not in escaneadas:
            escaneadas.append(coleccion)
        recursos[tipo] = recursos.get(tipo, 0) + 1
        esquema = idx.esquema_de(coleccion)
        g = find_gaps(obj, esquema, idx)
        if g:
            por_recurso.append({"tipo": tipo, "recurso": nombre,
                                "esquema": norm_schema(esquema), "huecos": len(g)})
        huecos.extend(g)

    # --- agent ---
    ag = cons.request("GET", cons._url(f"{agent_name}"))
    _anota("agent", agent_id, ag, "agents")

    # --- ALL the discovery doc's collections ---
    for clave, sufijo in sorted(colecciones_listables(idx).items()):
        if clave == "flows.pages" and not incluir_paginas:
            continue
        # The ancestor is resolved (e.g. "flows" for "flows.pages"). SEVERAL candidates are
        # stored (up to `max_padres`): if the first has no pages, the second may.
        padres: list[str] = [agent_name]
        fallo: str | None = None
        for segmento in clave.split(".")[:-1]:
            encontrados: list[str] = []
            for p in padres:
                try:
                    items = _listar(cons, p, segmento)
                except Exception as exc:
                    fallo = str(exc)[:200]
                    break
                encontrados.extend(str(i.get("name")) for i in items
                                    if i.get("name"))
            if fallo:
                break
            if not encontrados:
                fallo = (f"no hay ningun '{segmento}' que usar como ancestro")
                break
            padres = encontrados[:max_padres]
        if fallo:
            sin_alcanzar.append({"coleccion": clave, "motivo": fallo,
                                 **_nota_de_coleccion(clave)})
            continue
        for parent in padres:
            try:
                items = _listar(cons, parent, sufijo)
            except Exception as exc:
                sin_alcanzar.append({"coleccion": clave, "motivo": str(exc)[:200],
                                     **_nota_de_coleccion(clave)})
                continue
            if clave not in alcanzables:
                alcanzables.append(clave)
            for item in items[:max_recursos]:
                etiqueta = (item.get("displayName") or item.get("name") or "")
                _anota(clave, etiqueta, item, clave)

    return {
        "agente": agent_id,
        "proyecto": project,
        "recursos_escaneados": recursos,
        "colecciones": len(colecciones_listables(idx)),
        "colecciones_escaneadas": sorted(escaneadas),
        "colecciones_alcanzables": sorted(alcanzables),
        "sin_alcanzar": sin_alcanzar,
        "huecos": agrupar_gaps(huecos),
        "por_recurso": por_recurso,
    }
