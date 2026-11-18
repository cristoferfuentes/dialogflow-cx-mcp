"""Core: set a PLAYBOOK as the target of a TransitionRoute.

Why this is needed (measured, see `cxp/limits.py`):

* Proto PUBLIC (`global-dialogflow.googleapis.com`): `TransitionRoute.target` is
  a `oneof` with ONLY `target_page` and `target_flow`. `targetPlaybook` does not exist.
  A PATCH with `targetPlaybook` is **silently dropped**.
* Console proto INTERNAL (`dialogflow.clients6.google.com`): the console model
  (`lG` in the bundle) has
  `["condition","enableNonConsecutiveTriggering","event","name","targetFlow","targetPage","targetPlaybook","triggerFulfillment"]`
  -> if `targetPlaybook` exists and is part of the SAME oneof `target`.

Practical consequence: you must remove `targetPage`/`targetFlow` from the route before
setting `targetPlaybook`, or the server responds
`oneof field 'target' is already set`."""
from __future__ import annotations

import copy
from typing import Any

from .cxapi import CxApi

TARGET_KEYS = ("targetPage", "targetFlow", "targetPlaybook")


def _playbook_resource(agent_id: str, playbook_id: str, project: str, location: str) -> str:
    return f"projects/{project}/locations/{location}/agents/{agent_id}/playbooks/{playbook_id}"


def find_route(routes: list[dict], ref: str | int) -> tuple[int, dict]:
    """Locate the route by `name` (uuid), by exact condition, or by index."""
    if isinstance(ref, int):
        if not 0 <= ref < len(routes):
            raise LookupError(f"route index out of range: {ref} (there are {len(routes)})")
        return ref, routes[ref]
    for i, r in enumerate(routes):
        if r.get("name") == ref:
            return i, r
    for i, r in enumerate(routes):
        if r.get("condition") == ref:
            return i, r
    # partial match by condition (useful if the text was truncated)
    hits = [i for i, r in enumerate(routes) if ref and ref in (r.get("condition") or "")]
    if len(hits) == 1:
        return hits[0], routes[hits[0]]
    raise LookupError(f"route not found: {ref!r} (candidates by condition: {hits})")


def build_routes_with_playbook(
    routes: list[dict],
    index: int,
    playbook_resource: str,
    clear_targets: bool = True,
) -> list[dict]:
    """Return the list of routes with the playbook target in `index`."""
    out = copy.deepcopy(routes)
    route = out[index]
    if clear_targets:
        for key in ("targetPage", "targetFlow"):
            route.pop(key, None)
    route["targetPlaybook"] = playbook_resource
    return out


def summarize(routes: list[dict]) -> list[dict]:
    """Compact view of the routes: condition, target, and whether it has a target."""
    rows = []
    for r in routes:
        target = None
        kind = "NO TARGET"
        for key in TARGET_KEYS:
            if r.get(key):
                target = str(r[key]).split("/")[-1]
                kind = key
                break
        rows.append(
            {
                "name": r.get("name"),
                "condition": r.get("condition"),
                "target_kind": kind,
                "target": target,
            }
        )
    return rows


def set_route_playbook(
    api: CxApi,
    agent_id: str,
    flow_id: str,
    page_id: str,
    route_ref: str | int,
    playbook_id: str,
    backend: str = "console",
    dry_run: bool = False,
    timeout: float | None = 300.0,
) -> dict:
    """Set a playbook as the target of a transition route. Returns the report."""
    from .guard import assert_writable

    assert_writable(agent_id, api.cfg)

    page = api.get_page(agent_id, flow_id, page_id, backend=backend)
    routes = page.get("transitionRoutes") or []
    if not routes:
        raise LookupError("the page has no transitionRoutes")

    index, current = find_route(routes, route_ref)
    playbook_resource = _playbook_resource(
        agent_id, playbook_id, api.cfg.project, api.cfg.location
    )
    new_routes = build_routes_with_playbook(routes, index, playbook_resource)

    before = summarize(routes)
    result: dict[str, Any] = {
        "agent": agent_id,
        "flow": flow_id,
        "page": page_id,
        "backend": backend,
        "route": {
            "index": index,
            "name": current.get("name"),
            "condition": current.get("condition"),
            "destination_before": before[index]["target"],
            "destination_after": playbook_id,
        },
        "routes_before": before,
    }

    if dry_run:
        result["dry_run"] = True
        result["body"] = {"transitionRoutes": new_routes}
        return result

    api.update_page(
        agent_id,
        flow_id,
        page_id,
        {"transitionRoutes": new_routes},
        update_mask="transitionRoutes",
        backend=backend,
        timeout=timeout,
    )

    after_page = api.get_page(agent_id, flow_id, page_id, backend=backend)
    after_routes = after_page.get("transitionRoutes") or []
    result["routes_after"] = summarize(after_routes)
    result["total_routes"] = len(after_routes)
    result["verified_by_read"] = after_routes[index].get("targetPlaybook") == playbook_resource
    result["note"] = (
        "The public GET does not expose targetPlaybook in the routes; the real "
        "verification is at RUNTIME (the conversation trace). Use cx_verify_route_target."
    )
    return result


def diff_routes(original: list[dict], clone: list[dict]) -> dict:
    """Compare the routes of two agents by route `name` (stable across copies)."""
    by_name_orig = {r.get("name"): r for r in original}
    by_name_clone = {r.get("name"): r for r in clone}
    rows = []
    for name, r in original:
        c = by_name_clone.get(name)
        if c is None:
            rows.append({"name": name, "condition": r.get("condition"), "status": "MISSING IN THE CLONE"})
            continue
        cond_same = (r.get("condition") or "") == (c.get("condition") or "")
        t_orig = next((k for k in TARGET_KEYS if r.get(k)), None)
        t_clone = next((k for k in TARGET_KEYS if c.get(k)), None)
        rows.append(
            {
                "name": name,
                "condition": r.get("condition"),
                "condition_equal": cond_same,
                "destination_original": t_orig,
                "clone_destination": t_clone,
                "status": "OK" if cond_same and t_orig == t_clone else "DIFFERS",
            }
        )
    for name, c in clone.items():
        if name not in by_name_orig:
            rows.append({"name": name, "condition": c.get("condition"), "status": "EXTRA IN THE CLONE"})
    return {"routes": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# Recipe DELETE + CREATE
# The PATCH of a page of a large agent eats the server deadline
# (408 at ~240 s, measured) because the write triggers revalidation of the
# entire agent. CREATE validates less, so the page is recreated.
# ---------------------------------------------------------------------------
PAGE_CONTENT_FIELDS = (
    "entryFulfillment",
    "form",
    "transitionRoutes",
    "eventHandlers",
    "transitionRouteGroups",
    "parameterType",
    "labels",
)


def page_content(page: dict) -> dict:
    """The content of a page that must be dragged when recreating it."""
    body: dict = {"displayName": page.get("displayName")}
    for field in PAGE_CONTENT_FIELDS:
        if page.get(field):
            body[field] = page[field]
    return body


def recreate_page_with_playbook(
    api: CxApi,
    agent_id: str,
    flow_id: str,
    page_id: str,
    route_ref: str | int,
    playbook_id: str,
    backend: str = "public",
    timeout: float = 300.0,
    rename: bool = True,
    repoint: bool = True,
) -> dict:
    """Recreate the page with the target as playbook (CREATE instead of PATCH).

    Steps: CREATE with a temporary name -> DELETE of the old one -> rename ->
    re-point the incoming references (by id, only those pointing to the
    old page)."""
    from .guard import assert_writable

    assert_writable(agent_id, api.cfg)

    base = f"{api.cfg.agent_base(agent_id, backend)}/flows/{flow_id}/pages"
    old = api.get_page(agent_id, flow_id, page_id, backend=backend)
    routes = old.get("transitionRoutes") or []
    index, current = find_route(routes, route_ref)
    playbook_resource = _playbook_resource(
        agent_id, playbook_id, api.cfg.project, api.cfg.location
    )
    new_routes = build_routes_with_playbook(routes, index, playbook_resource)

    body = page_content(old)
    body["displayName"] = f"{old.get('displayName')}_mcp_tmp_{int(__import__('time').time())}"
    body["transitionRoutes"] = new_routes

    created = api.http.json("POST", base, api._headers(), body, timeout=timeout)
    new_id = created["name"].split("/")[-1]
    out: dict[str, Any] = {
        "strategy": "DELETE+CREATE",
        "old_page": page_id,
        "new_page": new_id,
        "route": {
            "index": index,
            "name": current.get("name"),
            "condition": current.get("condition"),
            "destination": playbook_id,
        },
        "steps": ["CREATE with a temporary name", "DELETE of the old page"],
    }

    api.http.json("DELETE", f"{base}/{page_id}", api._headers(), timeout=timeout)
    out["steps"].append("DELETE done")

    if rename:
        api.update_page(
            agent_id,
            flow_id,
            new_id,
            {"displayName": old.get("displayName")},
            update_mask="displayName",
            backend=backend,
            timeout=timeout,
        )
        out["steps"].append("renamed to the original displayName")

    if repoint:
        repointed = []
        for flow in api.list_flows(agent_id, backend):
            fid = flow["name"].split("/")[-1]
            for page in api.list_pages(agent_id, fid, backend):
                pid = page["name"].split("/")[-1]
                payload: dict[str, Any] = {}
                mask: list[str] = []
                for route in page.get("transitionRoutes") or []:
                    if route.get("targetPage", "").endswith(f"/{page_id}"):
                        route["targetPage"] = route["targetPage"].rsplit("/", 1)[0] + f"/{new_id}"
                        payload.setdefault("transitionRoutes", []).append(route)
                        mask.append("transitionRoutes")
                for eh in page.get("eventHandlers") or []:
                    if eh.get("targetPage", "").endswith(f"/{page_id}"):
                        eh["targetPage"] = eh["targetPage"].rsplit("/", 1)[0] + f"/{new_id}"
                        payload.setdefault("eventHandlers", []).append(eh)
                        mask.append("eventHandlers")
                if payload:
                    api.update_page(
                        agent_id, fid, pid, payload, ",".join(sorted(set(mask))), backend=backend, timeout=timeout
                    )
                    repointed.append(f"{fid}/{pid}")
        out["repointed"] = repointed
        out["steps"].append(f"re-pointed references: {repointed}")

    check = api.get_page(agent_id, flow_id, new_id, backend=backend)
    out["routes"] = summarize(check.get("transitionRoutes") or [])
    out["target_playbook_visible"] = any(
        "targetPlaybook" in r for r in (check.get("transitionRoutes") or [])
    )
    out["note"] = (
        "The GET may not expose targetPlaybook; confirm with cx_verify_route_target."
    )
    return out
