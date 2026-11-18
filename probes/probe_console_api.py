#!/usr/bin/env python3
"""Tests the console's INTERNAL API (clients6/v3alpha1) with the first party
session (Google cookies + SAPISIDHASH + API key).

Goal: check whether `TransitionRoute` brings `targetPlaybook` and recover the
route -> playbook map of the ORIGINAL agent (read only).

Usage:
  .venv/bin/python probe_console_api.py [--agent original|clon] [--flow ID] [--page ID]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import probe_agents, probe_targets

from cxp.auth import first_party_headers, pick_google_cookies, pick_sapisid
from cxp.config import load_config
from cxp.httpclient import HttpStatusError, Session

# The console API key is read from the environment (`CXP_API_KEY`); it is never hardwired.
API_KEY = os.environ.get("CXP_API_KEY", "")

AGENTS = probe_agents()
# The flows and pages also come from the environment:
#   CXP_PROBE_TARGETS="default=<agent>,<flow>,<page>"
_TARGETS = probe_targets()
FLOWS = {k: v["flow"] for k, v in _TARGETS.items()}
PAGES = {k: v["page"] for k, v in _TARGETS.items()}


def build_headers(cfg) -> dict:
    state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    cookies = state.get("cookies", [])
    sapisid = pick_sapisid(cookies)
    if not sapisid:
        raise SystemExit("no SAPISID in .session-state.json: there is no Google session")
    headers = first_party_headers(cfg.console_origin, sapisid)
    extra = pick_google_cookies(cookies)
    if extra:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in extra.items())
    headers["Accept"] = "application/json"
    headers["x-goog-user-project"] = cfg.quota_project
    return headers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="original", choices=list(AGENTS))
    ap.add_argument("--flow", default="")
    ap.add_argument("--page", default="")
    ap.add_argument("--version", default="v3alpha1")
    ap.add_argument("--host", default="dialogflow.clients6.google.com")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cfg = load_config()
    headers = build_headers(cfg)
    agent = AGENTS[args.agent]
    flow = args.flow or FLOWS[args.agent]
    page = args.page or PAGES[args.agent]

    http = Session(timeout=60.0, maxseg=cfg.tcp_maxseg, retries=1)
    base = f"https://{args.host}/{args.version}/projects/{cfg.project}/locations/{cfg.location}/agents/{agent}"

    targets = [
        ("flow", f"{base}/flows/{flow}"),
        ("page", f"{base}/flows/{flow}/pages/{page}"),
    ]
    out: dict = {}
    for label, url in targets:
        full = url + f"?key={API_KEY}"
        try:
            data = http.json("GET", full, headers)
            print(f"== {label}: OK  {len(json.dumps(data))} B", flush=True)
            out[label] = data
        except HttpStatusError as exc:
            print(f"== {label}: HTTP {exc.status}  {exc.body[:300]}", flush=True)
            out[label] = {"_status": exc.status, "_body": exc.body[:2000]}
        except Exception as exc:
            print(f"== {label}: {type(exc).__name__}: {exc}", flush=True)
            out[label] = {"_error": f"{type(exc).__name__}: {exc}"}

    # Summary of the routes and their target
    for label in ("flow", "page"):
        data = out.get(label) or {}
        routes = data.get("transitionRoutes") or []
        if not routes:
            continue
        print(f"\n== {label}: {len(routes)} transition routes", flush=True)
        for r in routes:
            keys = sorted(r.keys())
            target = r.get("targetPlaybook") or r.get("targetFlow") or r.get("targetPage")
            print(f"   {r.get('name','').split('/')[-1]}  cond={str(r.get('condition'))[:70]}", flush=True)
            print(f"      target={target}  keys={keys}", flush=True)

    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\n== written to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
