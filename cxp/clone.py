"""1:1 clone of an agent: the clone is cleared and the original is recreated inside it.

Everything goes through `ConsoleApi` (the same path as the rest of the MCP) and the permission
guard lives in the low layer: **writes only go to the target agent**.

MEASUREMENTS that condition the design (2026-11-08, this host)
----------------------------------------------------------
* the backend **serializes writes** to the same agent: throughput stalls at
  ~1.5-2 ops/s regardless of concurrency (measured curve: POST
  1->1.57 | 2->1.01 | 4->1.34 | 8->1.12 ops/s). That is why **creation goes IN
  SERIES**. **Deletion** does allow 2 in parallel (2.10 ops/s vs 1.75):
  they are independent elements of the same group.
* **reads do parallelize**: 8 GET in ~1 s (121 ms each) -> they go in a
  thread pool.
* the `/batch` endpoint **does not exist** in the internal backend (HTTP 404).
* `POST flow` **ignores** the `pages` in the body -> each page goes in its own POST.
  However, its `transitionRoutes` **do** apply if the target already exists.
* the API **validates targets**: a route to a nonexistent page -> HTTP 400.
  That is why pages are created in topological order (DAG of references) and
  cycles are broken by patching afterwards.
* `DELETE tool` with the tool referenced by a playbook -> HTTP 400: that is why
  playbooks are deleted BEFORE the tools.
* since the backend serializes, **writing less** is the only margin: the routes
  of a flow go in their own POST when they are already resolvable, the order of the
  pages minimizes the PATCH of the cycles and invalid `openApiSpec` are
  detected BEFORE of sending it (the POST the backend is going to reject is not paid)."""
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

# Macro ${PLAYBOOK:nombre} in a playbook's text. The backend VALIDATES it on
# create (the reference must exist), so a playbook that mentions itself cannot be
# created in one go: it is created with the macro neutralized and restored
# afterwards with an PATCH.
MACRO_PLAYBOOK = re.compile(r"\$\{PLAYBOOK:([^}]+)\}")

DEFAULT_FLOW = "00000000-0000-0000-0000-000000000000"
DEFAULT_INTENTS = ("00000000-0000-0000-0000-000000000000",
                   "00000000-0000-0000-0000-000000000001")

# Special end-of-flow targets: they are NOT pages, they are always valid
# (they can be put in a POST without waiting for anything to exist).
FIN_FLOW = frozenset({"END_FLOW", "END_FLOW_WITH_HUMAN_ESCALATION"})


def rid_pagina(nombre: str) -> str:
    """Last segment of a page name (or its end marker)."""
    return (nombre or "").split("/")[-1]

# Collections that are cloned and in what order CREATE. Order matters:
# entityTypes -> intents -> webhooks -> tools -> playbooks -> generators
# (a playbook references tools; a generator could reference playbooks).
COLECCIONES_CREATE = ["entityTypes", "intents", "webhooks", "tools",
                       "playbooks", "generators"]

# Order of DELETED: first the playbooks (they reference the tools), then the
# flows (their pages are removed), and after that the rest.
COLECCIONES_DELETE = ["playbooks", "flows", "tools", "webhooks",
                      "intents", "entityTypes", "generators"]

# Fields the server returns but that cannot (or must not) be written.
NO_ESCRIBIR = frozenset({
    "etag", "createTime", "updateTime", "name", "parent", "session",
    "environment", "derivedTransitions", "nluSettingsList", "tokenCount",
    "projectNumber", "supportedLanguageCodes",
})


def _suffix(resource: str, rid: str) -> str:
    return f"{resource}/{rid}"


class Cloner:
    """1:1 agent clone (source = read-only, target = writable)."""

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
        self.mapa: dict[str, str] = {}      # "pages/old" -> "pages/new"
        self.errores: list[dict[str, Any]] = []
        self._lock = threading.Lock()       # the deletion runs with 2 threads
        self._t_fases: dict[str, float] = {}
        self._pendientes_pagina: list[tuple[str, str, list, list]] = []
        self._flows_con_patch: set[str] = set()  # flows that need PATCH of routes
        self.omitidos: list[str] = []   # resources that API does not allow recreating
        self.desviaciones: list[str] = []  # recreated with fewer fields than the originals

    # ------------------------------------------------------------- utilities
    def _base(self) -> str:
        """Path for URLs: `console_api._url()` expects '/'- initial."""
        return f"/projects/{self.project}/locations/{self.location}/agents"

    def _pref(self, agent: str) -> str:
        """Resource NAME prefix (without '/': exactly as returned by the API)."""
        return f"projects/{self.project}/locations/{self.location}/agents/{agent}/"

    def _url(self, agent: str, suffix: str = "", **params: Any) -> str:
        return self.cons._url(f"{self._base()}/{agent}{suffix}", **params)

    def _act(self, fase: str, accion: str, ms: float, http: int = 200,
             ok: bool = True, detalle: str = "") -> dict[str, Any]:
        row = {"fase": fase, "accion": accion, "ms": round(ms, 1),
               "http": http, "ok": ok, "detalle": detalle[:700]}
        if not ok:
            # Annotate the error with the known limit that explains it (MCP
            # memory: `cx_limites_api`).
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
        """Write with retries IN SERIES (the backend serializes the same way)."""
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
        """Is the failure retryable? (datastore connection, 429, 5xx)

        It is NOT retryable when the data store no longer exists ("do not
        exist"): it is permanent and the retry only wastes 2 s."""
        if exc.status in (429, 500, 502, 503, 504):
            return True
        if "do not exist" in exc.body or "do not exists" in exc.body:
            return False
        return "Failed to connect" in exc.body or "connect datastore" in exc.body

    # ---------------------------------------------------------------- remapping
    def _remap_str(self, s: str) -> str:
        """Rewrite a resource name from ORIGINAL to CLONE.

        Names can be NESTED: the special end-of-flow target is
        `.../flows/{flow}/pages/END_FLOW` and a page's target is
        `.../flows/{flow}/pages/{page}`. EACH pairs must be remapped
        `resource/id` of the route, not just the first (otherwise the flow was
        remapped but the inner page kept the original's id)."""
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
        """Replace references to agent ORIGINAL with those of CLONE."""
        if isinstance(obj, str):
            return self._remap_str(obj)
        if isinstance(obj, list):
            return [self._remap(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self._remap(v) for k, v in obj.items()}
        return obj

    @staticmethod
    def _body_limpio(rec: dict[str, Any]) -> dict[str, Any]:
        """Remove what the server does not accept on create."""
        return {k: v for k, v in rec.items() if k not in NO_ESCRIBIR}

    def _nuevo_id(self, data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        return (data.get("name") or "").split("/")[-1]

    # ============================================================ 1. SNAPSHOT
    def snapshot(self) -> dict[str, Any]:
        """Read the complete ORIGINAL agent (parallel reads)."""
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

        # Pages for each flow (second wave, in parallel)
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

    # ============================================================= 2. CLEAN
    def limpiar(self) -> dict[str, Any]:
        """Delete ALL elements from the clone (the agent is kept).

        The ORDER is dictated by the server, not by convenience (measured):

        * `DELETE` accepts `force=true` for flows, tools, webhooks,
          entityTypes, generators and transitionRouteGroups -> deletes the resource
          even if others reference it. It does **NOT** accept it for playbooks or
          intents: there the references must be removed first.
        * therefore the flows are deleted FIRST (with force): their pages go
          and with them the references to the playbooks.
        * playbooks reference each other (`referencedPlaybooks`): they are
          deleted in INVERSE order of dependencies (the one that references, first).
        * intents are deleted before the entityTypes (an intent
          references its entity types)."""
        estado = self._estado(self.destino)
        # Groups: within a group, deletions are INDEPENDENT and may
        # run in parallel; between groups, ORDER is dictated by the server.
        # `serial=True` -> the group is NOT parallelized (the order is imposed by the
        # server: playbooks that reference each other, intents that
        # reference their entity types).
        grupos: list[tuple[str, list[tuple[str, str, str, dict | None]], bool]] = []

        # 1. flows (force): with them the pages and their references fall.
        flows = []
        for rec in estado.get("flows") or []:
            rid = self._id_de(rec)
            if rid == DEFAULT_FLOW:
                continue          # the default flow cannot be deleted
            flows.append(("flows", rid, rec.get("displayName") or rid, {"force": "true"}))
        grupos.append(("flows", flows, True))

        # 2. playbooks: INVERSE order of dependencies (reference first).
        pbs = [(self._id_de(p), p) for p in (estado.get("playbooks") or [])]
        pbs_orden = list(reversed(self._orden_por_deps(
            pbs, lambda r: self._refs_nombres(r.get("referencedPlaybooks")))))
        grupos.append(("playbooks",
                       [("playbooks", rid, rec.get("displayName") or rid, None)
                        for rid, rec in pbs_orden], True))
        # 3. the rest. Intents go BEFORE than the entityTypes (an intent
        # references its entity types) -> separate group.
        for coll in ("intents", "entityTypes", "tools", "webhooks", "generators"):
            lote = []
            for rec in estado.get(coll) or []:
                rid = self._id_de(rec)
                if coll == "intents" and rid in DEFAULT_INTENTS:
                    continue      # default intents are not deleted
                if coll == "tools" and rid.startswith("df-"):
                    continue      # Dialogflow builtin tool: it is not deleted
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
        """Delete a group of elements.

        The `playbooks` are deleted IN SERIES (their order is imposed by the
        server: the one that references, first). The rest of the group are
        independent and are deleted with `self.escrituras` threads: measured, N=2 is
        the optimum (2.10 ops/s versus 1.75 in series; with N>=3 the backend
        locks up and throughput LOW)."""
        def _uno(item: tuple[str, str, str, dict | None]) -> bool:
            coll, rid, dn, query = item
            url = self._url(self.destino, f"/{coll}/{rid}")
            data = self._escribir("borrado", f"DELETE {coll} {dn[:28]}", "DELETE",
                                  url, query=query)
            if data is None and coll == "playbooks":
                # Without `force`: the remaining references are removed and it
                # retries once.
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
        """Empty the routes of the remaining flows (only once)."""
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
        """Current elements of an agent (parallel reads)."""
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

    # ============================================================ 3. RECREATE
    @staticmethod
    def _spec_invalida(body: dict[str, Any]) -> bool:
        """Is the `openApiSpec` of this tool one of those that API rejects?

        The API responds "Found errors in the Open API specification [Null or
        empty definition]" when the spec comes without `textSchema` (measured: one
        tool with `{'authentication': ...}` and another with `{}`).

        NOTE with the two false positives:
          * a tool WITHOUT `openApiSpec` (the data store ones, which carry
            `dataStoreSpec`) has nothing to remove -> its spec is valid;
          * a tool with `dataStoreSpec` is created that way, not by the spec.

        Detecting it BEFORE of the POST saves a failed write."""
        spec = body.get("openApiSpec")
        if not isinstance(spec, dict):
            return False            # no spec: there is nothing to remove
        if body.get("dataStoreSpec"):
            return False            # the tool goes through the data store
        texto = spec.get("textSchema")
        return not (isinstance(texto, str) and texto.strip())

    def _orden_flows(self, flows: list[dict]) -> list[dict]:
        """Flow creation order: AFTER of the flows they reference.

        A flow route can point to another flow. If that target does not exist
        when the flow is created, the API responds 400 (it validates the targets)."""
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
        puestos: set[str] = {DEFAULT_FLOW}      # already exists in any agent
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
                # Cycle between flows (rare): broken by entry order.
                f = pendientes.pop(0)
                orden.append(f)
                puestos.add(self._id_de(f))
        return orden

    def _rutas_incluibles(self, f: dict) -> tuple[list, list, bool]:
        """Routes of a flow that can be put into its own POST.

        Only if ALL its routes are resolvable at flow creation time:
        no page target (pages come later), no playbook target (playbooks
        come later) and with the target flow already created.

        Returns (routes, handlers, complete). If `completo` is False, the flow
        still needs the PATCH of routes (and will be created without them)."""
        rutas = list(f.get("transitionRoutes") or [])
        handlers = list(f.get("eventHandlers") or [])
        if not rutas and not handlers:
            return [], [], True

        def ok(r: dict) -> bool:
            if r.get("targetPage"):
                # `END_FLOW` / `END_FLOW_WITH_HUMAN_ESCALATION` are not pages:
                # they are end-of-flow markers and ALWAYS are valid.
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
        """Create in the clone everything from the original, with the ids remapped."""
        creado = {"entityTypes": 0, "intents": 0, "webhooks": 0, "tools": 0,
                  "playbooks": 0, "generators": 0, "flows": 0, "pages": 0,
                  "flow_routes": 0, "agent": 0}

        # --- 3.1 leaf resources: entityTypes, intents, webhooks, tools --------
        # (playbooks are AFTER of the flows: they reference `referencedFlows`)
        for coll in ("entityTypes", "intents", "webhooks", "tools"):
            for rec in snap.get(coll) or []:
                old_id = (rec.get("name") or "").split("/")[-1]
                if coll == "intents" and old_id in DEFAULT_INTENTS:
                    # Default intents exist in ALL agent: they are not
                    # created, but their name is remapped to itself.
                    self.mapa[_suffix("intents", old_id)] = _suffix("intents", old_id)
                    continue
                if coll == "tools" and old_id.startswith("df-"):
                    # Tool BUILTIN of Dialogflow (code-interpreter): it already exists in
                    # any agent and the API rejects creating it by hand.
                    self.mapa[_suffix("tools", old_id)] = _suffix("tools", old_id)
                    self.omitidos.append(f"tool builtin {rec.get('displayName')}")
                    continue
                body = self._remap(self._body_limpio(rec))
                # API rejects a `openApiSpec` WITHOUT `textSchema` ("Null or
                # empty definition"). Detected BEFORE before sending it: this
                # saves the failed POST and the error does not pollute the report.
                # NOTE: this is ONLY for `tools` (the other resources carry no spec) and the URL is ALWAYS that of ITS collection.
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
                    # The original spec is not accepted by API (empty OpenAPI ->
                    # "Null or empty definition"; data store deleted -> "Failed
                    # to connect"). It is recreated WITHOUT spec: the element exists and
                    # the playbooks that reference it no longer fail.
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

        # --- 3.2 flows (resolvable routes go in POST itself) ---------
        # A flow is created AFTER from the flows its routes point to, and
        # if ALL its routes are resolvable at that moment, they are created with them
        # -> that flow no longer needs the PATCH of routes (one less write).
        flows = snap.get("flows") or []
        for f in self._orden_flows(flows):
            old_id = (f.get("name") or "").split("/")[-1]
            if old_id == DEFAULT_FLOW:
                # The default flow already exists in the clone: it remaps to itself.
                # Its routes must be set to ALWAYS with a PATCH (it cannot be
                # created again).
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
                # The backend did not accept the routes in POST: it is created without them
                # and will be left for the PATCH of section 3.7.
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

        # --- 3.3 playbooks (reference TOOLS and FLOWS: they come after both) -----
        # And they reference EACH OTHER (`referencedPlaybooks`): topological order.
        # Special case: a playbook can reference ITSELF SAME (the
        # orchestrator does) -> in the POST that reference cannot yet
        # exist, so it is created without it and added with a PATCH afterwards.
        for old_id, rec in self._orden_por_deps(
                [(self._id_de(rec), rec) for rec in (snap.get("playbooks") or [])],
                lambda r: self._refs_nombres(r.get("referencedPlaybooks"))):
            body_full = self._remap(self._body_limpio(rec))
            refs = body_full.get("referencedPlaybooks") or []
            propios = [x for x in refs if x.split("/")[-1] == old_id]
            body = copy.deepcopy(body_full)
            if propios:
                # Self-reference is a loop: API rejects it.
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

        # --- 3.4 generators (may reference playbooks) -----------------
        for rec in snap.get("generators") or []:
            old_id = (rec.get("name") or "").split("/")[-1]
            body = self._remap(self._body_limpio(rec))
            data = self._escribir("creacion", f"POST generator {rec.get('displayName','')[:24]}",
                                  "POST", self._url(self.destino, "/generators"), body=body)
            new_id = self._nuevo_id(data)
            if new_id:
                self.mapa[_suffix("generators", old_id)] = _suffix("generators", new_id)
                creado["generators"] += 1

        # --- 3.5 pages in topological order (routes validate targets) --
        orden = self._orden_paginas(snap)
        for flow_old, page in orden:
            flow_new = self.mapa.get(_suffix("flows", flow_old), "").split("/")[-1]
            if not flow_new:
                continue
            old_page = (page.get("name") or "").split("/")[-1]
            body = self._body_limpio(page)
            # Targets that do not exist yet (cycles): the page is created without
            # routes OR handlers and patched afterwards. The special target
            # `END_FLOW` is not a page: it does not count.
            # Routes are stored WITHOUT remapping: they are remapped in the PATCH,
            # once the full map exists (if remapped here, pending
            # targets keep the ORIGINAL id).
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

        # --- 3.6 patches for pages with pending targets -------------
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

        # --- 3.7 routes and eventHandlers of the flows -------------------------
        # Only the flows whose routes could NOT go in their POST (page or
        # playbook targets, which do not exist yet).
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

        # --- 3.8 agent-level adjustments ----------------------------------
        creado["agent"] = self._parche_agente(snap.get("agente") or {})
        creado["paginas_pendientes"] = len(self._pendientes_pagina)
        return creado

    _pendientes_pagina: list[tuple[str, str, list]] = []

    @staticmethod
    def _id_de(rec: dict[str, Any]) -> str:
        return (rec.get("name") or "").split("/")[-1]

    @staticmethod
    def _refs_nombres(valor: Any) -> set[str]:
        """Ids (last segment) of a list of resource names."""
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
        """Topological order: each resource goes AFTER the ones it references.

        The API validates references on create (a page/playbook pointing
        to something nonexistent -> HTTP 400), so the order matters."""
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
                # cycle: created in whatever order remains (missing routes are
                # patched afterwards).
                orden.extend(resto)
                break
        return orden

    @classmethod
    def _sin_macros(cls, obj: Any) -> Any:
        """Neutralize the ${PLAYBOOK:x} macros (inert token)."""
        if isinstance(obj, str):
            return MACRO_PLAYBOOK.sub(lambda m: "{{PLAYBOOK:" + m.group(1) + "}}", obj)
        if isinstance(obj, list):
            return [cls._sin_macros(x) for x in obj]
        if isinstance(obj, dict):
            return {k: cls._sin_macros(v) for k, v in obj.items()}
        return obj

    @classmethod
    def _neutralizar_autorref(cls, obj: Any, propio: str) -> Any:
        """Neutralize ONLY the `${PLAYBOOK:<propio>}` macro.

        The API rejects loops: "Playbook X caused loop in playbook routes"
        (measured). A playbook that mentions itself cannot be created
        or updated via API. In the original that self-reference
        appears inside a text that FORBIDS invoke itself, so
        neutralizing it does not change the agent's behavior.

        The remaining macros (${FLOW:x}, ${TOOL:x}, ${PLAYBOOK:otro}) are left
        intact: the API validates them and accepts them if the target exists."""
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
        """Top-level fields with a macro that self-references."""
        out = []
        for k, v in body.items():
            if k in ("instruction", "codeBlock", "goal") and \
                    re.search(r"\$\{PLAYBOOK:" + re.escape(propio) + r"\}",
                              json.dumps(v, ensure_ascii=False)):
                out.append(k)
        return out

    def _optimizar_orden(self, orden: list[tuple[str, dict]],
                         deps: dict[str, set[str]]) -> list[tuple[str, dict]]:
        """Local search: minimizes the pages that must be PATCH afterwards.

        A page is "pendiente" if, when created, it has a target that
        does not exist yet (cycle): it is created without those routes and patched later.

        The inevitable minimum comes from the CYCLES of the page graph: within
        a strongly connected component of k pages, the penultimate one created
        still has an unresolved target, so **at least k-1 pages
        of each cycle require PATCH** (measured: 2 cycles). The greedy method
        stopped at 7; here it is lowered to that minimum with a stronger local search:
        several starting orders + full swaps per pass.

        Each PATCH avoided is ~470 ms of write (the backend serializes)."""
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
            """Local descent: tries ALL swaps and repeats."""
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

        # Starting orders: the greedy one (above) and its per-block variations.
        partidas = [list(orden)]
        invertida = list(reversed(orden))
        partidas.append(invertida)
        # and two rotations: they cut the cycle at another point.
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
        """Minimum unavoidable PATCHes: (k-1) per graph cycle."""
        nodos = set(deps)
        # Iterative Tarjan -> strongly connected components
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
        """Global topological order of the pages (the API validates targets)."""
        todas: list[tuple[str, dict]] = []
        for flow_old, pgs in (snap.get("pages") or {}).items():
            for p in pgs:
                todas.append((flow_old, p))
        # dependency: page -> pages its routes point to
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
                # CYCLE: breaks by choosing the page with FEWER unresolved targets
                # (it is the one that leaves the fewest pending PATCHes).
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
        # Dependencies (page targets) to optimize the order.
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
        """Copy the agent settings (without identity: name/displayName).

        `defaultLanguageCode` is NOT copied: the API responds "Agent default
        language code cannot be updated" (measured)."""
        copia = dict(agente_orig)
        for k in ("name", "displayName", "description", "projectNumber",
                  "supportedLanguageCodes", "startPlaybook", "defaultLanguageCode"):
            copia.pop(k, None)
        if not copia:
            return 0
        copia = self._remap(copia)
        # The clone's default flow has the same reserved id.
        if "startFlow" in copia:
            copia["startFlow"] = self._remap_str(agente_orig.get("startFlow", ""))
        mask = ",".join(sorted(copia.keys()))
        self._escribir("creacion", "PATCH agente (ajustes)",
                       "PATCH", self._url(self.destino, ""),
                       body=copia, query={"updateMask": mask})
        return 1

    # ============================================================ 4. VERIFY
    @staticmethod
    def _firma_pagina(p: dict[str, Any]) -> dict[str, Any]:
        """Comparable signature of a page: routes, handlers and target type.

        Ids are ignored (they differ by definition: they are remapped), but the
        GRAPH of transitions must be the same."""
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
        """Comparable signature of a tool: the number alone is not enough.

        The tool count does not reveal that 23 of them were recreated WITHOUT their
        `openApiSpec` (that is only visible in the CONTENT)."""
        spec = t.get("openApiSpec")
        texto = spec.get("textSchema") if isinstance(spec, dict) else None
        return {
            "spec": bool(spec),
            "textSchema_len": len(texto) if isinstance(texto, str) else 0,
            "dataStore": bool(t.get("dataStoreSpec")),
            "inlineActions": len(t.get("inlineActions") or []),
        }

    def verificar(self, snap: dict[str, Any]) -> dict[str, Any]:
        """Compare the recreated clone with the snapshot of the original."""
        estado = self._estado(self.destino)
        dif: dict[str, Any] = {}
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "generators"):
            orig = snap.get(coll) or []
            nuevo = estado.get(coll) or []
            dif[coll] = {"original": len(orig), "clon": len(nuevo),
                         "ok": len(orig) == len(nuevo)}
        # Tools: in addition to the number, the CONTENT (spec). A regression that
        # removed the openApiSpec from 20 tools would NOT show up in the count.
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
        # ROUTES of the flows: the number alone is not enough. If the backend ignored
        # the `transitionRoutes` of a POST (as it ignores the `pages`), the
        # clone would be left without routes and that shows here.
        firma_o = {f.get("displayName"): self._firma_pagina(f) for f in flows_o}
        firma_n = {f.get("displayName"): self._firma_pagina(f) for f in flows_n}
        det_flow = {k: [firma_o.get(k), firma_n.get(k)]
                    for k in set(firma_o) | set(firma_n)
                    if firma_o.get(k) != firma_n.get(k)}
        dif["flows"]["rutas_iguales"] = not det_flow
        dif["flows"]["detalle_rutas"] = det_flow
        dif["flows"]["ok"] = dif["flows"]["ok"] and not det_flow

        # Pages: by flow (displayName) and by page (displayName), with the
        # routing signature (not just the number).
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
        """Run the full clone and return the report with timings.

        The snapshot (reads from ORIGINAL) and the cleanup (writes to the
        CLONE) are INDEPENDENT -> they overlap in one thread: saves ~4 s.

        `ms_fases` = WALL time of each phase (what it actually takes);
        `ms_suma_por_fase` = sum of the times of each call (greater than the
        wall time when the reads run in parallel)."""
        import threading

        t0 = time.perf_counter()
        caja: dict[str, Any] = {}

        def _snapshot() -> None:
            caja["snap"] = self.snapshot()

        th = threading.Thread(target=_snapshot, name="snapshot-original")
        th.start()
        limpio = self.limpiar()          # in parallel with the snapshot
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
