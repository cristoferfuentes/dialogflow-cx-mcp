"""REGLAS DE NEGOCIO de la CREACION de playbooks en Dialogflow CX (medidas).

Este modulo es la parte **offline** (sin red) de la creacion de playbooks: arma
el cuerpo del POST, escanea las macros del texto y decide que hacer con la
autorreferencia. Todo lo que hay aqui esta MEDIDO (ver `cxp/limits.py` y las
lecciones `2026-11-04d/e/g` y `2026-11-08` de AGENTS.md); nada se deduce del
descriptor oficial, que en este punto miente por omision.

LAS REGLAS (y su sintoma exacto)
--------------------------------

R1  `displayName` y `goal` son obligatorios. Sin `goal` la API responde
    *"Playbook goal cannot be empty"*. El backend **revalida el playbook
    entero** en cada escritura: hay que mandar el cuerpo completo.

R2  El backend escanea las macros del texto **solo en `goal` y en
    `instruction.steps`** (medido: macro en `goal` -> HTTP 400
    *"Referenced resource X does not exist under <agente>"*).
    **`instruction.guidelines` NO se escanea** (medido): es el unico sitio
    donde puede vivir una macro autorreferente.

R3  `${TOOL:x}` **exige** `referencedTools` explicito en el cuerpo. Sin el
    binding: *"Tool X used in goal or steps are not listed in the referenced
    tools."*. Los ids van como nombre completo de recurso:
    `projects/{p}/locations/{l}/agents/{a}/tools/{id}`.

R4  `${FLOW:x}` y `${PLAYBOOK:x}` los **deriva** el backend: `referencedFlows`
    y `referencedPlaybooks` son `readOnly: true` en el descriptor -> mandarlos
    no guarda nada. No mandarlos.

R5  La macro se resuelve **por displayName** y el destino tiene que existir ya
    *under the agent* -> los playbooks se crean en **orden topologico** (primero
    los que otros referencian).

R6  La **autorreferencia** (`${PLAYBOOK:<si mismo>}`) es IMPOSIBLE por API:
    la comprobacion de bucles se ejecuta SIEMPRE, incluso con la mascara de
    campo hoja `updateMask=instruction.steps` (medido: HTTP 400 *"Playbook X
    caused loop in playbook routes."*). Politicas de esquivo:
      * `guidelines` -> la macro se mueve a `instruction.guidelines` (unico
        campo no escaneado): el texto se conserva VERBATIM, pero la macro deja
        de estar en `steps` (su posicion funcional) y no se registra la
        auto-arista en `referencedPlaybooks` (readOnly).
      * `inert` -> la macro se neutraliza en su sitio (`${PLAYBOOK:x}` ->
        `{{PLAYBOOK:x}}`): conserva la posicion, pierde la expansion.
      * `error` -> no se crea y se explican las dos opciones.

R7  **`playbooks:import` NUNCA.** Crea un recurso fantasma sin id
    (`name = ".../playbooks/"`) que el runtime resuelve por prefijo y rompe
    TODAS las invocaciones de playbook del agente:
    *"Playbook '' (ID: ) is missing a goal"*.

R8  **POST != PATCH.** Al crear (POST) las macros validan bien si el recurso
    ya existe. Al actualizar (PATCH) el backend revalida el playbook entero
    cuando la mascara toca un campo de nivel superior (`goal`, `instruction`) y
    falla con *"Flow X does not exist under the agent"* (bug de CX, los flows
    SI existen). Con mascara de **campo hoja** (`instruction.steps`,
    `instruction.guidelines`) no revalida las referencias -> PATCH limpio, en
    sitio y conservando el id.

R9  **PATCH sin mascara = los campos omitidos se BORRAN** (medido): se van
    `codeBlock`, `llmModelSettings` e `inlineActions`. Nunca sin mascara.

R10 `playbookType` es escribible (`TASK` | `ROUTINE`). Un macro
    `${PLAYBOOK:X}` que apunte a un playbook de tipo ROUTINE hace fallar el
    POST: *"X is a workflow playbook, which is not supported for task
    playbooks."*

Campos que NO se escriben nunca (son `readOnly` o los deriva el backend):
`name`, `createTime`, `updateTime`, `tokenCount`, `inlineActions`,
`referencedFlows`, `referencedPlaybooks`.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

# Macro de recurso: ${PLAYBOOK:nombre} | ${FLOW:nombre} | ${TOOL:nombre}
MACRO_RE = re.compile(r"\$\{(PLAYBOOK|FLOW|TOOL):([^{}]+)\}")

# Campos del cuerpo que el backend ESCANEA (medido).
CAMPOS_ESCANEADOS = ("goal", "steps")
# Campos que el backend NO escanea (medido): el escape hatch.
CAMPOS_NO_ESCANEADOS = ("guidelines",)
# Campo de estado desconocido: se escanea por prudencia.
CAMPOS_NO_MEDIDOS = ("codeBlock",)

# Politicas para la autorreferencia.
POLITICAS_AUTOREF = ("error", "guidelines", "inert")


class ReglaNegocioError(ValueError):
    """La peticion no cumple una regla de negocio de CX.

    El mensaje es el que devolveria la API (o su causa), para que el agente
    que lo lee sepa que hacer sin tener que gastar una escritura en descubrirlo.
    """


@dataclass(frozen=True)
class Macro:
    """Una macro de recurso encontrada en el texto de un playbook."""

    kind: str      # PLAYBOOK | FLOW | TOOL
    ref: str       # displayName (o id) referenciado
    campo: str     # goal | steps | guidelines | codeBlock

    @property
    def escaneada(self) -> bool:
        """El backend la valida al escribir?"""
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
    """Todas las macros del playbook, con el campo donde esta cada una.

    Se escanean los tres campos (los escaneados, el que no y el no medido)
    porque el informe tiene que ser COMPLETO: aunque `guidelines` no lo
    valide el backend, quien llama tiene que saber que hay una macro ahi.
    """
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
    """La macro `${PLAYBOOK:<display_name>}` que se apunta a si misma."""
    for m in macros:
        if m.kind == "PLAYBOOK" and m.ref == display_name:
            return m
    return None


# ---------------------------------------------------------------------- cuerpo
def normalizar_steps(steps: Iterable[Any] | None) -> list[dict[str, Any]]:
    """`["texto", ...]` -> `[{"text": "texto"}, ...]` (lo que espera la API)."""
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
    """Arma el cuerpo del POST de un playbook (respeta R1, R3, R4).

    Nunca incluye `referencedFlows` ni `referencedPlaybooks`: son de solo
    lectura y el backend los deriva de las macros (mandarlos no guarda nada).
    """
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


# ------------------------------------------------------- politica autorreferencia
def _texto_sin_macro(texto: str, macro: Macro) -> str:
    """Quita la macro del texto (deja el resto intacto)."""
    return MACRO_RE.sub(
        lambda m: "" if (m.group(1) == macro.kind and m.group(2).strip() == macro.ref)
        else m.group(0), texto or "")


def _texto_inerte(texto: str, macro: Macro) -> str:
    """Neutraliza la macro: `${PLAYBOOK:x}` -> `{{PLAYBOOK:x}}` (token inerte)."""
    return MACRO_RE.sub(
        lambda m: ("{{PLAYBOOK:" + m.group(2).strip() + "}}")
        if (m.group(1) == macro.kind and m.group(2).strip() == macro.ref)
        else m.group(0), texto or "")


def aplicar_politica_autorref(body: dict[str, Any], macros: list[Macro],
                              display_name: str,
                              politica: str = "error") -> tuple[dict[str, Any], list[str]]:
    """Aplica la politica de la autorreferencia. Devuelve (cuerpo, notas).

    `politica`:
      * `error`      -> lanza `ReglaNegocioError` explicando las dos salidas.
      * `guidelines` -> la macro se mueve a `instruction.guidelines` (unico
        campo que el backend no escanea): el texto se conserva verbatim.
      * `inert`      -> la macro se neutraliza en su sitio (token inerte).
    """
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

    # politica == "guidelines"
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
    # `guidelines` no se escanea: ahi puede quedar la macro tal cual.
    guia = instr.get("guidelines") or ""
    if propia.campo == "guidelines":
        pass                      # ya estaba ahi: no hay nada que mover
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


# ------------------------------------------------------------------ resolucion
class Resolvedor:
    """Resuelve `displayName` -> id de los recursos del agente (con cache).

    Es la unica pieza que necesita red; se inyecta para que el resto del
    modulo sea testeable offline.
    """

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
    """Comprueba las reglas ANTES de gastar la escritura.

    Devuelve el cuerpo final + el informe. Lanza `ReglaNegocioError` con el
    mensaje que devolveria la API si algo no cuadra (asi el error llega con
    causa y no como un HTTP 400 sin explicar).
    """
    instr = body.get("instruction") or {}
    macros = macros or escanear_macros(
        body.get("goal", ""), instr.get("guidelines", ""),
        instr.get("steps"), body.get("codeBlock"))

    informe_macros: list[dict[str, Any]] = []
    faltan: list[str] = []
    # referencedTools: explicitos + los que exigen las macros ${TOOL:...}
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
        # El backend responde con el nombre del recurso, no con el token.
        detalle = ", ".join(sorted({f.split(":", 1)[1] for f in faltan}))
        raise ReglaNegocioError(
            f"referencias que no existen under the agent: {detalle} "
            "(el backend responde 'Referenced resource X does not exist under "
            "<agente>'). Los playbooks se crean en ORDEN TOPOLOGICO: primero "
            "los que otros referencian.")

    # R3: toda macro ${TOOL:x} necesita su binding explicito.
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
