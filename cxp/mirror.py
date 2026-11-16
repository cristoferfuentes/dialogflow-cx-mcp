"""The API v3beta1 mirror.

The console's internal backend (`dialogflow.clients6.google.com`) serves the
**same discovery doc** as the public API, but with whichever version is requested
(`v3beta1`, `v3alpha1`, `v3`). That enables two things:

1. **Faithful reflection**: the map of resources and methods comes from the official descriptor
   (162 methods / 32 resources in v3beta1), not from a hand-written list that
   goes stale.
2. **The aggregate**: the internal backend is the only one that keeps
   `transitionRoutes[].targetPlaybook` (the public API drops it
   silently). The public proto does not have it -> you must read the backend's discovery doc,
   not `dialogflow.googleapis.com`'s.

Method description:

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
    """Map of the API (v3beta1 by default) as declared by the backend."""

    def __init__(self, cfg: Config, session: Session | None = None):
        self.cfg = cfg
        self.http = session or Session(timeout=cfg.socket_timeout, maxseg=cfg.tcp_maxseg,
                                        retries=cfg.http_retries,
                                        connect_timeout=cfg.connect_timeout)
        self._docs: dict[str, dict[str, Any]] = {}

    # --------------------------------------------------------------- discovery
    def _cache_path(self, version: str) -> Path:
        return CACHE_DIR / f"{version}.json"

    def load(self, force: bool = False, version: str = "") -> dict[str, Any]:
        """Download (and cache) the discovery doc of the internal backend.

        The backend serves `v3beta1`, `v3alpha1`, `v3` and `v2beta1` with the same
        discovery doc that API publishes: that is why a specific version
        can be requested and not only the one from `cfg.api_version`."""
        version = version or self.cfg.api_version
        if version in self._docs and not force:
            return self._docs[version]
        path = self._cache_path(version)
        if path.exists() and not force:
            try:
                self._docs[version] = json.loads(path.read_text(encoding="utf-8"))
                return self._docs[version]
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
        self._docs[version] = doc
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return doc

    # ------------------------------------------------------------------ index
    def _walk(self, node: dict, prefix: str = "") -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, res in (node or {}).items():
            path = f"{prefix}{name}"
            for mname, mdef in (res.get("methods") or {}).items():
                out[_method_id(path, mname)] = mdef
            out.update(self._walk(res.get("resources") or {}, f"{path}."))
        return out

    def index(self, version: str = "") -> dict[str, dict]:
        """All methods: id -> definition."""
        doc = self.load(version=version)
        return self._walk(doc.get("resources") or {})

    def method_ids(self, version: str = "") -> list[str]:
        return sorted(self.index(version))

    def resources(self, version: str = "") -> dict[str, list[str]]:
        """resource -> list of methods."""
        out: dict[str, list[str]] = {}
        for mid in self.method_ids(version):
            res = mid.rsplit(".", 1)[0]
            out.setdefault(res, []).append(mid)
        return {k: sorted(v) for k, v in sorted(out.items())}

    def spec(self, method_id: str, version: str = "") -> dict:
        idx = self.index(version)
        if method_id not in idx:
            raise MirrorError(
                f"metodo desconocido: {method_id!r}. "
                f"Usa cx_discover para ver el mapa."
            )
        spec = dict(idx[method_id])
        spec["id"] = method_id
        return spec

    def describe(self, method_id: str, version: str = "") -> dict:
        """Compact description of a method (for the agent)."""
        spec = self.spec(method_id, version)
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

    # URL
    def url(self, method_id: str, *, name: str = "", parent: str = "",
            params: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
            version: str = "") -> tuple[str, str]:
        """Returns (url, http_method) to invoke the method.

        `version` allows requesting the method in another version of the proto served
        by the backend (`v3beta1`, `v3alpha1`, `v3`)."""
        version = version or self.cfg.api_version
        spec = self.spec(method_id, version)
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
        # The template starts with the version ("v3beta1/{+name}").
        path = re.sub(r"^v[0-9a-z]+/", "", path)
        if not path.startswith("/"):
            path = "/" + path

        from urllib.parse import urlencode
        q: dict[str, Any] = {"key": self.cfg.api_key}
        for k, v in (query or {}).items():
            if v is not None:
                q[k] = v
        # Path parameters that were not used can also go in the query
        # (e.g. `languageCode`).
        # MEASURED (2026-11-15): the host depends on the LOCATION of the path: for
        # `global` it is the global host and for the rest, the regional host
        # (`{location}-dialogflow.clients6.google.com`).
        from .console_api import host_de_path
        return (f"https://{host_de_path(path)}/{version}{path}?{urlencode(q)}",
                http)

    # ------------------------------------------------------------- shortcuts
    def resource_name(self, agent_id: str, *parts: str, project: str = "",
                      location: str = "") -> str:
        """Full name of a resource: projects/p/locations/l/agents/a/flows/f."""
        project = project or self.cfg.project
        location = location or self.cfg.location
        base = f"projects/{project}/locations/{location}/agents/{agent_id}"
        for p in parts:
            if p:
                base += "/" + p
        return base
