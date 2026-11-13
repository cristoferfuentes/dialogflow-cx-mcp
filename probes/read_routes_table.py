#!/usr/bin/env python3
"""Lee la TABLA "Routes" del panel lateral de la consola: por cada transition
route da la condicion y su TRANSICION (playbook destino).

Es la via de solo lectura para recuperar el mapa ruta -> playbook.

Uso:
  .venv/bin/python read_routes_table.py --agent original [--out /tmp/mapa.json]
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

JS_NODES = """
() => [...document.querySelectorAll('graph-node')].map(g => {
  const t = (g.innerText||'').trim().split('\\n')[0];
  const bb = g.getBoundingClientRect();
  return {t, x: Math.round(bb.x + bb.width/2), y: Math.round(bb.y + bb.height/2)};
})
"""

# Lee las filas de la tabla del panel "Routes": celdas -> texto
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
        print(f"== no se encontro el nodo {args.page_node!r}; nodos={[n['t'] for n in nodes]}")

    filas = page.evaluate(JS_ROWS)
    print(f"== filas de la tabla: {len(filas)}")
    for i, celdas in enumerate(filas):
        print(f"  [{i}] " + " | ".join(c[:60] for c in celdas))

    # Version "plana": condicion -> transicion
    mapa = []
    for celdas in filas:
        if len(celdas) < 4:
            continue
        intent, cond, fulfill, trans = celdas[0], celdas[1], celdas[2], celdas[3]
        if not cond or cond.lower() in ("condition",):
            continue
        mapa.append({"condicion": cond, "transicion": trans, "intent": intent})

    print(f"\n== mapa ({len(mapa)} rutas)")
    for m in mapa:
        print(f"   {m['condicion'][:70]:72s} -> {m['transicion']}")

    if args.out:
        Path(args.out).write_text(json.dumps(mapa, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"== escrito {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
