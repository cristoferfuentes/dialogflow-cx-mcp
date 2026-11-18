#!/usr/bin/env python3
"""Searches 'crp' (case-insensitive) in the WHOLE original agent of dev:
playbooks (goal + instruction), flows, pages (routes) and transition route groups.

Read-only. Usage:
  .venv/bin/python search_crp.py --agent <uuid> [--out /tmp/crp.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config  # noqa: E402
from cxp.console_api import ConsoleApi  # noqa: E402


def text(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def search(agent: str) -> dict:
    cfg = load_config()
    api = ConsoleApi(cfg)
    out: dict = {"agent": agent, "playbooks": [], "matches": []}

    pbs = api.list_playbooks(agent)
    out["n_playbooks"] = len(pbs)
    out["playbooks"] = [
        {"name": p.get("name"), "displayName": p.get("displayName")} for p in pbs
    ]

    def record(kind: str, obj, label: str) -> None:
        t = text(obj).lower()
        if "crp" in t:
            out["matches"].append({"type": kind, "label": label, "object": obj})

    for p in pbs:
        record("playbook", p, p.get("displayName", ""))

    flows = api.list_flows(agent)
    out["n_flows"] = len(flows)
    for f in flows:
        record("flow", f, f.get("displayName", ""))
        fid = (f.get("name") or "").rsplit("/", 1)[-1]
        try:
            pages = api.list_pages(agent, fid)
        except Exception as exc:  # noqa: BLE001
            out.setdefault("errors", []).append(f"flow {fid}: {exc}")
            continue
        for pg in pages:
            record("page", pg, pg.get("displayName", ""))
            for r in (pg.get("transitionRoutes") or []):
                record("route", r, f"{pg.get('displayName')} :: {r.get('condition')}")

    try:
        groups = api.list_transition_route_groups(agent)
        for g in groups:
            record("route_group", g, g.get("displayName", ""))
    except Exception as exc:  # noqa: BLE001
        out.setdefault("errors", []).append(f"groups: {exc}")

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    res = search(args.agent)
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2))
    print(json.dumps(
        {
            "n_playbooks": res.get("n_playbooks"),
            "n_flows": res.get("n_flows"),
            "errors": res.get("errors", []),
            "matches": [
                {"type": c["type"], "label": c["label"]} for c in res["matches"]
            ],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
