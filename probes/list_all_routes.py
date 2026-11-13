#!/usr/bin/env python3
"""Lista TODAS las transition routes de una pagina en la consola, aunque la
lista este virtualizada (solo renderiza las visibles).

Recorre el contenedor con scroll y recoge la condicion de cada tarjeta.
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

JS_CONDS = """
() => [...document.querySelectorAll('transition_routes df-node-card .node-card-button')]
        .map(b => b.innerText.trim())
"""

JS_SCROLL = """
(top) => {
  const list = document.querySelector('.transition-route-list');
  let el = list, sc = null;
  while (el) {
    const st = getComputedStyle(el);
    if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 4) { sc = el; break; }
    el = el.parentElement;
  }
  if (!sc) return {ok:false, reason:'sin contenedor con scroll'};
  sc.scrollTop = top;
  return {ok:true, cls:(sc.className||'').toString().slice(0,60), sh:sc.scrollHeight, ch:sc.clientHeight, top:sc.scrollTop};
}
"""


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
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(9)

    # "View all (N)"
    try:
        ver = page.get_by_text("View all (", exact=False).first
        if ver.count() > 0 and ver.is_visible():
            ver.click()
            time.sleep(2)
    except Exception:
        pass

    vistas: list[str] = []
    for top in (0, 150, 300, 450, 600, 900, 1400):
        info = page.evaluate(JS_SCROLL, top)
        page.wait_for_timeout(900)
        conds = page.evaluate(JS_CONDS)
        nuevos = [c for c in conds if c not in vistas]
        vistas.extend(nuevos)
        print(f"== scrollTop={top} {info}  -> {len(conds)} visibles, {len(vistas)} acumuladas", flush=True)
        if not nuevos and top > 0:
            break

    print(f"\n== {len(vistas)} rutas:")
    for i, c in enumerate(vistas):
        print(f"   [{i}] {c}", flush=True)

    if args.out:
        Path(args.out).write_text(json.dumps(vistas, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"== escrito {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
