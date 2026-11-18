"""How a route against the console backend is built.

The internal backend (`dialogflow.clients6.google.com`) serves **several versions**
of the proto and also has endpoints that are **not in any descriptor** (the
ones the console itself uses). To reach them an ONLY rule is needed that
is explicit and generic:

    "v3alpha1/projects/p/locations/global/agents/a/currentUsers"   -> version + absolute
    "projects/p/agent/visibility"                                   -> absolute (it is already of a project)
    "/users/me/userSettings/recentProjects"                         -> ABSOLUTE from the root
    "agents/{agent}/flows"                                          -> relative (it is prefixed)

Rules (in this order):

1. If the route starts with `v.../` (`v3`, `v3beta1`, `v3alpha1`, `v2beta1`...),
   that prefix IS the version of the call and is stripped from the route.
2. If the route starts with `/`, it is **absolute from the backend root**: `projects/.../locations/...` is
   not prepended (this is how endpoints that
   do not hang off a project are reached, such as `users/me/userSettings/recentProjects`).
3. If the route starts with `projects/`, it is left as is (regardless of whether the
   project is another one or whether it carries `locations`).
4. In any other case it is **relative**: `projects/{project}/locations/{location}/` is
   prepended.

This module does no network: it only resolves. That makes it trivial to test."""
from __future__ import annotations

import re
from typing import Any

# `v3`, `v3beta1`, `v3alpha1`, `v2beta1`, `v2`...
VERSION_RE = re.compile(r"^(v[0-9]+(?:[a-z]+[0-9]*)?)(?=/|$)")

# The versions the backend serves (measured: all four respond; `v2beta1`
# only for `agent/visibility` and `conversationProfiles`).
SERVED_VERSIONS = ("v3beta1", "v3alpha1", "v3", "v2beta1")


class PathError(ValueError):
    pass


def split_version(path: str) -> tuple[str, str]:
    """Splits the version prefix from a route.

    `("v3alpha1/users/me", "v3alpha1")` -> `("users/me", "v3alpha1")`
    `("users/me", "")`                  -> `("users/me", "")`
    """
    p = (path or "").strip()
    m = VERSION_RE.match(p)
    if not m:
        return p, ""
    version = m.group(1)
    rest = p[m.end():]
    if rest.startswith("/"):
        rest = rest[1:]
    return rest, version


def is_absolute(path: str) -> bool:
    """`True` if the route should not receive the project/location prefix."""
    p = (path or "").strip()
    return p.startswith("/") or p.startswith("projects/")


def _canonical(path: str) -> str:
    """Resolve the `.` and `..` segments of a path, POSIX style.

    A `..` that would leave the root is an error (it is not a path the
    caller can be allowed to ask for)."""
    parts: list[str] = []
    for seg in (path or "").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                raise PathError("the path escapes the root (traversal)")
            parts.pop()
            continue
        parts.append(seg)
    if not parts:
        raise PathError("empty path")
    return "/".join(parts)


def resolve_path(path: str, *, version: str = "", project: str = "",
                 location: str = "", name: str = "", parent: str = "",
                 params: dict[str, Any] | None = None,
                 agent: str = "") -> tuple[str, str, str]:
    """Resolve a request route.

    Returns `(final_route, final_version, extra_query)`.

    - `final_route`: without version and without host prefix ("projects/p/...").
    - `final_version`: the version to use (the route's if it carries one, otherwise
      the one passed in, otherwise `v3beta1`).
    - `extra_query`: parameters moved to the query because the
      discovery doc did not carry them (e.g. `languageCode`)."""
    p, route_version = split_version(path)
    final_version = route_version or version or "v3beta1"

    values: dict[str, Any] = dict(params or {})
    if name:
        values["name"] = name
    if parent:
        values["parent"] = parent

    # A marker with no value is an error, not an empty string: better to fail
    # here than to send "projects//agents/". The value may come in `params`.
    for marker, value in (("{project}", project), ("{location}", location),
                          ("{agent}", values.get("agent") or agent)):
        if marker in p and not value:
            raise PathError(f"missing value for {marker} in {path!r}")

    # Full names can be passed on their own: if `name` is a resource `name`
    # and the path is a template, it is used as the value of `{name}`.
    # `{+name}` is the "the value IS the route" form of the discovery doc.
    for k, v in values.items():
        p = p.replace("{" + k + "}", str(v))
        p = p.replace("{+" + k + "}", str(v).lstrip("/"))

    if "{agent}" in p and not values.get("agent"):
        values["agent"] = agent or ""
        p = p.replace("{agent}", str(values["agent"]))

    if not p:
        raise PathError("empty path")

    # --- canonicalization (SECURITY) --------------------------------------
    # The path is user-supplied (the generic passthrough lets the caller give
    # any path) and it ends up **verbatim** in the URL. If it carried `..`,
    # the server would resolve it and land on a resource that the permission
    # check had not seen: `agents/OK/../../agents/FORBIDDEN` passes the guard
    # (it reads `OK`) and the server writes on `FORBIDDEN`.
    #
    # It is canonicalized HERE, BEFORE the project prefix is added, so that a
    # `..` can neither reach another agent nor escape the prefix
    # (`../../OTHER/locations/...` would otherwise become another project).
    if re.search(r"%2e|%2f", p, re.IGNORECASE):
        raise PathError(
            "the path carries an encoded traversal (%2e/%2f): it is not accepted"
        )
    # A path that starts with "/" is ABSOLUTE from the backend root; that has
    # to be decided BEFORE canonicalizing (which drops the empty segments).
    absolute = p.startswith("/")
    p = _canonical(p)

    # --- prefixes ---------------------------------------------------------
    if absolute:
        pass  # absolute from the root: no project prefix is added
    elif not p.startswith("projects/"):
        if not project:
            raise PathError(f"the project is required for the relative route {path!r}")
        p = f"projects/{project}/locations/{location or 'global'}/{p}"

    # The gaps in `{project}` / `{location}` that may remain.
    p = p.replace("{project}", project or "").replace("{location}", location or "global")

    leftovers = re.findall(r"\{[A-Za-z0-9_.]+\}", p)
    if leftovers:
        raise PathError(f"missing parameters for the path: {sorted(set(leftovers))}")

    # Parameters not present in the template are query parameters.
    used = set(re.findall(r"\{([A-Za-z0-9_.]+)\}", path))
    extra_query = {k: v for k, v in values.items()
                   if k not in used and k not in ("name", "parent", "agent")}
    return p, final_version, extra_query


def url_for(version: str, path: str, host: str, query: dict[str, Any] | None = None,
            **extra: Any) -> str:
    """URL backend completion for an already-resolved route."""
    from urllib.parse import urlencode
    q: dict[str, Any] = {k: v for k, v in (query or {}).items() if v is not None}
    q.update({k: v for k, v in extra.items() if v is not None})
    suffix = f"?{urlencode(q)}" if q else ""
    return f"https://{host}/{version}/{path.lstrip('/')}{suffix}"
