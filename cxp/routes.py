"""Nucleo: poner un PLAYBOOK como destino de una TransitionRoute.

Por que hace falta esto (medido, ver 02-LIMITES-DE-LA-API.md del workspace):

* Proto PUBLICO (`global-dialogflow.googleapis.com`): `TransitionRoute.target` es
  un `oneof` con SOLO `target_page` y `target_flow`. `targetPlaybook` no existe.
  Un PATCH con `targetPlaybook` se **descarta en silencio**.
* Proto INTERNO de la consola (`dialogflow.clients6.google.com`): el modelo de
  la consola (`lG` en el bundle) tiene
  `["condition","enableNonConsecutiveTriggering","event","name","targetFlow","targetPage","targetPlaybook","triggerFulfillment"]`
  -> si existe `targetPlaybook` y es parte del MISMO oneof `target`.

Consecuencia practica: hay que quitar `targetPage`/`targetFlow` de la ruta antes
de poner `targetPlaybook`, o el servidor responde
`oneof field 'target' is already set`.
"""
from __future__ import annotations

import copy
from typing import Any

from .cxapi import CxApi

TARGET_KEYS = ("targetPage", "targetFlow", "targetPlaybook")


def _playbook_resource(agent_id: str, playbook_id: str, project: str, location: str) -> str:
    return f"projects/{project}/locations/{location}/agents/{agent_id}/playbooks/{playbook_id}"


def find_route(routes: list[dict], ref: str | int) -> tuple[int, dict]:
    """Ubica la ruta por `name` (uuid), por condicion exacta o por indice."""
    if isinstance(ref, int):
        if not 0 <= ref < len(routes):
            raise LookupError(f"indice de ruta fuera de rango: {ref} (hay {len(routes)})")
        return ref, routes[ref]
    for i, r in enumerate(routes):
        if r.get("name") == ref:
            return i, r
    for i, r in enumerate(routes):
        if r.get("condition") == ref:
            return i, r
    # coincidencia parcial por condicion (util si el texto fue truncado)
    hits = [i for i, r in enumerate(routes) if ref and ref in (r.get("condition") or "")]
    if len(hits) == 1:
        return hits[0], routes[hits[0]]
    raise LookupError(f"ruta no encontrada: {ref!r} (candidatas por condicion: {hits})")


def build_routes_with_playbook(
    routes: list[dict],
    index: int,
    playbook_resource: str,
    clear_targets: bool = True,
) -> list[dict]:
    """Devuelve la lista de rutas con el destino a playbook en `index`."""
    out = copy.deepcopy(routes)
    route = out[index]
    if clear_targets:
        for key in ("targetPage", "targetFlow"):
            route.pop(key, None)
    route["targetPlaybook"] = playbook_resource
    return out


def summarize(routes: list[dict]) -> list[dict]:
    """Vista compacta de las rutas: condicion, destino y si tiene destino."""
    rows = []
    for r in routes:
        target = None
        kind = "SIN DESTINO"
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
    """Pone un playbook como destino de una transition route. Devuelve el informe."""
    from .guard import assert_writable

    assert_writable(agent_id, api.cfg)

    page = api.get_page(agent_id, flow_id, page_id, backend=backend)
    routes = page.get("transitionRoutes") or []
    if not routes:
        raise LookupError("la pagina no tiene transitionRoutes")

    index, current = find_route(routes, route_ref)
    playbook_resource = _playbook_resource(
        agent_id, playbook_id, api.cfg.project, api.cfg.location
    )
    new_routes = build_routes_with_playbook(routes, index, playbook_resource)

    before = summarize(routes)
    result: dict[str, Any] = {
        "agente": agent_id,
        "flow": flow_id,
        "pagina": page_id,
        "backend": backend,
        "ruta": {
            "indice": index,
            "name": current.get("name"),
            "condition": current.get("condition"),
            "destino_antes": before[index]["target"],
            "destino_despues": playbook_id,
        },
        "rutas_antes": before,
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
    result["rutas_despues"] = summarize(after_routes)
    result["rutas_totales"] = len(after_routes)
    result["verificado_por_lectura"] = after_routes[index].get("targetPlaybook") == playbook_resource
    result["nota"] = (
        "El GET publico no expone targetPlaybook en las rutas; la verificacion real "
        "es de RUNTIME (traza de la conversacion). Usa cx_verify_route_target."
    )
    return result


def diff_routes(original: list[dict], clone: list[dict]) -> dict:
    """Compara las rutas de dos agentes por `name` de ruta (estable entre copias)."""
    by_name_orig = {r.get("name"): r for r in original}
    by_name_clone = {r.get("name"): r for r in clone}
    rows = []
    for name, r in original:
        c = by_name_clone.get(name)
        if c is None:
            rows.append({"name": name, "condition": r.get("condition"), "estado": "FALTA EN EL CLON"})
            continue
        cond_same = (r.get("condition") or "") == (c.get("condition") or "")
        t_orig = next((k for k in TARGET_KEYS if r.get(k)), None)
        t_clone = next((k for k in TARGET_KEYS if c.get(k)), None)
        rows.append(
            {
                "name": name,
                "condition": r.get("condition"),
                "condicion_igual": cond_same,
                "destino_original": t_orig,
                "destino_clon": t_clone,
                "estado": "OK" if cond_same and t_orig == t_clone else "DIFIERE",
            }
        )
    for name, c in clone.items():
        if name not in by_name_orig:
            rows.append({"name": name, "condition": c.get("condition"), "estado": "EXTRA EN EL CLON"})
    return {"rutas": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# Receta DELETE + CREATE
#
# El PATCH de una pagina de un agente grande se come el deadline del servidor
# (408 a los ~240 s, medido) porque la escritura dispara la revalidacion del
# agente entero. CREATE valida menos, asi que se recrea la pagina.
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
    """El contenido de una pagina que hay que arrastrar al recrearla."""
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
    """Recrea la pagina con el destino a playbook (CREATE en vez de PATCH).

    Pasos: CREATE con nombre temporal -> DELETE de la vieja -> renombrar ->
    re-apuntar las referencias entrantes (por id, solo las que apuntan a la
    pagina vieja).
    """
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
        "estrategia": "DELETE+CREATE",
        "pagina_vieja": page_id,
        "pagina_nueva": new_id,
        "ruta": {
            "indice": index,
            "name": current.get("name"),
            "condition": current.get("condition"),
            "destino": playbook_id,
        },
        "pasos": ["CREATE con nombre temporal", "DELETE de la pagina vieja"],
    }

    api.http.json("DELETE", f"{base}/{page_id}", api._headers(), timeout=timeout)
    out["pasos"].append("DELETE hecho")

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
        out["pasos"].append("renombrada al displayName original")

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
        out["reapuntadas"] = repointed
        out["pasos"].append(f"referencias re-apuntadas: {repointed}")

    check = api.get_page(agent_id, flow_id, new_id, backend=backend)
    out["rutas"] = summarize(check.get("transitionRoutes") or [])
    out["target_playbook_visible"] = any(
        "targetPlaybook" in r for r in (check.get("transitionRoutes") or [])
    )
    out["nota"] = (
        "El GET puede no exponer targetPlaybook; confirma con cx_verify_route_target."
    )
    return out
