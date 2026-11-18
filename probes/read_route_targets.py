#!/usr/bin/env python3
"""Reads the targets (playbook) of the transition routes of a page, in the
"Conversational Agents" console (real Chrome via CDP).

It writes NOTHING: it only opens the editor of each route and reads the value of
the "Transition" selector. On the ORIGINAL agent it is read-only by design.

Usage:
  .venv/bin/python read_route_targets.py --agent original [--out /tmp/mapa.json]
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

JS_LEER = """
() => {
  const target = document.querySelector('df-transition-target');
  if (!target) return {error: 'no df-transition-target (editor not open)'};
  const radio = target.querySelector('mat-radio-button.mat-mdc-radio-checked');
  const type = radio ? radio.getAttribute('value') : null;
  const sel = target.querySelector('span.mat-mdc-select-min-line');
  const destination = sel ? sel.innerText.trim() : null;
  const cond = document.querySelector('transition_routes');
  return {type, destination};
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

    # Expand the full route list ("View all (N)").
    try:
        ver = page.get_by_text("View all (", exact=False).first
        if ver.count() > 0 and ver.is_visible():
            ver.click()
            print("== route list expanded", flush=True)
            time.sleep(2)
    except Exception as exc:
        print(f"== warning: the list could not be expanded ({exc})", flush=True)

    cards = page.locator("transition_routes df-node-card .node-card-button")
    n = cards.count()
    print(f"== routes visible in the list: {n}", flush=True)

    out: list[dict] = []
    for i in range(n):
        condition = (cards.nth(i).inner_text() or "").strip()
        try:
            cards.nth(i).click(timeout=15_000)
        except Exception as exc:
            out.append({"index": i, "condition": condition, "error": f"click: {exc}"})
            continue
        time.sleep(1.6)
        try:
            data = page.evaluate(JS_LEER)
        except Exception as exc:
            data = {"error": str(exc)}
        out.append({"index": i, "condition": condition, **data})
        print(f"   [{i}] {condition[:60]:62s} -> {data.get('type')} / {data.get('destination')}", flush=True)

    print(f"\n== {len(out)} routes read", flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"== written {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
