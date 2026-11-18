"""MCP configuration, read from a `.env` (no external dependencies).

All the MCP needs is **credentials**, not a browser:

    CXP_PROJECT=<project>
    CXP_LOCATION=global
    CXP_QUOTA_PROJECT=<quota project>   # optional: if absent, it is CXP_PROJECT
    CXP_CONSOLE_ORIGIN=https://conversational-agents.cloud.google.com
    CXP_API_KEY=AIzaSy...
    CXP_COOKIE="SAPISID=...; SID=...; HSID=...; SSID=...; APISID=..."

    `CXP_COOKIE` must be the **full jar** of `.google.com` (copied from the
    browser). With only the 5 mandatory cookies the backend responds 401 as
    soon as the session renews (measured).

    # --- PERMISSIONS PER AGENT (allowlist) --------------------------------
    # An agent with no entry here is NOT accessible at all.
    # AT LEAST ONE of the two lists must have an agent: the MCP works on the
    # agents of the allowlist, and it does not have a "default agent".
    # You do NOT need both lists: CXP_AGENT_READWRITE (read + write),
    # CXP_AGENT_READ (read only), or one agent in each.
    # EACH list accepts as many agents as needed (comma-separated).
    # The allowlist is what the MCP works on AND the security boundary of the
    # harness that consumes it (the cookies reach everything the account can
    # reach; the allowlist is what keeps the agent inside the agents you
    # chose).
    CXP_AGENT_READ=<uuid>[,<uuid>...]      # read only (GET/list)
    CXP_AGENT_READWRITE=<uuid>[,<uuid>...] # read + write (POST/PATCH/DELETE)
    CXP_PROJECTS=<project>,...              # optional: limits the projects

    CXP_READ_ONLY_AGENTS=<uuid>,<uuid>     # (legacy) merged into CXP_AGENT_READ
    CXP_SESSION_FILE=<path to a storage_state .json>   # alternative to CXP_COOKIE

Permission entries accept two forms: `<uuid>` (the permission travels with the
uuid, valid in any project) or `<project>/<uuid>` (pairs project and
agent). The MCP works by default against `v3beta1` (`CXP_API_VERSION`).

The `.env` is looked up in: (1) the `CXP_ENV_FILE` variable, (2) the package
directory, (3) the working directory, (4) `~/.config/cx-playbook.env`."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent  # mcp-cx-playbook/

# The 5 cookies that the internal backend REQUIRES (measured: with 4 of 5 -> HTTP 401).
# NOTE: they are the MINIMUM, not the full list. MEASURED (2026-11-13): with only those
# 5 the backend responds 401 "invalid authentication credentials" as soon as the
# browser session renews (the session cookies `__Secure-*PSIDTS` /
# `__Host-GAPS` / `SIDCC` / `OSID` are also revalidated). The remedy is to send
# the JAR COMPLETE from `.google.com` (23 cookies in the real browser) and keep
# requiring these 5 as a minimum sanity check.
REQUIRED_COOKIES = ("SAPISID", "SID", "HSID", "SSID", "APISID")

# Session cookie prefixes that must be sent in full for the backend to accept
# the call (measured: removing them -> 401).
SESSION_COOKIE_PREFIXES = ("__Secure-", "__Host-")


def parse_env(text: str) -> dict[str, str]:
    """Minimal `.env` parser: KEY=VALUE, optional quotes, `#` comments."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def find_env_file() -> Path | None:
    candidatos = []
    if os.environ.get("CXP_ENV_FILE"):
        candidatos.append(Path(os.environ["CXP_ENV_FILE"]).expanduser())
    candidatos.append(PKG_DIR / ".env")
    candidatos.append(Path.cwd() / ".env")
    candidatos.append(Path.home() / ".config" / "cx-playbook.env")
    for c in candidatos:
        if c.exists():
            return c
    return None


@dataclass
class Config:
    project: str = ""
    location: str = "global"
    console_origin: str = "https://conversational-agents.cloud.google.com"
    api_key: str = ""
    # Internal backend proto version. v3beta1 is the one to use
    # (in `v3` fields such as `llmModelSettings.parameters` do not exist and are silently dropped).
    api_version: str = "v3beta1"
    # Quota project (`x-goog-user-project`). Comes from `CXP_QUOTA_PROJECT` and,
    # if not set, is `CXP_PROJECT` itself. It is a DEPLOYMENT datum: the
    # MCP must not carry any hardcoded one.
    quota_project: str = ""
    # Credentials
    cookies: dict[str, str] = field(default_factory=dict)
    sapisid: str = ""
    # Agents: the allowlist is the ONLY source of agents. There is no
    # "default agent": the MCP works on the agents of `agent_read` (read-only)
    # and `agent_readwrite` (read + write), and on nothing else.
    read_only_agents: list[str] = field(default_factory=list)  # legacy
    # Permissions (allowlist)
    agent_read: list[str] = field(default_factory=list)
    agent_readwrite: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    # Credentials file path (informational)
    env_file: Path | None = None

    # Red
    socket_timeout: float = 45.0        # READ timeout
    connect_timeout: float = 8.0         # CONNECT timeout (IPs pinned in /etc/hosts)
    tcp_maxseg: int = 1200
    http_retries: int = 1
    # TTL of the catalog cache (id->displayName of flows/pages/playbooks).
    # Catalogs change very little and their payloads are huge (list_playbooks
    # of the clone = 770 KB) -> they are cached. They are invalidated on write.
    catalog_ttl: float = 120.0

    # --- Login helper only (OUTSIDE of MCP) -----------------------
    # The MCP uses none of this: it lives in the package for import convenience.
    display: str = ":99"
    cdp_port: int = 9222
    novnc_url: str = ""
    real_profile_dir: Path = field(default_factory=lambda: PKG_DIR / ".browser-profile-real")
    state_file: Path = field(default_factory=lambda: PKG_DIR / ".session-state.json")

    def console_url(self, agent_id: str, flow_id: str = "", page_id: str = "") -> str:
        url = (f"{self.console_origin}/projects/{self.project}/locations/{self.location}"
               f"/agents/{agent_id}/")
        if flow_id:
            url += f"(cx/flows/{flow_id}/flow_creation//right-panel:simulator)"
        else:
            url += "(cx)"
        if page_id:
            url += f"?pageId={page_id}"
        return url

    # ---------------------------------------------------------------- cookies
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    def missing_cookies(self) -> list[str]:
        return [c for c in REQUIRED_COOKIES if not self.cookies.get(c)]

    def credentials_ok(self) -> bool:
        return bool(self.api_key and self.sapisid and not self.missing_cookies())

    # ---------------------------------------------------------------- agents
    def is_read_only(self, agent_id: str) -> bool:
        """Legacy: the agent is on the read-only list."""
        return agent_id in self.read_only_agents

    def allowed_agent_ids(self) -> list[str]:
        """The agent ids of the allowlist (read + read/write), without duplicates."""
        ids: list[str] = []
        for entry in self.agent_readwrite + self.agent_read:
            agent = (entry or "").strip().split("/")[-1].strip()
            if agent and agent not in ids:
                ids.append(agent)
        return ids

    def only_agent(self) -> str:
        """The single allowed agent (or '' when there is none or more than one).

        The MCP itself has **no default agent**: it works on the agents of the
        allowlist. This helper is for the probes, which need one agent to work
        on."""
        ids = self.allowed_agent_ids()
        return ids[0] if len(ids) == 1 else ""

    def permissions_error(self) -> str:
        """Why the MCP cannot operate on any agent ('' when the allowlist is fine).

        The allowlist is the only source of agents: with no agent in
        `CXP_AGENT_READ` nor in `CXP_AGENT_READWRITE` there is nothing to work
        on, and the MCP says so instead of failing later with a 403.

        **At least one of the two lists must have an agent** (you do NOT need
        both): `CXP_AGENT_READWRITE` (read + write), `CXP_AGENT_READ` (read
        only), or one agent in each."""
        if not self.agent_readwrite and not self.agent_read:
            return (
                "no allowed agent: write at least one agent in CXP_AGENT_READ "
                "(read only) or in CXP_AGENT_READWRITE (read and write). At "
                "least ONE of the two lists must have an agent (not necessarily "
                "both): the allowlist is what the MCP works on AND the "
                "security boundary of the harness that consumes it"
            )
        return ""

    # ---------------------------------------------------------------- helpers
    @property
    def console_backend(self) -> str:
        return "https://dialogflow.clients6.google.com"

    def agent_base(self, agent_id: str) -> str:
        return (f"{self.console_backend}/{self.api_version}/projects/{self.project}"
                f"/locations/{self.location}/agents/{agent_id}")


def _cookies_from_raw(raw: str) -> dict[str, str]:
    """'SAPISID=..; SID=..' -> dict.

    Keeps **all** cookies, not just the 5 mandatory ones: the
    backend revalidates the rest of the session (measured: with 5 cookies and the session
    renewed -> HTTP 401; with the full jar -> 200)."""
    out: dict[str, str] = {}
    for parte in raw.split(";"):
        if "=" not in parte:
            continue
        k, _, v = parte.strip().partition("=")
        k = k.strip()
        v = v.strip()
        if k and v:
            out[k] = v
    return out


def _cookies_from_session_file(path: Path) -> dict[str, str]:
    """Cookies from `.google.com` of a Playwright `storage_state`.

    **All** the google.com ones are kept (not just the 5 mandatory ones): see the
    `REQUIRED_COOKIES` note."""
    state = json.loads(path.read_text(encoding="utf-8"))
    by_name: dict[str, str] = {}
    for c in state.get("cookies", []):
        dom = (c.get("domain") or "").lstrip(".").lower()
        if dom == "google.com" or dom.endswith(".google.com"):
            if c.get("name") and c.get("value"):
                by_name[c["name"]] = c["value"]
    return by_name


def _split_list(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    """Load the `.env` configuration (or the `CXP_*` variables)."""
    env: dict[str, str] = {}
    path = Path(env_file) if env_file else find_env_file()
    if path and path.exists():
        env.update(parse_env(path.read_text(encoding="utf-8")))
    # Environment variables take priority over the file.
    for k in list(env) + [f"CXP_{n}" for n in (
        "PROJECT", "LOCATION", "CONSOLE_ORIGIN", "API_KEY", "COOKIE", "SAPISID",
        "SID", "HSID", "SSID", "APISID", "READ_ONLY_AGENTS",
        "SESSION_FILE", "API_VERSION", "AGENT_READ", "AGENT_READWRITE", "PROJECTS",
    )]:
        if os.environ.get(k):
            env[k] = os.environ[k]

    cfg = Config(env_file=path)
    cfg.project = env.get("CXP_PROJECT", "")
    cfg.location = env.get("CXP_LOCATION", "global")
    cfg.console_origin = env.get(
        "CXP_CONSOLE_ORIGIN", "https://conversational-agents.cloud.google.com"
    ).rstrip("/")
    cfg.api_key = env.get("CXP_API_KEY", "")
    cfg.api_version = env.get("CXP_API_VERSION", "v3beta1").strip() or "v3beta1"
    # Project quota: `CXP_QUOTA_PROJECT` and, if not set, the main
    # project. (Used by the login helper and the public API client,
    # which are NOT the main MCP path.)
    cfg.quota_project = env.get("CXP_QUOTA_PROJECT", "").strip() or cfg.project
    # Login helper only (OUTSIDE of MCP).
    cfg.display = env.get("CXP_DISPLAY", ":99").strip() or ":99"
    try:
        cfg.cdp_port = int(env.get("CXP_CDP_PORT", "9222"))
    except ValueError:
        cfg.cdp_port = 9222
    cfg.novnc_url = env.get("CXP_NOVNC_URL", "").strip()
    cfg.read_only_agents = _split_list(env.get("CXP_READ_ONLY_AGENTS", ""))

    # --- Permissions: agent allowlist ---------------------------------
    # `CXP_AGENT_READWRITE` (read + write) and `CXP_AGENT_READ` (read-only).
    # The legacy `CXP_READ_ONLY_AGENTS` joins the read list.
    # There is NO default agent: the allowlist is the only source of agents.
    cfg.agent_readwrite = _split_list(env.get("CXP_AGENT_READWRITE", ""))
    cfg.agent_read = _split_list(env.get("CXP_AGENT_READ", ""))
    for a in cfg.read_only_agents:
        if a not in cfg.agent_read:
            cfg.agent_read.append(a)
    cfg.projects = _split_list(env.get("CXP_PROJECTS", ""))

    # Credentials: raw cookie > loose cookies > session file.
    if env.get("CXP_COOKIE"):
        cfg.cookies = _cookies_from_raw(env["CXP_COOKIE"])
    for name in REQUIRED_COOKIES:
        v = env.get(f"CXP_{name}")
        if v:
            cfg.cookies.setdefault(name, v)
    if not cfg.cookies and env.get("CXP_SESSION_FILE"):
        sf = Path(env["CXP_SESSION_FILE"]).expanduser()
        if sf.exists():
            cfg.cookies = _cookies_from_session_file(sf)
    cfg.sapisid = env.get("CXP_SAPISID", "").strip() or cfg.cookies.get("SAPISID", "")
    return cfg
