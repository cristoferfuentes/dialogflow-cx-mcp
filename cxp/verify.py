"""Verificacion de RUNTIME: la unica forma de saber si la ruta llego al playbook.

El GET de la API no muestra el destino a playbook de una transition route (ni
siquiera por el backend de la consola). La verdad esta en la **traza** de la
conversacion:

    State Machine Update | CONVERSATION_EVENT : sys.no-match-default | page=...
    State Machine Update | TRANSITION : | page=transicion_canal | DEST=playbooks/...
    PLAYBOOK_INVOCATION | PB=pyb_...

Procedimiento (ver 05-EJECUTAR-CONVERSACIONES.md del workspace):

1. turno 0: `detectIntent` con el contexto en `queryParams.parameters`
   (`canal`, `es_cliente`, ...) -> saludo.
2. turno 1: `detectIntent` con una utterance que NO matchea -> se dispara
   `sys.no-match-default` -> se entra a `transicion_canal` -> la ruta que
   matchea por condicion deberia saltar al playbook.
3. `GET /conversations/{id}` (el id de conversion es el id de sesion) -> traza.
"""
from __future__ import annotations

import uuid

from .cxapi import CxApi

START_PAGE = "projects/{p}/locations/{l}/agents/{a}/flows/00000000-0000-0000-0000-000000000000/pages/START_PAGE"


def _walk(node, out: list[str]):
    if isinstance(node, dict):
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)
    elif isinstance(node, str):
        out.append(node)


def run_turn(
    api: CxApi,
    agent_id: str,
    session_id: str,
    text: str,
    parameters: dict | None = None,
    current_page: str | None = None,
    timeout: float = 300.0,
) -> dict:
    body: dict = {
        "session": session_id,
        "queryInput": {"text": {"text": text}},
    }
    qp: dict = {}
    if parameters:
        qp["parameters"] = parameters
    if current_page:
        qp["currentPage"] = current_page
    if qp:
        body["queryParams"] = qp
    url = f"{api.cfg.agent_base(agent_id, 'public')}:detectIntent"
    return api.http.json("POST", url, api._headers(), body, timeout=timeout)  # type: ignore[return-value]


def get_conversation(api: CxApi, agent_id: str, conversation_id: str) -> dict:
    url = f"{api.cfg.agent_base(agent_id, 'public')}/conversations/{conversation_id}"
    return api.http.json("GET", url, api._headers())  # type: ignore[return-value]


def verify_route_target(
    api: CxApi,
    agent_id: str,
    flow_id: str,
    parameters: dict,
    text_turn0: str = "hola",
    text_turn1: str = "xyzzy condicion imposible de fulfill 4711",
    timeout: float = 300.0,
) -> dict:
    """Ejecuta 2 turnos y busca el DEST=playbooks/... en la traza."""
    session_id = str(uuid.uuid4())
    start_page = START_PAGE.format(
        p=api.cfg.project, l=api.cfg.location, a=agent_id
    )
    turn0 = run_turn(
        api, agent_id, session_id, text_turn0, parameters=parameters, current_page=start_page, timeout=timeout
    )
    qr0 = turn0.get("queryResult", {}) if isinstance(turn0, dict) else {}
    params_ok = bool(qr0.get("parameters"))

    turn1 = run_turn(api, agent_id, session_id, text_turn1, timeout=timeout)
    qr1 = turn1.get("queryResult", {}) if isinstance(turn1, dict) else {}

    conv = get_conversation(api, agent_id, session_id)
    strings: list[str] = []
    _walk(conv, strings)
    dests = [s for s in strings if "playbooks/" in s]
    transitions = [s for s in strings if "TRANSITION" in s]
    playbooks = sorted({s for s in strings if s.startswith("pyb_") or "/playbooks/" in s})

    return {
        "session_id": session_id,
        "contexto_llego": params_ok,
        "match_turno_0": (qr0.get("match") or {}).get("matchType"),
        "match_turno_1": (qr1.get("match") or {}).get("matchType"),
        "current_playbooks": (qr1.get("generativeInfo") or {}).get("currentPlaybooks"),
        "destinos_playbook": dests[:10],
        "transiciones": transitions[:10],
        "playbooks_en_traza": playbooks[:10],
        "llego_a_playbook": bool(dests),
        "nota": "si `llego_a_playbook` es False, la ruta todavia no tiene destino a playbook",
    }


def delete_conversation(api: CxApi, agent_id: str, conversation_id: str) -> bool:
    url = f"{api.cfg.agent_base(agent_id, 'public')}/conversations/{conversation_id}"
    try:
        api.http.json("DELETE", url, api._headers())
        return True
    except Exception:
        return False
