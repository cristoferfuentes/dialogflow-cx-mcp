"""Cliente del backend interno de la consola de Dialogflow CX.

Este backend (`dialogflow.clients6.google.com/{version}`) es el que usa la
consola "Conversational Agents" y **sí** expone `targetPlaybook` en las
`transitionRoutes` (el proto publico lo descarta en silencio). Acepta la
**autenticacion de primera parte**: las cookies de Google del navegador y una
firma `SAPISIDHASH`.

Sirve las tres versiones (`v3alpha1`, `v3beta1`, `v3`) con el mismo discovery
doc que la API publica: por eso el MCP puede ser un **espejo de v3beta1** y
ademas hacer lo que la API publica no puede (destino a playbook).

RECETA (medida, no inventada)
------------------------------
Cabeceras minimas que hacen que el backend acepte la llamada:

    authorization: SAPISIDHASH ts_h SAPISID1PHASH ts_h SAPISID3PHASH ts_h
    cookie:        las 5 cookies de sesion de `.google.com`
    x-origin:      <console_origin>
    x-referer:     <console_origin>
    x-goog-authuser: 0
    referer:       https://dialogflow.clients6.google.com/static/proxy.html?usegapi=1

* `h = sha1(f"{ts} {valor_de_SAPISID} {console_origin}")`, `ts` = epoch actual.
  Los tres hash son IGUALES: la consola usa el valor de `SAPISID` para los tres.
* Las cookies **obligatorias** son exactamente 5: `SAPISID`, `SID`, `HSID`,
  `SSID`, `APISID` (medido: con 4 de 5 -> HTTP 401).
* **No mandar cabecera `Origin`**: si se manda, el servidor exige que `Origin` y
  `Referer` coincidan -> HTTP 400 "Origin and Referer header don't match". El
  origen de la app va en `X-Origin`/`X-Referer`.
* Se puede ir en HTTP/1.1: el h2 no es necesario (medido).
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlencode

from .config import Config
from .guard import (
    WRITE_VERBS,
    PermissionDeniedError,
    agent_from_path,
    assert_readable,
    assert_writable,
)
from .httpclient import HttpStatusError, Session

CONSOLE_HOST = "dialogflow.clients6.google.com"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/157.0.0.0 Safari/537.36")

# Mascara de actualizacion: la misma que usa la consola al guardar una pagina.
UPDATE_MASK = (
    "name,displayName,description,entryFulfillment,form,transitionRouteGroups,"
    "agentTransitionRouteGroups,transitionRoutes,eventHandlers,advancedSettings,"
    "speakerIdSettings,etag,flow,knowledgeConnectorSettings"
)

# Campos que el servidor devuelve pero que NO se pueden (o no se deben) escribir.
READ_ONLY_FIELDS = frozenset({
    "etag", "derivedTransitions", "nluSettingsList", "createTime", "updateTime",
    "parent", "session", "environment",
})


class ConsoleApiError(RuntimeError):
    pass


def _parse_multipart(text: str, boundary: str) -> list[dict[str, Any]]:
    """Parte una respuesta `multipart/mixed` del endpoint `/batch`.

    Cada parte trae una respuesta HTTP embebida:
        Content-Type: application/http
        Content-ID: <response-1>

        HTTP/1.1 200 OK
        Content-Type: application/json

        {"name": ...}
    """
    out: list[dict[str, Any]] = []
    delim = "--" + boundary
    for chunk in text.split(delim)[1:]:
        chunk = chunk.lstrip("\r\n")
        if chunk.startswith("--"):        # cierre del multipart
            break
        if "\r\n\r\n" not in chunk:
            continue
        _part_head, rest = chunk.split("\r\n\r\n", 1)
        if "\r\n\r\n" in rest:
            resp_head, body = rest.split("\r\n\r\n", 1)
        else:
            resp_head, body = rest, ""
        status = 0
        first = resp_head.split("\r\n")[0]
        parts = first.split(" ")
        if len(parts) > 1 and parts[1].isdigit():
            status = int(parts[1])
        body = body.rstrip("\r\n")
        try:
            data = json.loads(body) if body.strip() else {}
        except ValueError:
            data = {"_raw": body[:400]}
        out.append({"status": status, "data": data})
    return out


def sapisid_authorization(origin: str, sapisid: str, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    h = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h} SAPISID1PHASH {ts}_{h} SAPISID3PHASH {ts}_{h}"


class ConsoleApi:
    """Cliente del proto interno de la consola (primera parte)."""

    def __init__(self, cfg: Config, session: Session | None = None):
        self.cfg = cfg
        self.http = session or Session(timeout=cfg.socket_timeout, maxseg=cfg.tcp_maxseg,
                                        retries=cfg.http_retries,
                                        connect_timeout=cfg.connect_timeout)
        self._sapisid = cfg.sapisid
        self._cookie = cfg.cookie_header()
        # Cache de catalogos: (kind, agent, project) -> (ts, valor)
        self._catalog: dict[tuple[str, str, str], tuple[float, Any]] = {}

    # ------------------------------------------------------------- catalogos
    def _cached(self, key: tuple[str, str, str], factory) -> Any:
        now = time.time()
        hit = self._catalog.get(key)
        if hit and now - hit[0] < self.cfg.catalog_ttl:
            return hit[1]
        value = factory()
        self._catalog[key] = (now, value)
        return value

    def invalidate(self, kind: str, agent_id: str = "") -> None:
        """Olvida un catalogo (se llama al escribir en ese recurso)."""
        for k in list(self._catalog):
            if k[0] == kind and (not agent_id or k[1] == agent_id):
                self._catalog.pop(k, None)

    def flow_index(self, agent_id: str, project: str = "",
                   force: bool = False) -> tuple[dict[str, str], dict[str, str]]:
        """(displayName->id, id->displayName) de los flows. Cacheado."""
        def build() -> tuple[dict[str, str], dict[str, str]]:
            by_display = {f.get("displayName"): f["name"].split("/")[-1]
                          for f in self.list_flows(agent_id, project)
                          if f.get("displayName")}
            return by_display, {i: n for n, i in by_display.items()}
        if force:
            self.invalidate("flows", agent_id)
        return self._cached(("flows", agent_id, project), build)

    def page_index(self, agent_id: str, flow_id: str, project: str = "",
                   force: bool = False) -> tuple[dict[str, str], dict[str, str]]:
        """(displayName->id, id->displayName) de las paginas de un flow."""
        def build() -> tuple[dict[str, str], dict[str, str]]:
            by_display = {p.get("displayName"): p["name"].split("/")[-1]
                          for p in self.list_pages(agent_id, flow_id, project)
                          if p.get("displayName")}
            return by_display, {i: n for n, i in by_display.items()}
        if force:
            self.invalidate("pages", agent_id)
        return self._cached(("pages", agent_id, project + "/" + flow_id), build)

    def playbook_index(self, agent_id: str, project: str = "",
                       force: bool = False) -> tuple[dict[str, str], dict[str, str]]:
        """(displayName->id, id->displayName) de los playbooks. Cacheado.

        OJO: el listado de playbooks del clon pesa **770 KB** (lleva goal,
        instruction, referencedPlaybooks...). Para resolver un nombre no hace
        falta traerlo entero, pero la API no ofrece mascara -> se cachea.
        """
        def build() -> tuple[dict[str, str], dict[str, str]]:
            by_display = {pb.get("displayName"): pb["name"].split("/")[-1]
                          for pb in self.list_playbooks(agent_id, project)
                          if pb.get("displayName")}
            return by_display, {i: n for n, i in by_display.items()}
        if force:
            self.invalidate("playbooks", agent_id)
        return self._cached(("playbooks", agent_id, project), build)

    # ------------------------------------------------------------- cabeceras
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "authorization": sapisid_authorization(self.cfg.console_origin, self._sapisid),
            "cookie": self._cookie,
            "referer": f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1",
            "user-agent": USER_AGENT,
            "x-goog-authuser": "0",
            "x-origin": self.cfg.console_origin,
            "x-referer": self.cfg.console_origin,
        }
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str, **params: str) -> str:
        q = {"key": self.cfg.api_key}
        q.update({k: v for k, v in params.items() if v is not None})
        return f"https://{CONSOLE_HOST}/{self.cfg.api_version}{path}?{urlencode(q)}"

    def _base(self, project: str = "") -> str:
        return (f"/projects/{project or self.cfg.project}"
                f"/locations/{self.cfg.location}")

    # ---------------------------------------------------------------- nucleo
    def request(self, method: str, url: str, body: dict[str, Any] | None = None,
                query: dict[str, Any] | None = None, timeout: float | None = 300.0,
                update_mask: str | None = None) -> Any:
        """Llamada cruda al backend. `url` incluye ya la version y el path.

        **Aqui se aplica el permiso, en la capa baja**: cualquier verbo de
        escritura pasa por `assert_writable` y cualquier lectura por
        `assert_readable`, deducidos del agente que aparece en el path. Asi
        ningun camino (ni siquiera el passthrough generico `cx_api`) puede
        saltarse la lista blanca de agentes.
        """
        verb = method.upper()
        project, agent = agent_from_path(url)
        if agent:
            try:
                if verb in WRITE_VERBS:
                    assert_writable(project, agent, self.cfg)
                else:
                    assert_readable(project, agent, self.cfg)
            except PermissionDeniedError as exc:
                # La URL hace falta para saber QUE peticion se rechazo.
                raise PermissionDeniedError(f"{exc} [url={url}]") from exc
        if query:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode({k: v for k, v in query.items()
                                         if v is not None})
        if update_mask:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode({"updateMask": update_mask})
        headers = self._headers({"content-type": "application/json"} if body is not None else None)
        return self.http.json(verb, url, headers, body, timeout=timeout)

    # ------------------------------------------------------------- lecturas
    def list_agents(self, project: str = "") -> list[dict]:
        url = self._url(f"{self._base(project)}/agents", pageSize="200")
        data = self.http.json("GET", url, self._headers())
        return data.get("agents", [])  # type: ignore[union-attr]

    def list_flows(self, agent_id: str, project: str = "") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows",
                        pageSize="200", languageCode="es")
        data = self.http.json("GET", url, self._headers())
        return data.get("flows", [])  # type: ignore[union-attr]

    def get_flow(self, agent_id: str, flow_id: str, project: str = "",
                 language_code: str | None = "es") -> dict:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}",
                        languageCode=language_code)
        return self.http.json("GET", url, self._headers())  # type: ignore[return-value]

    def list_pages(self, agent_id: str, flow_id: str, project: str = "") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}/pages",
                        pageSize="200", languageCode="es")
        data = self.http.json("GET", url, self._headers())
        return data.get("pages", [])  # type: ignore[union-attr]

    def get_page(self, agent_id: str, flow_id: str, page_id: str,
                 project: str = "", language_code: str | None = "es") -> dict:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}/pages/{page_id}",
                        languageCode=language_code)
        return self.http.json("GET", url, self._headers())  # type: ignore[return-value]

    def list_playbooks(self, agent_id: str, project: str = "") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/playbooks", pageSize="200")
        data = self.http.json("GET", url, self._headers())
        return data.get("playbooks", [])  # type: ignore[union-attr]

    def playbooks_by_display_name(self, agent_id: str, project: str = "") -> dict[str, str]:
        return {pb.get("displayName"): pb["name"].split("/")[-1]
                for pb in self.list_playbooks(agent_id, project) if pb.get("displayName")}

    def _list_child(self, agent_id: str, collection: str, project: str = "",
                    page_size: str = "200") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/{collection}",
                        pageSize=page_size)
        data = self.http.json("GET", url, self._headers())
        return data.get(collection, [])  # type: ignore[union-attr]

    def list_intents(self, agent_id: str, project: str = "") -> list[dict]:
        return self._list_child(agent_id, "intents", project)

    def list_entity_types(self, agent_id: str, project: str = "") -> list[dict]:
        return self._list_child(agent_id, "entityTypes", project)

    def list_tools(self, agent_id: str, project: str = "") -> list[dict]:
        return self._list_child(agent_id, "tools", project)

    def list_webhooks(self, agent_id: str, project: str = "") -> list[dict]:
        return self._list_child(agent_id, "webhooks", project)

    def list_transition_route_groups(self, agent_id: str, project: str = "") -> list[dict]:
        return self._list_child(agent_id, "transitionRouteGroups", project)

    def list_generators(self, agent_id: str, project: str = "") -> list[dict]:
        return self._list_child(agent_id, "generators", project)

    def list_conversations(self, agent_id: str, project: str = "",
                           page_size: str = "50") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/conversations",
                        pageSize=page_size)
        data = self.http.json("GET", url, self._headers())
        return data.get("conversations", [])  # type: ignore[union-attr]

    def get_conversation(self, agent_id: str, conversation_id: str,
                         project: str = "") -> dict:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}"
                        f"/conversations/{conversation_id}")
        return self.http.json("GET", url, self._headers())  # type: ignore[return-value]

    def list_changelogs(self, agent_id: str, project: str = "") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/changelogs",
                        pageSize="50")
        data = self.http.json("GET", url, self._headers())
        return data.get("changelogs", [])  # type: ignore[union-attr]

    # ------------------------------------------------------------------ batch
    def batch(self, requests: list[dict[str, Any]], timeout: float | None = 300.0,
              project: str = "") -> list[dict[str, Any]]:
        """Varias llamadas a la API en UNA sola peticion HTTP (`/batch`).

        El descriptor de v3beta1 declara `batchPath: "batch"`: el frontend de
        Google acepta un `multipart/mixed` con N sub-peticiones HTTP y las
        ejecuta en el servidor. Esto convierte N viajes de ida y vuelta (cada
        uno ~0,5 s de latencia) en uno solo.

        **Medido (2026-11-08):** las escrituras al MISMO agente las serializa
        el backend (8 en paralelo = 899 ms cada una, peor que 500 ms en
        serie), asi que el batch no las hace mas rapidas: sirve para
        empaquetar el viaje, no para paralelizar el efecto.

        Cada sub-peticion: {"method", "path", "body"?, "query"?}. `path` es
        relativo a la version ("projects/p/locations/l/agents/a/flows").
        La **guarda se aplica a cada sub-peticion**: un batch que mezcle
        agentes de lectura y escritura se evalua sub a sub.
        """
        if not requests:
            return []
        boundary = "cx_batch_boundary_3f9a"
        chunks: list[str] = []
        for i, req in enumerate(requests):
            method = (req.get("method") or "GET").upper()
            path = req.get("path") or ""
            if not path:
                raise ConsoleApiError(f"sub-peticion {i} sin path")
            # La guarda, sub a sub: nadie se cuela por el batch.
            project_r, agent_r = agent_from_path(path)
            if agent_r:
                if method in WRITE_VERBS:
                    assert_writable(project_r or project or self.cfg.project, agent_r, self.cfg)
                else:
                    assert_readable(project_r or project or self.cfg.project, agent_r, self.cfg)
            q = {"key": self.cfg.api_key}
            q.update({k: v for k, v in (req.get("query") or {}).items() if v is not None})
            url = f"/{self.cfg.api_version}/{path.lstrip('/')}?{urlencode(q)}"
            body = req.get("body")
            payload = json.dumps(body) if body is not None else ""
            chunks.append(
                f"--{boundary}\r\n"
                f"Content-Type: application/http\r\n"
                f"Content-ID: <{i + 1}>\r\n"
                f"\r\n"
                f"{method} {url} HTTP/1.1\r\n"
                f"Content-Type: application/json\r\n"
                f"\r\n"
                f"{payload}\r\n"
            )
        payload_bytes = ("".join(chunks) + f"--{boundary}--\r\n").encode("utf-8")
        headers = self._headers({"content-type": f"multipart/mixed; boundary={boundary}"})
        url = f"https://{CONSOLE_HOST}/{self.cfg.api_version}/batch?key={self.cfg.api_key}"
        status, text = self.http.request("POST", url, headers, payload_bytes,
                                         timeout=timeout)
        if status >= 400:
            raise HttpStatusError(status, text)
        if text.lstrip().startswith("{"):
            # El servidor respondio con el formato JSON de ESF.
            try:
                data = json.loads(text)
            except ValueError:
                return [{"status": 0, "data": {"_raw": text[:400]}}]
            responses = data.get("responses") or []
            return [{"status": int(r.get("status") or r.get("httpStatus") or 0),
                     "data": r.get("body") if isinstance(r.get("body"), (dict, list))
                     else r.get("body", {})}
                    for r in responses]
        return _parse_multipart(text, boundary)

    def batch_secundario(self, requests: list[dict[str, Any]],
                         project: str = "", hilos: int = 8) -> list[dict[str, Any]]:
        """Rodeo de `batch()`: las sub-peticiones, en HILOS.

        **Medido (2026-11-08):** el backend interno NO implementa `/batch`
        (HTTP 404: el descriptor lo declara, pero clients6 no lo sirve). Este
        metodo hace las mismas llamadas N en paralelo (hilos), que es lo que
        el batch pretendia: un viaje por sub-peticion, pero a la vez.

        OJO: el backend serializa las ESCRITURAS del mismo agente, asi que
        el paralelismo solo compensa en las LECTURAS.
        """
        from concurrent.futures import ThreadPoolExecutor

        def uno(req: dict[str, Any]) -> dict[str, Any]:
            method = str(req.get("method") or "GET").upper()
            path = req.get("path") or ""
            q = {"key": self.cfg.api_key}
            q.update({k: v for k, v in (req.get("query") or {}).items() if v is not None})
            # URL ABSOLUTA: el cliente HTTP resuelve el host (una ruta
            # relativa -> gaierror "No address associated with hostname").
            url = (f"https://{CONSOLE_HOST}/{self.cfg.api_version}/"
                   f"{path.lstrip('/')}?{urlencode(q)}")
            headers = self._headers({"content-type": "application/json"}
                                    if req.get("body") is not None else None)
            try:
                data = self.http.json(method, url, headers, req.get("body"), timeout=300.0)
                return {"status": 200, "data": data}
            except HttpStatusError as exc:
                return {"status": exc.status, "data": {"error": exc.body[:400]}}
        if not requests:
            return []
        with ThreadPoolExecutor(max_workers=min(hilos, len(requests))) as pool:
            return list(pool.map(uno, requests))

    # ------------------------------------------------------------- escritura
    def patch_page(self, agent_id: str, flow_id: str, page_id: str,
                   body: dict[str, Any], update_mask: str = UPDATE_MASK,
                   language_code: str | None = "es", project: str = "",
                   timeout: float | None = 300.0) -> dict:
        assert_writable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}"
                        f"/pages/{page_id}",
                        alt="json", updateMask=update_mask, languageCode=language_code)
        headers = self._headers({"content-type": "application/json"})
        return self.http.json("PATCH", url, headers, body, timeout=timeout)  # type: ignore[return-value]

    def patch_flow(self, agent_id: str, flow_id: str, body: dict[str, Any],
                   update_mask: str | None = None, language_code: str | None = "es",
                   project: str = "", timeout: float | None = 300.0) -> dict:
        """PATCH de un flow. La mascara sale de las claves del cuerpo si no se pasa."""
        assert_writable(project or self.cfg.project, agent_id, self.cfg)
        if not update_mask:
            update_mask = ",".join(sorted(k for k in body if k not in READ_ONLY_FIELDS))
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}",
                        alt="json", updateMask=update_mask, languageCode=language_code)
        headers = self._headers({"content-type": "application/json"})
        return self.http.json("PATCH", url, headers, body, timeout=timeout)  # type: ignore[return-value]

    def set_transition_routes(self, agent_id: str, flow_id: str, page_id: str,
                              routes: list[dict], timeout: float | None = 300.0,
                              project: str = "") -> dict:
        """Escribe la lista COMPLETA de transition routes de una pagina.

        Es lo que hace la consola: manda el array entero en el PATCH. El `etag`
        se omite a proposito: el GET interno no lo devuelve y un etag viejo
        haria fallar el PATCH.
        """
        page = self.get_page(agent_id, flow_id, page_id, project=project)
        body = {
            "name": page.get("name"),
            "displayName": page.get("displayName"),
            "flow": (page.get("name") or "").rsplit("/pages/", 1)[0],
            "transitionRoutes": routes,
        }
        return self.patch_page(agent_id, flow_id, page_id, body, timeout=timeout,
                               project=project)

    def set_route_playbook(self, agent_id: str, flow_id: str, page_id: str,
                           route_ref: str | int, playbook_resource: str,
                           timeout: float | None = 300.0, project: str = "") -> dict:
        """Pone un playbook como destino de UNA transition route DE PAGINA."""
        from .routing import find_route

        page = self.get_page(agent_id, flow_id, page_id, project=project)
        routes = page.get("transitionRoutes") or []
        index, current = find_route(routes, route_ref)

        new_routes = copy.deepcopy(routes)
        route = new_routes[index]
        route.pop("targetPage", None)
        route.pop("targetFlow", None)
        route["targetPlaybook"] = playbook_resource

        body = {
            "name": page.get("name"),
            "displayName": page.get("displayName"),
            "flow": (page.get("name") or "").rsplit("/pages/", 1)[0],
            "transitionRoutes": new_routes,
        }
        self.patch_page(agent_id, flow_id, page_id, body, timeout=timeout, project=project)

        after = self.get_page(agent_id, flow_id, page_id, project=project)
        a_routes = after.get("transitionRoutes") or []
        return {
            "agente": agent_id, "flow": flow_id, "pagina": page_id,
            "ruta": {
                "indice": index,
                "name": current.get("name"),
                "condition": current.get("condition"),
                "destino": playbook_resource.split("/")[-1],
            },
            "rutas_totales": len(a_routes),
            "verificado": any(
                r.get("name") == current.get("name")
                and r.get("targetPlaybook") == playbook_resource
                for r in a_routes
            ),
        }

    # ------------------------------------------- EL AGREGADO: router de un flow
    def patch_resource(self, name: str, body: dict[str, Any],
                       update_mask: str | None = None, language_code: str | None = None,
                       project: str = "", timeout: float | None = 300.0) -> dict:
        """PATCH GENERICO de cualquier recurso, addressed por su `name`.

        `name` es el `name` completo que devuelve la API
        (`projects/p/locations/l/agents/a/flows/f/pages/p`). Con esto el MCP
        puede escribir en cualquier recurso del agente (agente, flow, pagina,
        playbook, tool, intent, entity type, webhook, grupo de rutas...) sin
        un metodo por recurso.

        Si no se pasa `update_mask`, sale de las claves del cuerpo (menos los
        campos derivados). Si no se pasa `language_code`, no se manda.
        """
        assert_writable(*agent_from_path(name), self.cfg)
        if not update_mask:
            update_mask = ",".join(sorted(k for k in body if k not in READ_ONLY_FIELDS))
        params: dict[str, Any] = {"alt": "json", "updateMask": update_mask}
        if language_code:
            params["languageCode"] = language_code
        url = self._url("/" + name, **params)
        headers = self._headers({"content-type": "application/json"})
        return self.http.json("PATCH", url, headers, body, timeout=timeout)  # type: ignore[return-value]

    @staticmethod
    def _agent_of_name(name: str) -> str:
        """El id del agente que aparece en el `name` de un recurso."""
        return agent_from_path(name)[1]

    def set_flow_route_playbook(self, agent_id: str, flow_id: str,
                                route_ref: str | int, playbook_resource: str,
                                timeout: float | None = 300.0, project: str = "") -> dict:
        """Pone un playbook como destino de UNA transition route DE FLOW.

        Esto es lo que la API publica NO puede expresar: `TransitionRoute.target`
        es un `oneof` con `target_page` y `target_flow`; `target_playbook` no
        existe y un PATCH que lo envie se descarta en silencio. El backend
        interno de la consola si lo conserva (medido: persiste y se lee).

        Escribe el array COMPLETO de rutas del flow (igual que la consola) y
        verifica releyendo.
        """
        from .routing import find_route

        flow = self.get_flow(agent_id, flow_id, project=project)
        routes = flow.get("transitionRoutes") or []
        index, current = find_route(routes, route_ref)

        new_routes = copy.deepcopy(routes)
        route = new_routes[index]
        route.pop("targetPage", None)
        route.pop("targetFlow", None)
        route["targetPlaybook"] = playbook_resource

        body = {k: v for k, v in flow.items() if k not in READ_ONLY_FIELDS}
        body["transitionRoutes"] = new_routes
        # Mascara estrecha: solo se escribe `transitionRoutes`; el resto del
        # flow no se toca (medido: el resto sobrevive al PATCH).
        self.patch_flow(agent_id, flow_id, body, update_mask="transitionRoutes",
                        timeout=timeout, project=project)

        after = self.get_flow(agent_id, flow_id, project=project)
        a_routes = after.get("transitionRoutes") or []
        return {
            "agente": agent_id, "flow": flow_id,
            "nivel": "flow (router del flow)",
            "ruta": {
                "indice": index,
                "name": current.get("name"),
                "condition": current.get("condition"),
                "destino": playbook_resource.split("/")[-1],
            },
            "rutas_totales": len(a_routes),
            "verificado": any(
                r.get("name") == current.get("name")
                and r.get("targetPlaybook") == playbook_resource
                for r in a_routes
            ),
        }
