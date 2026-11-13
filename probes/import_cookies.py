"""Importa cookies de sesion de Google al perfil persistente del navegador.

Por que existe: en esta maquina el navegador NO puede completar el flujo de
login de `accounts.google.com` (medido: el handshake se queda colgado), pero
las cookies de sesion son portables. El usuario puede exportar las cookies de
SU Chrome de escritorio (donde si entra a Google, con su VPN) y el MCP las
carga aqui para operar con primera parte (SAPISIDHASH).

Uso:
    .venv/bin/python import_cookies.py <cookies.json> [--url <url de comprobacion>]

Formatos aceptados para <cookies.json>:
  1. Lista de objetos estilo Playwright:
     [{"name":"__Secure-3PSID","value":"...","domain":".google.com","path":"/"}, ...]
  2. El volcado de Playwright `storage_state`:
     {"cookies":[{"name":...,"value":...,"domain":...}], "origins":[...]}
  3. Un objeto simple {"__Secure-3PSID": "...", "SAPISID": "..."}
     (se asume domain=".google.com", path="/")
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.auth import pick_sapisid
from cxp.browser import ConsoleBrowser
from cxp.config import load_config

# Cookies que hacen falta para la primera parte (UDS) de Google.
NEEDED = ("__Secure-3PSID", "SAPISID", "__Secure-1PSID", "SID", "HSID", "SSID", "APISID")


def normalize(raw: object) -> list[dict]:
    """Convierte cualquiera de los 3 formatos admitidos a la lista de Playwright."""
    cookies: list[dict] = []
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]
    if isinstance(raw, dict):
        # {nombre: valor}
        for name, value in raw.items():
            cookies.append({"name": name, "value": value, "domain": ".google.com", "path": "/"})
        return cookies
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".google.com"
        item = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", c.get("http_only", True))),
        }
        if c.get("expires") or c.get("expirationDate"):
            item["expires"] = float(c.get("expires") or c.get("expirationDate"))
        if c.get("sameSite"):
            ss = str(c["sameSite"]).lower()
            item["sameSite"] = {"strict": "Strict", "lax": "Lax", "none": "None"}.get(ss, "Lax")
        cookies.append(item)
    return cookies


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cookies", help="fichero JSON con las cookies")
    ap.add_argument("--url", default="https://conversational-agents.cloud.google.com/")
    ap.add_argument("--keep-open", action="store_true", help="deja el navegador abierto")
    args = ap.parse_args()

    raw = json.loads(Path(args.cookies).read_text(encoding="utf-8"))
    cookies = normalize(raw)
    found = {c["name"] for c in cookies} & set(NEEDED)
    print(f"== cookies leidas: {len(cookies)}; de sesion: {sorted(found)}", flush=True)
    if not found:
        print("== AVISO: no hay ninguna cookie de sesion de Google en el fichero", flush=True)

    cfg = load_config()
    br = ConsoleBrowser(cfg)
    br._launch(headless=True)
    br._ctx.add_cookies(cookies)
    print(f"== inyectadas {len(cookies)} cookies en el perfil", flush=True)

    try:
        br.open_console(args.url, wait_ms=3000)
    except Exception as exc:
        print(f"== la navegacion fallo: {type(exc).__name__}: {str(exc)[:90]}", flush=True)

    ok, reason, email = br._looks_logged_in()
    print(f"== sesion: logged_in={ok}  ({reason})  email={email}", flush=True)

    sap = pick_sapisid(br.cookies())
    if sap:
        print("== SAPISID disponible: la primera parte (UDS) va a funcionar", flush=True)
        try:
            br._ctx.storage_state(path=str(cfg.state_file))
            print(f"== estado guardado en {cfg.state_file}", flush=True)
        except Exception as exc:
            print(f"== no se pudo guardar el estado: {exc}", flush=True)

    if args.keep_open:
        print("== navegador abierto; pulsa Ctrl+C para salir", flush=True)
        try:
            while True:
                br._page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            pass
    br.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
