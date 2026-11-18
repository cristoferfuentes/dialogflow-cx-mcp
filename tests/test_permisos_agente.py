"""The allowlist IS the list of agents (there is no default agent).

What these tests pin down:

1. `CXP_DEFAULT_AGENT` does not exist any more: the agents come from
   `CXP_AGENT_READ` (read-only) and `CXP_AGENT_READWRITE` (read + write).
2. With no agent in either list the MCP has nothing to work on, and it says
   so (`permissions_error`).
3. A call without an explicit agent works only when the allowlist leaves a
   single candidate; with several, the MCP says which ones are available
   instead of guessing.
4. The agents that are NOT on the allowlist do not show up in the listings
   (`cx_list_agents`, `cx_status`).
5. A write on a read-only agent is refused.
6. The server declares its rules to the agent that consumes it (no direct
   calls to the public API; re-plan or ask for more permissions).
"""
from __future__ import annotations

import pytest

from cxp.config import Config, load_config
from cxp.guard import (
    PermissionDeniedError,
    ReadOnlyAgentError,
    agent_level,
    agents_por_nivel,
    assert_url_allowed,
    resolve_agent,
)

ESCRITURA = "11111111-1111-4111-8111-111111111111"
ESCRITURA2 = "11111111-1111-4111-8111-111111111112"
SOLO_LECTURA = "22222222-2222-4222-8222-222222222222"
SOLO_LECTURA2 = "22222222-2222-4222-8222-222222222223"
PROHIBIDO = "33333333-3333-4333-8333-333333333333"


def _cfg(escritura: list[str] | None = None,
         lectura: list[str] | None = None) -> Config:
    c = Config(project="proj", location="global")
    c.agent_readwrite = list(escritura if escritura is not None else [ESCRITURA])
    c.agent_read = list(lectura if lectura is not None else [SOLO_LECTURA])
    return c


# ------------------------------------------------- la lista blanca es la lista
def test_no_hay_agente_por_defecto_en_la_configuracion():
    """The field `default_agent` is gone: the allowlist is the source."""
    assert not hasattr(Config(), "default_agent")


def test_cxp_default_agent_se_ignora(tmp_path):
    """A leftover `CXP_DEFAULT_AGENT` in the `.env` does nothing."""
    env = tmp_path / ".env"
    env.write_text(
        "CXP_PROJECT=proj\n"
        f"CXP_DEFAULT_AGENT={PROHIBIDO}\n"
        f"CXP_AGENT_READ={SOLO_LECTURA}\n"
        f"CXP_AGENT_READWRITE={ESCRITURA}\n",
        encoding="utf-8",
    )
    cfg = load_config(env_file=env)
    assert cfg.allowed_agent_ids() == [ESCRITURA, SOLO_LECTURA]
    assert PROHIBIDO not in cfg.allowed_agent_ids()


def test_sin_agentes_no_hay_nada_que_hacer():
    cfg = Config(project="proj", location="global")
    assert cfg.allowed_agent_ids() == []
    assert cfg.permissions_error() != ""
    with pytest.raises(PermissionDeniedError):
        resolve_agent("", cfg)


def test_con_agentes_no_hay_error_de_permisos():
    cfg = _cfg()
    assert cfg.permissions_error() == ""
    assert cfg.allowed_agent_ids() == [ESCRITURA, SOLO_LECTURA]


# ------------------------------------------------------------- resolucion
def test_con_un_solo_agente_se_deduce():
    cfg = _cfg(escritura=[ESCRITURA], lectura=[])
    assert resolve_agent("", cfg) == ESCRITURA


def test_con_varios_agentes_hay_que_indicar_cual():
    cfg = _cfg(escritura=[ESCRITURA], lectura=[SOLO_LECTURA])
    with pytest.raises(PermissionDeniedError) as exc:
        resolve_agent("", cfg)
    mensaje = str(exc.value)
    assert ESCRITURA in mensaje and SOLO_LECTURA in mensaje


def test_el_agente_explicito_gana():
    """An explicit agent is used (its permission is checked afterwards)."""
    cfg = _cfg()
    assert resolve_agent(PROHIBIDO, cfg) == PROHIBIDO
    assert resolve_agent(f"proj/{SOLO_LECTURA}", cfg) == SOLO_LECTURA


def test_agents_por_nivel():
    cfg = _cfg()
    assert agents_por_nivel(cfg, "read/write") == [ESCRITURA]
    assert agents_por_nivel(cfg, "read") == [SOLO_LECTURA]


def test_la_entrada_con_proyecto_manda():
    """`<project>/<uuid>`: the agent only exists in that project."""
    cfg = _cfg(escritura=[], lectura=[f"otro-proyecto/{SOLO_LECTURA}"])
    assert agent_level("otro-proyecto", SOLO_LECTURA, cfg) == "read"
    assert agent_level("otro", SOLO_LECTURA, cfg) == "none"


def test_la_tabla_de_permisos_tiene_una_fila_por_agente():
    """The legacy list and an explicit entry do not duplicate the agent."""
    from cxp.guard import permission_table

    cfg = _cfg(escritura=[ESCRITURA], lectura=[f"proj/{SOLO_LECTURA}"])
    cfg.read_only_agents = [SOLO_LECTURA]
    cfg.agent_read.append(SOLO_LECTURA)
    filas = permission_table(cfg)
    assert len(filas) == 2
    fila = next(f for f in filas if f["agente"] == SOLO_LECTURA)
    assert fila["proyecto"] == "proj"


# ------------------------------------------------- varias listas, varios agentes
def test_cada_lista_acepta_varios_agentes(tmp_path):
    """Both lists accept as many agents as needed (comma-separated)."""
    env = tmp_path / ".env"
    env.write_text(
        "CXP_PROJECT=proj\n"
        f"CXP_AGENT_READWRITE={ESCRITURA},{ESCRITURA2}\n"
        f"CXP_AGENT_READ={SOLO_LECTURA},{SOLO_LECTURA2}\n",
        encoding="utf-8",
    )
    cfg = load_config(env_file=env)
    assert cfg.agent_readwrite == [ESCRITURA, ESCRITURA2]
    assert cfg.agent_read == [SOLO_LECTURA, SOLO_LECTURA2]
    assert cfg.allowed_agent_ids() == [
        ESCRITURA, ESCRITURA2, SOLO_LECTURA, SOLO_LECTURA2,
    ]
    # each agent keeps its own level
    assert agent_level("proj", ESCRITURA, cfg) == "read/write"
    assert agent_level("proj", ESCRITURA2, cfg) == "read/write"
    assert agent_level("proj", SOLO_LECTURA, cfg) == "read"
    assert agent_level("proj", SOLO_LECTURA2, cfg) == "read"
    # and the ones that are not on the lists are still refused
    assert agent_level("proj", PROHIBIDO, cfg) == "none"
    assert cfg.permissions_error() == ""


def test_la_escritura_va_a_cualquier_agente_de_la_lista_de_escritura():
    """The SECOND agent of `CXP_AGENT_READWRITE` is writable too."""
    cfg = _cfg(escritura=[ESCRITURA, ESCRITURA2], lectura=[SOLO_LECTURA])
    url = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
           f"/locations/global/agents/{ESCRITURA2}/flows/f")
    assert assert_url_allowed(url, "PATCH", cfg) == [("proj", ESCRITURA2)]


def test_la_tabla_de_permisos_lista_todos_los_agentes():
    from cxp.guard import permission_table

    cfg = _cfg(escritura=[ESCRITURA, ESCRITURA2],
               lectura=[SOLO_LECTURA, SOLO_LECTURA2])
    filas = {f["agente"]: f["permiso"] for f in permission_table(cfg)}
    assert filas == {
        ESCRITURA: "read/write",
        ESCRITURA2: "read/write",
        SOLO_LECTURA: "read",
        SOLO_LECTURA2: "read",
    }


# ------------------------------------------------- los mensajes, en ingles
def test_los_mensajes_de_permisos_estan_en_ingles():
    """The agent that consumes the MCP reads these messages: they are English."""
    from cxp.guard import NO_AGENTS_MSG

    cfg = _cfg()
    url = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
           f"/locations/global/agents/{PROHIBIDO}/flows/f")
    with pytest.raises(PermissionDeniedError) as exc:
        assert_url_allowed(url, "PATCH", cfg)
    mensaje = str(exc.value)
    assert "has no permission" in mensaje
    assert "CXP_AGENT_READWRITE" in mensaje

    url_ro = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
              f"/locations/global/agents/{SOLO_LECTURA}/flows/f")
    with pytest.raises(ReadOnlyAgentError) as exc:
        assert_url_allowed(url_ro, "PATCH", cfg)
    assert "READ-ONLY" in str(exc.value)

    # with no agent at all the message says which variables are required
    vacia = Config(project="proj", location="global")
    assert "no allowed agent" in vacia.permissions_error()
    assert "CXP_AGENT_READ" in NO_AGENTS_MSG
    assert "REQUIRED" in NO_AGENTS_MSG
    assert "security boundary" in NO_AGENTS_MSG


# ------------------------------------------------------------- listados
def _agente_bruto(uuid: str, proyecto: str = "proj") -> dict:
    return {"name": f"projects/{proyecto}/locations/global/agents/{uuid}",
            "displayName": f"agente {uuid[:8]}"}


def test_cx_list_agents_solo_muestra_los_permitidos(monkeypatch):
    import server

    cfg = _cfg()
    monkeypatch.setattr(server, "CFG", cfg)
    monkeypatch.setattr(
        server._API, "list_agents",
        lambda project="", location="": [_agente_bruto(ESCRITURA),
                                         _agente_bruto(SOLO_LECTURA),
                                         _agente_bruto(PROHIBIDO)],
    )
    out = server.cx_list_agents()
    ids = [a["id"] for a in out["agentes"]]
    assert ids == [ESCRITURA, SOLO_LECTURA]
    assert PROHIBIDO not in ids
    assert out["ocultos_por_permisos"] == 1
    assert out["filtrado_por_permisos"] is True


def test_cx_status_solo_muestra_los_permitidos(monkeypatch):
    import server

    cfg = _cfg()
    cfg.api_key = "k"
    cfg.sapisid = "s"
    cfg.cookies = {"SAPISID": "s", "SID": "s", "HSID": "s", "SSID": "s", "APISID": "s"}
    monkeypatch.setattr(server, "CFG", cfg)
    monkeypatch.setattr(
        server._API, "list_agents",
        lambda project="", location="": [_agente_bruto(ESCRITURA),
                                         _agente_bruto(PROHIBIDO)],
    )
    out = server.cx_status()
    assert [a["id"] for a in out["agentes_visibles"]] == [ESCRITURA]
    assert out["agentes_ocultos"] == 1
    assert out["agentes_permitidos"] == [ESCRITURA, SOLO_LECTURA]
    assert out["permisos_ok"] is True


def test_cx_status_avisa_si_no_hay_ningun_agente(monkeypatch):
    import server

    cfg = Config(project="proj", location="global")
    monkeypatch.setattr(server, "CFG", cfg)
    out = server.cx_status()
    assert out["permisos_ok"] is False
    assert "CXP_AGENT_READ" in out["error_permisos"]


# --------------------------------------------------------------- escritura
def test_la_escritura_en_un_agente_de_solo_lectura_se_rechaza():
    cfg = _cfg()
    url = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
           f"/locations/global/agents/{SOLO_LECTURA}/flows/f")
    with pytest.raises(ReadOnlyAgentError) as exc:
        assert_url_allowed(url, "PATCH", cfg)
    assert "CXP_AGENT_READWRITE" in str(exc.value)


def test_la_escritura_en_un_agente_fuera_de_la_lista_se_rechaza():
    cfg = _cfg()
    url = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
           f"/locations/global/agents/{PROHIBIDO}/flows/f")
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(url, "PATCH", cfg)


def test_el_permiso_se_comprueba_ANTES_de_tocar_el_recurso(monkeypatch):
    """A read-only agent says "SOLO LECTURA", not "flow not found".

    The permission is checked before the flow/page is resolved, so the agent
    that gets the refusal knows it is a permission problem (and not a missing
    resource)."""
    import server

    cfg = _cfg()
    cfg.api_key = "k"
    cfg.sapisid = "s"
    cfg.cookies = {"SAPISID": "s", "SID": "s", "HSID": "s", "SSID": "s", "APISID": "s"}
    monkeypatch.setattr(server, "CFG", cfg)

    for salida in (
        server.cx_update_flow(flow="no-existe", agent=SOLO_LECTURA, body={}),
        server.cx_update_page(flow="no-existe", page="tampoco", agent=SOLO_LECTURA,
                              body={}),
        server.cx_set_route_playbook(agent=SOLO_LECTURA, flow="no-existe",
                                     page="tampoco", playbook="x", dry_run=False),
        server.cx_set_flow_route_playbook(agent=SOLO_LECTURA, flow="no-existe",
                                          playbook="x", dry_run=False),
        server.cx_set_group_route_playbook(agent=SOLO_LECTURA, group="g",
                                           playbook="x", dry_run=False),
        server.cx_set_console_field(recurso="agent", campo="webClientSettings",
                                    valor={}, agent=SOLO_LECTURA, dry_run=False),
    ):
        assert "SOLO LECTURA" in (salida.get("error") or ""), salida


# ------------------------------------------------------------- las reglas
def test_el_servidor_declara_las_reglas():
    """The agent that consumes the MCP receives the rules with the handshake."""
    import server

    assert server.mcp.instructions == server.REGLAS
    reglas = server.REGLAS
    # 1) never call the public API directly
    assert "dialogflow.googleapis.com" in reglas
    assert "USE ONLY THESE TOOLS" in reglas
    # 2) only the agents of the allowlist
    assert "CXP_AGENT_READ" in reglas and "CXP_AGENT_READWRITE" in reglas
    # 3) writes only on the readwrite ones
    assert "WRITES ONLY ON CXP_AGENT_READWRITE AGENTS" in reglas
    # 4) when blocked: re-plan or ask for more permissions
    assert "Re-plan the task" in reglas
    assert "which permission has to be added" in reglas


def test_cx_status_devuelve_las_reglas(monkeypatch):
    """The rules are also in `cx_status`, the first call any agent makes."""
    import server

    cfg = _cfg()
    monkeypatch.setattr(server, "CFG", cfg)
    out = server.cx_status()
    assert out["reglas"] == server.REGLAS
    assert "USE ONLY THESE TOOLS" in out["reglas"]
