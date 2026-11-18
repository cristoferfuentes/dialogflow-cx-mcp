"""RULES OF BUSINESS of the CREATION of playbooks in Dialogflow CX (measured).

This module is the **offline** part (no network) of playbook creation: it builds
the POST body, scans the text macros and decides what to do with
self-reference. Everything here is MEASURED (see `cxp/limits.py` and
lessons `2026-11-04d/e/g` and `2026-11-08` of AGENTS.md); nothing is inferred from the
official discovery doc, which at this point lies by omission.

THE RULES (and their exact symptom)
--------------------------------

R1  `displayName` and `goal` are mandatory. Without `goal` the API responds
    *"Playbook goal cannot be empty"*. The backend **revalidates the whole
    playbook** on every write: you have to send the complete body.

R2  The backend scans the text macros **only in `goal` and in
    `instruction.steps`** (measured: a macro in `goal` -> HTTP 400
    *"Referenced resource X does not exist under <agent>"*).
    **`instruction.guidelines` is NOT scanned** (measured): it is the only
    place where a self-referencing macro can live.

R3  `${TOOL:x}` **requires** an explicit `referencedTools` in the body.
    Without the binding: *"Tool X used in goal or steps are not listed in the referenced
    tools."*. The ids go as full resource name:
    `projects/{p}/locations/{l}/agents/{a}/tools/{id}`.

R4  `${FLOW:x}` and `${PLAYBOOK:x}` are **derived** by the backend: `referencedFlows`
    and `referencedPlaybooks` are `readOnly: true` in the discovery doc -> sending them
    saves nothing. Do not send them.

R5  The macro is resolved **by displayName** and the target must already exist
    *under the agent* -> playbooks are created in **topological order** (first
    the ones others reference).

R6  **Self-reference** (`${PLAYBOOK:<itself>}`) is IMPOSSIBLE by API:
    the loop check runs ALWAYS, even with the leaf field mask
    `updateMask=instruction.steps` (measured: HTTP 400 *"Playbook X
    caused loop in playbook routes."*). Workaround policies:
      * `guidelines` -> the macro moves to `instruction.guidelines` (only
        field not scanned): the text is preserved VERBATIM, but the macro stops
        being in `steps` (its functional position) and the
        self-reference is not recorded in `referencedPlaybooks` (readOnly).
      * `inert` -> the macro is neutralized in place (`${PLAYBOOK:x}` ->
        `{{PLAYBOOK:x}}`): keeps the position, loses the expansion.
      * `error` -> not created and both options are explained.

R7  **`playbooks:import` NEVER.** Creates a ghost resource without id
    (`name = ".../playbooks/"`) that the runtime resolves by prefix and breaks
    ALL the playbook invocations of the agent:
    *"Playbook '' (ID: ) is missing a goal"*.

R8  **POST != PATCH.** When creating (POST) the macros validate well if the resource
    already exists. When updating (PATCH) the backend revalidates the whole playbook
    when the mask touches a top level field (`goal`, `instruction`) and
    fails with *"Flow X does not exist under the agent"* (CX bug, the flows
    DO exist). With a **leaf field** mask (`instruction.steps`,
    `instruction.guidelines`) it does not revalidate references -> PATCH clean, in
    place and keeping the id.

R9  **PATCH without mask = omitted fields are DELETE** (measured): `codeBlock`,
    `llmModelSettings` and `inlineActions` are dropped. Never without mask.

R10 `playbookType` is writable (`TASK` | `ROUTINE`). A macro
    `${PLAYBOOK:X}` pointing to a ROUTINE-type playbook makes the
    POST fail: *"X is a workflow playbook, which is not supported for task
    playbooks."*

Fields that are NEVER written (they are `readOnly` or derived by the backend):
`name`, `createTime`, `updateTime`, `tokenCount`, `inlineActions`,
`referencedFlows`, `referencedPlaybooks`."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

# Resource macro: ${PLAYBOOK:name} | ${FLOW:name} | ${TOOL:name}
MACRO_RE = re.compile(r"\$\{(PLAYBOOK|FLOW|TOOL):([^{}]+)\}")

# Body fields that the backend SCANS (measured).
SCANNED_FIELDS = ("goal", "steps")
# Fields the backend does NOT scan (measured): the escape hatch.
UNSCANNED_FIELDS = ("guidelines",)
# Unknown status field: scanned as a precaution.
UNMEASURED_FIELDS = ("codeBlock",)

# Policies for self-reference.
SELF_REF_POLICIES = ("error", "guidelines", "inert")


class BusinessRuleError(ValueError):
    """The request does not satisfy a CX business rule.

    The message is what the API would return (or its cause), so that the agent
    reading it knows what to do without spending a write to find out."""


@dataclass(frozen=True)
class Macro:
    """A resource macro found in the text of a playbook."""

    kind: str      # PLAYBOOK | FLOW | TOOL
    ref: str       # displayName (or id) referenced
    field: str     # goal | steps | guidelines | codeBlock

    @property
    def scanned(self) -> bool:
        """Does the backend validate it on write?"""
        return self.field in SCANNED_FIELDS or self.field in UNMEASURED_FIELDS

    @property
    def token(self) -> str:
        return f"${{{self.kind}:{self.ref}}}"


# --------------------------------------------------------------------- macros
def _macros_in_text(text: str, field: str) -> list[Macro]:
    return [Macro(kind=m.group(1), ref=m.group(2).strip(), field=field)
            for m in MACRO_RE.finditer(text or "")]


def scan_macros(goal: str = "", guidelines: str = "", steps: Iterable[Any] | None = None,
                 code_block: Any = "") -> list[Macro]:
    """All playbook macros, with the field where each one is.

    All three fields are scanned (the scanned ones, the one that is not and the unmeasured one)
    because the report has to be COMPLETE: even if `guidelines` does not
    validate it in the backend, the caller has to know there is a macro there."""
    out: list[Macro] = []
    out += _macros_in_text(goal or "", "goal")
    out += _macros_in_text(guidelines or "", "guidelines")
    for step in (steps or []):
        text = step.get("text", "") if isinstance(step, dict) else str(step)
        out += _macros_in_text(text, "steps")
    if isinstance(code_block, dict):
        out += _macros_in_text(code_block.get("code", ""), "codeBlock")
    elif code_block:
        out += _macros_in_text(str(code_block), "codeBlock")
    return out


def macros_of_type(macros: Iterable[Macro], kind: str) -> list[Macro]:
    return [m for m in macros if m.kind == kind]


def self_referencing_macro(macros: Iterable[Macro], display_name: str) -> Macro | None:
    """The `${PLAYBOOK:<display_name>}` macro that points to itself."""
    for m in macros:
        if m.kind == "PLAYBOOK" and m.ref == display_name:
            return m
    return None


# ---------------------------------------------------------------------- body
def normalize_steps(steps: Iterable[Any] | None) -> list[dict[str, Any]]:
    """`["text", ...]` -> `[{"text": "text"}, ...]` (what the API expects)."""
    out: list[dict[str, Any]] = []
    for step in (steps or []):
        if isinstance(step, dict):
            out.append(step)
        elif isinstance(step, str):
            out.append({"text": step})
        else:
            raise BusinessRuleError(
                f"invalid playbook step: {step!r} (use a str or a dict)")
    return out


def build_body(display_name: str, goal: str, *,
                     guidelines: str = "", steps: Iterable[Any] | None = None,
                     code_block: Any = None, llm_model_settings: dict | None = None,
                     playbook_type: str = "", referenced_tools: Iterable[str] | None = None,
                     input_parameter_definitions: list | None = None,
                     output_parameter_definitions: list | None = None) -> dict[str, Any]:
    """Build the body of a playbook's POST (respect R1, R3, R4).

    Never include `referencedFlows` or `referencedPlaybooks`: they are read-only
    and the backend derives them from the macros (sending them saves nothing)."""
    display_name = (display_name or "").strip()
    goal = (goal or "").strip()
    if not display_name:
        raise BusinessRuleError(
            "missing 'displayName': a playbook without a name cannot be addressed")
    if not goal:
        raise BusinessRuleError(
            "missing 'goal': the API answers 'Playbook goal cannot be empty'")
    body: dict[str, Any] = {
        "displayName": display_name,
        "goal": goal,
        "instruction": {
            "guidelines": guidelines or "",
            "steps": normalize_steps(steps),
        },
    }
    if code_block:
        body["codeBlock"] = code_block
    if llm_model_settings:
        body["llmModelSettings"] = llm_model_settings
    if playbook_type:
        body["playbookType"] = playbook_type
    if referenced_tools:
        body["referencedTools"] = list(referenced_tools)
    if input_parameter_definitions:
        body["inputParameterDefinitions"] = list(input_parameter_definitions)
    if output_parameter_definitions:
        body["outputParameterDefinitions"] = list(output_parameter_definitions)
    return body


# ------------------------------------------------------- self-reference policy
def _text_without_macro(text: str, macro: Macro) -> str:
    """Remove the macro from the text (leave the rest intact)."""
    return MACRO_RE.sub(
        lambda m: "" if (m.group(1) == macro.kind and m.group(2).strip() == macro.ref)
        else m.group(0), text or "")


def _inert_text(text: str, macro: Macro) -> str:
    """Neutralize the macro: `${PLAYBOOK:x}` -> `{{PLAYBOOK:x}}` (inert token)."""
    return MACRO_RE.sub(
        lambda m: ("{{PLAYBOOK:" + m.group(2).strip() + "}}")
        if (m.group(1) == macro.kind and m.group(2).strip() == macro.ref)
        else m.group(0), text or "")


def apply_self_ref_policy(body: dict[str, Any], macros: list[Macro],
                          display_name: str,
                          policy: str = "error") -> tuple[dict[str, Any], list[str]]:
    """Apply the self-reference policy. Return (body, notes).

    `policy`:
      * `error`      -> raises `BusinessRuleError` explaining the two outputs.
      * `guidelines` -> the macro moves to `instruction.guidelines` (the only
        field the backend does not scan): the text is preserved verbatim.
      * `inert`      -> the macro is neutralized in place (inert token)."""
    if policy not in SELF_REF_POLICIES:
        raise BusinessRuleError(
            f"unknown self-reference policy: {policy!r} "
            f"(one of: {', '.join(SELF_REF_POLICIES)})")
    own = self_referencing_macro(macros, display_name)
    if own is None:
        return body, []

    if policy == "error":
        raise BusinessRuleError(
            f"the playbook references itself ({own.token} in "
            f"'{own.field}'): the API rejects it with 'caused loop in "
            "playbook routes' (that check ALWAYS runs, also "
            "with updateMask=instruction.steps). Options: "
            "self_reference='guidelines' (the macro moves to "
            "instruction.guidelines, the only field that is not scanned: the "
            "text is preserved but it stops being in its functional position) "
            "or self_reference='inert' (it is neutralized to {{PLAYBOOK:x}}, "
            "the position is preserved but the macro stops expanding). In "
            "no case can the self-reference be registered in "
            "referencedPlaybooks: that field is read-only.")

    new_body = dict(body)
    instr = dict(new_body.get("instruction") or {})
    notes: list[str] = []

    if policy == "inert":
        if own.field == "goal":
            new_body["goal"] = _inert_text(new_body.get("goal", ""), own)
        elif own.field == "steps":
            new_body["instruction"] = instr
            instr["steps"] = [_inert_text(p.get("text", ""), own)
                              if isinstance(p, dict) else _inert_text(str(p), own)
                              for p in (instr.get("steps") or [])]
        elif own.field == "guidelines":
            instr["guidelines"] = _inert_text(instr.get("guidelines", ""), own)
            new_body["instruction"] = instr
        elif own.field == "codeBlock":
            cb = dict(new_body.get("codeBlock") or {})
            cb["code"] = _inert_text(cb.get("code", ""), own)
            new_body["codeBlock"] = cb
        notes.append(
            f"self-reference neutralized in '{own.field}' ({own.token} -> "
            "{{PLAYBOOK:x}}): the text keeps its position but the macro stops "
            "expanding at runtime")
        return new_body, notes

    # policy == "guidelines"
    moved = own.token
    if own.field == "goal":
        new_body["goal"] = _text_without_macro(new_body.get("goal", ""), own)
    elif own.field == "steps":
        instr["steps"] = [_text_without_macro(p.get("text", ""), own)
                          if isinstance(p, dict) else _text_without_macro(str(p), own)
                          for p in (instr.get("steps") or [])]
    elif own.field == "codeBlock":
        cb = dict(new_body.get("codeBlock") or {})
        cb["code"] = _text_without_macro(cb.get("code", ""), own)
        new_body["codeBlock"] = cb
    # `guidelines` is not scanned: the macro can stay there as is.
    guide = instr.get("guidelines") or ""
    if own.field == "guidelines":
        pass                      # it was already there: there is nothing to move
    else:
        guide = (guide + "\n" + moved).strip() if guide else moved
    instr["guidelines"] = guide
    new_body["instruction"] = instr
    notes.append(
        f"self-reference moved from '{own.field}' to 'instruction.guidelines' "
        f"({moved} preserved verbatim): it is the only field the backend does "
        "not scan, so the POST does not see the loop. Deviations: the macro "
        "stops being in its functional position (steps) and the self-reference "
        "is not registered in referencedPlaybooks (readOnly)")
    return new_body, notes


# ------------------------------------------------------------------ resolution
class Resolver:
    """Resolve `displayName` -> agent resource ids (with cache).

    It is the only piece that needs network; it is injected so the rest of the
    module is testable offline."""

    def __init__(self, playbook: Callable[[str], str | None],
                 flow: Callable[[str], str | None],
                 tool: Callable[[str], str | None],
                 resource_name_fn: Callable[[str, str], str]):
        self._playbook = playbook
        self._flow = flow
        self._tool = tool
        self._resource_name_fn = resource_name_fn

    def resolver(self, kind: str, ref: str) -> str | None:
        if kind == "PLAYBOOK":
            return self._playbook(ref)
        if kind == "FLOW":
            return self._flow(ref)
        if kind == "TOOL":
            return self._tool(ref)
        return None

    def name(self, kind: str, rid: str) -> str:
        return self._resource_name_fn(kind, rid)


def preflight(body: dict[str, Any], macros: list[Macro], res: Resolver,
              display_name: str, referenced_tools: list[str] | None = None,
              policy: str = "error") -> dict[str, Any]:
    """Check the BEFORE rules for spending the write.

    Return the final body + the report. Raise `BusinessRuleError` with the
    message the API would return if something does not add up (so the error arrives with
    cause and not as an unexplained HTTP 400)."""
    instr = body.get("instruction") or {}
    macros = macros or scan_macros(
        body.get("goal", ""), instr.get("guidelines", ""),
        instr.get("steps"), body.get("codeBlock"))

    macro_report: list[dict[str, Any]] = []
    missing: list[str] = []
    # referencedTools: explicit + those required by the ${TOOL:...} macros
    explicit_tools = list(referenced_tools or [])
    tools_by_macro: list[str] = []

    for m in macros:
        row: dict[str, Any] = {
            "type": m.kind, "reference": m.ref, "field": m.field,
            "scanned": m.scanned, "macro": m.token,
        }
        if m.kind == "PLAYBOOK" and m.ref == display_name:
            row["status"] = "self_reference"
            row["id"] = None
            macro_report.append(row)
            continue
        if not m.scanned:
            row["status"] = "not validated by the API (field not scanned)"
            macro_report.append(row)
            continue
        rid = res.resolver(m.kind, m.ref)
        row["id"] = rid
        if rid is None:
            row["status"] = "DOES NOT EXIST under the agent"
            missing.append(m.token)
        else:
            row["status"] = "ok"
            if m.kind == "TOOL":
                tools_by_macro.append(res.name("TOOL", rid))
        macro_report.append(row)

    if missing:
        # The backend responds with the resource name, not with the token.
        detail = ", ".join(sorted({f.split(":", 1)[1] for f in missing}))
        raise BusinessRuleError(
            f"references that do not exist under the agent: {detail} "
            "(the backend answers 'Referenced resource X does not exist under "
            "<agent>'). The playbooks are created in TOPOLOGICAL ORDER: first "
            "the ones others reference.")

    # R3: every ${TOOL:x} macro needs its explicit binding.
    refs_tool = [m.ref for m in macros
                 if m.kind == "TOOL" and m.scanned and m.ref != display_name]
    bindings = set(explicit_tools)
    for tool_name in tools_by_macro:
        bindings.add(tool_name)
    without_binding = [r for r in refs_tool
                       if not any(tool_name.endswith("/tools/" + r) or
                                  tool_name == r or tool_name.endswith("/" + r)
                                  for tool_name in bindings)]
    if without_binding:
        raise BusinessRuleError(
            f"TOOL macro(s) without 'referencedTools': {', '.join(without_binding)}. "
            "The backend answers 'Tool X used in goal or steps are not listed "
            "in the referenced tools.' The automatic binding is added, or "
            "'referenced_tools' is passed with the full name of the resource.")

    final_body, notes = apply_self_ref_policy(body, macros, display_name, policy)
    if bindings:
        final_body["referencedTools"] = sorted(bindings)

    return {
        "body": final_body,
        "report": {
            "macros": macro_report,
            "referenced_tools": sorted(bindings),
            "self_reference": self_referencing_macro(macros, display_name) is not None,
            "notes": notes,
        },
    }
