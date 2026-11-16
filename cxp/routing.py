"""Pure helpers over the transition routes (no network, no browser)."""
from __future__ import annotations

TARGET_KEYS = ("targetPage", "targetFlow", "targetPlaybook")


def find_route(routes: list[dict], ref: str | int) -> tuple[int, dict]:
    """Locate the route by `name` (uuid), by exact condition, or by index."""
    if isinstance(ref, int):
        if not 0 <= ref < len(routes):
            raise LookupError(f"route index out of range:{ref} (there are {len(routes)})")
        return ref, routes[ref]
    if ref is None or ref == "":
        raise LookupError("the route reference is missing")
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
        f"route not found:{ref!r} (partial matches: {hits})"
    )


def summarize(routes: list[dict], playbooks: dict[str, str] | None = None) -> list[dict]:
    """Compact view: condition, target type and human-readable target name.

    `playbooks`: **id -> displayName** map of the agent's playbooks (the
    one returned by `ConsoleApi.playbook_index()[1]`). If the route target
    is a playbook, its human-readable name is shown; if it is a page or a flow,
    its id is shown (the caller resolves that part)."""
    inv = dict(playbooks or {})
    rows = []
    for r in routes:
        kind, target = "NO TARGET", None
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
