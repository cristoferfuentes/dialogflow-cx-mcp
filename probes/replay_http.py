#!/usr/bin/env python3
"""Replica (HTTP puro, sin navegador) de una peticion al backend interno de la
consola, usando EXACTAMENTE las cabeceras de cable que se han capturado.

El truco esta en `x-browser-validation` / `x-browser-year`: la consola los
manda y el backend los exige para validar la credencial de primera parte
(sin ellos -> HTTP 401 "invalid authentication credentials").

Uso: .venv/bin/python replay_http.py [--agent clon] [--write]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.config import load_config

from _entorno import agentes_de_sonda
from cxp.console_api import CONSOLE_HOST, sapisid_authorization
from cxp.httpclient import Session

AGENTS = agentes_de_sonda()

# Cabeceras que el navegador manda SIEMPRE en esta API (capturadas de cable).
BROWSER_HEADERS = {
    "accept": "*/*",
    "accept-language": "es-419,es;q=0.9,es;q=0.9;q=0.8",
    "sec-ch-ua": '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "Linux",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/157.0.0.0 Safari/537.36"),
    "x-browser-channel": "stable",
    "x-browser-copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
    "x-browser-year": "2026",
}


def browser_fingerprint(cfg) -> tuple[dict[str, str], str]:
    """(cabeceras del navegador, cookie header) tomados del Chrome real."""
    from cxp.chrome_cdp import RealChrome

    ch = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir, port=cfg.cdp_port)
    page = ch.connect()
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")

    extra: dict[str, str] = {}
    cookies: list[dict] = []

    def on_extra(ev):
        h = ev.get("headers") or {}
        for k in ("x-browser-validation", "x-browser-year", "x-browser-channel",
                  "x-browser-copyright"):
            if k in h:
                extra[k] = h[k]

    def on_cookie(ev):
        for c in (ev.get("associatedCookies") or []):
            ck = c.get("cookie") or {}
            if ck.get("name"):
                cookies.append(ck)

    cdp.on("Network.requestWillBeSentExtraInfo", on_extra)
    cdp.on("Network.requestWillBeSentExtraInfo", on_cookie)

    # Se dispara una peticion cualquiera al backend para que el navegador
    # revele sus cabeceras de validacion.
    page.goto(f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1",
              wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)
    try:
        page.evaluate(
            """async (u) => { try { await fetch(u, {credentials:'include'}); } catch (e) {} }""",
            f"https://{CONSOLE_HOST}/v3alpha1/projects/{cfg.project}/locations/global/agents"
            f"/{AGENTS['clon'][0]}?key={cfg.api_key}",
        )
    except Exception:
        pass
    time.sleep(2)

    live = {c["name"]: c["value"] for c in page.context.cookies() if c.get("value")}
    cookie_header = "; ".join(f"{k}={v}" for k, v in live.items())
    return extra, cookie_header


def build_headers(cfg, cookie_header: str, extra: dict[str, str]) -> dict[str, str]:
    state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    by_name = {c.get("name"): c.get("value") for c in state.get("cookies", []) if c.get("value")}
    sapisid = by_name.get("SAPISID") or by_name.get("__Secure-3PSID")
    ts = int(time.time())
    headers = {k: v for k, v in BROWSER_HEADERS.items()}
    headers.update(extra)
    headers["authorization"] = sapisid_authorization(cfg.console_origin, sapisid, ts)
    headers["x-goog-authuser"] = "0"
    headers["x-goog-encode-response-if-executable"] = "base64"
    headers["x-javascript-user-agent"] = "google-api-javascript-client/1.1.0"
    headers["x-origin"] = cfg.console_origin
    headers["x-referer"] = cfg.console_origin
    headers["x-requested-with"] = "XMLHttpRequest"
    headers["referer"] = f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1"
    headers["cookie"] = cookie_header
    return headers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="clon", choices=list(AGENTS))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cfg = load_config()
    print("== sacando la huella del navegador real...", flush=True)
    extra, cookie_header = browser_fingerprint(cfg)
    print(f"   x-browser-validation: {extra.get('x-browser-validation')}", flush=True)
    print(f"   cookies: {len(cookie_header.split('; '))}", flush=True)

    headers = build_headers(cfg, cookie_header, extra)
    a, f, pg = AGENTS[args.agent]
    url = (f"https://{CONSOLE_HOST}/v3alpha1/projects/{cfg.project}/locations/global/agents/{a}"
           f"/flows/{f}/pages/{pg}?" + urlencode({"key": cfg.api_key, "languageCode": "es"}))

    http = Session(timeout=60.0, maxseg=cfg.tcp_maxseg, retries=1)
    t = time.time()
    status, body = http.request("GET", url, headers)
    print(f"\n== HTTP {status} en {time.time()-t:.1f}s", flush=True)
    if status == 200:
        data = json.loads(body)
        print("   etag:", data.get("etag"))
        for r in data.get("transitionRoutes") or []:
            print(f"   - {r.get('name','').split('/')[-1]}  targetPlaybook="
                  f"{str(r.get('targetPlaybook')).split('/')[-1]}")
        if args.out:
            Path(args.out).write_text(body, encoding="utf-8")
            print("   escrito", args.out)
    else:
        print("   ", " ".join(body.split())[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
