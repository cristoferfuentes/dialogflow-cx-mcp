#!/usr/bin/env python3
"""Reads the "Routes" TABLE of the console side panel: for each transition
route it gives the condition and its TRANSITION (target playbook).

It is the read-only way to recover the route -> playbook map.

Usage:
  .venv/bin/python read_routes_table.py --agent original [--out /tmp/mapa.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import agentes_de_sonda

from cxp.browser import ConsoleBrowser
from cxp.config import load_config

AGENTS = agentes_de_sonda()
FRAME_URL = (
    "https://conversational-agents.cloud.google.com/cx/projects/{p}/locations/{l}/agents/{a}"
    "/flows/{f}?pageId={pg}&embedded=true"
)

JS_NODES = """
() => [...document.querySelectorAll('graph-node')].map(g => {
  const t = (g.innerText||'').trim().split('\\n')[0];
  const bb = g.getBoundingClientRect();
  return {t, x: Math.round(bb.x + bb.width/2), y: Math.round(bb.y + bb.height/2)};
})
"""

# Reads the rows of the "Routes" panel table: cells -> text
JS_ROWS = """
() => {
  const rows = [...document.querySelectorAll('tr, [role="row"]')];
  const out = [];
  for (const r of rows) {
    const cells = [...r.querySelectorAll('td, th, [role="cell"], [role="gridcell"]')]
      .map(c => (c.innerText||'').trim().replace(/\\s+/g,' '));
    if (cells.length >= 2) out.push(cells);
  }
  return out;
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="original", choices=list(AGENTS))
    ap.add_argument("--page-node", default="transicion_canal")
    ap.add_argument("--out", default="")
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
    objetivo = next((n for n in nodes if args.page_node in n["t"]), None)
    if objetivo:
        page.mouse.click(objetivo["x"], objetivo["y"])
        time.sleep(4)
    else:
        print(f"== the node {args.page_node!r} was not found; nodes={[n['t'] for n in nodes]}")

    filas = page.evaluate(JS_ROWS)
    print(f"== table rows: {len(filas)}")
    for i, celdas in enumerate(filas):
        print(f"  [{i}] " + " | ".join(c[:60] for c in celdas))

    # "Flat" version: condition -> transition
    mapa = []
    for celdas in filas:
        if len(celdas) < 4:
            continue
        intent, cond, _fulfill, trans = celdas[0], celdas[1], celdas[2], celdas[3]
        if not cond or cond.lower() in ("condition",):
            continue
        mapa.append({"condicion": cond, "transicion": trans, "intent": intent})

    print(f"\n== map ({len(mapa)} routes)")
    for m in mapa:
        print(f"   {m['condicion'][:70]:72s} -> {m['transicion']}")

    if args.out:
        Path(args.out).write_text(json.dumps(mapa, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"== written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
