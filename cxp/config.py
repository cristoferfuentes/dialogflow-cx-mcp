"""Configuración del MCP, leída de un `.env` (sin dependencias externas).

Todo lo que el MCP necesita son **credenciales**, no un navegador:

    CXP_PROJECT=<proyecto>
    CXP_LOCATION=global
    CXP_QUOTA_PROJECT=<proyecto de cuota>   # opcional: si no, es CXP_PROJECT
    CXP_CONSOLE_ORIGIN=https://conversational-agents.cloud.google.com
    CXP_API_KEY=AIzaSy...
    CXP_COOKIE="SAPISID=...; SID=...; HSID=...; SSID=...; APISID=..."
    CXP_DEFAULT_AGENT=<uuid del agente con el que se trabaja>

    # --- PERMISOS POR AGENTE (lista blanca) --------------------------------
    # Un agente sin entrada aqui NO es accesible en absoluto.
    CXP_AGENT_READ=<uuid>,<uuid>          # leer (GET/list)
    CXP_AGENT_READWRITE=<uuid>            # leer + escribir (POST/PATCH/DELETE)
    CXP_PROJECTS=<proyecto>,...           # opcional: limita los proyectos

    CXP_READ_ONLY_AGENTS=<uuid>,<uuid>     # (legacy) se une a CXP_AGENT_READ
    CXP_SESSION_FILE=<ruta a un storage_state .json>   # alternativa a CXP_COOKIE

Las entradas de permiso admiten dos formas: `<uuid>` (el permiso viaja con el
uuid, vale en cualquier proyecto) o `<proyecto>/<uuid>` (empareja proyecto y
agente). El MCP trabaja por defecto contra `v3beta1` (`CXP_API_VERSION`).

El `.env` se busca en: (1) la variable `CXP_ENV_FILE`, (2) el directorio del
paquete, (3) el directorio de trabajo, (4) `~/.config/cx-playbook.env`.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent  # mcp-cx-playbook/

# Las 5 cookies que el backend interno EXIGE (medido: con 4 de 5 -> HTTP 401).
REQUIRED_COOKIES = ("SAPISID", "SID", "HSID", "SSID", "APISID")


def parse_env(text: str) -> dict[str, str]:
    """Parser minimo de `.env`: KEY=VALUE, comillas opcionales, `#` comments."""
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
    # Version del proto del backend interno. v3beta1 es la que hay que usar
    # (en `v3` no existen campos como `llmModelSettings.parameters` y se
    # descartan en silencio).
    api_version: str = "v3beta1"
    # Proyecto de cuota (`x-goog-user-project`). Viene de `CXP_QUOTA_PROJECT` y,
    # si no se pone, es el propio `CXP_PROJECT`. Es un dato de DESPLEGUE: el
    # MCP no debe llevar ninguno fijado en el codigo.
    quota_project: str = ""
    # Credenciales
    cookies: dict[str, str] = field(default_factory=dict)
    sapisid: str = ""
    # Agentes
    default_agent: str = ""
    read_only_agents: list[str] = field(default_factory=list)  # legacy
    # Permisos (lista blanca)
    agent_read: list[str] = field(default_factory=list)
    agent_readwrite: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    # Ruta del fichero de credenciales (informativo)
    env_file: Path | None = None

    # Red
    socket_timeout: float = 45.0        # timeout de LECTURA
    connect_timeout: float = 8.0         # timeout de CONNECT (IPs fijadas en /etc/hosts)
    tcp_maxseg: int = 1200
    http_retries: int = 1
    # TTL del cache de catalogos (id->displayName de flows/pages/playbooks).
    # Los catalogos cambian muy poco y sus payloads son enormes (list_playbooks
    # del clon = 770 KB) -> se cachean. Se invalidan al escribir.
    catalog_ttl: float = 120.0

    # --- Solo para el helper de login (FUERA del MCP) -----------------------
    # El MCP no usa nada de esto: vive en el package por comodidad de imports.
    display: str = ":99"
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

    # ---------------------------------------------------------------- agentes
    def is_read_only(self, agent_id: str) -> bool:
        """Legacy: el agente esta en la lista de solo lectura."""
        return agent_id in self.read_only_agents

    # ---------------------------------------------------------------- helpers
    @property
    def console_backend(self) -> str:
        return "https://dialogflow.clients6.google.com"

    def agent_base(self, agent_id: str) -> str:
        return (f"{self.console_backend}/{self.api_version}/projects/{self.project}"
                f"/locations/{self.location}/agents/{agent_id}")


def _cookies_from_raw(raw: str) -> dict[str, str]:
    """'SAPISID=..; SID=..' -> dict (solo las cookies que nos interesan)."""
    out: dict[str, str] = {}
    for parte in raw.split(";"):
        if "=" not in parte:
            continue
        k, _, v = parte.strip().partition("=")
        if k in REQUIRED_COOKIES and v:
            out[k] = v
    return out


def _cookies_from_session_file(path: Path) -> dict[str, str]:
    """Extrae las 5 cookies de un `storage_state` de Playwright (.google.com)."""
    state = json.loads(path.read_text(encoding="utf-8"))
    by_name: dict[str, str] = {}
    for c in state.get("cookies", []):
        dom = (c.get("domain") or "").lstrip(".").lower()
        if dom == "google.com" or dom.endswith(".google.com"):
            if c.get("name") in REQUIRED_COOKIES and c.get("value"):
                by_name[c["name"]] = c["value"]
    return by_name


def _split_list(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def load_config(env_file: str | os.PathLike | None = None) -> Config:
    """Carga la configuracion del `.env` (o de las variables `CXP_*`)."""
    env: dict[str, str] = {}
    path = Path(env_file) if env_file else find_env_file()
    if path and path.exists():
        env.update(parse_env(path.read_text(encoding="utf-8")))
    # Las variables de entorno tienen prioridad sobre el fichero.
    for k in list(env) + [f"CXP_{n}" for n in (
        "PROJECT", "LOCATION", "CONSOLE_ORIGIN", "API_KEY", "COOKIE", "SAPISID",
        "SID", "HSID", "SSID", "APISID", "DEFAULT_AGENT", "READ_ONLY_AGENTS",
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
    # Proyecto de cuota: `CXP_QUOTA_PROJECT` y, si no se pone, el proyecto
    # principal. (Lo usan el helper de login y el cliente de la API publica,
    # que NO son el camino principal del MCP.)
    cfg.quota_project = env.get("CXP_QUOTA_PROJECT", "").strip() or cfg.project
    # Solo para el helper de login (FUERA del MCP).
    cfg.display = env.get("CXP_DISPLAY", ":99").strip() or ":99"
    cfg.novnc_url = env.get("CXP_NOVNC_URL", "").strip()
    cfg.default_agent = env.get("CXP_DEFAULT_AGENT", "")
    cfg.read_only_agents = _split_list(env.get("CXP_READ_ONLY_AGENTS", ""))

    # --- Permisos: lista blanca de agentes ---------------------------------
    # `CXP_AGENT_READWRITE` (leer + escribir) y `CXP_AGENT_READ` (solo leer).
    # El legacy `CXP_READ_ONLY_AGENTS` se une a la lista de lectura.
    cfg.agent_readwrite = _split_list(env.get("CXP_AGENT_READWRITE", ""))
    cfg.agent_read = _split_list(env.get("CXP_AGENT_READ", ""))
    for a in cfg.read_only_agents:
        if a not in cfg.agent_read:
            cfg.agent_read.append(a)
    # El agente por defecto siempre es accesible (al menos, para leer).
    if cfg.default_agent:
        if cfg.default_agent not in cfg.agent_read and \
                cfg.default_agent not in cfg.agent_readwrite:
            cfg.agent_read.append(cfg.default_agent)
    cfg.projects = _split_list(env.get("CXP_PROJECTS", ""))

    # Credenciales: cookie cruda > cookies sueltas > fichero de sesion.
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
    cfg.sapisid = cfg.cookies.get("SAPISID", "")
    return cfg
