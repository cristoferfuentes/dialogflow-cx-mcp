#!/usr/bin/env python3
"""Captura el trafico REAL de la consola (incluido el iframe /cx/) via CDP:
cabeceras completas y cuerpos de respuesta del backend interno.

Los eventos de red de Playwright no se disparan sobre contextos conectados por
CDP, y la sesion CDP de la pagina principal no ve los iframes: hay que abrir
una sesion CDP por frame.

Uso: .venv/bin/python capture_console_traffic.py --agent clon [--out /tmp/t.json]
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
CONSOLE_URL = (
    "https://conversational-agents.cloud.google.com/projects/{p}/locations/{l}/agents/{a}"
    "/(cx/flows/{f}/flow_creation//right-panel:simulator)?pageId={pg}"
)


def attach_frames(page):
    """Abre una sesion CDP por cada frame de la pagina."""
    sesses = []
    for fr in page.frames:
        try:
            s = page.context.new_cdp_session(fr)
            s.send("Network.enable")
            sesses.append((fr, s))
        except Exception:
            continue
    return sesses


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="clon", choices=list(AGENTS))
    ap.add_argument("--out", default="")
    ap.add_argument("--wait", type=int, default=15000)
    args = ap.parse_args()

    cfg = load_config()
    a, f, pg = AGENTS[args.agent]
    url = CONSOLE_URL.format(p=cfg.project, l=cfg.location, a=a, f=f, pg=pg)

    br = ConsoleBrowser(cfg)
    br.use_real_chrome = True
    br._launch(headless=False)
    page = br._page

    requests: list[dict] = []
    bodies: list[tuple[str, str]] = []
    # requestId -> (url, session)
    seen: dict[str, tuple[str, object]] = {}

    def make_handlers(sess):
        def on_req(ev):
            r = ev.get("request", {})
            u = r.get("url", "")
            if "clients6.google.com" not in u:
                return
            seen[ev["requestId"]] = (u, sess)
            requests.append({"method": r.get("method"), "url": u, "headers": r.get("headers") or {},
                             "postData": (r.get("postData") or "")[:4000]})

        def on_fin(ev):
            rid = ev.get("requestId")
            info = seen.get(rid or "")
            if not info:
                return
            u, sess = info
            try:
                res = sess.send("Network.getResponseBody", {"requestId": rid})
                bodies.append((u, res.get("body", "")))
            except Exception:
                pass

        sess.on("Network.requestWillBeSent", on_req)
        sess.on("Network.loadingFinished", on_fin)

    # La pagina principal ya esta cargada; se navega y se enganchan las sesiones
    # de todos los frames (el iframe /cx/ se crea durante la carga).
    page.goto("about:blank")
    time.sleep(1)
    for fr in page.frames:
        pass
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(3)
    for fr, s in attach_frames(page):
        make_handlers(s)
    time.sleep(args.wait / 1000)

    print(f"== peticiones clients6: {len(requests)}", flush=True)
    flows = [r for r in requests if "/flows/" in r["url"]]
    for r in flows[:2]:
        print(f"\n=== {r['method']} {r['url'][:180]}", flush=True)
        for k, v in sorted(r["headers"].items()):
            print(f"    {k}: {v[:200]}", flush=True)

    print(f"\n== cuerpos capturados: {len(bodies)}", flush=True)
    for u, body in bodies:
        if "transitionRoutes" not in body:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        print(f"\n== {u[:120]}", flush=True)
        for p in (data.get("pages") or []):
            tr = p.get("transitionRoutes") or []
            if not tr:
                continue
            print(f"  PAGINA {p.get('displayName')} ({len(tr)} rutas)", flush=True)
            for r in tr:
                print(f"    - {r.get('name','').split('/')[-1]} | {r.get('condition')}", flush=True)
                print(f"        targetPlaybook: {r.get('targetPlaybook')}", flush=True)

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"requests": requests, "bodies": [{"url": u, "body": b} for u, b in bodies]},
            indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"== escrito {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
