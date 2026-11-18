"""The layer that covers the gaps of the public API of Dialogflow CX.

The problem (measured, not assumed)
-----------------------------------
The internal backend of the console (`dialogflow.clients6.google.com`) and the public API
(`dialogflow.googleapis.com`) serve **the same discovery doc**, so
comparing the descriptors finds NOTHING. What differs is **which fields
each one accepts**:

* The public API declares in its proto `TransitionRoute{target_page,target_flow}`
  and **silently drops** any other field.
* The console backend has a RICHER proto: it knows fields that the public API
  **does not even declare** (`TransitionRoute.targetPlaybook`,
  `Tool.actionSchemas`, `FormParameter.description`...).

Those fields are **gaps**: they can be read and (sometimes) written against the
console backend, but they cannot be requested through the public API.

This module brings the pieces:

1. `SchemaIndex`  -> the PUBLIC schema (the official discovery doc), with the
   EXACT schema that each resource uses (the doc mixes `V3` and
   `V3beta1` variants of the same name: the correct one must be chosen, not merged).
2. `find_gaps()`  -> walks a real resource and returns the fields that the
   backend exposes and the public API **does not declare**. It is computed **live**: it is not
   a hand-written list, so it ages well.
3. `apply_path()` / `read_path()` -> the route utilities (`a.b[0].c`) that
   allow touching a nested field without knowing anything about the resource's shape.

And `cxp.console_api.ConsoleApi.patch_resource()` does the generic PATCH per
`name` of resource, which is all that is needed to write to any
resource of the agent without writing one method per resource."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

DISCOVERY_HOST = "dialogflow.clients6.google.com"
DISCOVERY_URL = "https://{host}/$discovery/rest?version={version}"

# Schema name prefixes in the discovery doc. The same concept
# appears with different prefixes depending on the version (`V3beta1`, `V3`, `V2beta1`).
SCHEMA_PREFIXES = (
    "GoogleCloudDialogflowCxV3beta1",
    "GoogleCloudDialogflowCxV3alpha1",
    "GoogleCloudDialogflowCxV3",
    "GoogleCloudDialogflowV2beta1",
    "GoogleCloudDialogflowV2",
)

# Which prefix corresponds to each version of API.
PREFIX_BY_VERSION = {
    "v3beta1": "GoogleCloudDialogflowCxV3beta1",
    "v3alpha1": "GoogleCloudDialogflowCxV3alpha1",
    "v3": "GoogleCloudDialogflowCxV3",
}

# Fields the server derives that cannot (and should not) be sent on
# write. Measured: sending them makes PATCH fail or changes nothing.
DERIVED_FIELDS = frozenset({
    "etag", "createTime", "updateTime", "parent", "session", "environment",
    "derivedTransitions", "nluSettingsList", "projectNumber", "modelIdentifier",
    "referencedFlows", "referencedPlaybooks", "inlineActions", "tokenCount",
    "numTrainingPhrases", "defaultLanguageCode",
})

# Route expression: `field`, `field.sub`, `field[0]`, `field[0].sub[2]`.
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def norm_schema(name: str) -> str:
    """'GoogleCloudDialogflowCxV3beta1TransitionRoute' -> 'TransitionRoute'."""
    for pref in SCHEMA_PREFIXES:
        if name.startswith(pref):
            return name[len(pref):]
    return name


def parse_path(path: str) -> list[tuple[str, int | None]]:
    """`'a.b[0].c'` -> `[('a', None), ('b', 0), ('c', None)]`."""
    out: list[tuple[str, int | None]] = []
    for name, index in _PATH_TOKEN.findall(path or ""):
        if name:
            out.append((name, None))
        elif index != "":
            out.append(("", int(index)))
    return out


def read_path(obj: Any, path: str) -> Any:
    """Read the value of a route. Returns `None` if it does not exist (or if the route
    is empty: an empty route does not point to any field)."""
    if not path:
        return None
    cur = obj
    for name, index in parse_path(path):
        if index is not None:
            if not isinstance(cur, list) or index >= len(cur):
                return None
            cur = cur[index]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(name)
    return cur


def apply_path(obj: dict, path: str, value: Any) -> None:
    """Write `value` to route `path` of `obj` (in-place).

    Create any missing intermediate containers. If the last step is an
    index and the element does not exist, create it as a dictionary."""
    parts = parse_path(path)
    if not parts:
        raise ValueError("empty route")
    cur: Any = obj
    for i, (name, index) in enumerate(parts):
        last = (i == len(parts) - 1)
        if index is not None:
            if not isinstance(cur, list):
                raise ValueError(f"'{path}': expected a list at {name!r}")
            while len(cur) <= index:
                cur.append({})
            if last:
                cur[index] = value
            else:
                if not isinstance(cur[index], (dict, list)):
                    cur[index] = {}
                cur = cur[index]
        else:
            if not isinstance(cur, dict):
                raise ValueError(f"'{path}': expected an object at {name!r}")
            if last:
                cur[name] = value
            else:
                next_step = parts[i + 1]
                empty: Any = [] if next_step[1] is not None else {}
                if not isinstance(cur.get(name), (dict, list)):
                    cur[name] = empty
                cur = cur[name]


def top_level_keys(path: str) -> list[str]:
    """The update mask to send to touch `path`.

    `'transitionRoutes[0].targetPlaybook'` -> `['transitionRoutes']`
    (a PATCH with `updateMask=transitionRoutes` rewrites the entire array)."""
    parts = parse_path(path)
    return [parts[0][0]] if parts else []


def strip_derived(obj: dict, extra: Iterable[str] = ()) -> dict:
    """Copy of the object without the fields the server derives."""
    excluded = DERIVED_FIELDS | set(extra)
    return {k: v for k, v in obj.items() if k not in excluded}


@dataclass
class SchemaIndex:
    """The PUBLIC schema of the API, resolved per resource.

    The discovery doc contains the same concept with different prefixes
    (`V3beta1TransitionRoute` and `V3TransitionRoute`). Merging them would give false
    negatives (a field declared only in `V3` would look public). That is why
    the COMPLETE name of each schema is kept and the one matching
    the version of the API in use is chosen, with the `$ref` of the discovery doc as the source of
    truth for knowing which one each resource uses."""

    version: str = "v3beta1"
    props: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    canonical: dict[str, str] = dc_field(default_factory=dict)
    collections: dict[str, str] = dc_field(default_factory=dict)
    lists: dict[str, str] = dc_field(default_factory=dict)
    raw: dict[str, Any] = dc_field(default_factory=dict)

    # ------------------------------------------------------------- construction
    @classmethod
    def from_doc(cls, doc: dict[str, Any], version: str = "v3beta1") -> SchemaIndex:
        props: dict[str, dict[str, Any]] = {}
        for full, body in (doc.get("schemas") or {}).items():
            props[full] = body.get("properties") or {}

        by_short: dict[str, list[str]] = {}
        for full in props:
            by_short.setdefault(norm_schema(full), []).append(full)

        prefix = PREFIX_BY_VERSION.get(version, "")
        canonical: dict[str, str] = {}
        for short, full_names in by_short.items():
            chosen = next((f for f in full_names if f.startswith(prefix)), None)
            canonical[short] = chosen or full_names[0]

        idx = cls(version=version, props=props, canonical=canonical, raw=doc)
        idx.collections, idx.lists = idx._resolve_collections()
        return idx

    @classmethod
    def load(cls, path: str | Path, version: str = "v3beta1") -> SchemaIndex:
        return cls.from_doc(json.loads(Path(path).read_text(encoding="utf-8")),
                            version=version)

    def _resolve_collections(self) -> tuple[dict[str, str], dict[str, str]]:
        """(item, list) -> COMPLETE name of each collection's schema.

        Taken from the discovery doc, which declares TWO distinct types:

        * each collection's `get` -> the **item** schema
          (`testCases.results` -> `TestCaseResult`);
        * the `list` -> the **WRAPPER** schema
          (`testCases.results` -> `ListTestCaseResultsResponse`, which has
          `testCaseResults: [TestCaseResult]`).

        MEASURED (2026-11-15): sweeping the wrapper with the item schema (or vice
        versa) produces **false positives**: `testCaseResults` comes out as
        "field not declared" if the wrapper is compared against `TestCaseResult`.
        That is why the two are stored separately."""
        items: dict[str, str] = {}
        lists: dict[str, str] = {}

        def _walk(node: dict, route: str = "") -> None:
            for name, res in (node or {}).items():
                full_route = f"{route}.{name}" if route else name
                methods = res.get("methods") or {}
                ref_item = (methods.get("get", {}).get("response") or {}).get("$ref")
                ref_list = (methods.get("list", {}).get("response") or {}).get("$ref")
                key = ""
                if ".agents." in full_route:
                    key = full_route.split(".agents.")[1]
                elif full_route.endswith(".agents"):
                    key = "agents"
                if key:
                    if ref_item:
                        items[key] = ref_item
                    if ref_list:
                        lists[key] = ref_list
                _walk(res.get("resources") or {}, full_route)

        _walk(self.raw.get("resources") or {})
        return items, lists

    # ----------------------------------------------------------------- query
    def schema_of(self, collection: str) -> str:
        """The full schema name of the ITEM of a collection.

        ('tools' -> '...Tool'; 'testCases.results' -> '...TestCaseResult')"""
        ref = self.collections.get(collection)
        if ref:
            return ref
        return self.canonical.get(collection, "")

    def list_schema_of(self, collection: str) -> str:
        """The full schema name of the WRAPPER of a listing.

        ('tools' -> '...ListToolsResponse'; '' if the discovery doc does not declare it)."""
        return self.lists.get(collection, "")

    def list_item(self, list_schema: str) -> str:
        """The ITEM schema inside a listing wrapper ('' if none)."""
        pv = self.props_of(list_schema)
        for _field, spec in pv.items():
            ref = (spec.get("items") or {}).get("$ref")
            if ref:
                return ref.split("/")[-1]
        return ""

    def props_of(self, schema: str) -> dict[str, Any]:
        if schema in self.props:
            return self.props[schema]
        short = norm_schema(schema)
        full = self.canonical.get(short, "")
        return self.props.get(full, {})

    def declared(self, schema: str) -> set[str]:
        """Fields that the published API declares for that schema."""
        return set(self.props_of(schema))

    def sub_schema(self, schema: str, field: str) -> str:
        """The schema name COMPLETE of a field's value ('' if absent)."""
        pv = self.props_of(schema).get(field)
        if not pv:
            return ""
        ref = pv.get("$ref") or (pv.get("items") or {}).get("$ref")
        if ref:
            return ref.split("/")[-1]
        return ""


@dataclass
class Gap:
    """A field the backend exposes and the published API does not declare."""

    schema: str          # schema where it lives ("" if it could not be resolved)
    field: str           # field name
    route: str           # full route within the resource ("advancedSettings.enabled")
    value: Any = None    # actual value (trimmed)
    read_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = {"schema": norm_schema(self.schema) if self.schema else "",
             "field": self.field, "route": self.route}
        if self.value is not None:
            d["value"] = self.value
        if self.read_only:
            d["read_only"] = True
        return d


def _truncate(value: Any, limit: int = 200) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return value


def find_gaps(obj: Any, schema: str, idx: SchemaIndex,
              prefix: str = "") -> list[Gap]:
    """Fields of `obj` that the published API does NOT declare.

    The real JSON is traversed against the public schema: as soon as a field is
    not declared, it is noted and **no further descent** is made (its internal
    shape is unknown to the public schema)."""
    output: list[Gap] = []
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            output.extend(find_gaps(item, schema, idx, f"{prefix}[{i}]"))
        return output
    if not isinstance(obj, dict):
        return output
    declared = idx.declared(schema)
    for field, value in obj.items():
        route = f"{prefix}.{field}" if prefix else field
        if field not in declared:
            output.append(Gap(schema=schema, field=field, route=route,
                              value=_truncate(value),
                              read_only=field in DERIVED_FIELDS))
            continue
        sub = idx.sub_schema(schema, field)
        if sub:
            output.extend(find_gaps(value, sub, idx, route))
    return output


def group_gaps(gaps: Iterable[Gap]) -> list[dict[str, Any]]:
    """Group the gaps by (schema, field) and count where each one appears."""
    counts: dict[tuple[str, str], dict[str, Any]] = {}
    for g in gaps:
        key = (norm_schema(g.schema) if g.schema else "?", g.field)
        if key not in counts:
            counts[key] = {"schema": key[0], "field": g.field,
                           "example": g.route, "occurrences": 0,
                           "read_only": g.read_only}
        counts[key]["occurrences"] += 1
    return sorted(counts.values(), key=lambda d: (d["schema"], d["field"]))


def scan_collection(data: Any, idx: SchemaIndex, collection: str) -> dict[str, Any]:
    """Sweeps what a listing OR a loose resource returns, with its schema.

    MEASURED (2026-11-15): a listing returns a WRAPPER
    (`{"testCaseResults": [...]}`) whose schema differs from that of the ITEM
    (`TestCaseResult`). Sweeping the wrapper with the item schema gives false
    positives; sweeping it with the wrapper schema **only descends** to the items
    (because the array field declares its `$ref`), so a single traversal
    covers both.

    And the reverse: sweeping a LOOSE resource (a `GET {name}`) with the wrapper
    schema also gives false positives (all its fields come out "not
    declared"). That is why it **auto-detects**
    whether the response carries the wrapper's array field: if it does, it is a listing; if not, it is an item.

    Returns {"wrapper": [...], "items": [...], "n_items": n}."""
    wrapper_ref = idx.list_schema_of(collection)
    item_ref = idx.schema_of(collection)

    # Is the response a LISTING or a loose resource?
    is_list = False
    if wrapper_ref and isinstance(data, dict):
        for field, spec in idx.props_of(wrapper_ref).items():
            if (spec.get("items") or {}).get("$ref"):
                is_list = field in data
                break

    if is_list:
        gaps = find_gaps(data, wrapper_ref, idx)
        wrapper: list[Gap] = []
        items: list[Gap] = []
        for g in gaps:
            (items if g.schema and g.schema == item_ref else wrapper).append(g)
        return {"wrapper": group_gaps(wrapper), "items": group_gaps(items),
                "n_items": _count_items(data, wrapper_ref, idx),
                "wrapper_schema": norm_schema(wrapper_ref),
                "item_schema": norm_schema(item_ref)}

    # Loose resource (or an already unwrapped list of resources).
    item_list = data if isinstance(data, list) else [data]
    gaps = []
    for it in item_list:
        gaps.extend(find_gaps(it, item_ref, idx))
    return {"wrapper": [], "items": group_gaps(gaps),
            "n_items": len(item_list),
            "wrapper_schema": "", "item_schema": norm_schema(item_ref)}


def _count_items(data: Any, wrapper_ref: str, idx: SchemaIndex) -> int:
    """How many items the wrapper carries (0 if it is not a listing)."""
    if not isinstance(data, dict):
        return 0
    for _field, spec in idx.props_of(wrapper_ref).items():
        if (spec.get("items") or {}).get("$ref"):
            v = data.get(_field)
            return len(v) if isinstance(v, list) else 0
    return 0


# --- The MEASURED catalogue --------------------------------------------------
# What was verified by dint of PATCH and re-read against the internal backend.
# `status`:
#   "writable"  -> the server KEEPS it (verified by reading it back)
#   "derived"   -> the server COMPUTES it: it can be read but not written
#   "read_only" -> the backend exposes it and the public API does not even declare it
MEASURED_CATALOGUE: list[dict[str, Any]] = [
    {
        "field": "transitionRoutes[].targetPlaybook",
        "schema": "TransitionRoute",
        "resources": ["Page", "Flow", "TransitionRouteGroup"],
        "status": "writable",
        "note": "The public `target` is a `oneof` with `target_page` and "
                "`target_flow`: `target_playbook` does NOT exist in the public "
                "proto and a PATCH through the public API is dropped silently "
                "(the route is left without a destination). The console backend "
                "does keep it.",
        "tool": "cx_set_route_playbook, cx_set_flow_route_playbook, "
                "cx_set_group_route_playbook",
    },
    {
        "field": "form.parameters[].description",
        "schema": "FormParameter",
        "resources": ["Page"],
        "status": "writable",
        "note": "The published API declares `FormParameter` without "
                "`description`: the description of a form parameter can neither "
                "be set nor read through the published API.",
        "tool": "cx_set_console_field",
    },
    {
        "field": "form.parameters[].convertPhoneticAlphabets",
        "schema": "FormParameter",
        "resources": ["Page"],
        "status": "writable",
        "note": "MEASURED (2026-11-15, lab agent): the phonetic alphabet "
                "conversion flag of the form parameter is KEPT when written and "
                "read back, and it is not declared in any descriptor.",
        "tool": "cx_set_console_field",
    },
    {
        "field": "intents[].parameters[].description",
        "schema": "IntentParameter",
        "resources": ["Intent"],
        "status": "writable",
        "note": "MEASURED (2026-11-15, lab agent): the description of an intent "
                "parameter is kept when written and read back. The public proto "
                "declares `IntentParameter` without `description`.",
        "tool": "cx_set_console_field",
    },
    {
        "field": "advancedSettings.loggingSettings.disableLogging",
        "schema": "AdvancedSettingsLoggingSettings",
        "resources": ["Agent", "Page"],
        "status": "dropped",
        "note": "MEASURED (2026-11-15): when PATCHing `advancedSettings` the "
                "server EMPTIES the submessage (`loggingSettings` ends up as "
                "`{}`). It is the same behaviour as `advancedSettings.enabled`.",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "entryFulfillment.messages[].text.ssml",
        "schema": "ResponseMessageText",
        "resources": ["Page"],
        "status": "dropped",
        "note": "MEASURED (2026-11-15): the internal proto has `ssml` in "
                "`ResponseMessageText`, but when it is sent in "
                "`entryFulfillment.messages[].text` the server drops it "
                "(only `text` remains).",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "entities[].dtmfPattern",
        "schema": "EntityTypeEntity",
        "resources": ["EntityType"],
        "status": "writable",
        "note": "The DTMF pattern of an entity does not exist in the public "
                "proto. If it is written, the server keeps it (measured).",
        "tool": "cx_set_console_field",
    },
    {
        "field": "webClientSettings",
        "schema": "Agent",
        "resources": ["Agent"],
        "status": "writable",
        "note": "Console web client settings (for example `boxBorderRadius`, "
                "which is a STRING). They are not in the public proto.",
        "tool": "cx_set_console_field",
    },
    {
        "field": "actionSchemas",
        "schema": "Tool",
        "resources": ["Tool"],
        "status": "derived",
        "note": "The action schemas of the tool (up to 23 in a real tool) exist "
                "only in the console. The server DERIVES them from the "
                "`openApiSpec` or from the `dataStoreSpec`: writing the array "
                "changes nothing (measured).",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "advancedSettings.enabled / .playbackInterruptionSettings "
                 "/ .speechAdaptationSettings",
        "schema": "AdvancedSettings",
        "resources": ["Agent", "Page", "Flow"],
        "status": "derived",
        "note": "They are read, but when the message is written the server "
                "empties the unknown subfields: `enabled` disappears and "
                "`playbackInterruptionSettings` ends up as `{}` (measured).",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "speakerIdSettings",
        "schema": "Agent",
        "resources": ["Agent", "Page"],
        "status": "derived",
        "note": "The server returns it as `{}` and drops whatever is written "
                "inside it.",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "modelIdentifier / nluSettingsList / nluSettings.customConfigKey",
        "schema": "Flow",
        "resources": ["Flow"],
        "status": "derived",
        "note": "The backend derives them and regenerates them: writing "
                "`customConfigKey` returns a new key (measured).",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "trainingPhrases[].parts[].auto",
        "schema": "IntentTrainingPhrasePart",
        "resources": ["Intent"],
        "status": "derived",
        "note": "It marks the phrase as auto-generated. It is read; writing it "
                "changes nothing (measured).",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "dataStoreSpec.models / .groundingSettings",
        "schema": "ToolDataStoreTool",
        "resources": ["Tool"],
        "status": "derived",
        "note": "The summarization model of the data store tool and its "
                "grounding settings exist only in the console.",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "projectNumber",
        "schema": "Agent",
        "resources": ["Agent"],
        "status": "read_only",
        "note": "The project number the console sees. It is not in the public "
                "proto.",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "speechAdaptationSettings",
        "schema": "Playbook",
        "resources": ["Playbook"],
        "status": "derived",
        "note": "Speech adaptation settings of the playbook: only in the "
                "console.",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "etag",
        "schema": "Environment",
        "resources": ["Environment"],
        "status": "derived",
        "note": "The `etag` of the environment is computed by the server "
                "(measured in the lab agent: the `GET` returns it and the public "
                "proto of `Environment` does not declare it).",
        "tool": "cx_gaps (read)",
    },
    {
        "field": "actualValue / expectedValue / assertionType / warning",
        "schema": "TestRunDifference",
        "resources": ["TestCaseResult"],
        "status": "derived",
        "note": "The public proto of `TestRunDifference` only declares `type` "
                "and `description`: through the public API it is NOT possible "
                "to know **what** differs in a failed test case (neither the "
                "expected value, nor the obtained one, nor the warning). It "
                "only comes out when the case is run, and only if there are "
                "differences.",
        "tool": "cx_gaps (read, lab agent)",
    },
]

# The THREE RULES to create an environment (measured on 2026-11-15).
# There is no new field: the public API says that `versionConfigs` is a
# list of `{version}`, and `version` is a GENERIC resource name. What it
# does NOT say (and that is why it fails) is that you must include the ALL version of the graph.
ENVIRONMENT_RULES: list[dict[str, Any]] = [
    {
        "rule": "versionConfigs[].version",
        "what": "The version of EVERY flow of the agent (and also that of the "
                "'start flow').",
        "error": "Flow 'X' is referenced in version 'V' of flow 'F', but not "
                 "included in the environment.",
    },
    {
        "rule": "versionConfigs[].version",
        "what": "The version of EVERY playbook that the routes reference "
                "(transitive closure). The name of a playbook is "
                "'.../playbooks/{id}/versions/{v}'.",
        "error": "Playbook 'P' is referenced in version 'V' of flow 'F', but "
                 "not included in the environment.",
    },
    {
        "rule": "versionConfigs[].version",
        "what": "The version of EVERY tool that those playbooks reference. The "
                "name of a tool is '.../tools/{id}/versions/{v}'.",
        "error": "Tool 'T' is referenced in version 'V' of playbook 'P', but "
                 "not included in the environment.",
    },
]


def catalogue() -> list[dict[str, Any]]:
    """The measured catalogue, as a list of dicts (for the MCP)."""
    return [dict(c) for c in MEASURED_CATALOGUE]


# Collections that, in the INTERNAL proto, exist only nested under an
# environment. MEASURED (2026-11-15): `GET agents/{a}/sessions/{sid}/entityTypes`
# responds **404**: the internal name template is
# `projects/*/locations/*/agents/*/environments/*/sessions/*/entityTypes/*`.
# The public discovery doc declares the route at agent level, but it does not exist there.
COLLECTIONS_ONLY_IN_ENVIRONMENT = frozenset({"sessions.entityTypes"})

# Collections listed with their own method in `ConsoleApi`.
COLLECTIONS = ("intents", "entityTypes", "tools", "webhooks", "generators",
               "transitionRouteGroups", "playbooks")

# Collection -> `ConsoleApi` method that lists it.
METHOD_FOR_COLLECTION = {
    "intents": "list_intents",
    "entityTypes": "list_entity_types",
    "tools": "list_tools",
    "webhooks": "list_webhooks",
    "generators": "list_generators",
    "transitionRouteGroups": "list_transition_route_groups",
    "playbooks": "list_playbooks",
}


def load_schema_index(version: str = "v3beta1", cache_dir: Path | None = None,
                      timeout: float = 30.0) -> SchemaIndex:
    """The PUBLIC schema of the API, from the official discovery doc.

    It is downloaded once and cached on disk. **No authentication is needed**
    to read it: the discovery doc is public."""
    cache_dir = cache_dir or (Path(__file__).resolve().parent.parent / ".discovery")
    cache = Path(cache_dir) / f"{version}.json"
    if cache.exists():
        try:
            return SchemaIndex.from_doc(
                json.loads(cache.read_text(encoding="utf-8")), version=version)
        except (OSError, ValueError):
            pass
    import urllib.request
    url = DISCOVERY_URL.format(host=DISCOVERY_HOST, version=version)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        text = resp.read().decode("utf-8")
    doc = json.loads(text)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return SchemaIndex.from_doc(doc, version=version)


def resource_kind(name: str) -> str:
    """The resource type inferred from its `name` ('.../flows/{f}' -> 'flow')."""
    parts = (name or "").split("/")
    for i, p in enumerate(parts):
        if p == "agents" and i + 1 < len(parts):
            for q in parts[i + 2:]:
                if q in COLLECTIONS:
                    return q
    return ""


def listable_collections(idx: SchemaIndex) -> dict[str, str]:
    """Collection -> path suffix of `list`, as declared by the discovery doc.

    It is **computed** from the discovery doc (not a hand-written list): the
    resources are traversed and those having a `list` with `{+parent}` and hanging
    off an agent are kept. Thus, if Google adds a new collection, the sweep sees it
    without touching the code.

    Example: `flows.pages` -> `pages` (the list hangs off a flow)."""
    out: dict[str, str] = {}

    def _walk(node: dict, route: str = "") -> None:
        for name, res in (node or {}).items():
            full_route = f"{route}.{name}" if route else name
            path = ((res.get("methods") or {}).get("list") or {}).get("path") or ""
            if ".agents." in full_route and "/{+parent}/" in path:
                suffix = path.split("/{+parent}/", 1)[1].split("/")[0]
                out[full_route.split(".agents.", 1)[1]] = suffix
            _walk(res.get("resources") or {}, full_route)

    _walk(idx.raw.get("resources") or {})
    return out


def _list(cons, parent: str, suffix: str,
          page_size: str = "100") -> list[dict]:
    """`GET {parent}/{suffix}` -> the list of items (empty if there are none).

    MEASURED: the console's internal backend **caps `pageSize` at 100** in
    several endpoints ("Page size should be within [0, 100]" -> HTTP 400), while the public discovery doc says
    nothing. If the server rejects it, it is retried with fewer (50, 20)
    before giving up."""
    path = f"{parent}/{suffix}"
    if not path.startswith("/"):
        path = "/" + path
    last: Exception | None = None
    for size in (page_size, "50", "20", "5"):
        url = cons._url(path, pageSize=size)
        try:
            data = cons.request("GET", url)
        except Exception as exc:
            last = exc
            if "Page size" not in str(exc):
                raise
            continue
        if not isinstance(data, dict):
            return []
        items = data.get(suffix)
        if items is None:
            for value in data.values():
                if isinstance(value, list):
                    items = value
                    break
        return items if isinstance(items, list) else []
    raise last or RuntimeError(f"could not list {path}")


def _collection_note(key: str) -> dict[str, str]:
    """Explain why a collection cannot be reached at the agent level."""
    if key in COLLECTIONS_ONLY_IN_ENVIRONMENT:
        return {"note": "in the INTERNAL proto this collection only exists "
                        "nested under an environment "
                        "(environments/{id}/sessions/{sid}/...): at agent level "
                        "the route gives 404. It is swept in the lab agent "
                        "(environments.sessions.entityTypes).",
                "equivalent_in_environment": "environments.sessions.entityTypes"}
    return {}


def scan_agent(cons, agent_id: str, project: str = "",
               idx: SchemaIndex | None = None,
               include_pages: bool = True,
               max_resources: int = 25,
               max_parents: int = 3) -> dict[str, Any]:
    """Walk an agent and return the gaps it sees, LIVE.

    It uses no hand-written list: it compares the JSON returned by the
    backend with the public v3beta1 schema, resource by resource. Thus, if Google
    adds a new field to the console, the MCP detects it without touching the code.

    The walk is **generic**: it lists ALL the collections the
    descriptor declares under an agent (`listable_collections()`), including
    the nested ones (`flows.pages`, `tools.versions`, `playbooks.examples`,
    `environments.deployments`, `testCases.results`...). For the nested ones the ancestor must be
    resolved (`flows.pages` needs a flow): the first one that exists is used and
    where it comes from is noted.

    Returns:
        {"agent":..., "resources": {type: n}, "gaps": [...], "by_resource": {...}}"""
    idx = idx or load_schema_index(cons.cfg.api_version)
    project = project or cons.cfg.project
    base = f"/projects/{project}/locations/{cons.cfg.location}"
    agent_name = f"{base}/agents/{agent_id}"
    gaps: list[Gap] = []
    resources: dict[str, int] = {}
    by_resource: list[dict[str, Any]] = []
    unreachable: list[dict[str, str]] = []
    scanned: list[str] = []      # collections with SOMETHING inside
    reachable: list[str] = []      # collections that could be listed

    def _annotate(kind: str, name: str, obj: Any, collection: str) -> None:
        nonlocal gaps
        if collection not in scanned:
            scanned.append(collection)
        resources[kind] = resources.get(kind, 0) + 1
        schema = idx.schema_of(collection)
        g = find_gaps(obj, schema, idx)
        if g:
            by_resource.append({"type": kind, "resource": name,
                                "schema": norm_schema(schema), "gaps": len(g)})
        gaps.extend(g)

    # --- agent ---
    ag = cons.request("GET", cons._url(f"{agent_name}"))
    _annotate("agent", agent_id, ag, "agents")

    # --- ALL the discovery doc's collections ---
    for key, suffix in sorted(listable_collections(idx).items()):
        if key == "flows.pages" and not include_pages:
            continue
        # The ancestor is resolved (e.g. "flows" for "flows.pages"). SEVERAL candidates are
        # stored (up to `max_parents`): if the first has no pages, the second may.
        parents: list[str] = [agent_name]
        failure: str | None = None
        for segment in key.split(".")[:-1]:
            found: list[str] = []
            for p in parents:
                try:
                    items = _list(cons, p, segment)
                except Exception as exc:
                    failure = str(exc)[:200]
                    break
                found.extend(str(i.get("name")) for i in items
                             if i.get("name"))
            if failure:
                break
            if not found:
                failure = (f"there is no '{segment}' to use as an ancestor")
                break
            parents = found[:max_parents]
        if failure:
            unreachable.append({"collection": key, "reason": failure,
                                **_collection_note(key)})
            continue
        for parent in parents:
            try:
                items = _list(cons, parent, suffix)
            except Exception as exc:
                unreachable.append({"collection": key, "reason": str(exc)[:200],
                                    **_collection_note(key)})
                continue
            if key not in reachable:
                reachable.append(key)
            for item in items[:max_resources]:
                label = (item.get("displayName") or item.get("name") or "")
                _annotate(key, label, item, key)

    return {
        "agent": agent_id,
        "project": project,
        "scanned_resources": resources,
        "collections": len(listable_collections(idx)),
        "collections_scanned": sorted(scanned),
        "reachable_collections": sorted(reachable),
        "unreachable": unreachable,
        "gaps": group_gaps(gaps),
        "by_resource": by_resource,
    }
