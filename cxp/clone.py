"""Clonado 1:1 de un agente: se limpia el clon y se recrea el original dentro.

Todo pasa por `ConsoleApi` (el mismo camino que el resto del MCP) y la guarda
de permisos vive en la capa baja: **solo se escribe en el agente destino**.

MEDIDAS que condicionan el diseno (2026-11-08, este host)
----------------------------------------------------------
* el backend **serializa las escrituras** del mismo agente: el rendimiento se
  estanca en ~1,5-2 ops/s sea cual sea la concurrencia (curva medida: POST
  1->1,57 | 2->1,01 | 4->1,34 | 8->1,12 ops/s). Por eso la **creacion va EN
  SERIE**. El **borrado** si admite 2 en paralelo (2,10 ops/s contra 1,75):
  son elementos independientes del mismo grupo.
* las **lecturas si paralelizan**: 8 GET en ~1 s (121 ms cada uno) -> van en un
  pool de hilos.
* el endpoint `/batch` **no existe** en el backend interno (HTTP 404).
* `POST flow` **ignora** las `pages` del cuerpo -> cada pagina va en su POST.
  En cambio, sus `transitionRoutes` **si** se aplican si el destino ya existe.
* la API **valida los destinos**: una ruta a una pagina inexistente -> HTTP 400.
  Por eso las paginas se crean en orden topologico (DAG de referencias) y los
  ciclos se rompen parcheando despues.
* `DELETE tool` con la tool referenciada por un playbook -> HTTP 400: por eso
  se borran los playbooks ANTES que las tools.
* como el backend serializa, **escribir menos** es el unico margen: las rutas
  de un flow van en su propio POST cuando ya son resolubles, el orden de las
  paginas minimiza los PATCH de los ciclos y los `openApiSpec` invalidos se
  detectan ANTES de enviarlo (no se paga el POST que el backend va a rechazar).
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .console_api import ConsoleApi, HttpStatusError

# Macro ${PLAYBOOK:nombre} del texto de un playbook. El backend la VALIDA al
# crear (la referencia debe existir), asi que un playbook que se menciona a si
# mismo no se puede crear de una: se crea con la macro neutralizada y se
# restaura despues con un PATCH.
MACRO_PLAYBOOK = re.compile(r"\$\{PLAYBOOK:([^}]+)\}")

DEFAULT_FLOW = "00000000-0000-0000-0000-000000000000"
DEFAULT_INTENTS = ("00000000-0000-0000-0000-000000000000",
                   "00000000-0000-0000-0000-000000000001")

# Destinos especiales de fin de flow: NO son paginas, siempre son validos
# (se pueden meter en un POST sin esperar a que exista nada).
FIN_FLOW = frozenset({"END_FLOW", "END_FLOW_WITH_HUMAN_ESCALATION"})


def rid_pagina(nombre: str) -> str:
    """Ultimo segmento de un nombre de pagina (o su marcador de fin)."""
    return (nombre or "").split("/")[-1]

# Colecciones que se clonan y en que orden se CREAN. El orden importa:
#   entityTypes -> intents -> webhooks -> tools -> playbooks -> generators
# (un playbook referencia tools; un generador podria referenciar playbooks).
COLECCIONES_CREATE = ["entityTypes", "intents", "webhooks", "tools",
                       "playbooks", "generators"]

# Orden de BORRADO: primero los playbooks (referencian las tools), luego las
# flows (se van sus paginas), y despues el resto.
COLECCIONES_DELETE = ["playbooks", "flows", "tools", "webhooks",
                      "intents", "entityTypes", "generators"]

# Campos que el servidor devuelve pero que no se pueden (o no se deben) escribir.
NO_ESCRIBIR = frozenset({
    "etag", "createTime", "updateTime", "name", "parent", "session",
    "environment", "derivedTransitions", "nluSettingsList", "tokenCount",
    "projectNumber", "supportedLanguageCodes",
})


def _suffix(resource: str, rid: str) -> str:
    return f"{resource}/{rid}"


class Cloner:
    """Clonador 1:1 de un agente (origen = solo lectura, destino = escribible)."""

    def __init__(self, cons: ConsoleApi, origen: str, destino: str, project: str = "",
                 lecturas: int = 8, escrituras: int = 1):
        self.cons = cons
        self.origen = origen
        self.destino = destino
        self.project = project or cons.cfg.project
        self.location = cons.cfg.location
        self.lecturas = max(1, lecturas)
        self.escrituras = max(1, escrituras)
        self.acciones: list[dict[str, Any]] = []
        self.mapa: dict[str, str] = {}      # "pages/viejo" -> "pages/nuevo"
        self.errores: list[dict[str, Any]] = []
        self._lock = threading.Lock()       # el borrado va con 2 hilos
        self._t_fases: dict[str, float] = {}
        self._pendientes_pagina: list[tuple[str, str, list, list]] = []
        self._flows_con_patch: set[str] = set()  # flows que necesitan PATCH de rutas
        self.omitidos: list[str] = []   # recursos que la API no deja recrear
        self.desviaciones: list[str] = []  # recreados con menos campos de los originales

    # ------------------------------------------------------------- utilidades
    def _base(self) -> str:
        """Path para URLs: `console_api._url()` espera '/'- inicial."""
        return f"/projects/{self.project}/locations/{self.location}/agents"

    def _pref(self, agent: str) -> str:
        """Prefijo de NOMBRE de recurso (sin '/': tal y como lo devuelve la API)."""
        return f"projects/{self.project}/locations/{self.location}/agents/{agent}/"

    def _url(self, agent: str, suffix: str = "", **params: Any) -> str:
        return self.cons._url(f"{self._base()}/{agent}{suffix}", **params)

    def _act(self, fase: str, accion: str, ms: float, http: int = 200,
             ok: bool = True, detalle: str = "") -> dict[str, Any]:
        row = {"fase": fase, "accion": accion, "ms": round(ms, 1),
               "http": http, "ok": ok, "detalle": detalle[:700]}
        if not ok:
            # Anota el error con el limite conocido que lo explica (memoria
            # del MCP: `cx_limites_api`).
            from .limits import causa_de
            causa = causa_de(detalle)
            if causa:
                row["limite"] = causa["id"]
                row["causa"] = causa["causa"]
                row["workaround"] = causa["workaround"]
        self.acciones.append(row)
        with self._lock:
            self._t_fases[fase] = self._t_fases.get(fase, 0.0) + ms
        if not ok:
            self.errores.append(row)
        return row

    def _get(self, fase: str, accion: str, agent: str, suffix: str,
             params: dict | None = None, query: dict | None = None) -> Any:
        url = self._url(agent, suffix, **(params or {}))
        t0 = time.perf_counter()
        try:
            data = self.cons.request("GET", url, query=query)
            ms = (time.perf_counter() - t0) * 1000
            self._act(fase, accion, ms)
            return data
        except HttpStatusError as exc:
            ms = (time.perf_counter() - t0) * 1000
            self._act(fase, accion, ms, http=exc.status, ok=False,
                      detalle=str(exc))
            return None

    def _escribir(self, fase: str, accion: str, verb: str, url: str,
                  body: dict | None = None, query: dict | None = None,
                  reintentos: int = 0) -> Any:
        """Escritura con reintentos EN SERIE (el backend serializa igual)."""
        intento = 0
        while True:
            t0 = time.perf_counter()
            try:
                data = self.cons.request(verb, url, body=body, query=query)
                ms = (time.perf_counter() - t0) * 1000
                self._act(fase, accion, ms)
                return data
            except HttpStatusError as exc:
                ms = (time.perf_counter() - t0) * 1000
                if intento < reintentos and self._transitorio(exc):
                    intento += 1
                    time.sleep(2.0 * intento)
                    continue
                self._act(fase, accion, ms, http=exc.status, ok=False,
                          detalle=str(exc))
                return None

    @staticmethod
    def _transitorio(exc: HttpStatusError) -> bool:
        """¿El fallo es reintentable? (conexion a datastore, 429, 5xx)

        NO es reintentable cuando el data store ya no existe ("do not
        exist"): es permanente y el reintento solo gasta 2 s.
        """
        if exc.status in (429, 500, 502, 503, 504):
            return True
        if "do not exist" in exc.body or "do not exists" in exc.body:
            return False
        return "Failed to connect" in exc.body or "connect datastore" in exc.body

    # ---------------------------------------------------------------- remapeo
    def _remap_str(self, s: str) -> str:
        """Reescribe un nombre de recurso del ORIGINAL al CLON.

        Los nombres pueden ser ANIDADOS: el destino especial de fin de flow es
        `.../flows/{flow}/pages/END_FLOW` y el target de una pagina es
        `.../flows/{flow}/pages/{page}`. Hay que remapear CADA par
        `recurso/id` de la ruta, no solo el primero (si no, el flow se
        remapeaba pero la pagina de dentro se quedaba con el id del original).
        """
        pref = self._pref(self.origen)
        if not s.startswith(pref):
            return s
        partes = s[len(pref):].split("/")
        salida: list[str] = []
        i = 0
        while i < len(partes):
            if i + 1 < len(partes):
                clave = f"{partes[i]}/{partes[i + 1]}"
                nuevo = self.mapa.get(clave)
                if nuevo:
                    salida.append(nuevo)
                    i += 2
                    continue
            salida.append(partes[i])
            i += 1
        return f"{self._pref(self.destino)}{'/'.join(salida)}"

    def _remap(self, obj: Any) -> Any:
        """Sustituye referencias del agente ORIGINAL por las del CLON."""
        if isinstance(obj, str):
            return self._remap_str(obj)
        if isinstance(obj, list):
            return [self._remap(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self._remap(v) for k, v in obj.items()}
        return obj

    @staticmethod
    def _body_limpio(rec: dict[str, Any]) -> dict[str, Any]:
        """Quita lo que el servidor no acepta al crear."""
        return {k: v for k, v in rec.items() if k not in NO_ESCRIBIR}

    def _nuevo_id(self, data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        return (data.get("name") or "").split("/")[-1]

    # =========================================================== 1. SNAPSHOT
    def snapshot(self) -> dict[str, Any]:
        """Lee el agente ORIGINAL completo (lecturas en paralelo)."""
        snap: dict[str, Any] = {"agente": self.origen}
        oleada: list[tuple[str, Callable[[], Any]]] = []

        oleada.append(("agente", lambda: self._get(
            "lectura", f"GET agente {self.origen[:8]}", self.origen, "")))
        oleada.append(("flows", lambda: self._get(
            "lectura", "GET flows", self.origen, "/flows",
            query={"pageSize": 200, "languageCode": "es"})))
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "transitionRouteGroups", "generators"):
            oleada.append((coll, (lambda c=coll: self._get(
                "lectura", f"GET {c}", self.origen, f"/{c}",
                query={"pageSize": 200}))))

        with ThreadPoolExecutor(max_workers=min(self.lecturas, len(oleada))) as pool:
            for (key, _), valor in zip(oleada, pool.map(lambda t: t[1](), oleada), strict=True):
                snap[key] = valor or {}

        flows = (snap.get("flows") or {}).get("flows") or []
        snap["flows"] = flows
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "transitionRouteGroups", "generators"):
            snap[coll] = (snap.get(coll) or {}).get(coll) or []

        # Paginas de cada flow (segunda ola, en paralelo)
        oleada2: list[tuple[str, Callable[[], Any]]] = []
        for f in flows:
            fid = (f.get("name") or "").split("/")[-1]
            dn = (f.get("displayName") or "")[:24]
            oleada2.append((fid, (lambda fl=fid, dn=dn: self._get(
                "lectura", f"GET pages {dn}", self.origen,
                f"/flows/{fl}/pages",
                query={"pageSize": 200, "languageCode": "es"}))))
        paginas: dict[str, list] = {fid: [] for fid in
                                    ((f.get("name") or "").split("/")[-1] for f in flows)}
        with ThreadPoolExecutor(max_workers=min(self.lecturas, max(len(oleada2), 1))) as pool:
            for (fid, _), valor in zip(oleada2, pool.map(lambda t: t[1](), oleada2), strict=True):
                paginas[fid] = (valor or {}).get("pages") or []
        snap["pages"] = paginas
        return snap

    # ============================================================= 2. LIMPIAR
    def limpiar(self) -> dict[str, Any]:
        """Borra TODOS los elementos del clon (el agente se conserva).

        El ORDEN lo dicta el servidor, no la conveniencia (medido):

        * `DELETE` acepta `force=true` para flows, tools, webhooks,
          entityTypes, generators y transitionRouteGroups -> borra el recurso
          aunque otros lo referencien. **NO** lo acepta para playbooks ni
          intents: ahi hay que quitar las referencias antes.
        * por eso se borran PRIMERO los flows (con force): se van sus paginas
          y con ellas las referencias a los playbooks.
        * los playbooks se referencian entre si (`referencedPlaybooks`): se
          borran en orden INVERSO de dependencias (el que referencia, antes).
        * los intents se borren antes que los entityTypes (un intent
          referencia sus entity types).
        """
        estado = self._estado(self.destino)
        # Grupos: dentro de un grupo los borrados son INDEPENDIENTES y pueden
        # ir en paralelo; entre grupos, el ORDEN lo dicta el servidor.
        # `serial=True` -> el grupo NO se paraleliza (el orden lo impone el
        # servidor: playbooks que se referencian entre si, intents que
        # referencian sus entity types).
        grupos: list[tuple[str, list[tuple[str, str, str, dict | None]], bool]] = []

        # 1. flows (force): con ellas caen las paginas y sus referencias.
        flows = []
        for rec in estado.get("flows") or []:
            rid = self._id_de(rec)
            if rid == DEFAULT_FLOW:
                continue          # el flow por defecto no se puede borrar
            flows.append(("flows", rid, rec.get("displayName") or rid, {"force": "true"}))
        grupos.append(("flows", flows, True))

        # 2. playbooks: orden INVERSO de dependencias (referencia antes).
        pbs = [(self._id_de(p), p) for p in (estado.get("playbooks") or [])]
        pbs_orden = list(reversed(self._orden_por_deps(
            pbs, lambda r: self._refs_nombres(r.get("referencedPlaybooks")))))
        grupos.append(("playbooks",
                       [("playbooks", rid, rec.get("displayName") or rid, None)
                        for rid, rec in pbs_orden], True))
        # 3. el resto. Los intents van ANTES que los entityTypes (un intent
        #    referencia sus entity types) -> grupo aparte.
        for coll in ("intents", "entityTypes", "tools", "webhooks", "generators"):
            lote = []
            for rec in estado.get(coll) or []:
                rid = self._id_de(rec)
                if coll == "intents" and rid in DEFAULT_INTENTS:
                    continue      # los intents por defecto no se borran
                if coll == "tools" and rid.startswith("df-"):
                    continue      # tool builtin de Dialogflow: no se borra
                force = None if coll == "intents" else {"force": "true"}
                lote.append((coll, rid, rec.get("displayName") or rid, force))
            grupos.append((coll, lote, coll in ("intents", "entityTypes")))

        borrados = 0
        for _nombre, lote, serial in grupos:
            borrados += self._borrar_lote(lote, serial)
        return {"planeados": sum(len(lote) for _n, lote, _s in grupos), "borrados": borrados,
                "restantes": self._conteo(self.destino)}

    def _borrar_lote(self, lote: list[tuple[str, str, str, dict | None]],
                     serial: bool = False) -> int:
        """Borra un grupo de elementos.

        Los `playbooks` se borran EN SERIE (su orden es imposed por el
        servidor: el que referencia, antes). El resto del grupo son
        independientes y se borran con `self.escrituras` hilos: medido, N=2 es
        el optimo (2,10 ops/s contra 1,75 en serie; con N>=3 el backend se
        bloquea y el rendimiento BAJA).
        """
        def _uno(item: tuple[str, str, str, dict | None]) -> bool:
            coll, rid, dn, query = item
            url = self._url(self.destino, f"/{coll}/{rid}")
            data = self._escribir("borrado", f"DELETE {coll} {dn[:28]}", "DELETE",
                                  url, query=query)
            if data is None and coll == "playbooks":
                # Sin `force`: se quitan las referencias que queden y se
                # reintenta una vez.
                self._vaciar_rutas()
                data = self._escribir("borrado", f"DELETE {coll} {dn[:28]} (reintento)",
                                      "DELETE", url)
            return data is not None

        if not lote:
            return 0
        if serial or self.escrituras <= 1 or len(lote) == 1:
            return sum(1 for item in lote if _uno(item))
        with ThreadPoolExecutor(max_workers=min(self.escrituras, len(lote))) as pool:
            return sum(1 for ok in pool.map(_uno, lote) if ok)

    def _vaciar_rutas(self) -> None:
        """Vacia las rutas de los flows que quedan (una sola vez)."""
        if getattr(self, "_rutas_vaciadas", False):
            return
        self._rutas_vaciadas = True
        flows = self._estado(self.destino).get("flows") or []
        for f in flows:
            fid = self._id_de(f)
            if fid == DEFAULT_FLOW:
                continue
            body = {"name": f.get("name"), "transitionRoutes": []}
            self._escribir("borrado", f"vaciar rutas del flow {f.get('displayName','')[:20]}",
                           "PATCH", self._url(self.destino, f"/flows/{fid}"),
                           body=body, query={"updateMask": "transitionRoutes"})

    def _estado(self, agent: str) -> dict[str, Any]:
        """Elementos actuales de un agente (lecturas en paralelo)."""
        out: dict[str, Any] = {}
        oleada: list[tuple[str, Callable[[], Any]]] = []
        oleada.append(("flows", lambda: self._get(
            "lectura", "GET flows", agent, "/flows",
            query={"pageSize": 200, "languageCode": "es"})))
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "transitionRouteGroups", "generators"):
            oleada.append((coll, (lambda c=coll: self._get(
                "lectura", f"GET {c}", agent, f"/{c}", query={"pageSize": 200}))))
        with ThreadPoolExecutor(max_workers=min(self.lecturas, len(oleada))) as pool:
            for (key, _), valor in zip(oleada, pool.map(lambda t: t[1](), oleada), strict=True):
                out[key] = ((valor or {}).get(key) or [])
        return out

    def _conteo(self, agent: str) -> dict[str, int]:
        st = self._estado(agent)
        return {k: len(v) for k, v in st.items()}

    # =========================================================== 3. RECREAR
    @staticmethod
    def _spec_invalida(body: dict[str, Any]) -> bool:
        """¿El `openApiSpec` de este tool es de los que la API rechaza?

        La API responde "Found errors in the Open API specification [Null or
        empty definition]" cuando el spec viene sin `textSchema` (medido: un
        tool con `{'authentication': ...}` y otro con `{}`).

        OJO con los dos falsos positivos:
          * un tool SIN `openApiSpec` (los de data store, que llevan
            `dataStoreSpec`) no tiene nada que quitar -> su spec es valida;
          * un tool con `dataStoreSpec` se crea por ahi, no por el spec.

        Detectarlo ANTES del POST ahorra una escritura fallida.
        """
        spec = body.get("openApiSpec")
        if not isinstance(spec, dict):
            return False            # no hay spec: no hay nada que quitar
        if body.get("dataStoreSpec"):
            return False            # el tool va por data store
        texto = spec.get("textSchema")
        return not (isinstance(texto, str) and texto.strip())

    def _orden_flows(self, flows: list[dict]) -> list[dict]:
        """Orden de creacion de flows: DESPUES de los flows que referencian.

        Una ruta de flow puede apuntar a otro flow. Si ese destino no existe
        cuando se crea el flow, la API responde 400 (valida los destinos).
        """
        ids = [self._id_de(f) for f in flows]
        idset = set(ids)
        deps: dict[str, set[str]] = {}
        for f in flows:
            fid = self._id_de(f)
            d: set[str] = set()
            for r in (f.get("transitionRoutes") or []) + (f.get("eventHandlers") or []):
                tf = r.get("targetFlow")
                if tf:
                    t = tf.split("/")[-1]
                    if t in idset and t != fid:
                        d.add(t)
            deps[fid] = d
        orden: list[dict] = []
        puestos: set[str] = {DEFAULT_FLOW}      # ya existe en cualquier agente
        pendientes = list(flows)
        while pendientes:
            avance = False
            for f in list(pendientes):
                fid = self._id_de(f)
                if deps.get(fid, set()) <= puestos:
                    orden.append(f)
                    puestos.add(fid)
                    pendientes.remove(f)
                    avance = True
            if not avance:
                # Ciclo entre flows (raro): se rompe por el orden de entrada.
                f = pendientes.pop(0)
                orden.append(f)
                puestos.add(self._id_de(f))
        return orden

    def _rutas_incluibles(self, f: dict) -> tuple[list, list, bool]:
        """Rutas de un flow que se pueden meter en su propio POST.

        Solo si TODAS sus rutas son resolubles en el momento de crear el flow:
        sin destino de pagina (las paginas van despues), sin destino de
        playbook (los playbooks van despues) y con el flow destino ya creado.

        Devuelve (rutas, handlers, completo). Si `completo` es False, el flow
        necesita el PATCH de rutas de todas formas (y se creara sin ellas).
        """
        rutas = list(f.get("transitionRoutes") or [])
        handlers = list(f.get("eventHandlers") or [])
        if not rutas and not handlers:
            return [], [], True

        def ok(r: dict) -> bool:
            if r.get("targetPage"):
                # `END_FLOW` / `END_FLOW_WITH_HUMAN_ESCALATION` no son paginas:
                # son marcadores de fin de flow y SIEMPRE son validos.
                return rid_pagina(r["targetPage"]) in FIN_FLOW
            if r.get("targetPlaybook"):
                return False
            tf = r.get("targetFlow")
            if tf:
                t = tf.split("/")[-1]
                if t != DEFAULT_FLOW and _suffix("flows", t) not in self.mapa:
                    return False
            return True

        r_ok = [r for r in rutas if ok(r)]
        h_ok = [r for r in handlers if ok(r)]
        completo = len(r_ok) == len(rutas) and len(h_ok) == len(handlers)
        if not completo:
            return [], [], False
        return r_ok, h_ok, True

    def recrear(self, snap: dict[str, Any]) -> dict[str, Any]:
        """Crea en el clon todo lo del original, con los ids remapeados."""
        creado = {"entityTypes": 0, "intents": 0, "webhooks": 0, "tools": 0,
                  "playbooks": 0, "generators": 0, "flows": 0, "pages": 0,
                  "flow_routes": 0, "agent": 0}

        # --- 3.1 recursos hoja: entityTypes, intents, webhooks, tools --------
        # (los playbooks van DESPUES de los flows: referencian `referencedFlows`)
        for coll in ("entityTypes", "intents", "webhooks", "tools"):
            for rec in snap.get(coll) or []:
                old_id = (rec.get("name") or "").split("/")[-1]
                if coll == "intents" and old_id in DEFAULT_INTENTS:
                    # Los intents por defecto existen en TODO agente: no se
                    # crean, pero su nombre se remapea a si mismo.
                    self.mapa[_suffix("intents", old_id)] = _suffix("intents", old_id)
                    continue
                if coll == "tools" and old_id.startswith("df-"):
                    # Tool BUILTIN de Dialogflow (code-interpreter): ya existe en
                    # cualquier agente y la API rechaza crearlo a mano.
                    self.mapa[_suffix("tools", old_id)] = _suffix("tools", old_id)
                    self.omitidos.append(f"tool builtin {rec.get('displayName')}")
                    continue
                body = self._remap(self._body_limpio(rec))
                # La API rechaza un `openApiSpec` SIN `textSchema` ("Null or
                # empty definition"). Se detecta ANTES de enviarlo: asi se
                # ahorra el POST fallido y el error no ensucia el informe.
                # OJO: esto es SOLO para `tools` (los demas recursos no
                # llevan spec) y la URL es SIEMPRE la de SU coleccion.
                if coll == "tools" and self._spec_invalida(body):
                    body.pop("openApiSpec", None)
                    self.desviaciones.append(
                        f"tool {rec.get('displayName')}: recreado SIN spec "
                        "(el openApiSpec del original no tiene definicion y la "
                        "API lo rechaza)")
                data = self._escribir("creacion", f"POST {coll} {rec.get('displayName','')[:28]}",
                                      "POST", self._url(self.destino, f"/{coll}"), body=body,
                                      reintentos=1)
                if data is None and coll == "tools":
                    # El spec original no lo acepta la API (OpenAPI vacio ->
                    # "Null or empty definition"; data store borrado -> "Failed
                    # to connect"). Se recrea SIN spec: el elemento existe y
                    # los playbooks que lo referencian ya no fallan.
                    body_sin = {k: v for k, v in body.items()
                                if k not in ("openApiSpec", "dataStoreSpec")}
                    data = self._escribir("creacion",
                                          f"POST tool {rec.get('displayName','')[:26]} (sin spec)",
                                          "POST", self._url(self.destino, "/tools"), body=body_sin)
                    if data is not None:
                        self.desviaciones.append(
                            f"tool {rec.get('displayName')}: recreado SIN spec "
                            "(la API rechaza el spec original)")
                new_id = self._nuevo_id(data)
                if new_id:
                    self.mapa[_suffix(coll, old_id)] = _suffix(coll, new_id)
                    creado[coll] += 1
                else:
                    self.omitidos.append(f"{coll} {rec.get('displayName')}")

        # --- 3.2 flows (las rutas resolubles van en el propio POST) ---------
        # Un flow se crea DESPUES de los flows a los que apuntan sus rutas, y
        # si TODAS sus rutas son resolubles en ese momento, se crean con ellas
        # -> ese flow ya no necesita el PATCH de rutas (un escritura menos).
        flows = snap.get("flows") or []
        for f in self._orden_flows(flows):
            old_id = (f.get("name") or "").split("/")[-1]
            if old_id == DEFAULT_FLOW:
                # El flow por defecto ya existe en el clon: se remapea a si mismo.
                # Sus rutas hay que ponerlas SIEMPRE con un PATCH (no se puede
                # crear de nuevo).
                self.mapa[_suffix("flows", old_id)] = _suffix("flows", old_id)
                self._flows_con_patch.add(old_id)
                continue
            body = self._remap(self._body_limpio(f))
            body.pop("transitionRoutes", None)
            body.pop("eventHandlers", None)
            rutas, handlers, completo = self._rutas_incluibles(f)
            if completo and (rutas or handlers):
                body["transitionRoutes"] = self._remap(rutas)
                body["eventHandlers"] = self._remap(handlers)
            data = self._escribir("creacion", f"POST flow {f.get('displayName','')[:28]}",
                                  "POST", self._url(self.destino, "/flows"), body=body)
            if data is None and (rutas or handlers):
                # El backend no acepto las rutas en el POST: se crea sin ellas
                # y quedara para el PATCH de la seccion 3.7.
                body.pop("transitionRoutes", None)
                body.pop("eventHandlers", None)
                data = self._escribir("creacion",
                                      f"POST flow {f.get('displayName','')[:24]} (sin rutas)",
                                      "POST", self._url(self.destino, "/flows"), body=body)
                completo = False
            new_id = self._nuevo_id(data)
            if new_id:
                self.mapa[_suffix("flows", old_id)] = _suffix("flows", new_id)
                creado["flows"] += 1
                if completo and (rutas or handlers):
                    creado["flujos_rutas_en_post"] = creado.get("flujos_rutas_en_post", 0) + 1
                else:
                    self._flows_con_patch.add(old_id)

        # --- 3.3 playbooks (referencian TOOLS y FLOWS: van tras ambos) -----
        # Y se referencian ENTRE SI (`referencedPlaybooks`): orden topologico.
        # Caso especial: un playbook puede referenciarse A SI MISMO (el
        # orquestador lo hace) -> en el POST esa referencia todavia no puede
        # existir, asi que se crea sin ella y se anade con un PATCH despues.
        for old_id, rec in self._orden_por_deps(
                [(self._id_de(rec), rec) for rec in (snap.get("playbooks") or [])],
                lambda r: self._refs_nombres(r.get("referencedPlaybooks"))):
            body_full = self._remap(self._body_limpio(rec))
            refs = body_full.get("referencedPlaybooks") or []
            propios = [x for x in refs if x.split("/")[-1] == old_id]
            body = copy.deepcopy(body_full)
            if propios:
                # La autorreferencia es un bucle: la API la rechaza.
                body["referencedPlaybooks"] = [x for x in refs
                                               if x.split("/")[-1] != old_id]
            campos_autorref = self._tiene_autorref(body, rec.get("displayName") or "")
            if campos_autorref:
                body = self._neutralizar_autorref(body, rec.get("displayName") or "")
            data = self._escribir("creacion", f"POST playbook {rec.get('displayName','')[:26]}",
                                  "POST", self._url(self.destino, "/playbooks"), body=body)
            new_id = self._nuevo_id(data)
            if new_id:
                self.mapa[_suffix("playbooks", old_id)] = _suffix("playbooks", new_id)
                creado["playbooks"] += 1
                if propios or campos_autorref:
                    self.desviaciones.append(
                        f"playbook {rec.get('displayName')}: la autorreferencia "
                        "(${PLAYBOOK:<si mismo>}) no se puede reproducir por API "
                        "(la API rechaza los bucles: 'caused loop in playbook routes')")
            else:
                self.omitidos.append(f"playbook {rec.get('displayName')}")

        # --- 3.4 generadores (pueden referenciar playbooks) -----------------
        for rec in snap.get("generators") or []:
            old_id = (rec.get("name") or "").split("/")[-1]
            body = self._remap(self._body_limpio(rec))
            data = self._escribir("creacion", f"POST generator {rec.get('displayName','')[:24]}",
                                  "POST", self._url(self.destino, "/generators"), body=body)
            new_id = self._nuevo_id(data)
            if new_id:
                self.mapa[_suffix("generators", old_id)] = _suffix("generators", new_id)
                creado["generators"] += 1

        # --- 3.5 paginas en orden topologico (las rutas validan destinos) --
        orden = self._orden_paginas(snap)
        for flow_old, page in orden:
            flow_new = self.mapa.get(_suffix("flows", flow_old), "").split("/")[-1]
            if not flow_new:
                continue
            old_page = (page.get("name") or "").split("/")[-1]
            body = self._body_limpio(page)
            # Destinos que aun no existen (ciclos): se crea la pagina sin
            # rutas NI handlers y se parchea despues. El destino especial
            # `END_FLOW` no es una pagina: no cuenta.
            # Las rutas se guardan SIN remapear: se remapean en el PATCH,
            # cuando ya existe el mapa completo (si se remapean aqui, los
            # destinos pendientes se quedan con el id del ORIGINAL).
            rutas = page.get("transitionRoutes") or []
            handlers = page.get("eventHandlers") or []
            pendientes = [r for r in rutas + handlers
                          if r.get("targetPage")
                          and rid_pagina(r["targetPage"]) not in FIN_FLOW
                          and _suffix("pages", rid_pagina(r["targetPage"])) not in self.mapa]
            if pendientes:
                body_sin = self._remap({k: v for k, v in body.items()
                                        if k not in ("transitionRoutes", "eventHandlers")})
                data = self._escribir("creacion",
                                      f"POST page {page.get('displayName','')[:26]} (sin rutas)",
                                      "POST",
                                      self._url(self.destino, f"/flows/{flow_new}/pages"),
                                      body=body_sin)
                new_page = self._nuevo_id(data)
                if new_page:
                    self.mapa[_suffix("pages", old_page)] = _suffix("pages", new_page)
                    creado["pages"] += 1
                    self._pendientes_pagina.append((flow_new, new_page, rutas, handlers))
            else:
                data = self._escribir("creacion", f"POST page {page.get('displayName','')[:26]}",
                                      "POST",
                                      self._url(self.destino, f"/flows/{flow_new}/pages"),
                                      body=self._remap(body))
                new_page = self._nuevo_id(data)
                if new_page:
                    self.mapa[_suffix("pages", old_page)] = _suffix("pages", new_page)
                    creado["pages"] += 1

        # --- 3.6 parches de las paginas con destinos pendientes -------------
        for flow_new, page_new, rutas, handlers in self._pendientes_pagina:
            body = {"name": f"{self._pref(self.destino)}flows/{flow_new}/pages/{page_new}",
                    "transitionRoutes": self._remap(rutas),
                    "eventHandlers": self._remap(handlers)}
            self._escribir("creacion", f"PATCH page pendientes {page_new[:8]}",
                           "PATCH",
                           self._url(self.destino, f"/flows/{flow_new}/pages/{page_new}"),
                           body=body,
                           query={"updateMask": "transitionRoutes,eventHandlers",
                                  "languageCode": "es"})

        # --- 3.7 rutas y eventHandlers de los flows -------------------------
        # Solo los flows cuyas rutas NO pudieron ir en su POST (destinos de
        # pagina o de playbook, que todavia no existen).
        for f in flows:
            old_id = (f.get("name") or "").split("/")[-1]
            if old_id not in self._flows_con_patch:
                continue
            flow_new = self.mapa.get(_suffix("flows", old_id), "").split("/")[-1]
            if not flow_new:
                continue
            rutas = self._remap(f.get("transitionRoutes") or [])
            handlers = self._remap(f.get("eventHandlers") or [])
            if not rutas and not handlers:
                continue
            body = {"name": f"{self._pref(self.destino)}flows/{flow_new}",
                    "transitionRoutes": rutas, "eventHandlers": handlers}
            self._escribir("creacion", f"PATCH rutas flow {f.get('displayName','')[:26]}",
                           "PATCH", self._url(self.destino, f"/flows/{flow_new}"),
                           body=body,
                           query={"updateMask": "transitionRoutes,eventHandlers",
                                  "languageCode": "es"})
            creado["flow_routes"] += 1

        # --- 3.8 ajustes a nivel de agente ----------------------------------
        creado["agent"] = self._parche_agente(snap.get("agente") or {})
        creado["paginas_pendientes"] = len(self._pendientes_pagina)
        return creado

    _pendientes_pagina: list[tuple[str, str, list]] = []

    @staticmethod
    def _id_de(rec: dict[str, Any]) -> str:
        return (rec.get("name") or "").split("/")[-1]

    @staticmethod
    def _refs_nombres(valor: Any) -> set[str]:
        """Ids (ultimo segmento) de una lista de nombres de recurso."""
        if not valor:
            return set()
        if isinstance(valor, str):
            valor = [valor]
        out = set()
        for v in valor:
            if isinstance(v, str) and "/" in v:
                out.add(v.split("/")[-1])
        return out

    def _orden_por_deps(self, items: list[tuple[str, dict]],
                        refs_fn: Callable[[dict], set[str]]) -> list[tuple[str, dict]]:
        """Orden topologico: cada recurso va DESPUES de los que referencia.

        La API valida las referencias al crear (una pagina/playbook que apunta
        a algo inexistente -> HTTP 400), asi que el orden importa.
        """
        ids = {i for i, _ in items}
        dep: dict[str, set[str]] = {}
        for rid, rec in items:
            dep[rid] = {r for r in refs_fn(rec) if r in ids and r != rid}
        orden: list[tuple[str, dict]] = []
        puestos: set[str] = set()
        resto = list(items)
        while resto:
            avance = False
            for it in list(resto):
                if dep[it[0]] <= puestos:
                    orden.append(it)
                    puestos.add(it[0])
                    resto.remove(it)
                    avance = True
            if not avance:
                # ciclo: se crea en el orden que quede (las rutas que faltan se
                # parchean despues).
                orden.extend(resto)
                break
        return orden

    @classmethod
    def _sin_macros(cls, obj: Any) -> Any:
        """Neutraliza las macros ${PLAYBOOK:x} (token inerte)."""
        if isinstance(obj, str):
            return MACRO_PLAYBOOK.sub(lambda m: "{{PLAYBOOK:" + m.group(1) + "}}", obj)
        if isinstance(obj, list):
            return [cls._sin_macros(x) for x in obj]
        if isinstance(obj, dict):
            return {k: cls._sin_macros(v) for k, v in obj.items()}
        return obj

    @classmethod
    def _neutralizar_autorref(cls, obj: Any, propio: str) -> Any:
        """Neutraliza SOLO la macro `${PLAYBOOK:<propio>}`.

        La API rechaza los bucles: "Playbook X caused loop in playbook routes"
        (medido). Un playbook que se menciona a si mismo no se puede crear
        ni actualizar por API. En el original esa autorreferencia
        aparece dentro de un texto que PROHIBE invocarse a si mismo, asi que
        neutralizarla no cambia el comportamiento del agente.

        El resto de macros (${FLOW:x}, ${TOOL:x}, ${PLAYBOOK:otro}) se dejan
        intactas: la API las valida y las acepta si el destino existe.
        """
        if isinstance(obj, str):
            return MACRO_PLAYBOOK.sub(
                lambda m: ("{{PLAYBOOK:" + m.group(1) + "}}") if m.group(1) == propio
                else m.group(0), obj)
        if isinstance(obj, list):
            return [cls._neutralizar_autorref(x, propio) for x in obj]
        if isinstance(obj, dict):
            return {k: cls._neutralizar_autorref(v, propio) for k, v in obj.items()}
        return obj

    @classmethod
    def _tiene_autorref(cls, body: dict[str, Any], propio: str) -> list[str]:
        """Campos de primer nivel con una macro que se autorreferencia."""
        out = []
        for k, v in body.items():
            if k in ("instruction", "codeBlock", "goal") and \
                    re.search(r"\$\{PLAYBOOK:" + re.escape(propio) + r"\}",
                              json.dumps(v, ensure_ascii=False)):
                out.append(k)
        return out

    def _optimizar_orden(self, orden: list[tuple[str, dict]],
                         deps: dict[str, set[str]]) -> list[tuple[str, dict]]:
        """Busqueda local: minimiza las paginas que hay que PARCHEAR despues.

        Una pagina queda "pendiente" si al crearla tiene algun destino que
        todavia no existe (ciclo): se crea sin esas rutas y se parchea luego.

        El minimo inevitable sale de los CICLOS del grafo de paginas: dentro de
        un componente fuertemente conexo de k paginas, la penultima creada
        todavia tiene un destino sin resolver, asi que **al menos k-1 paginas
        de cada ciclo exigen PATCH** (medido: 2 ciclos). El metodo voraz se
        quedaba en 7; aqui se baja a ese minimo con una busqueda local mas
        potente: varias ordenes de partida + intercambios completos por pasada.

        Cada PATCH evitado son ~470 ms de escritura (el backend serializa).
        """
        def coste(o: list[tuple[str, dict]]) -> int:
            hechos: set[str] = set()
            n = 0
            for _f, p in o:
                pid = self._id_de(p)
                if any(t not in hechos for t in deps.get(pid, ())):
                    n += 1
                hechos.add(pid)
            return n

        n = len(orden)
        if n < 2:
            return list(orden)

        def barrido(base: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
            """Descenso local: prueban TODOS los intercambios y repite."""
            actual = list(base)
            c_actual = coste(actual)
            for _ in range(200):
                mejor = None
                for i in range(n):
                    for j in range(i + 1, n):
                        cand = actual[:]
                        cand[i], cand[j] = cand[j], cand[i]
                        c = coste(cand)
                        if c < c_actual:
                            mejor, c_actual = cand, c
                if mejor is None:
                    break
                actual = mejor
            return actual

        # Ordenes de partida: el voraz (arriba) y sus variaciones por bloque.
        partidas = [list(orden)]
        invertida = list(reversed(orden))
        partidas.append(invertida)
        # y dos rotaciones: cortan el ciclo en otro punto.
        for corte in (n // 3, 2 * n // 3):
            partidas.append(list(orden[corte:]) + list(orden[:corte]))

        mejor, mejor_coste = list(orden), coste(orden)
        for base in partidas:
            cand = barrido(base)
            c = coste(cand)
            if c < mejor_coste:
                mejor, mejor_coste = cand, c
        return mejor

    @staticmethod
    def _minimo_teorico(deps: dict[str, set[str]]) -> int:
        """Minimo de PATCHes inevitables: (k-1) por cada ciclo del grafo."""
        nodos = set(deps)
        # Tarjan iterativo -> componentes fuertemente conexas
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        en_pila: set[str] = set()
        pila: list[str] = []
        componentes: list[list[str]] = []
        contador = 0
        for raiz in sorted(nodos):
            if raiz in index:
                continue
            trabajo = [(raiz, iter(deps.get(raiz, ()) & nodos))]
            index[raiz] = low[raiz] = contador
            contador += 1
            pila.append(raiz)
            en_pila.add(raiz)
            while trabajo:
                v, it = trabajo[-1]
                avanzo = False
                for w in it:
                    if w not in index:
                        index[w] = low[w] = contador
                        contador += 1
                        pila.append(w)
                        en_pila.add(w)
                        trabajo.append((w, iter(deps.get(w, ()) & nodos)))
                        avanzo = True
                        break
                    if w in en_pila:
                        low[v] = min(low[v], index[w])
                if avanzo:
                    continue
                trabajo.pop()
                if trabajo:
                    u = trabajo[-1][0]
                    low[u] = min(low[u], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = pila.pop()
                        en_pila.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    componentes.append(comp)
        minimo = 0
        for comp in componentes:
            if len(comp) > 1:
                minimo += len(comp) - 1
            elif comp and comp[0] in deps.get(comp[0], set()):
                minimo += 1        # auto-referencia
        return minimo

    def _orden_paginas(self, snap: dict[str, Any]) -> list[tuple[str, dict]]:
        """Orden topologico global de las paginas (la API valida destinos)."""
        todas: list[tuple[str, dict]] = []
        for flow_old, pgs in (snap.get("pages") or {}).items():
            for p in pgs:
                todas.append((flow_old, p))
        # dependencia: pagina -> paginas que sus rutas apuntan
        dep: dict[str, set[str]] = {}
        for _flow, p in todas:
            pid = (p.get("name") or "").split("/")[-1]
            refs = set()
            for r in (p.get("transitionRoutes") or []) + (p.get("eventHandlers") or []):
                tp = r.get("targetPage")
                if tp and rid_pagina(tp) not in FIN_FLOW:
                    refs.add(rid_pagina(tp))
            dep[pid] = refs
        ordenadas: list[tuple[str, dict]] = []
        puestos: set[str] = set()
        restantes = list(todas)
        while restantes:
            avance = False
            for item in list(restantes):
                pid = (item[1].get("name") or "").split("/")[-1]
                refs = {r for r in dep.get(pid, set())
                        if r in dep and r != pid}
                if refs <= puestos:
                    ordenadas.append(item)
                    puestos.add(pid)
                    restantes.remove(item)
                    avance = True
            if not avance:
                # CICLO: se rompe eligiendo la pagina con MENOS destinos sin
                # resolver (es la que menos PATCHes pendientes deja).
                def sin_resolver(it: tuple[str, dict]) -> int:
                    n = 0
                    for r in ((it[1].get("transitionRoutes") or []) +
                              (it[1].get("eventHandlers") or [])):
                        tp = r.get("targetPage")
                        if not tp:
                            continue
                        tid = rid_pagina(tp)
                        if tid not in FIN_FLOW and tid in dep and tid not in puestos:
                            n += 1
                    return n

                cand = min(restantes, key=lambda it: (sin_resolver(it),
                                                      it[1].get("displayName") or ""))
                pid = (cand[1].get("name") or "").split("/")[-1]
                ordenadas.append(cand)
                puestos.add(pid)
                restantes.remove(cand)
        # Dependencias (destinos de pagina) para optimizar el orden.
        deps: dict[str, set[str]] = {}
        for _f, p in todas:
            pid = self._id_de(p)
            refs = set()
            for r in ((p.get("transitionRoutes") or []) +
                      (p.get("eventHandlers") or [])):
                tp = r.get("targetPage")
                if tp:
                    tid = rid_pagina(tp)
                    if tid not in FIN_FLOW:
                        refs.add(tid)
            deps[pid] = refs
        return self._optimizar_orden(ordenadas, deps)

    def _parche_agente(self, agente_orig: dict[str, Any]) -> int:
        """Copia los ajustes del agente (sin identidad: name/displayName).

        `defaultLanguageCode` NO se copia: la API responde "Agent default
        language code cannot be updated" (medido).
        """
        copia = dict(agente_orig)
        for k in ("name", "displayName", "description", "projectNumber",
                  "supportedLanguageCodes", "startPlaybook", "defaultLanguageCode"):
            copia.pop(k, None)
        if not copia:
            return 0
        copia = self._remap(copia)
        # El flow por defecto del clon tiene el mismo id reservado.
        if "startFlow" in copia:
            copia["startFlow"] = self._remap_str(agente_orig.get("startFlow", ""))
        mask = ",".join(sorted(copia.keys()))
        self._escribir("creacion", "PATCH agente (ajustes)",
                       "PATCH", self._url(self.destino, ""),
                       body=copia, query={"updateMask": mask})
        return 1

    # ============================================================ 4. VERIFICAR
    @staticmethod
    def _firma_pagina(p: dict[str, Any]) -> dict[str, Any]:
        """Firma comparable de una pagina: rutas, handlers y tipo de destino.

        Los ids se ignoran (son distintos por definicion: se remapean), pero el
        GRAFO de transiciones tiene que ser el mismo.
        """
        rutas = p.get("transitionRoutes") or []
        handlers = p.get("eventHandlers") or []

        def destino(r: dict[str, Any]) -> str:
            for k in ("targetPlaybook", "targetFlow", "targetPage"):
                if r.get(k):
                    v = r[k]
                    if k == "targetPage" and v.endswith("/END_FLOW"):
                        return "END_FLOW"
                    return k
            return "(sin destino)"

        return {
            "rutas": len(rutas),
            "handlers": len(handlers),
            "destinos": sorted(destino(r) for r in rutas + handlers),
            "condiciones": len([r for r in rutas if r.get("condition")]),
        }

    @staticmethod
    def _firma_tool(t: dict[str, Any]) -> dict[str, Any]:
        """Firma comparable de un tool: no basta con el numero.

        El conteo de tools no delata que 23 de ellos se recrearan SIN su
        `openApiSpec` (eso solo se ve en el CONTENIDO).
        """
        spec = t.get("openApiSpec")
        texto = spec.get("textSchema") if isinstance(spec, dict) else None
        return {
            "spec": bool(spec),
            "textSchema_len": len(texto) if isinstance(texto, str) else 0,
            "dataStore": bool(t.get("dataStoreSpec")),
            "inlineActions": len(t.get("inlineActions") or []),
        }

    def verificar(self, snap: dict[str, Any]) -> dict[str, Any]:
        """Compara el clon recreado con el snapshot del original."""
        estado = self._estado(self.destino)
        dif: dict[str, Any] = {}
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "generators"):
            orig = snap.get(coll) or []
            nuevo = estado.get(coll) or []
            dif[coll] = {"original": len(orig), "clon": len(nuevo),
                         "ok": len(orig) == len(nuevo)}
        # Tools: ademas del numero, el CONTENIDO (spec). Una regresion que
        # quitase el openApiSpec a 20 tools NO se veria en el conteo.
        firma_to = {t.get("displayName"): self._firma_tool(t)
                    for t in (snap.get("tools") or [])}
        firma_tn = {t.get("displayName"): self._firma_tool(t)
                    for t in (estado.get("tools") or [])}
        det_tools = {k: [firma_to.get(k), firma_tn.get(k)]
                     for k in set(firma_to) | set(firma_tn)
                     if firma_to.get(k) != firma_tn.get(k)}
        dif["tools"]["detalle_spec"] = det_tools
        dif["tools"]["specs_iguales"] = not det_tools
        flows_o = snap.get("flows") or []
        flows_n = estado.get("flows") or []
        dif["flows"] = {"original": len(flows_o), "clon": len(flows_n),
                        "ok": len(flows_o) == len(flows_n)}
        # RUTAS de los flows: no basta con el numero. Si el backend ignorase
        # las `transitionRoutes` de un POST (como ignora las `pages`), el
        # clon quedaria sin rutas y aqui se ve.
        firma_o = {f.get("displayName"): self._firma_pagina(f) for f in flows_o}
        firma_n = {f.get("displayName"): self._firma_pagina(f) for f in flows_n}
        det_flow = {k: [firma_o.get(k), firma_n.get(k)]
                    for k in set(firma_o) | set(firma_n)
                    if firma_o.get(k) != firma_n.get(k)}
        dif["flows"]["rutas_iguales"] = not det_flow
        dif["flows"]["detalle_rutas"] = det_flow
        dif["flows"]["ok"] = dif["flows"]["ok"] and not det_flow

        # Paginas: por flow (displayName) y por pagina (displayName), con la
        # firma de routing (no solo el numero).
        pag_o: dict[str, dict] = {}
        for f in flows_o:
            fid = (f.get("name") or "").split("/")[-1]
            for p in (snap.get("pages") or {}).get(fid, []):
                pag_o.setdefault(f.get("displayName"), {})[p.get("displayName")] = \
                    self._firma_pagina(p)
        pag_n: dict[str, dict] = {}
        for f in flows_n:
            fid = (f.get("name") or "").split("/")[-1]
            pgs = self._get("verificacion", f"GET pages {f.get('displayName','')[:22]}",
                            self.destino, f"/flows/{fid}/pages",
                            query={"pageSize": 200, "languageCode": "es"}) or {}
            for p in pgs.get("pages") or []:
                pag_n.setdefault(f.get("displayName"), {})[p.get("displayName")] = \
                    self._firma_pagina(p)
        total_o = sum(len(v) for v in pag_o.values())
        total_n = sum(len(v) for v in pag_n.values())
        det = {}
        for flow in set(pag_o) | set(pag_n):
            a = pag_o.get(flow, {})
            b = pag_n.get(flow, {})
            if a != b:
                det[flow] = {k: [a.get(k), b.get(k)] for k in set(a) | set(b)
                             if a.get(k) != b.get(k)}
        dif["pages"] = {"original": total_o, "clon": total_n,
                        "ok": total_o == total_n and not det,
                        "detalle": det}
        dif["routing_igual"] = not det
        return dif

    # ================================================================= RUN
    def run(self, verificar: bool = True) -> dict[str, Any]:
        """Ejecuta el clonado completo y devuelve el informe con tiempos.

        El snapshot (lecturas del ORIGINAL) y la limpieza (escrituras en el
        CLON) son INDEPENDIENTES -> se solapan en un hilo: ahorra ~4 s.

        `ms_fases` = tiempo de RELOJ de cada fase (lo que se tarda de verdad);
        `ms_suma_por_fase` = suma de los tiempos de cada llamada (mayor que el
        de reloj cuando las lecturas van en paralelo).
        """
        import threading

        t0 = time.perf_counter()
        caja: dict[str, Any] = {}

        def _snapshot() -> None:
            caja["snap"] = self.snapshot()

        th = threading.Thread(target=_snapshot, name="snapshot-original")
        th.start()
        limpio = self.limpiar()          # en paralelo con el snapshot
        th.join()
        snap = caja.get("snap") or {}
        t1 = time.perf_counter()
        creado = self.recrear(snap)
        t2 = time.perf_counter()
        dif = self.verificar(snap) if verificar else {}
        t3 = time.perf_counter()
        fases = {"lectura+limpieza": (t1 - t0), "creacion": (t2 - t1),
                 "verificacion": (t3 - t2)}
        total = (t3 - t0) * 1000
        return {
            "origen": self.origen,
            "destino": self.destino,
            "ms_total": round(total, 1),
            "ms_fases": {k: round(v * 1000, 1) for k, v in fases.items()},
            "ms_suma_por_fase": {k: round(v, 1) for k, v in self._t_fases.items()},
            "limpiar": limpio,
            "crear": creado,
            "paridad": dif,
            "omitidos": self.omitidos,
            "desviaciones": self.desviaciones,
            "acciones": self.acciones,
            "errores": self.errores,
        }
