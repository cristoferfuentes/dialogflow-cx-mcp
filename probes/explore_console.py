#!/usr/bin/env python3
"""Explores the console in the real Chrome: capture, selectors and traffic.

Usage: .venv/bin/python explore_console.py [--url URL] [--shot /tmp/x.png]
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

    # Record the traffic (the handler is hooked to the real context).
    br.start_recording()
    br._ctx.on("request", br._on_request)
    br._ctx.on("response", br._on_response)

    if args.url:
        page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(args.wait / 1000)

    try:
        page.screenshot(path=args.shot, full_page=False)
    except Exception as exc:
        print(f"== no screenshot: {exc}")

    data = {
        "url": page.url,
        "title": page.title(),
        "selectors": list_selectors(br, limit=200),
        "requests": [
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
    print(f"== title: {data['title']}")
    print(f"== requests: {len(data['requests'])}")
    for p in data["requests"]:
        print(f"   {p.get('status')} {p['method']} {p['url'][:140]}  (post {p['post_len']} B)")
    print(f"== selectors: {data['selectors'].get('n')}")
    selectors = data["selectors"]
    elements = selectors.get("elements") or []
    for e in elements:
        text = e.get("text") or ""
        print(f"   <{e['tag']}> role={e['role']!r} cls={e['cls'][:40]!r} :: {text[:70]!r}")
    print(f"== written in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
