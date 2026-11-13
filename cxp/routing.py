"""Helpers puros sobre las transition routes (sin red, sin navegador)."""
from __future__ import annotations

TARGET_KEYS = ("targetPage", "targetFlow", "targetPlaybook")


def find_route(routes: list[dict], ref: str | int) -> tuple[int, dict]:
    """Ubica la ruta por `name` (uuid), por condicion exacta o por indice."""
    if isinstance(ref, int):
        if not 0 <= ref < len(routes):
            raise LookupError(f"indice de ruta fuera de rango: {ref} (hay {len(routes)})")
        return ref, routes[ref]
    if ref is None or ref == "":
        raise LookupError("falta la referencia de la ruta")
    for i, r in enumerate(routes):
        if r.get("name") == ref or (r.get("name") or "").split("/")[-1] == ref:
            return i, r
    for i, r in enumerate(routes):
        if r.get("condition") == ref:
            return i, r
    hits = [i for i, r in enumerate(routes) if ref in (r.get("condition") or "")]
    if len(hits) == 1:
        return hits[0], routes[hits[0]]
    raise LookupError(
        f"ruta no encontrada: {ref!r} (coincidencias parciales: {hits})"
    )


def summarize(routes: list[dict], playbooks: dict[str, str] | None = None) -> list[dict]:
    """Vista compacta: condicion, tipo de destino y nombre legible del destino.

    `playbooks`: mapa **id -> displayName** de los playbooks del agente (el
    que devuelve `ConsoleApi.playbook_index()[1]`). Si el destino de la ruta
    es un playbook, se muestra su nombre legible; si es una pagina o un flow,
    se muestra su id (esa parte la resuelve el llamante).
    """
    inv = dict(playbooks or {})
    rows = []
    for r in routes:
        kind, target = "SIN DESTINO", None
        for key in TARGET_KEYS:
            if r.get(key):
                target = str(r[key]).split("/")[-1]
                kind = key
                break
        rows.append({
            "name": (r.get("name") or "").split("/")[-1],
            "condition": r.get("condition"),
            "target_kind": kind,
            "target": target,
            "target_name": inv.get(target, target) if target else None,
        })
    return rows
