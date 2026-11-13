#!/usr/bin/env python3
"""Lee los destinos (playbook) de las transition routes de una pagina, en la
consola "Conversational Agents" (Chrome real por CDP).

NO escribe nada: solo abre el editor de cada ruta y lee el valor del selector
"Transition". En el agente ORIGINAL es de solo lectura por diseño.

Uso:
  .venv/bin/python read_route_targets.py --agent original [--out /tmp/mapa.json]
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

JS_LEER = """
() => {
  const target = document.querySelector('df-transition-target');
  if (!target) return {error: 'sin df-transition-target (editor no abierto)'};
  const radio = target.querySelector('mat-radio-button.mat-mdc-radio-checked');
  const tipo = radio ? radio.getAttribute('value') : null;
  const sel = target.querySelector('span.mat-mdc-select-min-line');
  const destino = sel ? sel.innerText.trim() : null;
  const cond = document.querySelector('transition_routes');
  return {tipo, destino};
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="original", choices=list(AGENTS))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cfg = load_config()
    info = AGENTS[args.agent]
    url = FRAME_URL.format(p=cfg.project, l=cfg.location, a=info["agent"],
                            f=info["flow"], pg=info["page"])

    br = ConsoleBrowser(cfg)
    br.use_real_chrome = True
    br._launch(headless=False)
    page = br._page

    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(9)

    # Expandir la lista completa de rutas ("View all (N)").
    try:
        ver = page.get_by_text("View all (", exact=False).first
        if ver.count() > 0 and ver.is_visible():
            ver.click()
            print("== lista de rutas expandida", flush=True)
            time.sleep(2)
    except Exception as exc:
        print(f"== aviso: no se pudo expandir la lista ({exc})", flush=True)

    cards = page.locator("transition_routes df-node-card .node-card-button")
    n = cards.count()
    print(f"== rutas visibles en la lista: {n}", flush=True)

    out: list[dict] = []
    for i in range(n):
        cond = (cards.nth(i).inner_text() or "").strip()
        try:
            cards.nth(i).click(timeout=15_000)
        except Exception as exc:
            out.append({"indice": i, "condicion": cond, "error": f"click: {exc}"})
            continue
        time.sleep(1.6)
        try:
            data = page.evaluate(JS_LEER)
        except Exception as exc:
            data = {"error": str(exc)}
        out.append({"indice": i, "condicion": cond, **data})
        print(f"   [{i}] {cond[:60]:62s} -> {data.get('tipo')} / {data.get('destino')}", flush=True)

    print(f"\n== {len(out)} rutas leidas", flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"== escrito {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
