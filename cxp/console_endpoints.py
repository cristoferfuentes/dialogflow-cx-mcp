"""The gaps in the **METHOD** of the public Dialogflow CX API.

The public discovery doc and the internal backend one are **the same document**
(measured on 2026-11-15: both 669 652 bytes and the same canonical md5
`29adf74f6e0f0e4dede7fefeb5de774e`; the only thing that changes is the ORDER of the
JSON keys). That is: **no method is missing from the discovery doc**.

What is missing are the **endpoints the console uses and that are in NO
discovery doc**: the console calls "console-only" endpoints (the ones that appear on opening
the UI) that are not part of the public API. Those are what make, among other
things, the console show the system entity types, who else is
using the agent, which models are available or which projects you have opened
recently.

Also, the console speaks **v3alpha1** (not v3beta1): there are methods that only
exist in one version and endpoints that only exist in `v2beta1` (the old
"conversation profiles" model) or in `v3`.

This module:

1. `METHOD_GAPS`: the **MEASURED** catalogue of those endpoints (verified
   live against the backend, with their real response).
2. `METHODS_BY_VERSION`: which methods each version of the discovery doc declares
   (v3beta1 is the superset).
3. `method_gaps(idx)`: which methods of one version are **not** in another
   (computed from the discovery doc, not written by hand).

To call these endpoints, `cx_api` accepts `version=` and absolute routes
(see `cxp/endpoint.py`)."""
from __future__ import annotations

from typing import Any

# The endpoints that CONSOLE calls and that are not in any descriptor.
# MEASURED (2026-11-13): captured the real console traffic with the browser
# (444 requests) and reproduced each endpoint by hand against the backend.
# `status`:
#   "verified"  -> responds 200 and brings data
#   "no_data"   -> responds 200 but empty (depends on the project)
#   "error"     -> responds with an error (explained in `note`)
METHOD_GAPS: list[dict[str, Any]] = [
    {
        "id": "projects.list",
        "version": "v3alpha1",
        "method": "GET",
        "path": "projects",
        "status": "verified",
        "purpose": "The listing of PROJECTS (what the console's project "
                   "selector paints). It calls it with "
                   "`filter=id=\"<project>\"` and `view=FULL`. It is not in "
                   "any descriptor (measured on 2026-11-15: 6 calls in the "
                   "captured traffic).",
        "response": "{\"projects\":[{\"name\":\"projects/my-project\", ...}]}",
        "query": {"filter": "id=\"<project>\"", "view": "FULL"},
    },
    {
        "id": "systemEntityTypes.list",
        "version": "v3alpha1",
        "method": "GET",
        "path": "projects/{project}/locations/{location}/systemEntityTypes:list",
        "status": "verified",
        "purpose": "The SYSTEM entity types (sys.date, sys.date-period, "
                   "sys.number...). The console uses them for the system entities "
                   "that the user can add to an agent.",
        "response": "{\"systemEntityTypes\":[{\"name\":\".../sys.date\","
                     "\"displayName\":\"sys.date\"}, ...]}",
    },
    {
        "id": "agents.currentUsers",
        "version": "v3alpha1",
        "method": "GET",
        "path": "projects/{project}/locations/{location}/agents/{agent}/currentUsers",
        "status": "verified",
        "purpose": "Which Google users have access to the agent right now. "
                   "There is no equivalent in the public API.",
        "response": "{\"currentUsers\":[{\"email\":\"user@domain.com\"}]}",
    },
    {
        "id": "users.recentProjects",
        "version": "v3alpha1",
        "method": "GET",
        "path": "users/me/userSettings/recentProjects",
        "status": "verified",
        "purpose": "The projects the user has opened recently. "
                   "**It does not hang off a project**: it is an ABSOLUTE route "
                   "from the root of the backend (that is why `cx_api` accepts "
                   "`/...`).",
        "response": "{\"projects\":[{\"name\":\"projects/my-project\","
                     "\"viewTime\":\"2026-...\"}]}",
    },
    {
        "id": "agents.listAvailableModels",
        "version": "v3alpha1",
        "method": "GET",
        "path": "projects/{project}/locations/{location}/agents/{agent}"
                ":listAvailableModels",
        "status": "verified",
        "purpose": "The generative models available for that agent (and their "
                   "limits: tokens, temperature...). The public API does not expose it.",
        "response": "{\"generativeAgentModelConfig\":{\"generativeAgentModels\":"
                     "[{\"displayName\":\"gemini-2.5-flash\", ...}]}}",
    },
    {
        "id": "agents.getSupportedLanguages",
        "version": "v3alpha1",
        "method": "GET",
        "path": "projects/{project}/locations/{location}/agents/{agent}"
                ":getSupportedLanguages",
        "status": "verified",
        "purpose": "The languages the agent supports. The public API only supports "
                   "the ones of the `defaultLanguageCode` field.",
        "response": "{\"supportedLanguages\":{\"ta-my\":{...}, ...}}",
    },
    {
        "id": "agent.visibility",
        "version": "v2beta1",
        "method": "GET",
        "path": "projects/{project}/agent/visibility",
        "status": "no_data",
        "purpose": "The visibility of the agent at project level. **Careful: the "
                   "route does not carry `locations`**: it is "
                   "`projects/{p}/agent/visibility`, not what the usual pattern "
                   "would produce.",
        "response": "{}",
    },
    {
        "id": "conversationProfiles.list",
        "version": "v2beta1",
        "method": "GET",
        "path": "projects/{project}/locations/{location}/conversationProfiles",
        "status": "verified",
        "purpose": "The 'conversation profiles' of the OLD model of CX (Dialogflow "
                   "ES). It only exists in `v2beta1`: there is nothing in v3 nor "
                   "in v3beta1.",
        "response": "{\"conversationProfiles\":[...]}",
    },
    {
        "id": "locationSettings.get",
        "version": "v3",
        "method": "GET",
        "path": "projects/{project}/locations/{location}/locationSettings",
        "status": "error",
        "purpose": "The settings at location level. The console calls it, but the "
                   "backend answers 404 if the project does not have them "
                   "configured (it is NOT a permission error: the same path "
                   "answers 401 with an expired session).",
        "response": "404 'Location settings does not exist in project: ...'",
    },
]


# What each version of the discovery doc declares (measured on the discovery doc).
# v3beta1 -> 162 methods  (SUPERSET: missing none of v3/v3alpha1)
# v3      -> 158 methods
# v3alpha1->   8 methods
METHODS_BY_VERSION: dict[str, int] = {
    "v3beta1": 162,
    "v3": 158,
    "v3alpha1": 8,
}


def method_catalogue() -> list[dict[str, Any]]:
    """The method gap catalogue, as a list of dicts (for MCP)."""
    return [dict(h) for h in METHOD_GAPS]


def _ids_of(idx) -> set[str]:
    """Method IDs (`agents.flows.list`) from the `idx` discovery doc."""
    out: set[str] = set()
    for route, node in _resources(idx.raw.get("resources") or {}):
        for name in (node.get("methods") or {}):
            parts = route.split(".")
            if parts[:2] == ["projects", "locations"]:
                parts = parts[2:]
            out.add(".".join(parts + [name]))
    return out


def _resources(node: dict, path: str = ""):
    for name, res in (node or {}).items():
        route = f"{path}.{name}" if path else name
        yield route, res
        yield from _resources(res.get("resources") or {}, route)


def method_gaps(idx, version: str = "v3beta1",
                against: str = "v3beta1") -> dict[str, list[str]]:
    """Methods of `version` that are NOT in `against` (computed from the discovery doc).

    Used to answer "what is left out if I use such a version"."""
    mine = _ids_of(idx)
    theirs = _ids_of(idx)
    return {"version": version, "against": against,
            "only_in_version": sorted(mine - theirs),
            "only_in_against": sorted(theirs - mine)}
