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

ALLOWED = "11111111-1111-4111-8111-111111111111"
READ_ONLY = "22222222-2222-4222-8222-222222222222"
FORBIDDEN = "33333333-3333-4333-8333-333333333333"


@pytest.fixture()
def cfg() -> Config:
    c = Config(project="proj", location="global")
    c.agent_readwrite = [ALLOWED]
    c.agent_read = [READ_ONLY]
    return c


def url(agent: str, suffix: str = "/flows/f") -> str:
    return (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
            f"/locations/global/agents/{agent}{suffix}")


# --------------------------------------------------------------- extraction
def test_it_extracts_the_agent_from_the_path(cfg):
    assert agents_from_url(url(ALLOWED)) == [("proj", ALLOWED)]


def test_it_extracts_the_agent_from_the_query(cfg):
    """The generic passthrough can put the whole resource name in ?name=."""
    u = ("https://dialogflow.clients6.google.com/v3beta1/projects/proj/locations/global"
         f"?name=projects/proj/locations/global/agents/{FORBIDDEN}/flows/x")
    assert ("proj", FORBIDDEN) in agents_from_url(u)


def test_it_extracts_the_agents_from_both_places(cfg):
    u = (f"https://h/v3beta1/projects/proj/locations/global/agents/{ALLOWED}/flows/f"
         f"?name=projects/proj/locations/global/agents/{FORBIDDEN}/flows/x")
    assert set(agents_from_url(u)) == {("proj", ALLOWED), ("proj", FORBIDDEN)}


# ------------------------------------------------------------------- guard
def test_a_write_to_the_allowed_agent_goes_through(cfg):
    assert_url_allowed(url(ALLOWED), "PATCH", cfg)


def test_a_write_to_an_agent_that_is_not_listed_is_blocked(cfg):
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(url(FORBIDDEN), "PATCH", cfg)


def test_a_write_to_a_read_only_agent_is_blocked(cfg):
    with pytest.raises(ReadOnlyAgentError):
        assert_url_allowed(url(READ_ONLY), "PATCH", cfg)


def test_a_read_of_an_agent_that_is_not_listed_is_blocked(cfg):
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(url(FORBIDDEN), "GET", cfg)


def test_a_write_with_the_agent_only_in_the_query_is_blocked(cfg):
    u = ("https://dialogflow.clients6.google.com/v3beta1/projects/proj/locations/global"
         f"?name=projects/proj/locations/global/agents/{FORBIDDEN}/flows/x")
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(u, "PATCH", cfg)


def test_a_write_with_a_traversal_is_blocked(cfg):
    """`agents/OK/../../agents/X`: the server resolves the `..` and lands on X."""
    u = (f"https://h/v3beta1/projects/proj/locations/global/agents/{ALLOWED}"
         f"/flows/f/../../agents/{FORBIDDEN}")
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(u, "PATCH", cfg)


def test_a_write_without_an_agent_is_blocked(cfg):
    """Fail closed: every CX write is per-agent, so a write without an agent
    can only be a hand-crafted path trying to slip the check."""
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed("https://h/v3beta1/projects/proj/locations/global/operations/OP",
                           "POST", cfg)


def test_a_read_without_an_agent_goes_through(cfg):
    """Reading the project list (no agent) is legitimate."""
    assert assert_url_allowed("https://h/v3beta1/projects", "GET", cfg) == []


# ------------------------------------------------------- canonicalization
def test_resolve_path_resolves_the_dot_dot():
    p, _v, _q = resolve_path(f"agents/{ALLOWED}/flows/f/../../agents/{FORBIDDEN}",
                             project="proj", location="global", agent=ALLOWED)
    assert ".." not in p
    assert p == f"projects/proj/locations/global/agents/{ALLOWED}/agents/{FORBIDDEN}"


def test_resolve_path_rejects_the_encoded_traversal():
    with pytest.raises(PathError):
        resolve_path("agents/x/flows/%2e%2e/%2e%2e/agents/y",
                     project="proj", location="global", agent=ALLOWED)


def test_resolve_path_rejects_leaving_the_root():
    with pytest.raises(PathError):
        resolve_path("../../../etc/passwd", project="proj", location="global",
                     agent=ALLOWED)


# ------------------------------------------------------------------ batch
def test_the_batch_checks_every_sub_request(cfg):
    """`cx_batch` went straight to the HTTP client, skipping the guard."""
    api = ConsoleApi(cfg)
    result = api.batch_secondary([
        {"method": "PATCH", "path": f"/projects/proj/locations/global/agents/{ALLOWED}/flows/f"},
        {"method": "PATCH", "path": f"/projects/proj/locations/global/agents/{FORBIDDEN}/flows/f"},
    ], threads=1)
    assert result[0]["status"] != 403, "the allowed sub-request must go through"
    assert result[1]["status"] == 403, "the forbidden one must be blocked"
    assert "permission" in str(result[1]["data"]).lower()
