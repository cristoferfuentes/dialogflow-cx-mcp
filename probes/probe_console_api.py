#!/usr/bin/env python3
"""Prueba la API INTERNA de la consola (clients6/v3alpha1) con la sesion de
primera parte (cookies de Google + SAPISIDHASH + API key).

Objetivo: comprobar si `TransitionRoute` trae `targetPlaybook` y recuperar el
mapa ruta -> playbook del agente ORIGINAL (solo lectura).

Uso:
  .venv/bin/python probe_console_api.py [--agent original|clon] [--flow ID] [--page ID]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.auth import first_party_headers, pick_google_cookies, pick_sapisid
from cxp.config import load_config

from _entorno import agentes_de_sonda, targets_de_sonda
from cxp.httpclient import HttpStatusError, Session

API_KEY = "AIzaSyD1dO8oRagJkmtkSJ9oLI289jIT8M4Zk5s"

AGENTS = agentes_de_sonda()
# Los flows y las paginas tambien salen del entorno:
#   CXP_PROBE_TARGETS="por_defecto=<agente>,<flow>,<pagina>"
_TARGETS = targets_de_sonda()
FLOWS = {k: v["flow"] for k, v in _TARGETS.items()}
PAGES = {k: v["page"] for k, v in _TARGETS.items()}


def build_headers(cfg) -> dict:
    state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    cookies = state.get("cookies", [])
    sapisid = pick_sapisid(cookies)
    if not sapisid:
        raise SystemExit("sin SAPISID en .session-state.json: no hay sesion de Google")
    headers = first_party_headers(cfg.console_origin, sapisid)
    extra = pick_google_cookies(cookies)
    if extra:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in extra.items())
    headers["Accept"] = "application/json"
    headers["x-goog-user-project"] = cfg.quota_project
    return headers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="original", choices=list(AGENTS))
    ap.add_argument("--flow", default="")
    ap.add_argument("--page", default="")
    ap.add_argument("--version", default="v3alpha1")
    ap.add_argument("--host", default="dialogflow.clients6.google.com")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cfg = load_config()
    headers = build_headers(cfg)
    agent = AGENTS[args.agent]
    flow = args.flow or FLOWS[args.agent]
    page = args.page or PAGES[args.agent]

    http = Session(timeout=60.0, maxseg=cfg.tcp_maxseg, retries=1)
    base = f"https://{args.host}/{args.version}/projects/{cfg.project}/locations/{cfg.location}/agents/{agent}"

    targets = [
        ("flow", f"{base}/flows/{flow}"),
        ("page", f"{base}/flows/{flow}/pages/{page}"),
    ]
    out: dict = {}
    for label, url in targets:
        full = url + f"?key={API_KEY}"
        try:
            data = http.json("GET", full, headers)
            print(f"== {label}: OK  {len(json.dumps(data))} B", flush=True)
            out[label] = data
        except HttpStatusError as exc:
            print(f"== {label}: HTTP {exc.status}  {exc.body[:300]}", flush=True)
            out[label] = {"_status": exc.status, "_body": exc.body[:2000]}
        except Exception as exc:
            print(f"== {label}: {type(exc).__name__}: {exc}", flush=True)
            out[label] = {"_error": f"{type(exc).__name__}: {exc}"}

    # Resumen de las rutas y su destino
    for label in ("flow", "page"):
        data = out.get(label) or {}
        routes = data.get("transitionRoutes") or []
        if not routes:
            continue
        print(f"\n== {label}: {len(routes)} transition routes", flush=True)
        for r in routes:
            keys = sorted(r.keys())
            target = r.get("targetPlaybook") or r.get("targetFlow") or r.get("targetPage")
            print(f"   {r.get('name','').split('/')[-1]}  cond={str(r.get('condition'))[:70]}", flush=True)
            print(f"      destino={target}  claves={keys}", flush=True)

    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\n== escrito en {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
