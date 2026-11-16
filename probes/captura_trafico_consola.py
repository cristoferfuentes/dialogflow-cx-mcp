#!/usr/bin/env python3
"""Captures the REAL traffic of the Dialogflow CX console (CDP, without MITM).

What it is for: the public discovery doc and the internal backend one are THE SAME,
and the public proto drops fields that the backend keeps. To know **what the console
really sends** (endpoints and bodies) you have to watch it work.

Saves, for every request to the internal backend:
  * method, URL and query
  * the BODY of the request (postData)

Afterwards, `probes/analiza_trafico_consola.py` compares them with the discovery doc:
  * endpoints the console calls and the discovery doc does NOT declare
  * body fields the public proto does NOT declare

Usage:
    CXP_PROBE_TARGETS="por_defecto=<agent>,<flow>,<page>" \\
        .venv/bin/python probes/captura_trafico_consola.py --out /tmp/trafico.json
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import targets_de_sonda
from _redact import escribe_json_privado

from cxp.chrome_cdp import RealChrome
from cxp.config import load_config

BACKEND = "dialogflow.clients6.google.com"
CONSOLE_URL = (
    "https://conversational-agents.cloud.google.com/projects/{p}/locations/{l}/agents/{a}"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/trafico_consola.json")
    ap.add_argument("--wait", type=int, default=25000)
    ap.add_argument("--url", default="")
    args = ap.parse_args()

    cfg = load_config()
    objetivos = targets_de_sonda()
    if not objetivos:
        print('no targets: set CXP_PROBE_TARGETS="name=<agent>,<flow>,<page>"')
        return 1
    objetivo = next(iter(objetivos.values()))
    agente = objetivo["agent"]
    url = args.url or CONSOLE_URL.format(p=cfg.project, l=cfg.location, a=agente)
    print("URL:", url)

    chrome = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir,
                         port=cfg.cdp_port)
    chrome.launch(url)
    page = chrome.connect()
    ctx = page.context
    print("chrome opened; waiting", args.wait, "ms")

    peticiones: list[dict] = []

    def _on_request(ev):
        req = ev.get("request") or {}
        u = req.get("url") or ""
        if BACKEND not in u:
            return
        p = urlparse(u)
        post = req.get("postData")
        if isinstance(post, dict):
            post = post.get("text")
        peticiones.append({
            "metodo": req.get("method"),
            "ruta": p.path,
            "query": p.query,
            "url": u,
            "cuerpo": post,
            "cabeceras": req.get("headers") or {},
        })

    # One CDP session per frame: the /cx/ iframe goes on its own.
    sesiones = []
    for frame in list(page.frames):
        try:
            s = ctx.new_cdp_session(frame)
            s.send("Network.enable")
            s.on("Network.requestWillBeSent", _on_request)
            sesiones.append(s)
        except Exception as exc:
            print("  frame without session:", exc)
    try:
        s = ctx.new_cdp_session(page)
        s.send("Network.enable")
        s.on("Network.requestWillBeSent", _on_request)
        sesiones.append(s)
    except Exception as exc:
        print("  page without session:", exc)

    time.sleep(args.wait / 1000)

    # Reload so the hook is active before the requests.
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception as exc:
        print("could not reload:", exc)
    time.sleep(args.wait / 1000)

    escribe_json_privado(args.out, peticiones)
    print(f"backend requests captured: {len(peticiones)} -> {args.out}")
    for p in peticiones[:40]:
        print(f"  {p['metodo']:<6} {p['ruta']}")

    try:
        chrome.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
