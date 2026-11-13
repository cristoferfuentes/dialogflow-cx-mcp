#!/usr/bin/env python3
"""Captura el payload INTERNO del flow (con targetPlaybook) usando la sesion
CDP cruda (los eventos `response` de Playwright no se disparan sobre un
contexto conectado por CDP).

Uso: .venv/bin/python capture_flow_payload.py --agent original [--out /tmp/x.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.browser import ConsoleBrowser
from cxp.config import load_config

from _entorno import agentes_de_sonda

AGENTS = agentes_de_sonda()
FRAME_URL = (
    "https://conversational-agents.cloud.google.com/cx/projects/{p}/locations/{l}/agents/{a}"
    "/flows/{f}?pageId={pg}&embedded=true"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="original", choices=list(AGENTS))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cfg = load_config()
    a, f, pg = AGENTS[args.agent]
    url = FRAME_URL.format(p=cfg.project, l=cfg.location, a=a, f=f, pg=pg)

    br = ConsoleBrowser(cfg)
    br.use_real_chrome = True
    br._launch(headless=False)
    page = br._page

    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    reqs: dict[str, str] = {}
    bodies: list[tuple[str, str]] = []

    def on_received(ev):
        r = ev.get("response", {})
        u = r.get("url", "")
        if "clients6.google.com" in u:
            reqs[ev["requestId"]] = u

    def on_finished(ev):
        rid = ev.get("requestId")
        u = reqs.get(rid or "")
        if not u or "/flows/" not in u:
            return
        try:
            res = cdp.send("Network.getResponseBody", {"requestId": rid})
            body = res.get("body", "")
        except Exception as exc:
            print(f"== no se pudo leer el cuerpo de {u[:80]}: {exc}", flush=True)
            return
        if "transitionRoutes" in body or "targetPlaybook" in body:
            bodies.append((u, body))
            print(f"== capturada {u[:110]} ({len(body)} B)", flush=True)

    cdp.on("Network.responseReceived", on_received)
    cdp.on("Network.loadingFinished", on_finished)

    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(14)

    print(f"== payloads con rutas: {len(bodies)}", flush=True)
    for u, body in bodies:
        try:
            data = json.loads(body)
        except Exception:
            print(f"== {u[:100]}: no es JSON", flush=True)
            continue
        print(f"\n== {u[:110]}", flush=True)
        for p in (data.get("pages") or []):
            tr = p.get("transitionRoutes") or []
            if not tr:
                continue
            print(f"  PAGINA {p.get('displayName')}  ({len(tr)} rutas)", flush=True)
            for r in tr:
                print(f"    - {r.get('name','').split('/')[-1]}  {r.get('condition')}", flush=True)
                print(f"        claves: {sorted(r.keys())}", flush=True)
                print(f"        targetPlaybook: {r.get('targetPlaybook')}", flush=True)
        for r in (data.get("transitionRoutes") or []):
            print(f"  [flow] {r.get('condition')}  claves={sorted(r.keys())}  pb={r.get('targetPlaybook')}", flush=True)

    if args.out and bodies:
        Path(args.out).write_text(bodies[-1][1], encoding="utf-8")
        print(f"\n== escrito {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
