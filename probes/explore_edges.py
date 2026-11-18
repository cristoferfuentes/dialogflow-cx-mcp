#!/usr/bin/env python3
"""Explores the 'Edge Information' panel of the graph: that is where the
route -> playbook map comes from (each edge is a transition route of the page).

Usage: .venv/bin/python explore_edges.py [--agent original|clon]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import probe_agents

from cxp.browser import ConsoleBrowser
from cxp.config import load_config

AGENTS = probe_agents()
FRAME_URL = (
    "https://conversational-agents.cloud.google.com/cx/projects/{p}/locations/{l}/agents/{a}"
    "/flows/{f}?pageId={pg}&embedded=true"
)

JS_NODES = """
() => [...document.querySelectorAll('graph-node')].map(g => {
  const t = (g.innerText||'').trim().split('\\n')[0];
  const bb = g.getBoundingClientRect();
  return {t, x: Math.round(bb.x + bb.width/2), y: Math.round(bb.y + bb.height/2),
          cls:(g.className||'').toString().slice(0,60)};
})
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="original", choices=list(AGENTS))
    ap.add_argument("--shot", default="/tmp/edges.png")
    args = ap.parse_args()

    cfg = load_config()
    a, f, pg = AGENTS[args.agent]
    url = FRAME_URL.format(p=cfg.project, l=cfg.location, a=a, f=f, pg=pg)

    br = ConsoleBrowser(cfg)
    br.use_real_chrome = True
    br._launch(headless=False)
    page = br._page
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(10)

    nodes = page.evaluate(JS_NODES)
    print(f"== graph nodes: {len(nodes)}")
    for n in nodes:
        print(f"   {n['t']!r} @({n['x']},{n['y']}) cls={n['cls']}")

    # Click the page node (the one with 'transicion_canal')
    target = next((n for n in nodes if "transicion_canal" in n["t"]), None)
    if target:
        print(f"\n== clicking the node {target['t']!r}", flush=True)
        page.mouse.click(target["x"], target["y"])
        time.sleep(3)

    # Read the "Edge Information" panel
    try:
        txt = page.evaluate("""() => {
            const els=[...document.querySelectorAll('*')].filter(e=>e.children.length===0
                && ((e.textContent||'').trim()==='Edge Information'));
            const t=els[0]?.parentElement;
            return t ? t.innerText.slice(0,2000) : null;
        }""")
        print("\n== Edge Information:\n", txt)
    except Exception as exc:
        print(f"edge info failed: {exc}")

    try:
        page.screenshot(path=args.shot, full_page=False)
        print(f"\n== screenshot {args.shot}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
