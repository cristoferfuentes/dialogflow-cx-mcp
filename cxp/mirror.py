"""El espejo de la API v3beta1.

El backend interno de la consola (`dialogflow.clients6.google.com`) sirve el
**mismo discovery doc** que la API publica, pero con la version que se le pida
(`v3beta1`, `v3alpha1`, `v3`). Eso permite dos cosas:

1. **Reflejo fiel**: el mapa de recursos y metodos sale del descriptor oficial
   (162 metodos / 32 recursos en v3beta1), no de una lista escrita a mano que
   se queda obsoleta.
2. **El agregado**: el backend interno es el unico que conserva
   `transitionRoutes[].targetPlaybook` (la API publica lo descarta en
   silencio). El proto publico no lo tiene -> hay que leer el discovery doc
   del backend, no el de `dialogflow.googleapis.com`.

Descripcion de los metodos:

    mirrors = Mirror(cfg)
    mirrors.method_ids()          # ['agents.list', 'agents.flows.train', ...]
    mirrors.spec("agents.flows.list")
    mirrors.url("agents.flows.list", parent="projects/p/locations/l/agents/a")
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import PKG_DIR, Config
from .httpclient import HttpStatusError, Session

DISCOVERY_HOST = "dialogflow.clients6.google.com"
CACHE_DIR = PKG_DIR / ".discovery"

_PLACEHOLDER = re.compile(r"\{(\+?)([A-Za-z0-9_.]+)\}")


class MirrorError(RuntimeError):
    pass


def _method_id(resource_path: str, method_name: str) -> str:
    """'projects.locations.agents.flows' + 'train' -> 'agents.flows.train'."""
    parts = resource_path.split(".")
    if parts[:2] == ["projects", "locations"]:
        parts = parts[2:]
    return ".".join(parts + [method_name])


class Mirror:
    """Mapa de la API (v3beta1 por defecto) tal y como lo declara el backend."""

    def __init__(self, cfg: Config, session: Session | None = None):
        self.cfg = cfg
        self.http = session or Session(timeout=cfg.socket_timeout, maxseg=cfg.tcp_maxseg,
                                        retries=cfg.http_retries,
                                        connect_timeout=cfg.connect_timeout)
        self._doc: dict[str, Any] | None = None

    # --------------------------------------------------------------- discovery
    def _cache_path(self, version: str) -> Path:
        return CACHE_DIR / f"{version}.json"

    def load(self, force: bool = False) -> dict[str, Any]:
        """Descarga (y cachea) el discovery doc del backend interno."""
        if self._doc is not None and not force:
            return self._doc
        version = self.cfg.api_version
        path = self._cache_path(version)
        if path.exists() and not force:
            try:
                self._doc = json.loads(path.read_text(encoding="utf-8"))
                return self._doc
            except ValueError:
                pass
        url = f"https://{DISCOVERY_HOST}/$discovery/rest?version={version}"
        from .console_api import sapisid_authorization
        headers = {
            "accept": "*/*",
            "authorization": sapisid_authorization(self.cfg.console_origin, self.cfg.sapisid),
            "cookie": self.cfg.cookie_header(),
            "referer": f"https://{DISCOVERY_HOST}/static/proxy.html?usegapi=1",
            "x-goog-authuser": "0",
            "x-origin": self.cfg.console_origin,
            "x-referer": self.cfg.console_origin,
        }
        status, text = self.http.request("GET", url, headers)
        if status >= 400:
            raise HttpStatusError(status, text)
        doc = json.loads(text)
        self._doc = doc
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return doc

    # ------------------------------------------------------------------ indice
    def _walk(self, node: dict, prefix: str = "") -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, res in (node or {}).items():
            path = f"{prefix}{name}"
            for mname, mdef in (res.get("methods") or {}).items():
                out[_method_id(path, mname)] = mdef
            out.update(self._walk(res.get("resources") or {}, f"{path}."))
        return out

    def index(self) -> dict[str, dict]:
        """Todos los metodos: id -> definicion."""
        doc = self.load()
        return self._walk(doc.get("resources") or {})

    def method_ids(self) -> list[str]:
        return sorted(self.index())

    def resources(self) -> dict[str, list[str]]:
        """recurso -> lista de metodos."""
        out: dict[str, list[str]] = {}
        for mid in self.method_ids():
            res = mid.rsplit(".", 1)[0]
            out.setdefault(res, []).append(mid)
        return {k: sorted(v) for k, v in sorted(out.items())}

    def spec(self, method_id: str) -> dict:
        idx = self.index()
        if method_id not in idx:
            raise MirrorError(
                f"metodo desconocido: {method_id!r}. "
                f"Usa cx_discover para ver el mapa."
            )
        spec = dict(idx[method_id])
        spec["id"] = method_id
        return spec

    def describe(self, method_id: str) -> dict:
        """Descripcion compacta de un metodo (para el agente)."""
        spec = self.spec(method_id)
        params = spec.get("parameters") or {}
        return {
            "id": method_id,
            "http": spec.get("httpMethod"),
            "path": spec.get("path"),
            "parametros": {
                k: {"en": v.get("location"), "obligatorio": bool(v.get("required")),
                    "tipo": v.get("type")}
                for k, v in params.items()
            },
            "cuerpo": spec.get("request"),
            "respuesta": spec.get("response"),
        }

    # -------------------------------------------------------------------- URL
    def url(self, method_id: str, *, name: str = "", parent: str = "",
            params: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None) -> tuple[str, str]:
        """Devuelve (url, http_method) para invocar el metodo."""
        spec = self.spec(method_id)
        template = spec.get("path") or ""
        http = spec.get("httpMethod") or "GET"
        values: dict[str, Any] = {}
        if name:
            values["name"] = name
        if parent:
            values["parent"] = parent
        values.update({k: v for k, v in (params or {}).items() if v is not None})

        missing: list[str] = []

        def _sub(match: re.Match) -> str:
            key = match.group(2)
            if key in values and values[key] not in (None, ""):
                return str(values[key])
            missing.append(key)
            return match.group(0)

        path = _PLACEHOLDER.sub(_sub, template)
        if missing:
            raise MirrorError(
                f"faltan parametros para {method_id}: {sorted(set(missing))} "
                f"(plantilla: {template})"
            )
        # La plantilla empieza por la version ("v3beta1/{+name}").
        path = re.sub(r"^v[0-9a-z]+/", "", path)
        if not path.startswith("/"):
            path = "/" + path

        from urllib.parse import urlencode
        q: dict[str, Any] = {"key": self.cfg.api_key}
        for k, v in (query or {}).items():
            if v is not None:
                q[k] = v
        # Los parametros de path que no se han usado tambien pueden ir en query
        # (p. ej. `languageCode`).
        return (f"https://{DISCOVERY_HOST}/{self.cfg.api_version}{path}?{urlencode(q)}",
                http)

    # ------------------------------------------------------------- shortcuts
    def resource_name(self, agent_id: str, *parts: str, project: str = "",
                      location: str = "") -> str:
        """Nombre completo de un recurso: projects/p/locations/l/agents/a/flows/f."""
        project = project or self.cfg.project
        location = location or self.cfg.location
        base = f"projects/{project}/locations/{location}/agents/{agent_id}"
        for p in parts:
            if p:
                base += "/" + p
        return base
