#!/usr/bin/env python3
"""COMPLETE sweep of field gaps over the agent from the `.env`.

The sweep is generic: it comes from `cxp.gaps.listable_collections()`, which reads
the discovery doc. This script only runs it and saves it as JSON.

    .venv/bin/python probes/full_sweep.py [--agent <uuid>] [--out /tmp/full_sweep.json]
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
from cxp.gaps import listable_collections, load_schema_index, scan_agent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--out", default="/tmp/full_sweep.json")
    ap.add_argument("--project", dest="project", default="")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    if not agent:
        print("no agent in the .env")
        return 1
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)

    print(f"agent: {agent}")
    print(f"listable collections in the discovery doc: {len(listable_collections(idx))}")
    t0 = time.perf_counter()
    res = scan_agent(cons, agent, args.project or cfg.project, idx=idx)
    ms = (time.perf_counter() - t0) * 1000

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"sweep in {ms:.0f} ms -> {args.out}")
    print(f"resources scanned: {res['scanned_resources']}")
    print(f"grouped gaps: {len(res['gaps'])}")
    if res["unreachable"]:
        print(f"collections NOT reached ({len(res['unreachable'])}):")
        for s in res["unreachable"]:
            print(f"  - {s['collection']}: {s['reason'][:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
