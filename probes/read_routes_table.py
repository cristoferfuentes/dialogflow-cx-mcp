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
    target = next((n for n in nodes if args.page_node in n["t"]), None)
    if target:
        page.mouse.click(target["x"], target["y"])
        time.sleep(4)
    else:
        print(f"== the node {args.page_node!r} was not found; nodes={[n['t'] for n in nodes]}")

    rows = page.evaluate(JS_ROWS)
    print(f"== table rows: {len(rows)}")
    for i, cells in enumerate(rows):
        print(f"  [{i}] " + " | ".join(c[:60] for c in cells))

    # "Flat" version: condition -> transition
    mapping = []
    for cells in rows:
        if len(cells) < 4:
            continue
        intent, condition, _fulfill, transition = cells[0], cells[1], cells[2], cells[3]
        if not condition or condition.lower() in ("condition",):
            continue
        mapping.append({"condition": condition, "transition": transition, "intent": intent})

    print(f"\n== map ({len(mapping)} routes)")
    for m in mapping:
        print(f"   {m['condition'][:70]:72s} -> {m['transition']}")

    if args.out:
        Path(args.out).write_text(json.dumps(mapping, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"== written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
