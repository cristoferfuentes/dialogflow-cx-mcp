"""Security tests of the per-agent allowlist.

They cover the three ways of reaching an agent that the guard has to close:

1. the agent in the path (the normal case);
2. the agent **only in the query** (`?name=projects/.../agents/X/...`);
3. the agent behind `..` (the server resolves it, so the path is canonicalized
   before it is compared).

Plus the rule "fail closed": a write whose URL mentions no agent at all is
rejected, and the batch applies the guard **per sub-request**.
"""
from __future__ import annotations

import pytest

from cxp.config import Config
from cxp.console_api import ConsoleApi
from cxp.endpoint import PathError, resolve_path
from cxp.guard import (
    PermissionDeniedError,
    ReadOnlyAgentError,
    agents_from_url,
    assert_url_allowed,
)

PERMITIDO = "11111111-1111-4111-8111-111111111111"
SOLO_LECTURA = "22222222-2222-4222-8222-222222222222"
PROHIBIDO = "33333333-3333-4333-8333-333333333333"


@pytest.fixture()
def cfg() -> Config:
    c = Config(project="proj", location="global")
    c.agent_readwrite = [PERMITIDO]
    c.agent_read = [SOLO_LECTURA]
    return c


def url(agente: str, sufijo: str = "/flows/f") -> str:
    return (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
            f"/locations/global/agents/{agente}{sufijo}")


# --------------------------------------------------------------- extraction
def test_extrae_el_agente_del_path(cfg):
    assert agents_from_url(url(PERMITIDO)) == [("proj", PERMITIDO)]


def test_extrae_el_agente_de_la_query(cfg):
    """The generic passthrough can put the whole resource name in ?name=."""
    u = ("https://dialogflow.clients6.google.com/v3beta1/projects/proj/locations/global"
         f"?name=projects/proj/locations/global/agents/{PROHIBIDO}/flows/x")
    assert ("proj", PROHIBIDO) in agents_from_url(u)


def test_extrae_los_agentes_de_los_dos_lugares(cfg):
    u = (f"https://h/v3beta1/projects/proj/locations/global/agents/{PERMITIDO}/flows/f"
         f"?name=projects/proj/locations/global/agents/{PROHIBIDO}/flows/x")
    assert set(agents_from_url(u)) == {("proj", PERMITIDO), ("proj", PROHIBIDO)}


# ------------------------------------------------------------------- guard
def test_escritura_al_agente_permitido_pasa(cfg):
    assert_url_allowed(url(PERMITIDO), "PATCH", cfg)


def test_escritura_a_un_agente_que_no_esta_lista_se_bloquea(cfg):
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(url(PROHIBIDO), "PATCH", cfg)


def test_escritura_a_un_agente_de_solo_lectura_se_bloquea(cfg):
    with pytest.raises(ReadOnlyAgentError):
        assert_url_allowed(url(SOLO_LECTURA), "PATCH", cfg)


def test_lectura_de_un_agente_que_no_esta_lista_se_bloquea(cfg):
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(url(PROHIBIDO), "GET", cfg)


def test_la_escritura_con_el_agente_solo_en_la_query_se_bloquea(cfg):
    u = ("https://dialogflow.clients6.google.com/v3beta1/projects/proj/locations/global"
         f"?name=projects/proj/locations/global/agents/{PROHIBIDO}/flows/x")
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(u, "PATCH", cfg)


def test_la_escritura_con_traversal_se_bloquea(cfg):
    """`agents/OK/../../agents/X`: the server resolves the `..` and lands on X."""
    u = (f"https://h/v3beta1/projects/proj/locations/global/agents/{PERMITIDO}"
         f"/flows/f/../../agents/{PROHIBIDO}")
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(u, "PATCH", cfg)


def test_la_escritura_sin_agente_se_bloquea(cfg):
    """Fail closed: every CX write is per-agent, so a write without an agent
    can only be a hand-crafted path trying to slip the check."""
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed("https://h/v3beta1/projects/proj/locations/global/operations/OP",
                           "POST", cfg)


def test_la_lectura_sin_agente_pasa(cfg):
    """Reading the project list (no agent) is legitimate."""
    assert assert_url_allowed("https://h/v3beta1/projects", "GET", cfg) == []


# ------------------------------------------------------- canonicalization
def test_resolve_path_resuelve_los_dot_dot():
    p, _v, _q = resolve_path(f"agents/{PERMITIDO}/flows/f/../../agents/{PROHIBIDO}",
                             project="proj", location="global", agent=PERMITIDO)
    assert ".." not in p
    assert p == f"projects/proj/locations/global/agents/{PERMITIDO}/agents/{PROHIBIDO}"


def test_resolve_path_rechaza_el_traversal_codificado():
    with pytest.raises(PathError):
        resolve_path("agents/x/flows/%2e%2e/%2e%2e/agents/y",
                     project="proj", location="global", agent=PERMITIDO)


def test_resolve_path_rechaza_salir_de_la_raiz():
    with pytest.raises(PathError):
        resolve_path("../../../etc/passwd", project="proj", location="global",
                     agent=PERMITIDO)


# ------------------------------------------------------------------ batch
def test_el_batch_comprueba_cada_sub_peticion(cfg):
    """`cx_batch` went straight to the HTTP client, skipping the guard."""
    api = ConsoleApi(cfg)
    resultado = api.batch_secundario([
        {"method": "PATCH", "path": f"/projects/proj/locations/global/agents/{PERMITIDO}/flows/f"},
        {"method": "PATCH", "path": f"/projects/proj/locations/global/agents/{PROHIBIDO}/flows/f"},
    ], hilos=1)
    assert resultado[0]["status"] != 403, "the allowed sub-request must go through"
    assert resultado[1]["status"] == 403, "the forbidden one must be blocked"
    assert "permission" in str(resultado[1]["data"]).lower()
