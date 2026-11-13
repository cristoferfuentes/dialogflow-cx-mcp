"""Acceso a la API de Dialogflow CX por los dos backends.

* `public`  -> global-dialogflow.googleapis.com  (proto PUBLICO: TransitionRoute
                NO tiene targetPlaybook; el campo se descarta en silencio).
* `console` -> dialogflow.clients6.google.com    (proto INTERNO de la consola:
                TransitionRoute SI tiene targetPlaybook).

Autenticacion: bearer token de gcloud (ADC) o primera parte (SAPISIDHASH) de la
sesion de la consola.
"""
from __future__ import annotations

from .auth import gcloud_access_token
from .config import Config
from .httpclient import Session


class CxApi:
    def __init__(self, cfg: Config, session: Session | None = None):
        self.cfg = cfg
        self.http = session or Session(
            timeout=cfg.socket_timeout, maxseg=cfg.tcp_maxseg, retries=cfg.http_retries
        )
        self._token: str | None = None
        self._extra_headers: dict[str, str] = {}

    # ------------------------------------------------------------- auth
    def use_bearer(self) -> bool:
        self._token = gcloud_access_token()
        self._extra_headers = {}
        return bool(self._token)

    def use_first_party(self, headers: dict[str, str]) -> None:
        """Autenticacion de primera parte (la de la consola)."""
        self._token = None
        self._extra_headers = dict(headers)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "x-goog-user-project": self.cfg.quota_project,
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        headers.update(self._extra_headers)
        if extra:
            headers.update(extra)
        return headers

    def _base(self, agent_id: str, backend: str) -> str:
        return self.cfg.agent_base(agent_id, backend)

    # ------------------------------------------------------------- lecturas
    def list_agents(self, backend: str = "public") -> list[dict]:
        url = f"{self._base('x', backend)}".replace("/agents/x", "/agents")
        data = self.http.json("GET", f"{url}?pageSize=200", self._headers())
        return data.get("agents", [])  # type: ignore[union-attr]

    def list_flows(self, agent_id: str, backend: str = "public") -> list[dict]:
        url = f"{self._base(agent_id, backend)}/flows?pageSize=500"
        data = self.http.json("GET", url, self._headers())
        return data.get("flows", [])  # type: ignore[union-attr]

    def list_pages(self, agent_id: str, flow_id: str, backend: str = "public") -> list[dict]:
        url = f"{self._base(agent_id, backend)}/flows/{flow_id}/pages?pageSize=500"
        data = self.http.json("GET", url, self._headers())
        return data.get("pages", [])  # type: ignore[union-attr]

    def get_page(self, agent_id: str, flow_id: str, page_id: str, backend: str = "public") -> dict:
        url = f"{self._base(agent_id, backend)}/flows/{flow_id}/pages/{page_id}"
        return self.http.json("GET", url, self._headers())  # type: ignore[return-value]

    def list_playbooks(self, agent_id: str, backend: str = "public") -> list[dict]:
        url = f"{self._base(agent_id, backend)}/playbooks?pageSize=500"
        data = self.http.json("GET", url, self._headers())
        return data.get("playbooks", [])  # type: ignore[union-attr]

    # ------------------------------------------------------------- escritura
    def update_page(
        self,
        agent_id: str,
        flow_id: str,
        page_id: str,
        body: dict,
        update_mask: str,
        backend: str = "public",
        timeout: float | None = None,
        language_code: str | None = None,
    ) -> dict:
        url = (
            f"{self._base(agent_id, backend)}/flows/{flow_id}/pages/{page_id}"
            f"?updateMask={update_mask}"
        )
        if language_code:
            url += f"&languageCode={language_code}"
        return self.http.json(  # type: ignore[return-value]
            "PATCH", url, self._headers(), body, timeout=timeout
        )

    # ------------------------------------------------------------- resolucion
    def resolve_flow(self, agent_id: str, flow: str, backend: str = "public") -> str:
        flows = self.list_flows(agent_id, backend)
        for f in flows:
            if f["name"].endswith(f"/{flow}") or f.get("displayName") == flow:
                return f["name"].split("/")[-1]
        raise LookupError(f"flow no encontrado: {flow}")

    def resolve_page(self, agent_id: str, flow_id: str, page: str, backend: str = "public") -> str:
        pages = self.list_pages(agent_id, flow_id, backend)
        for p in pages:
            if p["name"].endswith(f"/{page}") or p.get("displayName") == page:
                return p["name"].split("/")[-1]
        raise LookupError(f"pagina no encontrada: {page}")

    def resolve_playbook(self, agent_id: str, playbook: str, backend: str = "public") -> str:
        pbs = self.list_playbooks(agent_id, backend)
        for p in pbs:
            if p["name"].endswith(f"/{playbook}") or p.get("displayName") == playbook:
                return p["name"].split("/")[-1]
        raise LookupError(f"playbook no encontrado: {playbook}")
