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

# Macro ${PLAYBOOK:name} in a playbook's text. The backend VALIDATES it on
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


def rid_page(name: str) -> str:
    """Last segment of a page name (or its end marker)."""
    return (name or "").split("/")[-1]

# Collections that are cloned and in what order CREATE. Order matters:
# entityTypes -> intents -> webhooks -> tools -> playbooks -> generators
# (a playbook references tools; a generator could reference playbooks).
CREATE_COLLECTIONS = ["entityTypes", "intents", "webhooks", "tools",
                      "playbooks", "generators"]

# Order of DELETED: first the playbooks (they reference the tools), then the
# flows (their pages are removed), and after that the rest.
DELETE_COLLECTIONS = ["playbooks", "flows", "tools", "webhooks",
                      "intents", "entityTypes", "generators"]

# Fields the server returns but that cannot (or must not) be written.
DO_NOT_WRITE = frozenset({
    "etag", "createTime", "updateTime", "name", "parent", "session",
    "environment", "derivedTransitions", "nluSettingsList", "tokenCount",
    "projectNumber", "supportedLanguageCodes",
})


def _suffix(resource: str, rid: str) -> str:
    return f"{resource}/{rid}"


class Cloner:
    """1:1 agent clone (source = read-only, target = writable)."""

    def __init__(self, cons: ConsoleApi, source: str, destination: str,
                 project: str = "", reads: int = 8, writes: int = 1):
        self.cons = cons
        self.source = source
        self.destination = destination
        self.project = project or cons.cfg.project
        self.location = cons.cfg.location
        self.reads = max(1, reads)
        self.writes = max(1, writes)
        self.actions: list[dict[str, Any]] = []
        self.id_map: dict[str, str] = {}   # "pages/old" -> "pages/new"
        self.errors: list[dict[str, Any]] = []
        self._lock = threading.Lock()       # the deletion runs with 2 threads
        self._t_phases: dict[str, float] = {}
        self._pending_pages: list[tuple[str, str, list, list]] = []
        self._flows_with_patch: set[str] = set()  # flows that need PATCH of routes
        self.omitted: list[str] = []   # resources that API does not allow recreating
        self.deviations: list[str] = []  # recreated with fewer fields than the originals

    # ------------------------------------------------------------- utilities
    def _base(self) -> str:
        """Path for URLs: `console_api._url()` expects '/'- initial."""
        return f"/projects/{self.project}/locations/{self.location}/agents"

    def _pref(self, agent: str) -> str:
        """Resource NAME prefix (without '/': exactly as returned by the API)."""
        return f"projects/{self.project}/locations/{self.location}/agents/{agent}/"

    def _url(self, agent: str, suffix: str = "", **params: Any) -> str:
        return self.cons._url(f"{self._base()}/{agent}{suffix}", **params)

    def _act(self, phase: str, action: str, ms: float, http: int = 200,
             ok: bool = True, detail: str = "") -> dict[str, Any]:
        row = {"phase": phase, "action": action, "ms": round(ms, 1),
               "http": http, "ok": ok, "detail": detail[:700]}
        if not ok:
            # Annotate the error with the known limit that explains it (MCP
            # memory: `cx_limites_api`).
            from .limits import cause_of
            cause = cause_of(detail)
            if cause:
                row["limit"] = cause["id"]
                row["cause"] = cause["cause"]
                row["workaround"] = cause["workaround"]
        self.actions.append(row)
        with self._lock:
            self._t_phases[phase] = self._t_phases.get(phase, 0.0) + ms
        if not ok:
            self.errors.append(row)
        return row

    def _get(self, phase: str, action: str, agent: str, suffix: str,
             params: dict | None = None, query: dict | None = None) -> Any:
        url = self._url(agent, suffix, **(params or {}))
        t0 = time.perf_counter()
        try:
            data = self.cons.request("GET", url, query=query)
            ms = (time.perf_counter() - t0) * 1000
            self._act(phase, action, ms)
            return data
        except HttpStatusError as exc:
            ms = (time.perf_counter() - t0) * 1000
            self._act(phase, action, ms, http=exc.status, ok=False,
                      detail=str(exc))
            return None

    def _write(self, phase: str, action: str, verb: str, url: str,
               body: dict | None = None, query: dict | None = None,
               retries: int = 0) -> Any:
        """Write with retries IN SERIES (the backend serializes the same way)."""
        attempt = 0
        while True:
            t0 = time.perf_counter()
            try:
                data = self.cons.request(verb, url, body=body, query=query)
                ms = (time.perf_counter() - t0) * 1000
                self._act(phase, action, ms)
                return data
            except HttpStatusError as exc:
                ms = (time.perf_counter() - t0) * 1000
                if attempt < retries and self._transient(exc):
                    attempt += 1
                    time.sleep(2.0 * attempt)
                    continue
                self._act(phase, action, ms, http=exc.status, ok=False,
                          detail=str(exc))
                return None

    @staticmethod
    def _transient(exc: HttpStatusError) -> bool:
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
        pref = self._pref(self.source)
        if not s.startswith(pref):
            return s
        parts = s[len(pref):].split("/")
        output: list[str] = []
        i = 0
        while i < len(parts):
            if i + 1 < len(parts):
                key = f"{parts[i]}/{parts[i + 1]}"
                new = self.id_map.get(key)
                if new:
                    output.append(new)
                    i += 2
                    continue
            output.append(parts[i])
            i += 1
        return f"{self._pref(self.destination)}{'/'.join(output)}"

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
    def _clean_body(rec: dict[str, Any]) -> dict[str, Any]:
        """Remove what the server does not accept on create."""
        return {k: v for k, v in rec.items() if k not in DO_NOT_WRITE}

    def _new_id(self, data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        return (data.get("name") or "").split("/")[-1]

    # ============================================================ 1. SNAPSHOT
    def snapshot(self) -> dict[str, Any]:
        """Read the complete ORIGINAL agent (parallel reads)."""
        snap: dict[str, Any] = {"agent": self.source}
        wave: list[tuple[str, Callable[[], Any]]] = []

        wave.append(("agent", lambda: self._get(
            "read", f"GET agent {self.source[:8]}", self.source, "")))
        wave.append(("flows", lambda: self._get(
            "read", "GET flows", self.source, "/flows",
            query={"pageSize": 200, "languageCode": "es"})))
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "transitionRouteGroups", "generators"):
            wave.append((coll, (lambda c=coll: self._get(
                "read", f"GET {c}", self.source, f"/{c}",
                query={"pageSize": 200}))))

        with ThreadPoolExecutor(max_workers=min(self.reads, len(wave))) as pool:
            for (key, _), value in zip(wave, pool.map(lambda t: t[1](), wave),
                                       strict=True):
                snap[key] = value or {}

        flows = (snap.get("flows") or {}).get("flows") or []
        snap["flows"] = flows
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "transitionRouteGroups", "generators"):
            snap[coll] = (snap.get(coll) or {}).get(coll) or []

        # Pages for each flow (second wave, in parallel)
        wave2: list[tuple[str, Callable[[], Any]]] = []
        for f in flows:
            fid = (f.get("name") or "").split("/")[-1]
            dn = (f.get("displayName") or "")[:24]
            wave2.append((fid, (lambda fl=fid, dn=dn: self._get(
                "read", f"GET pages {dn}", self.source,
                f"/flows/{fl}/pages",
                query={"pageSize": 200, "languageCode": "es"}))))
        pages: dict[str, list] = {fid: [] for fid in
                                   ((f.get("name") or "").split("/")[-1] for f in flows)}
        with ThreadPoolExecutor(max_workers=min(self.reads, max(len(wave2), 1))) as pool:
            for (fid, _), value in zip(wave2, pool.map(lambda t: t[1](), wave2),
                                       strict=True):
                pages[fid] = (value or {}).get("pages") or []
        snap["pages"] = pages
        return snap

    # ============================================================= 2. CLEAN
    def clean(self) -> dict[str, Any]:
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
        state = self._state(self.destination)
        # Groups: within a group, deletions are INDEPENDENT and may
        # run in parallel; between groups, ORDER is dictated by the server.
        # `serial=True` -> the group is NOT parallelized (the order is imposed by the
        # server: playbooks that reference each other, intents that
        # reference their entity types).
        groups: list[tuple[str, list[tuple[str, str, str, dict | None]], bool]] = []

        # 1. flows (force): with them the pages and their references fall.
        flows = []
        for rec in state.get("flows") or []:
            rid = self._id_of(rec)
            if rid == DEFAULT_FLOW:
                continue          # the default flow cannot be deleted
            flows.append(("flows", rid, rec.get("displayName") or rid, {"force": "true"}))
        groups.append(("flows", flows, True))

        # 2. playbooks: INVERSE order of dependencies (reference first).
        pbs = [(self._id_of(p), p) for p in (state.get("playbooks") or [])]
        pbs_ordered = list(reversed(self._order_by_deps(
            pbs, lambda r: self._ref_names(r.get("referencedPlaybooks")))))
        groups.append(("playbooks",
                       [("playbooks", rid, rec.get("displayName") or rid, None)
                        for rid, rec in pbs_ordered], True))
        # 3. the rest. Intents go BEFORE than the entityTypes (an intent
        # references its entity types) -> separate group.
        for coll in ("intents", "entityTypes", "tools", "webhooks", "generators"):
            batch = []
            for rec in state.get(coll) or []:
                rid = self._id_of(rec)
                if coll == "intents" and rid in DEFAULT_INTENTS:
                    continue      # default intents are not deleted
                if coll == "tools" and rid.startswith("df-"):
                    continue      # Dialogflow builtin tool: it is not deleted
                force = None if coll == "intents" else {"force": "true"}
                batch.append((coll, rid, rec.get("displayName") or rid, force))
            groups.append((coll, batch, coll in ("intents", "entityTypes")))

        deleted = 0
        for _name, batch, serial in groups:
            deleted += self._delete_batch(batch, serial)
        return {"planned": sum(len(batch) for _n, batch, _s in groups),
                "deleted": deleted,
                "remaining": self._counts(self.destination)}

    def _delete_batch(self, batch: list[tuple[str, str, str, dict | None]],
                      serial: bool = False) -> int:
        """Delete a group of elements.

        The `playbooks` are deleted IN SERIES (their order is imposed by the
        server: the one that references, first). The rest of the group are
        independent and are deleted with `self.writes` threads: measured, N=2 is
        the optimum (2.10 ops/s versus 1.75 in series; with N>=3 the backend
        locks up and throughput LOW)."""
        def _one(item: tuple[str, str, str, dict | None]) -> bool:
            coll, rid, dn, query = item
            url = self._url(self.destination, f"/{coll}/{rid}")
            data = self._write("delete", f"DELETE {coll} {dn[:28]}", "DELETE",
                               url, query=query)
            if data is None and coll == "playbooks":
                # Without `force`: the remaining references are removed and it
                # retries once.
                self._clear_routes()
                data = self._write("delete",
                                   f"DELETE {coll} {dn[:28]} (retry)",
                                   "DELETE", url)
            return data is not None

        if not batch:
            return 0
        if serial or self.writes <= 1 or len(batch) == 1:
            return sum(1 for item in batch if _one(item))
        with ThreadPoolExecutor(max_workers=min(self.writes, len(batch))) as pool:
            return sum(1 for ok in pool.map(_one, batch) if ok)

    def _clear_routes(self) -> None:
        """Empty the routes of the remaining flows (only once)."""
        if getattr(self, "_routes_emptied", False):
            return
        self._routes_emptied = True
        flows = self._state(self.destination).get("flows") or []
        for f in flows:
            fid = self._id_of(f)
            if fid == DEFAULT_FLOW:
                continue
            body = {"name": f.get("name"), "transitionRoutes": []}
            self._write("delete",
                        f"clear the routes of the flow {f.get('displayName','')[:20]}",
                        "PATCH", self._url(self.destination, f"/flows/{fid}"),
                        body=body, query={"updateMask": "transitionRoutes"})

    def _state(self, agent: str) -> dict[str, Any]:
        """Current elements of an agent (parallel reads)."""
        out: dict[str, Any] = {}
        wave: list[tuple[str, Callable[[], Any]]] = []
        wave.append(("flows", lambda: self._get(
            "read", "GET flows", agent, "/flows",
            query={"pageSize": 200, "languageCode": "es"})))
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "transitionRouteGroups", "generators"):
            wave.append((coll, (lambda c=coll: self._get(
                "read", f"GET {c}", agent, f"/{c}", query={"pageSize": 200}))))
        with ThreadPoolExecutor(max_workers=min(self.reads, len(wave))) as pool:
            for (key, _), value in zip(wave, pool.map(lambda t: t[1](), wave),
                                       strict=True):
                out[key] = ((value or {}).get(key) or [])
        return out

    def _counts(self, agent: str) -> dict[str, int]:
        st = self._state(agent)
        return {k: len(v) for k, v in st.items()}

    # ============================================================ 3. RECREATE
    @staticmethod
    def _invalid_spec(body: dict[str, Any]) -> bool:
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
        text = spec.get("textSchema")
        return not (isinstance(text, str) and text.strip())

    def _order_flows(self, flows: list[dict]) -> list[dict]:
        """Flow creation order: AFTER of the flows they reference.

        A flow route can point to another flow. If that target does not exist
        when the flow is created, the API responds 400 (it validates the targets)."""
        ids = [self._id_of(f) for f in flows]
        idset = set(ids)
        deps: dict[str, set[str]] = {}
        for f in flows:
            fid = self._id_of(f)
            d: set[str] = set()
            for r in (f.get("transitionRoutes") or []) + (f.get("eventHandlers") or []):
                tf = r.get("targetFlow")
                if tf:
                    t = tf.split("/")[-1]
                    if t in idset and t != fid:
                        d.add(t)
            deps[fid] = d
        order: list[dict] = []
        placed: set[str] = {DEFAULT_FLOW}      # already exists in any agent
        pending = list(flows)
        while pending:
            progress = False
            for f in list(pending):
                fid = self._id_of(f)
                if deps.get(fid, set()) <= placed:
                    order.append(f)
                    placed.add(fid)
                    pending.remove(f)
                    progress = True
            if not progress:
                # Cycle between flows (rare): broken by entry order.
                f = pending.pop(0)
                order.append(f)
                placed.add(self._id_of(f))
        return order

    def _includeable_routes(self, f: dict) -> tuple[list, list, bool]:
        """Routes of a flow that can be put into its own POST.

        Only if ALL its routes are resolvable at flow creation time:
        no page target (pages come later), no playbook target (playbooks
        come later) and with the target flow already created.

        Returns (routes, handlers, complete). If `complete` is False, the flow
        still needs the PATCH of routes (and will be created without them)."""
        routes = list(f.get("transitionRoutes") or [])
        handlers = list(f.get("eventHandlers") or [])
        if not routes and not handlers:
            return [], [], True

        def ok(r: dict) -> bool:
            if r.get("targetPage"):
                # `END_FLOW` / `END_FLOW_WITH_HUMAN_ESCALATION` are not pages:
                # they are end-of-flow markers and ALWAYS are valid.
                return rid_page(r["targetPage"]) in FIN_FLOW
            if r.get("targetPlaybook"):
                return False
            tf = r.get("targetFlow")
            if tf:
                t = tf.split("/")[-1]
                if t != DEFAULT_FLOW and _suffix("flows", t) not in self.id_map:
                    return False
            return True

        r_ok = [r for r in routes if ok(r)]
        h_ok = [r for r in handlers if ok(r)]
        complete = len(r_ok) == len(routes) and len(h_ok) == len(handlers)
        if not complete:
            return [], [], False
        return r_ok, h_ok, True

    def recreate(self, snap: dict[str, Any]) -> dict[str, Any]:
        """Create in the clone everything from the original, with the ids remapped."""
        created = {"entityTypes": 0, "intents": 0, "webhooks": 0, "tools": 0,
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
                    self.id_map[_suffix("intents", old_id)] = _suffix("intents", old_id)
                    continue
                if coll == "tools" and old_id.startswith("df-"):
                    # Tool BUILTIN of Dialogflow (code-interpreter): it already exists in
                    # any agent and the API rejects creating it by hand.
                    self.id_map[_suffix("tools", old_id)] = _suffix("tools", old_id)
                    self.omitted.append(f"tool builtin {rec.get('displayName')}")
                    continue
                body = self._remap(self._clean_body(rec))
                # API rejects a `openApiSpec` WITHOUT `textSchema` ("Null or
                # empty definition"). Detected BEFORE before sending it: this
                # saves the failed POST and the error does not pollute the report.
                # NOTE: this is ONLY for `tools` (the other resources carry no spec) and the URL is ALWAYS that of ITS collection.
                if coll == "tools" and self._invalid_spec(body):
                    body.pop("openApiSpec", None)
                    self.deviations.append(
                        f"tool {rec.get('displayName')}: recreated WITHOUT spec "
                        "(the original openApiSpec has no definition and the "
                        "API rejects it)")
                data = self._write("create",
                                   f"POST {coll} {rec.get('displayName','')[:28]}",
                                   "POST", self._url(self.destination, f"/{coll}"),
                                   body=body, retries=1)
                if data is None and coll == "tools":
                    # The original spec is not accepted by API (empty OpenAPI ->
                    # "Null or empty definition"; data store deleted -> "Failed
                    # to connect"). It is recreated WITHOUT spec: the element exists and
                    # the playbooks that reference it no longer fail.
                    body_without = {k: v for k, v in body.items()
                                    if k not in ("openApiSpec", "dataStoreSpec")}
                    data = self._write("create",
                                       f"POST tool {rec.get('displayName','')[:26]} (without spec)",
                                       "POST", self._url(self.destination, "/tools"),
                                       body=body_without)
                    if data is not None:
                        self.deviations.append(
                            f"tool {rec.get('displayName')}: recreated WITHOUT spec "
                            "(the API rejects the original spec)")
                new_id = self._new_id(data)
                if new_id:
                    self.id_map[_suffix(coll, old_id)] = _suffix(coll, new_id)
                    created[coll] += 1
                else:
                    self.omitted.append(f"{coll} {rec.get('displayName')}")

        # --- 3.2 flows (resolvable routes go in POST itself) ---------
        # A flow is created AFTER from the flows its routes point to, and
        # if ALL its routes are resolvable at that moment, they are created with them
        # -> that flow no longer needs the PATCH of routes (one less write).
        flows = snap.get("flows") or []
        for f in self._order_flows(flows):
            old_id = (f.get("name") or "").split("/")[-1]
            if old_id == DEFAULT_FLOW:
                # The default flow already exists in the clone: it remaps to itself.
                # Its routes must be set to ALWAYS with a PATCH (it cannot be
                # created again).
                self.id_map[_suffix("flows", old_id)] = _suffix("flows", old_id)
                self._flows_with_patch.add(old_id)
                continue
            body = self._remap(self._clean_body(f))
            body.pop("transitionRoutes", None)
            body.pop("eventHandlers", None)
            routes, handlers, complete = self._includeable_routes(f)
            if complete and (routes or handlers):
                body["transitionRoutes"] = self._remap(routes)
                body["eventHandlers"] = self._remap(handlers)
            data = self._write("create",
                               f"POST flow {f.get('displayName','')[:28]}",
                               "POST", self._url(self.destination, "/flows"), body=body)
            if data is None and (routes or handlers):
                # The backend did not accept the routes in POST: it is created without them
                # and will be left for the PATCH of section 3.7.
                body.pop("transitionRoutes", None)
                body.pop("eventHandlers", None)
                data = self._write("create",
                                   f"POST flow {f.get('displayName','')[:24]} (without routes)",
                                   "POST", self._url(self.destination, "/flows"), body=body)
                complete = False
            new_id = self._new_id(data)
            if new_id:
                self.id_map[_suffix("flows", old_id)] = _suffix("flows", new_id)
                created["flows"] += 1
                if complete and (routes or handlers):
                    created["flows_routes_in_post"] = created.get("flows_routes_in_post", 0) + 1
                else:
                    self._flows_with_patch.add(old_id)

        # --- 3.3 playbooks (reference TOOLS and FLOWS: they come after both) -----
        # And they reference EACH OTHER (`referencedPlaybooks`): topological order.
        # Special case: a playbook can reference ITSELF (the
        # orchestrator does) -> in the POST that reference cannot yet
        # exist, so it is created without it and added with a PATCH afterwards.
        for old_id, rec in self._order_by_deps(
                [(self._id_of(rec), rec) for rec in (snap.get("playbooks") or [])],
                lambda r: self._ref_names(r.get("referencedPlaybooks"))):
            body_full = self._remap(self._clean_body(rec))
            refs = body_full.get("referencedPlaybooks") or []
            own = [x for x in refs if x.split("/")[-1] == old_id]
            body = copy.deepcopy(body_full)
            if own:
                # Self-reference is a loop: API rejects it.
                body["referencedPlaybooks"] = [x for x in refs
                                               if x.split("/")[-1] != old_id]
            self_ref_fields = self._has_self_ref(body, rec.get("displayName") or "")
            if self_ref_fields:
                body = self._neutralize_self_ref(body, rec.get("displayName") or "")
            data = self._write("create",
                               f"POST playbook {rec.get('displayName','')[:26]}",
                               "POST", self._url(self.destination, "/playbooks"), body=body)
            new_id = self._new_id(data)
            if new_id:
                self.id_map[_suffix("playbooks", old_id)] = _suffix("playbooks", new_id)
                created["playbooks"] += 1
                if own or self_ref_fields:
                    self.deviations.append(
                        f"playbook {rec.get('displayName')}: the self-reference "
                        "(${PLAYBOOK:<itself>}) cannot be reproduced through the API "
                        "(the API rejects the loops: 'caused loop in playbook routes')")
            else:
                self.omitted.append(f"playbook {rec.get('displayName')}")

        # --- 3.4 generators (may reference playbooks) -----------------
        for rec in snap.get("generators") or []:
            old_id = (rec.get("name") or "").split("/")[-1]
            body = self._remap(self._clean_body(rec))
            data = self._write("create",
                               f"POST generator {rec.get('displayName','')[:24]}",
                               "POST", self._url(self.destination, "/generators"),
                               body=body)
            new_id = self._new_id(data)
            if new_id:
                self.id_map[_suffix("generators", old_id)] = _suffix("generators", new_id)
                created["generators"] += 1

        # --- 3.5 pages in topological order (routes validate targets) --
        order = self._order_pages(snap)
        for flow_old, page in order:
            flow_new = self.id_map.get(_suffix("flows", flow_old), "").split("/")[-1]
            if not flow_new:
                continue
            old_page = (page.get("name") or "").split("/")[-1]
            body = self._clean_body(page)
            # Targets that do not exist yet (cycles): the page is created without
            # routes OR handlers and patched afterwards. The special target
            # `END_FLOW` is not a page: it does not count.
            # Routes are stored WITHOUT remapping: they are remapped in the PATCH,
            # once the full map exists (if remapped here, pending
            # targets keep the ORIGINAL id).
            routes = page.get("transitionRoutes") or []
            handlers = page.get("eventHandlers") or []
            pending = [r for r in routes + handlers
                       if r.get("targetPage")
                       and rid_page(r["targetPage"]) not in FIN_FLOW
                       and _suffix("pages", rid_page(r["targetPage"])) not in self.id_map]
            if pending:
                body_without = self._remap({k: v for k, v in body.items()
                                            if k not in ("transitionRoutes",
                                                         "eventHandlers")})
                data = self._write("create",
                                   f"POST page {page.get('displayName','')[:26]} (without routes)",
                                   "POST",
                                   self._url(self.destination, f"/flows/{flow_new}/pages"),
                                   body=body_without)
                new_page = self._new_id(data)
                if new_page:
                    self.id_map[_suffix("pages", old_page)] = _suffix("pages", new_page)
                    created["pages"] += 1
                    self._pending_pages.append((flow_new, new_page, routes, handlers))
            else:
                data = self._write("create",
                                   f"POST page {page.get('displayName','')[:26]}",
                                   "POST",
                                   self._url(self.destination, f"/flows/{flow_new}/pages"),
                                   body=self._remap(body))
                new_page = self._new_id(data)
                if new_page:
                    self.id_map[_suffix("pages", old_page)] = _suffix("pages", new_page)
                    created["pages"] += 1

        # --- 3.6 patches for pages with pending targets -------------
        for flow_new, page_new, routes, handlers in self._pending_pages:
            body = {"name": f"{self._pref(self.destination)}flows/{flow_new}/pages/{page_new}",
                    "transitionRoutes": self._remap(routes),
                    "eventHandlers": self._remap(handlers)}
            self._write("create", f"PATCH pending pages {page_new[:8]}",
                        "PATCH",
                        self._url(self.destination, f"/flows/{flow_new}/pages/{page_new}"),
                        body=body,
                        query={"updateMask": "transitionRoutes,eventHandlers",
                               "languageCode": "es"})

        # --- 3.7 routes and eventHandlers of the flows -------------------------
        # Only the flows whose routes could NOT go in their POST (page or
        # playbook targets, which do not exist yet).
        for f in flows:
            old_id = (f.get("name") or "").split("/")[-1]
            if old_id not in self._flows_with_patch:
                continue
            flow_new = self.id_map.get(_suffix("flows", old_id), "").split("/")[-1]
            if not flow_new:
                continue
            routes = self._remap(f.get("transitionRoutes") or [])
            handlers = self._remap(f.get("eventHandlers") or [])
            if not routes and not handlers:
                continue
            body = {"name": f"{self._pref(self.destination)}flows/{flow_new}",
                    "transitionRoutes": routes, "eventHandlers": handlers}
            self._write("create", f"PATCH routes of the flow {f.get('displayName','')[:26]}",
                        "PATCH", self._url(self.destination, f"/flows/{flow_new}"),
                        body=body,
                        query={"updateMask": "transitionRoutes,eventHandlers",
                               "languageCode": "es"})
            created["flow_routes"] += 1

        # --- 3.8 agent-level adjustments ----------------------------------
        created["agent"] = self._patch_agent(snap.get("agent") or {})
        created["pending_pages"] = len(self._pending_pages)
        return created

    _pending_pages: list[tuple[str, str, list]] = []

    @staticmethod
    def _id_of(rec: dict[str, Any]) -> str:
        return (rec.get("name") or "").split("/")[-1]

    @staticmethod
    def _ref_names(value: Any) -> set[str]:
        """Ids (last segment) of a list of resource names."""
        if not value:
            return set()
        if isinstance(value, str):
            value = [value]
        out = set()
        for v in value:
            if isinstance(v, str) and "/" in v:
                out.add(v.split("/")[-1])
        return out

    def _order_by_deps(self, items: list[tuple[str, dict]],
                       refs_fn: Callable[[dict], set[str]]) -> list[tuple[str, dict]]:
        """Topological order: each resource goes AFTER the ones it references.

        The API validates references on create (a page/playbook pointing
        to something nonexistent -> HTTP 400), so the order matters."""
        ids = {i for i, _ in items}
        dep: dict[str, set[str]] = {}
        for rid, rec in items:
            dep[rid] = {r for r in refs_fn(rec) if r in ids and r != rid}
        order: list[tuple[str, dict]] = []
        placed: set[str] = set()
        rest = list(items)
        while rest:
            progress = False
            for it in list(rest):
                if dep[it[0]] <= placed:
                    order.append(it)
                    placed.add(it[0])
                    rest.remove(it)
                    progress = True
            if not progress:
                # cycle: created in whatever order remains (missing routes are
                # patched afterwards).
                order.extend(rest)
                break
        return order

    @classmethod
    def _without_macros(cls, obj: Any) -> Any:
        """Neutralize the ${PLAYBOOK:x} macros (inert token)."""
        if isinstance(obj, str):
            return MACRO_PLAYBOOK.sub(lambda m: "{{PLAYBOOK:" + m.group(1) + "}}", obj)
        if isinstance(obj, list):
            return [cls._without_macros(x) for x in obj]
        if isinstance(obj, dict):
            return {k: cls._without_macros(v) for k, v in obj.items()}
        return obj

    @classmethod
    def _neutralize_self_ref(cls, obj: Any, own: str) -> Any:
        """Neutralize ONLY the `${PLAYBOOK:<own>}` macro.

        The API rejects loops: "Playbook X caused loop in playbook routes"
        (measured). A playbook that mentions itself cannot be created
        or updated via API. In the original that self-reference
        appears inside a text that FORBIDS invoke itself, so
        neutralizing it does not change the agent's behavior.

        The remaining macros (${FLOW:x}, ${TOOL:x}, ${PLAYBOOK:other}) are left
        intact: the API validates them and accepts them if the target exists."""
        if isinstance(obj, str):
            return MACRO_PLAYBOOK.sub(
                lambda m: ("{{PLAYBOOK:" + m.group(1) + "}}") if m.group(1) == own
                else m.group(0), obj)
        if isinstance(obj, list):
            return [cls._neutralize_self_ref(x, own) for x in obj]
        if isinstance(obj, dict):
            return {k: cls._neutralize_self_ref(v, own) for k, v in obj.items()}
        return obj

    @classmethod
    def _has_self_ref(cls, body: dict[str, Any], own: str) -> list[str]:
        """Top-level fields with a macro that self-references."""
        out = []
        for k, v in body.items():
            if k in ("instruction", "codeBlock", "goal") and \
                    re.search(r"\$\{PLAYBOOK:" + re.escape(own) + r"\}",
                              json.dumps(v, ensure_ascii=False)):
                out.append(k)
        return out

    def _optimize_order(self, order: list[tuple[str, dict]],
                        deps: dict[str, set[str]]) -> list[tuple[str, dict]]:
        """Local search: minimizes the pages that must be PATCH afterwards.

        A page is "pending" if, when created, it has a target that
        does not exist yet (cycle): it is created without those routes and patched later.

        The inevitable minimum comes from the CYCLES of the page graph: within
        a strongly connected component of k pages, the penultimate one created
        still has an unresolved target, so **at least k-1 pages
        of each cycle require PATCH** (measured: 2 cycles). The greedy method
        stopped at 7; here it is lowered to that minimum with a stronger local search:
        several starting orders + full swaps per pass.

        Each PATCH avoided is ~470 ms of write (the backend serializes)."""
        def cost(o: list[tuple[str, dict]]) -> int:
            done: set[str] = set()
            n = 0
            for _f, p in o:
                pid = self._id_of(p)
                if any(t not in done for t in deps.get(pid, ())):
                    n += 1
                done.add(pid)
            return n

        n = len(order)
        if n < 2:
            return list(order)

        def sweep(base: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
            """Local descent: tries ALL swaps and repeats."""
            current = list(base)
            c_current = cost(current)
            for _ in range(200):
                best = None
                for i in range(n):
                    for j in range(i + 1, n):
                        cand = current[:]
                        cand[i], cand[j] = cand[j], cand[i]
                        c = cost(cand)
                        if c < c_current:
                            best, c_current = cand, c
                if best is None:
                    break
                current = best
            return current

        # Starting orders: the greedy one (above) and its per-block variations.
        starts = [list(order)]
        inverted = list(reversed(order))
        starts.append(inverted)
        # and two rotations: they cut the cycle at another point.
        for cut in (n // 3, 2 * n // 3):
            starts.append(list(order[cut:]) + list(order[:cut]))

        best, best_cost = list(order), cost(order)
        for base in starts:
            cand = sweep(base)
            c = cost(cand)
            if c < best_cost:
                best, best_cost = cand, c
        return best

    @staticmethod
    def _theoretical_minimum(deps: dict[str, set[str]]) -> int:
        """Minimum unavoidable PATCHes: (k-1) per graph cycle."""
        nodes = set(deps)
        # Iterative Tarjan -> strongly connected components
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        components: list[list[str]] = []
        counter = 0
        for root in sorted(nodes):
            if root in index:
                continue
            work = [(root, iter(deps.get(root, ()) & nodes))]
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                v, it = work[-1]
                advanced = False
                for w in it:
                    if w not in index:
                        index[w] = low[w] = counter
                        counter += 1
                        stack.append(w)
                        on_stack.add(w)
                        work.append((w, iter(deps.get(w, ()) & nodes)))
                        advanced = True
                        break
                    if w in on_stack:
                        low[v] = min(low[v], index[w])
                if advanced:
                    continue
                work.pop()
                if work:
                    u = work[-1][0]
                    low[u] = min(low[u], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    components.append(comp)
        minimum = 0
        for comp in components:
            if len(comp) > 1:
                minimum += len(comp) - 1
            elif comp and comp[0] in deps.get(comp[0], set()):
                minimum += 1        # self-reference
        return minimum

    def _order_pages(self, snap: dict[str, Any]) -> list[tuple[str, dict]]:
        """Global topological order of the pages (the API validates targets)."""
        all_pages: list[tuple[str, dict]] = []
        for flow_old, pgs in (snap.get("pages") or {}).items():
            for p in pgs:
                all_pages.append((flow_old, p))
        # dependency: page -> pages its routes point to
        dep: dict[str, set[str]] = {}
        for _flow, p in all_pages:
            pid = (p.get("name") or "").split("/")[-1]
            refs = set()
            for r in (p.get("transitionRoutes") or []) + (p.get("eventHandlers") or []):
                tp = r.get("targetPage")
                if tp and rid_page(tp) not in FIN_FLOW:
                    refs.add(rid_page(tp))
            dep[pid] = refs
        ordered: list[tuple[str, dict]] = []
        placed: set[str] = set()
        remaining = list(all_pages)
        while remaining:
            progress = False
            for item in list(remaining):
                pid = (item[1].get("name") or "").split("/")[-1]
                refs = {r for r in dep.get(pid, set())
                        if r in dep and r != pid}
                if refs <= placed:
                    ordered.append(item)
                    placed.add(pid)
                    remaining.remove(item)
                    progress = True
            if not progress:
                # CYCLE: breaks by choosing the page with FEWER unresolved targets
                # (it is the one that leaves the fewest pending PATCHes).
                def unresolved(it: tuple[str, dict]) -> int:
                    n = 0
                    for r in ((it[1].get("transitionRoutes") or []) +
                              (it[1].get("eventHandlers") or [])):
                        tp = r.get("targetPage")
                        if not tp:
                            continue
                        tid = rid_page(tp)
                        if tid not in FIN_FLOW and tid in dep and tid not in placed:
                            n += 1
                    return n

                cand = min(remaining, key=lambda it: (unresolved(it),
                                                      it[1].get("displayName") or ""))
                pid = (cand[1].get("name") or "").split("/")[-1]
                ordered.append(cand)
                placed.add(pid)
                remaining.remove(cand)
        # Dependencies (page targets) to optimize the order.
        deps: dict[str, set[str]] = {}
        for _f, p in all_pages:
            pid = self._id_of(p)
            refs = set()
            for r in ((p.get("transitionRoutes") or []) +
                      (p.get("eventHandlers") or [])):
                tp = r.get("targetPage")
                if tp:
                    tid = rid_page(tp)
                    if tid not in FIN_FLOW:
                        refs.add(tid)
            deps[pid] = refs
        return self._optimize_order(ordered, deps)

    def _patch_agent(self, original_agent: dict[str, Any]) -> int:
        """Copy the agent settings (without identity: name/displayName).

        `defaultLanguageCode` is NOT copied: the API responds "Agent default
        language code cannot be updated" (measured)."""
        copy_of = dict(original_agent)
        for k in ("name", "displayName", "description", "projectNumber",
                  "supportedLanguageCodes", "startPlaybook", "defaultLanguageCode"):
            copy_of.pop(k, None)
        if not copy_of:
            return 0
        copy_of = self._remap(copy_of)
        # The clone's default flow has the same reserved id.
        if "startFlow" in copy_of:
            copy_of["startFlow"] = self._remap_str(original_agent.get("startFlow", ""))
        mask = ",".join(sorted(copy_of.keys()))
        self._write("create", "PATCH agent (settings)",
                    "PATCH", self._url(self.destination, ""),
                    body=copy_of, query={"updateMask": mask})
        return 1

    # ============================================================ 4. VERIFY
    @staticmethod
    def _page_signature(p: dict[str, Any]) -> dict[str, Any]:
        """Comparable signature of a page: routes, handlers and target type.

        Ids are ignored (they differ by definition: they are remapped), but the
        GRAPH of transitions must be the same."""
        routes = p.get("transitionRoutes") or []
        handlers = p.get("eventHandlers") or []

        def destination(r: dict[str, Any]) -> str:
            for k in ("targetPlaybook", "targetFlow", "targetPage"):
                if r.get(k):
                    v = r[k]
                    if k == "targetPage" and v.endswith("/END_FLOW"):
                        return "END_FLOW"
                    return k
            return "(no destination)"

        return {
            "routes": len(routes),
            "handlers": len(handlers),
            "destinations": sorted(destination(r) for r in routes + handlers),
            "conditions": len([r for r in routes if r.get("condition")]),
        }

    @staticmethod
    def _tool_signature(t: dict[str, Any]) -> dict[str, Any]:
        """Comparable signature of a tool: the number alone is not enough.

        The tool count does not reveal that 23 of them were recreated WITHOUT their
        `openApiSpec` (that is only visible in the CONTENT)."""
        spec = t.get("openApiSpec")
        text = spec.get("textSchema") if isinstance(spec, dict) else None
        return {
            "spec": bool(spec),
            "textSchema_len": len(text) if isinstance(text, str) else 0,
            "dataStore": bool(t.get("dataStoreSpec")),
            "inlineActions": len(t.get("inlineActions") or []),
        }

    def verify(self, snap: dict[str, Any]) -> dict[str, Any]:
        """Compare the recreated clone with the snapshot of the original."""
        state = self._state(self.destination)
        diff: dict[str, Any] = {}
        for coll in ("playbooks", "intents", "entityTypes", "tools", "webhooks",
                     "generators"):
            orig = snap.get(coll) or []
            new = state.get(coll) or []
            diff[coll] = {"original": len(orig), "clone": len(new),
                          "ok": len(orig) == len(new)}
        # Tools: in addition to the number, the CONTENT (spec). A regression that
        # removed the openApiSpec from 20 tools would NOT show up in the count.
        signature_original = {t.get("displayName"): self._tool_signature(t)
                              for t in (snap.get("tools") or [])}
        signature_new = {t.get("displayName"): self._tool_signature(t)
                          for t in (state.get("tools") or [])}
        detail_tools = {k: [signature_original.get(k), signature_new.get(k)]
                        for k in set(signature_original) | set(signature_new)
                        if signature_original.get(k) != signature_new.get(k)}
        diff["tools"]["detail_spec"] = detail_tools
        diff["tools"]["equal_specs"] = not detail_tools
        flows_o = snap.get("flows") or []
        flows_n = state.get("flows") or []
        diff["flows"] = {"original": len(flows_o), "clone": len(flows_n),
                         "ok": len(flows_o) == len(flows_n)}
        # ROUTES of the flows: the number alone is not enough. If the backend ignored
        # the `transitionRoutes` of a POST (as it ignores the `pages`), the
        # clone would be left without routes and that shows here.
        signature_o = {f.get("displayName"): self._page_signature(f) for f in flows_o}
        signature_n = {f.get("displayName"): self._page_signature(f) for f in flows_n}
        detail_flow = {k: [signature_o.get(k), signature_n.get(k)]
                       for k in set(signature_o) | set(signature_n)
                       if signature_o.get(k) != signature_n.get(k)}
        diff["flows"]["equal_routes"] = not detail_flow
        diff["flows"]["detail_routes"] = detail_flow
        diff["flows"]["ok"] = diff["flows"]["ok"] and not detail_flow

        # Pages: by flow (displayName) and by page (displayName), with the
        # routing signature (not just the number).
        pages_o: dict[str, dict] = {}
        for f in flows_o:
            fid = (f.get("name") or "").split("/")[-1]
            for p in (snap.get("pages") or {}).get(fid, []):
                pages_o.setdefault(f.get("displayName"), {})[p.get("displayName")] = \
                    self._page_signature(p)
        pages_n: dict[str, dict] = {}
        for f in flows_n:
            fid = (f.get("name") or "").split("/")[-1]
            pgs = self._get("verify", f"GET pages {f.get('displayName','')[:22]}",
                            self.destination, f"/flows/{fid}/pages",
                            query={"pageSize": 200, "languageCode": "es"}) or {}
            for p in pgs.get("pages") or []:
                pages_n.setdefault(f.get("displayName"), {})[p.get("displayName")] = \
                    self._page_signature(p)
        total_o = sum(len(v) for v in pages_o.values())
        total_n = sum(len(v) for v in pages_n.values())
        detail = {}
        for flow in set(pages_o) | set(pages_n):
            a = pages_o.get(flow, {})
            b = pages_n.get(flow, {})
            if a != b:
                detail[flow] = {k: [a.get(k), b.get(k)] for k in set(a) | set(b)
                            if a.get(k) != b.get(k)}
        diff["pages"] = {"original": total_o, "clone": total_n,
                         "ok": total_o == total_n and not detail,
                         "detail": detail}
        diff["equal_routing"] = not detail
        return diff

    # ================================================================= RUN
    def run(self, verify: bool = True) -> dict[str, Any]:
        """Run the full clone and return the report with timings.

        The snapshot (reads from ORIGINAL) and the cleanup (writes to the
        CLONE) are INDEPENDENT -> they overlap in one thread: saves ~4 s.

        `ms_phases` = WALL time of each phase (what it actually takes);
        `ms_sum_per_phase` = sum of the times of each call (greater than the
        wall time when the reads run in parallel)."""
        import threading

        t0 = time.perf_counter()
        box: dict[str, Any] = {}

        def _snapshot() -> None:
            box["snap"] = self.snapshot()

        th = threading.Thread(target=_snapshot, name="snapshot-original")
        th.start()
        cleaned = self.clean()          # in parallel with the snapshot
        th.join()
        snap = box.get("snap") or {}
        t1 = time.perf_counter()
        created = self.recreate(snap)
        t2 = time.perf_counter()
        diff = self.verify(snap) if verify else {}
        t3 = time.perf_counter()
        phases = {"read+clean": (t1 - t0), "create": (t2 - t1),
                  "verify": (t3 - t2)}
        total = (t3 - t0) * 1000
        return {
            "source": self.source,
            "destination": self.destination,
            "ms_total": round(total, 1),
            "ms_phases": {k: round(v * 1000, 1) for k, v in phases.items()},
            "ms_sum_per_phase": {k: round(v, 1) for k, v in self._t_phases.items()},
            "clean": cleaned,
            "create": created,
            "parity": diff,
            "omitted": self.omitted,
            "deviations": self.deviations,
            "actions": self.actions,
            "errors": self.errors,
        }
