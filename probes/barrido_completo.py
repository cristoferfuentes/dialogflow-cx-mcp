#!/usr/bin/env python3
"""COMPLETE sweep of field gaps over the agent from the `.env`.

The sweep is generic: it comes from `cxp.gaps.colecciones_listables()`, which reads
the discovery doc. This script only runs it and saves it as JSON.

    .venv/bin/python probes/barrido_completo.py [--agente <uuid>] [--out /tmp/barrido.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi
from cxp.gaps import colecciones_listables, load_schema_index, scan_agent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agente", default="")
    ap.add_argument("--out", default="/tmp/barrido.json")
    ap.add_argument("--proyecto", default="")
    args = ap.parse_args()

    cfg = load_config()
    agente = args.agente or cfg.only_agent()
    if not agente:
        print("no agent in the .env")
        return 1
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)

    print(f"agent: {agente}")
    print(f"listable collections in the discovery doc: {len(colecciones_listables(idx))}")
    t0 = time.perf_counter()
    res = scan_agent(cons, agente, args.proyecto or cfg.project, idx=idx)
    ms = (time.perf_counter() - t0) * 1000

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"sweep in {ms:.0f} ms -> {args.out}")
    print(f"resources scanned: {res['recursos_escaneados']}")
    print(f"grouped gaps: {len(res['huecos'])}")
    if res["sin_alcanzar"]:
        print(f"collections NOT reached ({len(res['sin_alcanzar'])}):")
        for s in res["sin_alcanzar"]:
            print(f"  - {s['coleccion']}: {s['motivo'][:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
