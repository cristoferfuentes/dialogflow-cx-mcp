#!/usr/bin/env python3
"""Explora la consola en el Chrome real: captura, selectores y trafico.

Uso: .venv/bin/python explore_console.py [--url URL] [--shot /tmp/x.png]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.browser import ConsoleBrowser
from cxp.browser_ctl import list_selectors
from cxp.config import load_config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--shot", default="/tmp/consola.png")
    ap.add_argument("--wait", type=int, default=6000)
    ap.add_argument("--out", default="/tmp/explore.json")
    args = ap.parse_args()

    cfg = load_config()
    br = ConsoleBrowser(cfg)
    br.use_real_chrome = True
    br._launch(headless=False)
    page = br._page

    # Grabar el trafico (el manejador se engancha al contexto real).
    br.start_recording()
    br._ctx.on("request", br._on_request)
    br._ctx.on("response", br._on_response)

    if args.url:
        page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(args.wait / 1000)

    try:
        page.screenshot(path=args.shot, full_page=False)
    except Exception as exc:
        print(f"== sin captura: {exc}")

    data = {
        "url": page.url,
        "titulo": page.title(),
        "selectores": list_selectors(br, limite=200),
        "peticiones": [
            {"method": r.get("method"), "url": r.get("url"), "status": r.get("status"),
             "post_len": len(r.get("post_data") or ""),
             "post_data": (r.get("post_data") or "")[:2000],
             "headers": {k: v for k, v in (r.get("headers") or {}).items()
                         if k.lower() in ("content-type", "x-goog-user-project", "authorization",
                                          "x-goog-request-params", "referer", "origin", "x-goog-api-client")}}
            for r in br.stop_recording()
        ],
    }
    Path(args.out).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"== url: {data['url']}")
    print(f"== titulo: {data['titulo']}")
    print(f"== peticiones: {len(data['peticiones'])}")
    for p in data["peticiones"]:
        print(f"   {p.get('status')} {p['method']} {p['url'][:140]}  (post {p['post_len']} B)")
    print(f"== selectores: {data['selectores'].get('n')}")
    for e in data["selectores"].get("elementos", []):
        print(f"   <{e['tag']}> role={e['role']!r} cls={e['cls'][:40]!r} :: {e['texto'][:70]!r}")
    print(f"== escrito en {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
