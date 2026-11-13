"""Autenticacion: token OAuth (gcloud) o primera parte de la consola (SAPISIDHASH).

La consola "Conversational Agents" habla con `dialogflow.clients6.google.com` con
`googleapis.config/auth/useFirstPartyAuth` + `client/cors`: o sea, con las cookies
de Google del navegador (UDS) y NO con un bearer token.

`SAPISIDHASH` es el mecanismo de UDS:
    ts = epoch_seconds
    h  = sha1(f"{ts} {sapisid} {origin}")
    Authorization: SAPISIDHASH {ts}_{h}
"""
from __future__ import annotations

import hashlib
import subprocess
import time


def gcloud_access_token(timeout: float = 30.0) -> str | None:
    """Token de ADC via gcloud. None si no hay gcloud o no hay sesion."""
    try:
        out = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    tok = (out.stdout or "").strip()
    return tok or None


def sapisid_hash(origin: str, sapisid: str, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"{ts}_{digest}"


def first_party_headers(origin: str, sapisid: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Cabeceras con las que la consola llama a su backend."""
    headers = {
        "Authorization": f"SAPISIDHASH {sapisid_hash(origin, sapisid)}",
        "X-Goog-AuthUser": "0",
        "x-goog-api-client": "gl-node/20 js-node/20",
        "Origin": origin,
        "Referer": f"{origin}/",
        "X-Same-Domain": "1",
    }
    if extra:
        headers.update(extra)
    return headers


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)


def pick_sapisid(cookie_list: list[dict]) -> str | None:
    """Elige el SAPISID usable: __Secure-3PSID > SAPISID > __Secure-1PSID."""
    by_name = {c.get("name"): c.get("value") for c in cookie_list}
    for name in ("__Secure-3PSID", "SAPISID", "__Secure-1PSID", "APISID"):
        val = by_name.get(name)
        if val:
            return val
    return None


def pick_google_cookies(cookie_list: list[dict]) -> dict[str, str]:
    """Cookies de autenticacion de Google que hay que reenviar al backend."""
    wanted = (
        "__Secure-3PSID",
        "SAPISID",
        "__Secure-1PSID",
        "APISID",
        "ACCOUNT_CHOOSER",
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "NID",
    )
    out: dict[str, str] = {}
    for c in cookie_list:
        name = c.get("name")
        if name in wanted and c.get("value"):
            out[name] = c["value"]
    return out
