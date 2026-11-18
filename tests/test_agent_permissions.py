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
    agents_by_level,
    assert_url_allowed,
    resolve_agent,
)

WRITE_AGENT = "11111111-1111-4111-8111-111111111111"
WRITE_AGENT2 = "11111111-1111-4111-8111-111111111112"
READ_AGENT = "22222222-2222-4222-8222-222222222222"
READ_AGENT2 = "22222222-2222-4222-8222-222222222223"
FORBIDDEN = "33333333-3333-4333-8333-333333333333"


def _cfg(writes: list[str] | None = None,
         reads: list[str] | None = None) -> Config:
    c = Config(project="proj", location="global")
    c.agent_readwrite = list(writes if writes is not None else [WRITE_AGENT])
    c.agent_read = list(reads if reads is not None else [READ_AGENT])
    return c


# ------------------------------------------------- the allowlist is the list
def test_there_is_no_default_agent_in_the_configuration():
    """The field `default_agent` is gone: the allowlist is the source."""
    assert not hasattr(Config(), "default_agent")


def test_cxp_default_agent_is_ignored(tmp_path):
    """A leftover `CXP_DEFAULT_AGENT` in the `.env` does nothing."""
    env = tmp_path / ".env"
    env.write_text(
        "CXP_PROJECT=proj\n"
        f"CXP_DEFAULT_AGENT={FORBIDDEN}\n"
        f"CXP_AGENT_READ={READ_AGENT}\n"
        f"CXP_AGENT_READWRITE={WRITE_AGENT}\n",
        encoding="utf-8",
    )
    cfg = load_config(env_file=env)
    assert cfg.allowed_agent_ids() == [WRITE_AGENT, READ_AGENT]
    assert FORBIDDEN not in cfg.allowed_agent_ids()


def test_without_agents_there_is_nothing_to_do():
    cfg = Config(project="proj", location="global")
    assert cfg.allowed_agent_ids() == []
    assert cfg.permissions_error() != ""
    with pytest.raises(PermissionDeniedError):
        resolve_agent("", cfg)


def test_with_agents_there_is_no_permissions_error():
    cfg = _cfg()
    assert cfg.permissions_error() == ""
    assert cfg.allowed_agent_ids() == [WRITE_AGENT, READ_AGENT]


# ------------------------------------------------------------- resolution
def test_with_a_single_agent_it_is_deduced():
    cfg = _cfg(writes=[WRITE_AGENT], reads=[])
    assert resolve_agent("", cfg) == WRITE_AGENT


def test_with_several_agents_you_have_to_say_which_one():
    cfg = _cfg(writes=[WRITE_AGENT], reads=[READ_AGENT])
    with pytest.raises(PermissionDeniedError) as exc:
        resolve_agent("", cfg)
    message = str(exc.value)
    assert WRITE_AGENT in message and READ_AGENT in message


def test_the_explicit_agent_wins():
    """An explicit agent is used (its permission is checked afterwards)."""
    cfg = _cfg()
    assert resolve_agent(FORBIDDEN, cfg) == FORBIDDEN
    assert resolve_agent(f"proj/{READ_AGENT}", cfg) == READ_AGENT


def test_agents_by_level():
    cfg = _cfg()
    assert agents_by_level(cfg, "read/write") == [WRITE_AGENT]
    assert agents_by_level(cfg, "read") == [READ_AGENT]


def test_the_entry_with_a_project_wins():
    """`<project>/<uuid>`: the agent only exists in that project."""
    cfg = _cfg(writes=[], reads=[f"other-project/{READ_AGENT}"])
    assert agent_level("other-project", READ_AGENT, cfg) == "read"
    assert agent_level("other", READ_AGENT, cfg) == "none"


def test_the_permission_table_has_one_row_per_agent():
    """The legacy list and an explicit entry do not duplicate the agent."""
    from cxp.guard import permission_table

    cfg = _cfg(writes=[WRITE_AGENT], reads=[f"proj/{READ_AGENT}"])
    cfg.read_only_agents = [READ_AGENT]
    cfg.agent_read.append(READ_AGENT)
    rows = permission_table(cfg)
    assert len(rows) == 2
    row = next(r for r in rows if r["agent"] == READ_AGENT)
    assert row["project"] == "proj"


# ------------------------------------------------- several lists, several agents
def test_each_list_accepts_several_agents(tmp_path):
    """Both lists accept as many agents as needed (comma-separated)."""
    env = tmp_path / ".env"
    env.write_text(
        "CXP_PROJECT=proj\n"
        f"CXP_AGENT_READWRITE={WRITE_AGENT},{WRITE_AGENT2}\n"
        f"CXP_AGENT_READ={READ_AGENT},{READ_AGENT2}\n",
        encoding="utf-8",
    )
    cfg = load_config(env_file=env)
    assert cfg.agent_readwrite == [WRITE_AGENT, WRITE_AGENT2]
    assert cfg.agent_read == [READ_AGENT, READ_AGENT2]
    assert cfg.allowed_agent_ids() == [
        WRITE_AGENT, WRITE_AGENT2, READ_AGENT, READ_AGENT2,
    ]
    # each agent keeps its own level
    assert agent_level("proj", WRITE_AGENT, cfg) == "read/write"
    assert agent_level("proj", WRITE_AGENT2, cfg) == "read/write"
    assert agent_level("proj", READ_AGENT, cfg) == "read"
    assert agent_level("proj", READ_AGENT2, cfg) == "read"
    # and the ones that are not on the lists are still refused
    assert agent_level("proj", FORBIDDEN, cfg) == "none"
    assert cfg.permissions_error() == ""


def test_the_write_goes_to_any_agent_of_the_write_list():
    """The SECOND agent of `CXP_AGENT_READWRITE` is writable too."""
    cfg = _cfg(writes=[WRITE_AGENT, WRITE_AGENT2], reads=[READ_AGENT])
    url = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
           f"/locations/global/agents/{WRITE_AGENT2}/flows/f")
    assert assert_url_allowed(url, "PATCH", cfg) == [("proj", WRITE_AGENT2)]


def test_the_permission_table_lists_all_the_agents():
    from cxp.guard import permission_table

    cfg = _cfg(writes=[WRITE_AGENT, WRITE_AGENT2],
               reads=[READ_AGENT, READ_AGENT2])
    rows = {r["agent"]: r["permission"] for r in permission_table(cfg)}
    assert rows == {
        WRITE_AGENT: "read/write",
        WRITE_AGENT2: "read/write",
        READ_AGENT: "read",
        READ_AGENT2: "read",
    }


# ------------------------------------------------- the messages, in english
def test_the_permission_messages_are_in_english():
    """The agent that consumes the MCP reads these messages: they are English."""
    from cxp.guard import NO_AGENTS_MSG

    cfg = _cfg()
    url = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
           f"/locations/global/agents/{FORBIDDEN}/flows/f")
    with pytest.raises(PermissionDeniedError) as exc:
        assert_url_allowed(url, "PATCH", cfg)
    message = str(exc.value)
    assert "has no permission" in message
    assert "CXP_AGENT_READWRITE" in message

    url_ro = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
              f"/locations/global/agents/{READ_AGENT}/flows/f")
    with pytest.raises(ReadOnlyAgentError) as exc:
        assert_url_allowed(url_ro, "PATCH", cfg)
    assert "READ-ONLY" in str(exc.value)

    # with no agent at all the message says which variables are required
    empty = Config(project="proj", location="global")
    assert "no allowed agent" in empty.permissions_error()
    assert "CXP_AGENT_READ" in NO_AGENTS_MSG
    assert "REQUIRED" in NO_AGENTS_MSG
    assert "security boundary" in NO_AGENTS_MSG


# ------------------------------------------------------------- listings
def _raw_agent(uuid: str, project: str = "proj") -> dict:
    return {"name": f"projects/{project}/locations/global/agents/{uuid}",
            "displayName": f"agent {uuid[:8]}"}


def test_cx_list_agents_only_shows_the_allowed_ones(monkeypatch):
    import server

    cfg = _cfg()
    monkeypatch.setattr(server, "CFG", cfg)
    monkeypatch.setattr(
        server._API, "list_agents",
        lambda project="", location="": [_raw_agent(WRITE_AGENT),
                                         _raw_agent(READ_AGENT),
                                         _raw_agent(FORBIDDEN)],
    )
    out = server.cx_list_agents()
    ids = [a["id"] for a in out["agents"]]
    assert ids == [WRITE_AGENT, READ_AGENT]
    assert FORBIDDEN not in ids
    assert out["hidden_by_permissions"] == 1
    assert out["filtered_by_permissions"] is True


def test_cx_status_only_shows_the_allowed_ones(monkeypatch):
    import server

    cfg = _cfg()
    cfg.api_key = "k"
    cfg.sapisid = "s"
    cfg.cookies = {"SAPISID": "s", "SID": "s", "HSID": "s", "SSID": "s", "APISID": "s"}
    monkeypatch.setattr(server, "CFG", cfg)
    monkeypatch.setattr(
        server._API, "list_agents",
        lambda project="", location="": [_raw_agent(WRITE_AGENT),
                                         _raw_agent(FORBIDDEN)],
    )
    out = server.cx_status()
    assert [a["id"] for a in out["visible_agents"]] == [WRITE_AGENT]
    assert out["hidden_agents"] == 1
    assert out["allowed_agents"] == [WRITE_AGENT, READ_AGENT]
    assert out["permissions_ok"] is True


def test_cx_status_warns_when_there_is_no_agent(monkeypatch):
    import server

    cfg = Config(project="proj", location="global")
    monkeypatch.setattr(server, "CFG", cfg)
    out = server.cx_status()
    assert out["permissions_ok"] is False
    assert "CXP_AGENT_READ" in out["permissions_error"]


# --------------------------------------------------------------- writes
def test_a_write_on_a_read_only_agent_is_refused():
    cfg = _cfg()
    url = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
           f"/locations/global/agents/{READ_AGENT}/flows/f")
    with pytest.raises(ReadOnlyAgentError) as exc:
        assert_url_allowed(url, "PATCH", cfg)
    assert "CXP_AGENT_READWRITE" in str(exc.value)


def test_a_write_on_an_agent_outside_the_list_is_refused():
    cfg = _cfg()
    url = (f"https://dialogflow.clients6.google.com/v3beta1/projects/proj"
           f"/locations/global/agents/{FORBIDDEN}/flows/f")
    with pytest.raises(PermissionDeniedError):
        assert_url_allowed(url, "PATCH", cfg)


def test_the_permission_is_checked_BEFORE_touching_the_resource(monkeypatch):
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

    for output in (
        server.cx_update_flow(flow="does-not-exist", agent=READ_AGENT, body={}),
        server.cx_update_page(flow="does-not-exist", page="neither", agent=READ_AGENT,
                              body={}),
        server.cx_set_route_playbook(agent=READ_AGENT, flow="does-not-exist",
                                     page="neither", playbook="x", dry_run=False),
        server.cx_set_flow_route_playbook(agent=READ_AGENT, flow="does-not-exist",
                                          playbook="x", dry_run=False),
        server.cx_set_group_route_playbook(agent=READ_AGENT, group="g",
                                           playbook="x", dry_run=False),
        server.cx_set_console_field(resource="agent", field="webClientSettings",
                                    value={}, agent=READ_AGENT, dry_run=False),
    ):
        assert "SOLO LECTURA" in (output.get("error") or ""), output


# ------------------------------------------------------------- the rules
def test_the_server_declares_the_rules():
    """The agent that consumes the MCP receives the rules with the handshake."""
    import server

    # The constant that holds the rules (its name may be `RULES` or `REGLAS`).
    rules = getattr(server, "RULES", None) or getattr(server, "REGLAS", "")
    assert server.mcp.instructions == rules
    # 1) never call the public API directly
    assert "dialogflow.googleapis.com" in rules
    assert "USE ONLY THESE TOOLS" in rules
    # 2) only the agents of the allowlist
    assert "CXP_AGENT_READ" in rules and "CXP_AGENT_READWRITE" in rules
    # 3) writes only on the readwrite ones
    assert "WRITES ONLY ON CXP_AGENT_READWRITE AGENTS" in rules
    # 4) when blocked: re-plan or ask for more permissions
    assert "Re-plan the task" in rules
    assert "which permission has to be added" in rules


def test_cx_status_returns_the_rules(monkeypatch):
    """The rules are also in `cx_status`, the first call any agent makes."""
    import server

    cfg = _cfg()
    monkeypatch.setattr(server, "CFG", cfg)
    out = server.cx_status()
    assert out["rules"] == server.mcp.instructions
    assert "USE ONLY THESE TOOLS" in out["rules"]
