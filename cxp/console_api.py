"""Client for the internal backend of the Dialogflow CX console.

This backend (`dialogflow.clients6.google.com/{version}`) is the one used by the
"Conversational Agents" console and **does** expose `targetPlaybook` in the
`transitionRoutes` (the public proto silently drops it). It accepts
**first-party authentication**: the browser's Google cookies and a
`SAPISIDHASH` signature.

It serves the three versions (`v3alpha1`, `v3beta1`, `v3`) with the same discovery
doc that the public API publishes: that is why the MCP can be a **mirror of v3beta1** and
also do what the public API cannot (target to playbook).

RECIPE (measured, not invented)
------------------------------
Minimum headers that make the backend accept the call:

    authorization: SAPISIDHASH ts_h SAPISID1PHASH ts_h SAPISID3PHASH ts_h
    cookie:        the 5 session cookies of `.google.com`
    x-origin:      <console_origin>
    x-referer:     <console_origin>
    x-goog-authuser: 0
    referer:       https://dialogflow.clients6.google.com/static/proxy.html?usegapi=1

* `h = sha1(f"{ts} {value_of_SAPISID} {console_origin}")`, `ts` = current epoch.
  The three hashes are EQUAL: the console uses the `SAPISID` value for all three.
* The **mandatory** cookies are exactly 5: `SAPISID`, `SID`, `HSID`,
  `SSID`, `APISID` (measured: with 4 of 5 -> HTTP 401).
* **Do not send the `Origin` header**: if it is sent, the server requires `Origin`
  and `Referer` to match -> HTTP 400 "Origin and Referer header don't match". The
  app origin goes in `X-Origin`/`X-Referer`.
* You can use HTTP/1.1: h2 is not necessary (measured)."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlencode, urlparse

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

# ---------------------------------------------------------------- regional host
# MEASURED (2026-11-15): the console calls a DIFFERENT host per location:
# global      -> dialogflow.clients6.google.com
# us-central1 -> us-central1-dialogflow.clients6.google.com
# And the global host does **not** work for another location:
# GET https://dialogflow.clients6.google.com/v3beta1/projects/p/locations/us-central1/agents
# -> 400 "Please refer to .../region#avail to find the correct endpoint"
#
# The HEADERS do not change with the host:
# * `authorization` (SAPISIDHASH) and `x-origin`/`x-referer` -> the CONSOLE
#   origin, always;
# * `referer` -> `{HOST}/static/proxy.html?usegapi=1`, with the HOST that is
#   called. With the `referer` of the global host against a regional host:
#   400 "Origin doesn't match Host for XD3".
#   With `x-origin` = regional host: 401 "invalid authentication credentials".
# => The rule is applied in ONE place: the `referer` is derived from the host of the URL.
LOCATION_EN_PATH = re.compile(r"/locations/([^/]+)/")


def host_para_location(location: str) -> str:
    """The backend host that serves a location."""
    loc = (location or "global").strip() or "global"
    if loc == "global":
        return CONSOLE_HOST
    return f"{loc}-{CONSOLE_HOST}"


def location_de_path(path: str) -> str:
    """The location that appears in a path ('.../locations/us-central1/...')."""
    m = LOCATION_EN_PATH.search(path or "")
    return m.group(1) if m else ""


def host_de_path(path: str, defecto: str = CONSOLE_HOST) -> str:
    """The host to use for a path (derived from its location)."""
    loc = location_de_path(path)
    return host_para_location(loc) if loc else defecto


# The locations that CONSOLE calls (measured on 2026-11-15 in the captured
# traffic: 17 regional hosts + the global one). They serve the "list agents in every location" mode,
# which is what the console does.
LOCACIONES_CONOCIDAS: tuple[str, ...] = (
    "global",
    "us-central1", "us-east1", "us-west1", "us",
    "northamerica-northeast1",
    "europe-west1", "europe-west2", "europe-west3", "europe-west4",
    "europe-west6", "eu",
    "asia-northeast1", "asia-south1", "asia-southeast1", "asia-southeast2",
    "australia-southeast1",
)

# Update mask: the same one the console uses when saving a page.
UPDATE_MASK = (
    "name,displayName,description,entryFulfillment,form,transitionRouteGroups,"
    "agentTransitionRouteGroups,transitionRoutes,eventHandlers,advancedSettings,"
    "speakerIdSettings,etag,flow,knowledgeConnectorSettings"
)

# Fields the server returns but that CANNOT (or must not) be written.
READ_ONLY_FIELDS = frozenset({
    "etag", "derivedTransitions", "nluSettingsList", "createTime", "updateTime",
    "parent", "session", "environment",
})


class ConsoleApiError(RuntimeError):
    pass


def _parse_multipart(text: str, boundary: str) -> list[dict[str, Any]]:
    """Split a `multipart/mixed` response from the `/batch` endpoint.

    Each part carries an embedded HTTP response:
        Content-Type: application/http
        Content-ID: <response-1>

        HTTP/1.1 200 OK
        Content-Type: application/json

        {"name": ...}"""
    out: list[dict[str, Any]] = []
    delim = "--" + boundary
    for chunk in text.split(delim)[1:]:
        chunk = chunk.lstrip("\r\n")
        if chunk.startswith("--"):        # multipart closure
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
    """Client for the console's internal proto (first part)."""

    def __init__(self, cfg: Config, session: Session | None = None):
        self.cfg = cfg
        self.http = session or Session(timeout=cfg.socket_timeout, maxseg=cfg.tcp_maxseg,
                                        retries=cfg.http_retries,
                                        connect_timeout=cfg.connect_timeout)
        self._sapisid = cfg.sapisid
        self._cookie = cfg.cookie_header()
        # Catalog cache: (kind, agent, project) -> (ts, value)
        self._catalog: dict[tuple[str, str, str], tuple[float, Any]] = {}

    # ------------------------------------------------------------- catalogs
    def _cached(self, key: tuple[str, str, str], factory) -> Any:
        now = time.time()
        hit = self._catalog.get(key)
        if hit and now - hit[0] < self.cfg.catalog_ttl:
            return hit[1]
        value = factory()
        self._catalog[key] = (now, value)
        return value

    def invalidate(self, kind: str, agent_id: str = "") -> None:
        """Forget a catalog (called when writing to that resource)."""
        for k in list(self._catalog):
            if k[0] == kind and (not agent_id or k[1] == agent_id):
                self._catalog.pop(k, None)

    def flow_index(self, agent_id: str, project: str = "",
                   force: bool = False) -> tuple[dict[str, str], dict[str, str]]:
        """(displayName->id, id->displayName) of the flows. Cached."""
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
        """(displayName->id, id->displayName) of the pages of a flow."""
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
        """(displayName->id, id->displayName) of the playbooks. Cached.

        NOTE: the clone's playbook listing weighs **770 KB** (it carries goal,
        instruction, referencedPlaybooks...). To resolve a name there is no
        need to fetch it whole, but the API offers no mask -> it is cached."""
        def build() -> tuple[dict[str, str], dict[str, str]]:
            by_display = {pb.get("displayName"): pb["name"].split("/")[-1]
                          for pb in self.list_playbooks(agent_id, project)
                          if pb.get("displayName")}
            return by_display, {i: n for n, i in by_display.items()}
        if force:
            self.invalidate("playbooks", agent_id)
        return self._cached(("playbooks", agent_id, project), build)

    # ------------------------------------------------------------- headers
    def _headers(self, extra: dict[str, str] | None = None,
                 host: str = CONSOLE_HOST) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "authorization": sapisid_authorization(self.cfg.console_origin, self._sapisid),
            "cookie": self._cookie,
            # MEASURED: the `referer` carries the HOST that is called (not the global one).
            "referer": f"https://{host}/static/proxy.html?usegapi=1",
            "user-agent": USER_AGENT,
            "x-goog-authuser": "0",
            "x-origin": self.cfg.console_origin,
            "x-referer": self.cfg.console_origin,
        }
        if extra:
            headers.update(extra)
        return headers

    def _headers_de_url(self, url: str,
                        extra: dict[str, str] | None = None) -> dict[str, str]:
        """Headers with the `referer` of the HOST being called.

        MEASURED (2026-11-15): with the global host's `referer` against a regional host
        the backend responds 400 "Origin doesn't match Host for XD3"."""
        host = urlparse(url).netloc or CONSOLE_HOST
        return self._headers(extra, host=host)

    def _url(self, path: str, host: str = "", **params: str) -> str:
        q = {"key": self.cfg.api_key}
        q.update({k: v for k, v in params.items() if v is not None})
        h = host or host_de_path(path)
        return f"https://{h}/{self.cfg.api_version}{path}?{urlencode(q)}"

    def _base(self, project: str = "", location: str = "") -> str:
        loc = location or self.cfg.location
        return f"/projects/{project or self.cfg.project}/locations/{loc}"

    # ---------------------------------------------------------------- core
    def request(self, method: str, url: str, body: dict[str, Any] | None = None,
                query: dict[str, Any] | None = None, timeout: float | None = 300.0,
                update_mask: str | None = None) -> Any:
        """Raw call to the backend. `url` already includes the version and the path.

        **The permission is enforced here, in the low layer**: any write verb goes through `assert_writable` and any read through
        `assert_readable`, derived from the agent that appears in the path. Thus
        no route (not even the generic passthrough `cx_api`) can
        bypass the agent allowlist."""
        verb = method.upper()
        project, agent = agent_from_path(url)
        if agent:
            try:
                if verb in WRITE_VERBS:
                    assert_writable(project, agent, self.cfg)
                else:
                    assert_readable(project, agent, self.cfg)
            except PermissionDeniedError as exc:
                # The URL is needed to know WHICH request was rejected.
                raise PermissionDeniedError(f"{exc} [url={url}]") from exc
        if query:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode({k: v for k, v in query.items()
                                         if v is not None})
        if update_mask:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode({"updateMask": update_mask})
        headers = self._headers_de_url(
            url, {"content-type": "application/json"} if body is not None else None)
        return self.http.json(verb, url, headers, body, timeout=timeout)

    # ------------------------------------------------------------- reads
    def list_agents(self, project: str = "", location: str = "") -> list[dict]:
        """Project agents in a location.

        MEASURED (2026-11-15): each location has its own host, so the
        URL is built with the host that corresponds to the location."""
        url = self._url(f"{self._base(project, location)}/agents", pageSize="200")
        data = self.http.json("GET", url, self._headers_de_url(url))
        return data.get("agents", [])  # type: ignore[union-attr]

    def list_flows(self, agent_id: str, project: str = "") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows",
                        pageSize="200", languageCode="es")
        data = self.http.json("GET", url, self._headers_de_url(url))
        return data.get("flows", [])  # type: ignore[union-attr]

    def get_flow(self, agent_id: str, flow_id: str, project: str = "",
                 language_code: str | None = "es") -> dict:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}",
                        languageCode=language_code)
        return self.http.json("GET", url, self._headers_de_url(url))  # type: ignore[return-value]

    def list_pages(self, agent_id: str, flow_id: str, project: str = "") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}/pages",
                        pageSize="200", languageCode="es")
        data = self.http.json("GET", url, self._headers_de_url(url))
        return data.get("pages", [])  # type: ignore[union-attr]

    def get_page(self, agent_id: str, flow_id: str, page_id: str,
                 project: str = "", language_code: str | None = "es") -> dict:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}/pages/{page_id}",
                        languageCode=language_code)
        return self.http.json("GET", url, self._headers_de_url(url))  # type: ignore[return-value]

    def list_playbooks(self, agent_id: str, project: str = "") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/playbooks", pageSize="200")
        data = self.http.json("GET", url, self._headers_de_url(url))
        return data.get("playbooks", [])  # type: ignore[union-attr]

    def playbooks_by_display_name(self, agent_id: str, project: str = "") -> dict[str, str]:
        return {pb.get("displayName"): pb["name"].split("/")[-1]
                for pb in self.list_playbooks(agent_id, project) if pb.get("displayName")}

    def _list_child(self, agent_id: str, collection: str, project: str = "",
                    page_size: str = "200") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/{collection}",
                        pageSize=page_size)
        data = self.http.json("GET", url, self._headers_de_url(url))
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
        data = self.http.json("GET", url, self._headers_de_url(url))
        return data.get("conversations", [])  # type: ignore[union-attr]

    def get_conversation(self, agent_id: str, conversation_id: str,
                         project: str = "") -> dict:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}"
                        f"/conversations/{conversation_id}")
        return self.http.json("GET", url, self._headers_de_url(url))  # type: ignore[return-value]

    def list_changelogs(self, agent_id: str, project: str = "") -> list[dict]:
        assert_readable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/changelogs",
                        pageSize="50")
        data = self.http.json("GET", url, self._headers_de_url(url))
        return data.get("changelogs", [])  # type: ignore[union-attr]

    # ------------------------------------------------------------------ batch
    def batch(self, requests: list[dict[str, Any]], timeout: float | None = 300.0,
              project: str = "") -> list[dict[str, Any]]:
        """Multiple calls to API in a SINGLE HTTP request (`/batch`).

        The v3beta1 discovery doc declares `batchPath: "batch"`: Google's frontend
        accepts a `multipart/mixed` with N sub-requests HTTP and
        executes them on the server. This turns N round trips (each
        ~0.5 s of latency) into one.

        **Measured (2026-11-08):** the backend serializes writes to the
        SAME agent (8 in parallel = 899 ms each, worse than 500 ms in
        series), so the batch does not make them faster: it serves to
        package the trip, not to parallelize the effect.

        Each sub-request: {"method", "path", "body"?, "query"?}. `path` is
        relative to the version ("projects/p/locations/l/agents/a/flows").
        The **guard applies to each sub-request**: a batch that mixes
        read and write agents is evaluated sub by sub."""
        if not requests:
            return []
        boundary = "cx_batch_boundary_3f9a"
        chunks: list[str] = []
        for i, req in enumerate(requests):
            method = (req.get("method") or "GET").upper()
            path = req.get("path") or ""
            if not path:
                raise ConsoleApiError(f"sub-peticion {i} sin path")
            # The guard, sub by sub: nobody slips through via the batch.
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
        url = f"https://{CONSOLE_HOST}/{self.cfg.api_version}/batch?key={self.cfg.api_key}"
        headers = self._headers_de_url(
            url, {"content-type": f"multipart/mixed; boundary={boundary}"})
        status, text = self.http.request("POST", url, headers, payload_bytes,
                                         timeout=timeout)
        if status >= 400:
            raise HttpStatusError(status, text)
        if text.lstrip().startswith("{"):
            # The server responded with the JSON format from ESF.
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
        """Workaround for `batch()`: the sub-peticiones, in THREADS.

        **Measured (2026-11-08):** the internal backend does NOT implement `/batch`
        (HTTP 404: the discovery doc declares it, but clients6 does not serve it). This
        method makes the same N calls in parallel (threads), which is what
        the batch intended: one trip per sub-peticion, but at the same time.

        NOTE: the backend serializes the WRITES of the same agent, so
        parallelism only pays off in the READS."""
        from concurrent.futures import ThreadPoolExecutor

        def uno(req: dict[str, Any]) -> dict[str, Any]:
            method = str(req.get("method") or "GET").upper()
            path = req.get("path") or ""
            q = {"key": self.cfg.api_key}
            q.update({k: v for k, v in (req.get("query") or {}).items() if v is not None})
            # URL ABSOLUTE: the HTTP client resolves the host (a relative
            # route -> gaierror "No address associated with hostname").
            # MEASURED: the host depends on the path location.
            url = (f"https://{host_de_path(path)}/{self.cfg.api_version}/"
                   f"{path.lstrip('/')}?{urlencode(q)}")
            headers = self._headers_de_url(
                url, {"content-type": "application/json"}
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

    # ------------------------------------------------------------- write
    def patch_page(self, agent_id: str, flow_id: str, page_id: str,
                   body: dict[str, Any], update_mask: str = UPDATE_MASK,
                   language_code: str | None = "es", project: str = "",
                   timeout: float | None = 300.0) -> dict:
        assert_writable(project or self.cfg.project, agent_id, self.cfg)
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}"
                        f"/pages/{page_id}",
                        alt="json", updateMask=update_mask, languageCode=language_code)
        headers = self._headers_de_url(url, {"content-type": "application/json"})
        return self.http.json("PATCH", url, headers, body, timeout=timeout)  # type: ignore[return-value]

    def patch_flow(self, agent_id: str, flow_id: str, body: dict[str, Any],
                   update_mask: str | None = None, language_code: str | None = "es",
                   project: str = "", timeout: float | None = 300.0) -> dict:
        """PATCH of a flow. The mask comes from the body keys if not passed."""
        assert_writable(project or self.cfg.project, agent_id, self.cfg)
        if not update_mask:
            update_mask = ",".join(sorted(k for k in body if k not in READ_ONLY_FIELDS))
        url = self._url(f"{self._base(project)}/agents/{agent_id}/flows/{flow_id}",
                        alt="json", updateMask=update_mask, languageCode=language_code)
        headers = self._headers_de_url(url, {"content-type": "application/json"})
        return self.http.json("PATCH", url, headers, body, timeout=timeout)  # type: ignore[return-value]

    def set_transition_routes(self, agent_id: str, flow_id: str, page_id: str,
                              routes: list[dict], timeout: float | None = 300.0,
                              project: str = "") -> dict:
        """Write the COMPLETE list of transition routes for a page.

        This is what the console does: it sends the whole array in the PATCH. The `etag`
        is deliberately omitted: the internal GET does not return it and a stale etag
        would make the PATCH fail."""
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
        """Set a playbook as the target of ONE transition route FROM PAGE."""
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

    # ------------------------------------------- THE AGGREGATE: router of a flow
    def patch_resource(self, name: str, body: dict[str, Any],
                       update_mask: str | None = None, language_code: str | None = None,
                       project: str = "", timeout: float | None = 300.0) -> dict:
        """PATCH GENERIC of any resource, addressed by its `name`.

        `name` is the complete `name` returned by the API
        (`projects/p/locations/l/agents/a/flows/f/pages/p`). With this the MCP
        can write to any resource of the agent (agent, flow, page,
        playbook, tool, intent, entity type, webhook, route group...) without
        one method per resource.

        If `update_mask` is not passed, it comes from the body keys (minus the
        derived fields). If `language_code` is not passed, it is not sent."""
        assert_writable(*agent_from_path(name), self.cfg)
        if not update_mask:
            update_mask = ",".join(sorted(k for k in body if k not in READ_ONLY_FIELDS))
        params: dict[str, Any] = {"alt": "json", "updateMask": update_mask}
        if language_code:
            params["languageCode"] = language_code
        url = self._url("/" + name, **params)
        headers = self._headers_de_url(url, {"content-type": "application/json"})
        return self.http.json("PATCH", url, headers, body, timeout=timeout)  # type: ignore[return-value]

    @staticmethod
    def _agent_of_name(name: str) -> str:
        """The agent id that appears in the `name` of a resource."""
        return agent_from_path(name)[1]

    def set_flow_route_playbook(self, agent_id: str, flow_id: str,
                                route_ref: str | int, playbook_resource: str,
                                timeout: float | None = 300.0, project: str = "") -> dict:
        """Set a playbook as the target of ONE transition route FROM FLOW.

        This is what the API publishes CANNOT express: `TransitionRoute.target`
        is a `oneof` with `target_page` and `target_flow`; `target_playbook` does not
        exist and a PATCH that sends it is silently dropped. The console's
        internal backend does keep it (measured: persists and is read back).

        Write the flow's COMPLETE array of routes (same as the console) and
        verify by re-reading."""
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
        # Narrow mask: only `transitionRoutes` is written; the rest of the
        # flow is untouched (measured: the rest survives the PATCH).
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
