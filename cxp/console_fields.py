"""Write GENERIC of a field that the public API does not declare.

The idea: there is no per-resource method and no per-gap method. There is ONE
operation:

    GET  resource (by its `name`)
    -> the value is applied on the requested route (`a.b[0].c`)
    -> the resource is PATCHed with the minimal mask (the top-level key)
    -> it is re-read and COMPARES

The final comparison is what gives the guarantee: the server can **drop**
a field silently (that is what the public API does; the console
backend also does it with some fields). That is why the response says what actually
changed and what was lost."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .gaps import apply_path, read_path, strip_derived, top_level_keys


def _truncate(value: Any, limit: int = 300) -> Any:
    import json
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit] + "..." if len(text) > limit else value


def diff_json(before: Any, after: Any, prefix: str = "") -> list[dict[str, Any]]:
    """Differences between two structures, in `{route, before, after}` form."""
    changes: list[dict[str, Any]] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            route = f"{prefix}.{key}" if prefix else key
            if key not in before:
                changes.append({"route": route, "before": None,
                                "after": _truncate(after[key])})
            elif key not in after:
                changes.append({"route": route, "before": _truncate(before[key]),
                                "after": None})
            else:
                changes.extend(diff_json(before[key], after[key], route))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        for i in range(max(len(before), len(after))):
            route = f"{prefix}[{i}]"
            if i >= len(before):
                changes.append({"route": route, "before": None,
                                "after": _truncate(after[i])})
            elif i >= len(after):
                changes.append({"route": route, "before": _truncate(before[i]),
                                "after": None})
            else:
                changes.extend(diff_json(before[i], after[i], route))
        return changes
    if before != after:
        changes.append({"route": prefix or "$", "before": _truncate(before),
                        "after": _truncate(after)})
    return changes


@dataclass
class Result:
    """The outcome of a `set_field`: what was asked, what was saved, what was lost."""
    resource: str
    field: str
    value: Any
    saved: bool = False
    verified: bool = False
    update_mask: list[str] = field(default_factory=list)
    before: Any = None
    after: Any = None
    changes: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "resource": self.resource,
            "field": self.field,
            "value": _truncate(self.value),
            "update_mask": self.update_mask,
            # The same list under the camelCase name the API uses for the query
            # parameter: the docs quote `updateMask`.
            "updateMask": self.update_mask,
            "saved": self.saved,
            "verified": self.verified,
        }
        if self.before is not None:
            d["value_before"] = _truncate(self.before)
        if self.after is not None:
            d["value_after"] = _truncate(self.after)
        if self.changes:
            d["changes"] = self.changes[:40]
        if self.dropped:
            d["dropped_by_the_server"] = self.dropped
        if self.error:
            d["error"] = self.error
        return d


def set_field(api, resource_name: str, path: str, value: Any, *,
              dry_run: bool = True, project: str = "",
              language_code: str | None = None,
              extra_fields: tuple[str, ...] = ()) -> Result:
    """Write `value` to `path` of resource `resource_name`.

    `resource_name` is the complete `name` returned by the API
    (`projects/p/locations/l/agents/a/flows/f/pages/p`), so it works with
    ANY agent resource without writing a method per type.

    `path` is a route with indices: `transitionRoutes[0].targetPlaybook`."""
    res = Result(resource=resource_name, field=path, value=value)
    try:
        before = api.request("GET", api._url("/" + resource_name))
    except Exception as exc:
        res.error = f"could not read the resource: {exc}"
        return res

    value_before = read_path(before, path)
    res.before = value_before

    if dry_run:
        # Shows what WOULD PASS, without writing.
        simulated = copy.deepcopy(before)
        try:
            apply_path(simulated, path, value)
        except ValueError as exc:
            res.error = str(exc)
            return res
        res.after = read_path(simulated, path)
        res.update_mask = top_level_keys(path)
        res.changes = diff_json(before, simulated)
        return res

    body = strip_derived(copy.deepcopy(before), extra=extra_fields)
    try:
        apply_path(body, path, value)
    except ValueError as exc:
        res.error = str(exc)
        return res

    mask = top_level_keys(path)
    res.update_mask = mask
    try:
        api.patch_resource(resource_name, body, update_mask=",".join(mask),
                           language_code=language_code, project=project)
        res.saved = True
    except Exception as exc:
        res.error = f"the PATCH failed: {exc}"
        return res

    try:
        after = api.request("GET", api._url("/" + resource_name))
    except Exception as exc:
        res.error = f"it was written but could not be read back: {exc}"
        return res

    res.after = read_path(after, path)
    res.verified = res.after == value
    res.changes = diff_json(before, after)
    # What was asked to be written and the server did NOT save.
    lost = []
    if not res.verified:
        lost.append(path)
    for change in res.changes:
        if change["route"].startswith(path.split("[")[0]) and change["after"] is None:
            lost.append(change["route"])
    res.dropped = sorted(set(lost))
    return res
