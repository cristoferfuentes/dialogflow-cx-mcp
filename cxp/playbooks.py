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
        auto-arista is not recorded in `referencedPlaybooks` (readOnly).
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

# Resource macro: ${PLAYBOOK:nombre} | ${FLOW:nombre} | ${TOOL:nombre}
MACRO_RE = re.compile(r"\$\{(PLAYBOOK|FLOW|TOOL):([^{}]+)\}")

# Body fields that the backend SCANS (measured).
CAMPOS_ESCANEADOS = ("goal", "steps")
# Fields the backend does NOT scan (measured): the escape hatch.
CAMPOS_NO_ESCANEADOS = ("guidelines",)
# Unknown status field: scanned as a precaution.
CAMPOS_NO_MEDIDOS = ("codeBlock",)

# Policies for self-reference.
POLITICAS_AUTOREF = ("error", "guidelines", "inert")


class ReglaNegocioError(ValueError):
    """The request does not satisfy a CX business rule.

    The message is what the API would return (or its cause), so that the agent
    reading it knows what to do without spending a write to find out."""


@dataclass(frozen=True)
class Macro:
    """A resource macro found in the text of a playbook."""

    kind: str      # PLAYBOOK | FLOW | TOOL
    ref: str       # displayName (or id) referenced
    campo: str     # goal | steps | guidelines | codeBlock

    @property
    def escaneada(self) -> bool:
        """Does the backend validate it on write?"""
        return self.campo in CAMPOS_ESCANEADOS or self.campo in CAMPOS_NO_MEDIDOS

    @property
    def token(self) -> str:
        return f"${{{self.kind}:{self.ref}}}"


# --------------------------------------------------------------------- macros
def _macros_en_texto(texto: str, campo: str) -> list[Macro]:
    return [Macro(kind=m.group(1), ref=m.group(2).strip(), campo=campo)
            for m in MACRO_RE.finditer(texto or "")]


def escanear_macros(goal: str = "", guidelines: str = "", steps: Iterable[Any] | None = None,
                     code_block: Any = "") -> list[Macro]:
    """All playbook macros, with the field where each one is.

    All three fields are scanned (the scanned ones, the one that is not and the unmeasured one)
    because the report has to be COMPLETE: even if `guidelines` does not
    validate it in the backend, the caller has to know there is a macro there."""
    out: list[Macro] = []
    out += _macros_en_texto(goal or "", "goal")
    out += _macros_en_texto(guidelines or "", "guidelines")
    for paso in (steps or []):
        texto = paso.get("text", "") if isinstance(paso, dict) else str(paso)
        out += _macros_en_texto(texto, "steps")
    if isinstance(code_block, dict):
        out += _macros_en_texto(code_block.get("code", ""), "codeBlock")
    elif code_block:
        out += _macros_en_texto(str(code_block), "codeBlock")
    return out


def macros_de_tipo(macros: Iterable[Macro], kind: str) -> list[Macro]:
    return [m for m in macros if m.kind == kind]


def macro_autoreferente(macros: Iterable[Macro], display_name: str) -> Macro | None:
    """The `${PLAYBOOK:<display_name>}` macro that points to itself."""
    for m in macros:
        if m.kind == "PLAYBOOK" and m.ref == display_name:
            return m
    return None


# ---------------------------------------------------------------------- body
def normalizar_steps(steps: Iterable[Any] | None) -> list[dict[str, Any]]:
    """`["texto", ...]` -> `[{"text": "texto"}, ...]` (what the API expects)."""
    out: list[dict[str, Any]] = []
    for paso in (steps or []):
        if isinstance(paso, dict):
            out.append(paso)
        elif isinstance(paso, str):
            out.append({"text": paso})
        else:
            raise ReglaNegocioError(
                f"paso de playbook invalido: {paso!r} (usa un str o un dict)")
    return out


def construir_cuerpo(display_name: str, goal: str, *,
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
        raise ReglaNegocioError(
            "falta 'displayName': un playbook sin nombre no se puede direccionar")
    if not goal:
        raise ReglaNegocioError(
            "falta 'goal': la API responde 'Playbook goal cannot be empty'")
    body: dict[str, Any] = {
        "displayName": display_name,
        "goal": goal,
        "instruction": {
            "guidelines": guidelines or "",
            "steps": normalizar_steps(steps),
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
def _texto_sin_macro(texto: str, macro: Macro) -> str:
    """Remove the macro from the text (leave the rest intact)."""
    return MACRO_RE.sub(
        lambda m: "" if (m.group(1) == macro.kind and m.group(2).strip() == macro.ref)
        else m.group(0), texto or "")


def _texto_inerte(texto: str, macro: Macro) -> str:
    """Neutralize the macro: `${PLAYBOOK:x}` -> `{{PLAYBOOK:x}}` (inert token)."""
    return MACRO_RE.sub(
        lambda m: ("{{PLAYBOOK:" + m.group(2).strip() + "}}")
        if (m.group(1) == macro.kind and m.group(2).strip() == macro.ref)
        else m.group(0), texto or "")


def aplicar_politica_autorref(body: dict[str, Any], macros: list[Macro],
                              display_name: str,
                              politica: str = "error") -> tuple[dict[str, Any], list[str]]:
    """Apply the self-reference policy. Return (body, notes).

    `politica`:
      * `error`      -> raises `ReglaNegocioError` explaining the two outputs.
      * `guidelines` -> the macro moves to `instruction.guidelines` (the only
        field the backend does not scan): the text is preserved verbatim.
      * `inert`      -> the macro is neutralized in place (inert token)."""
    if politica not in POLITICAS_AUTOREF:
        raise ReglaNegocioError(
            f"politica de autorreferencia desconocida: {politica!r} "
            f"(una de: {', '.join(POLITICAS_AUTOREF)})")
    propia = macro_autoreferente(macros, display_name)
    if propia is None:
        return body, []

    if politica == "error":
        raise ReglaNegocioError(
            f"el playbook se referencia a si mismo ({propia.token} en "
            f"'{propia.campo}'): la API lo rechaza con 'caused loop in "
            "playbook routes' (esa comprobacion se ejecuta SIEMPRE, tambien "
            "con updateMask=instruction.steps). Salidas: "
            "self_reference='guidelines' (la macro se mueve a "
            "instruction.guidelines, el unico campo que no se escanea: el "
            "texto se conserva pero deja de estar en su posicion funcional) "
            "o self_reference='inert' (se neutraliza a {{PLAYBOOK:x}}, se "
            "conserva la posicion pero la macro deja de expandirse). En "
            "ningun caso se puede registrar la auto-arista en "
            "referencedPlaybooks: ese campo es de solo lectura.")

    nuevo = dict(body)
    instr = dict(nuevo.get("instruction") or {})
    notas: list[str] = []

    if politica == "inert":
        if propia.campo == "goal":
            nuevo["goal"] = _texto_inerte(nuevo.get("goal", ""), propia)
        elif propia.campo == "steps":
            nuevo["instruction"] = instr
            instr["steps"] = [_texto_inerte(p.get("text", ""), propia)
                              if isinstance(p, dict) else _texto_inerte(str(p), propia)
                              for p in (instr.get("steps") or [])]
        elif propia.campo == "guidelines":
            instr["guidelines"] = _texto_inerte(instr.get("guidelines", ""), propia)
            nuevo["instruction"] = instr
        elif propia.campo == "codeBlock":
            cb = dict(nuevo.get("codeBlock") or {})
            cb["code"] = _texto_inerte(cb.get("code", ""), propia)
            nuevo["codeBlock"] = cb
        notas.append(
            f"autorreferencia neutralizada en '{propia.campo}' ({propia.token} -> "
            "{{PLAYBOOK:x}}): el texto conserva la posicion pero la macro deja "
            "de expandirse en runtime")
        return nuevo, notas

    # policy == "guidelines"
    movida = propia.token
    if propia.campo == "goal":
        nuevo["goal"] = _texto_sin_macro(nuevo.get("goal", ""), propia)
    elif propia.campo == "steps":
        instr["steps"] = [_texto_sin_macro(p.get("text", ""), propia)
                          if isinstance(p, dict) else _texto_sin_macro(str(p), propia)
                          for p in (instr.get("steps") or [])]
    elif propia.campo == "codeBlock":
        cb = dict(nuevo.get("codeBlock") or {})
        cb["code"] = _texto_sin_macro(cb.get("code", ""), propia)
        nuevo["codeBlock"] = cb
    # `guidelines` is not scanned: the macro can stay there as is.
    guia = instr.get("guidelines") or ""
    if propia.campo == "guidelines":
        pass                      # it was already there: there is nothing to move
    else:
        guia = (guia + "\n" + movida).strip() if guia else movida
    instr["guidelines"] = guia
    nuevo["instruction"] = instr
    notas.append(
        f"autorreferencia movida de '{propia.campo}' a 'instruction.guidelines' "
        f"({movida} conservado verbatim): es el unico campo que el backend no "
        "escanea, asi que el POST no ve el bucle. Desviaciones: la macro deja "
        "de estar en su posicion funcional (steps) y la auto-arista no se "
        "registra en referencedPlaybooks (readOnly)")
    return nuevo, notas


# ------------------------------------------------------------------ resolution
class Resolvedor:
    """Resolve `displayName` -> agent resource ids (with cache).

    It is the only piece that needs network; it is injected so the rest of the
    module is testable offline."""

    def __init__(self, playbook: Callable[[str], str | None],
                 flow: Callable[[str], str | None],
                 tool: Callable[[str], str | None],
                 nombre_recurso: Callable[[str, str], str]):
        self._playbook = playbook
        self._flow = flow
        self._tool = tool
        self._nombre_recurso = nombre_recurso

    def resolver(self, kind: str, ref: str) -> str | None:
        if kind == "PLAYBOOK":
            return self._playbook(ref)
        if kind == "FLOW":
            return self._flow(ref)
        if kind == "TOOL":
            return self._tool(ref)
        return None

    def nombre(self, kind: str, rid: str) -> str:
        return self._nombre_recurso(kind, rid)


def preflight(body: dict[str, Any], macros: list[Macro], res: Resolvedor,
              display_name: str, referenced_tools: list[str] | None = None,
              politica: str = "error") -> dict[str, Any]:
    """Check the BEFORE rules for spending the write.

    Return the final body + the report. Raise `ReglaNegocioError` with the
    message the API would return if something does not add up (so the error arrives with
    cause and not as an unexplained HTTP 400)."""
    instr = body.get("instruction") or {}
    macros = macros or escanear_macros(
        body.get("goal", ""), instr.get("guidelines", ""),
        instr.get("steps"), body.get("codeBlock"))

    informe_macros: list[dict[str, Any]] = []
    faltan: list[str] = []
    # referencedTools: explicit + those required by the ${TOOL:...} macros
    tools_explicitos = list(referenced_tools or [])
    tools_por_macro: list[str] = []

    for m in macros:
        fila: dict[str, Any] = {
            "tipo": m.kind, "referencia": m.ref, "campo": m.campo,
            "escaneada": m.escaneada, "macro": m.token,
        }
        if m.kind == "PLAYBOOK" and m.ref == display_name:
            fila["estado"] = "autorreferencia"
            fila["id"] = None
            informe_macros.append(fila)
            continue
        if not m.escaneada:
            fila["estado"] = "no validada por la API (campo no escaneado)"
            informe_macros.append(fila)
            continue
        rid = res.resolver(m.kind, m.ref)
        fila["id"] = rid
        if rid is None:
            fila["estado"] = "NO EXISTE under the agent"
            faltan.append(m.token)
        else:
            fila["estado"] = "ok"
            if m.kind == "TOOL":
                tools_por_macro.append(res.nombre("TOOL", rid))
        informe_macros.append(fila)

    if faltan:
        # The backend responds with the resource name, not with the token.
        detalle = ", ".join(sorted({f.split(":", 1)[1] for f in faltan}))
        raise ReglaNegocioError(
            f"referencias que no existen under the agent: {detalle} "
            "(el backend responde 'Referenced resource X does not exist under "
            "<agente>'). Los playbooks se crean en ORDEN TOPOLOGICO: primero "
            "los que otros referencian.")

    # R3: every ${TOOL:x} macro needs its explicit binding.
    refs_tool = [m.ref for m in macros
                 if m.kind == "TOOL" and m.escaneada and m.ref != display_name]
    bindings = set(tools_explicitos)
    for nombre_tool in tools_por_macro:
        bindings.add(nombre_tool)
    sin_binding = [r for r in refs_tool
                   if not any(nombre_tool.endswith("/tools/" + r) or
                              nombre_tool == r or nombre_tool.endswith("/" + r)
                              for nombre_tool in bindings)]
    if sin_binding:
        raise ReglaNegocioError(
            f"macro(s) de TOOL sin 'referencedTools': {', '.join(sin_binding)}. "
            "El backend responde 'Tool X used in goal or steps are not listed "
            "in the referenced tools.' Se anade el binding automatico o se "
            "pasa 'referenced_tools' con el nombre completo del recurso.")

    cuerpo, notas = aplicar_politica_autorref(body, macros, display_name, politica)
    if bindings:
        cuerpo["referencedTools"] = sorted(bindings)

    return {
        "cuerpo": cuerpo,
        "informe": {
            "macros": informe_macros,
            "referenced_tools": sorted(bindings),
            "autorreferencia": macro_autoreferente(macros, display_name) is not None,
            "notas": notas,
        },
    }
