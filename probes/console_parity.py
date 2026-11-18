#!/usr/bin/env python3
"""API vs CONSOLE parity, measured live.

Three questions, three measurements:

1. **METHOD**: which endpoints does the console use that are in NO discovery doc
   (public or internal)? They are compared against the discovery doc, not from
   memory. Plus: do the catalog ones answer TODAY?

2. **TRANSPORT**: the console calls the REGIONAL hosts
   (`{location}-dialogflow.clients6.google.com`) for the locations that are not
   `global`. Can the MCP? (measured rule: `x-origin` = the console origin,
   `referer` = `{HOST}/static/proxy.html?usegapi=1`, signature over the console
   origin).

3. **FIELD**: which fields does the backend expose that the public proto does NOT
   declare? (live sweep, reuses `cxp.gaps.scan_agent`).

    .venv/bin/python probes/console_parity.py [--agent <uuid>] [--json /tmp/parity.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp import console_endpoints
from cxp.config import load_config
from cxp.console_api import CONSOLE_HOST, ConsoleApi
from cxp.gaps import load_schema_index, scan_agent
from cxp.httpclient import Session

# The endpoints the console calls that are NOT in any discovery doc.
# They come from the captured traffic (probes/capture_console_traffic_cdp.py): the console
# catalog plus the one of projects, which was NOT in `cxp/console_endpoints.py`.
ENDPOINT_PROJECTS = {
    "id": "projects.list",
    "version": "v3alpha1",
    "method": "GET",
    "path": "projects",
    "status": "verified",
    "purpose": "The project listing (what the console project selector "
                "paints). It calls it with `filter=id=\"<project>\"` and "
                "`view=FULL`. It is NOT in any discovery doc.",
    "response": "{\"projects\":[{\"name\":\"projects/my-project\", ...}]}",
}


def _status(cons: ConsoleApi, version: str, route: str, query: dict | None = None) -> dict:
    """Calls a console endpoint and summarizes the result."""
    url = f"https://{CONSOLE_HOST}/{version}/{route.lstrip('/')}?key={cons.cfg.api_key}"
    try:
        data = cons.request("GET", url, query=query)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}
    if isinstance(data, dict):
        keys = sorted(data.keys())
        empty = not keys
    else:
        keys = []
        empty = not data
    return {"status": "empty" if empty else "ok", "keys": keys}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--location-extra", default="us-central1")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    res: dict = {"agent": agent, "project": project}

    # ------------------------------------------- 1) endpoints outside any doc
    print("=" * 78)
    print("1) CONSOLE ENDPOINTS THAT ARE NOT IN ANY DISCOVERY DOC")
    print("=" * 78)
    catalog = list(console_endpoints.METHOD_GAPS) + [ENDPOINT_PROJECTS]
    methods: list[dict] = []
    for h in catalog:
        route = (h.get("path") or "").replace("{project}", project) \
                                 .replace("{location}", cfg.location) \
                                 .replace("{agent}", agent)
        if not route.startswith("projects/"):
            route = "/" + route
        q = None
        if h["id"] == "projects.list":
            q = {"filter": f'id="{project}"', "view": "FULL"}
        t0 = time.perf_counter()
        state = _status(cons, h.get("version", "v3alpha1"), route, q)
        ms = (time.perf_counter() - t0) * 1000
        row = {"id": h["id"], "version": h.get("version"), "route": route,
               "live": state, "ms": round(ms)}
        methods.append(row)
        print(f"  {h['id']:<28} {state['status']:<9} {ms:>6.0f} ms  "
              f"{','.join(state.get('keys') or [])[:60]}")
    res["endpoints_outside_descriptor"] = methods

    # ------------------------------------------------ 2) regional hosts
    print()
    print("=" * 78)
    print("2) TRANSPORT: can the MCP reach a location that is not `global`?")
    print("=" * 78)
    loc = args.location_extra
    host = f"{loc}-{CONSOLE_HOST}"
    url = (f"https://{host}/v3alpha1/projects/{project}/locations/{loc}/agents"
           f"?key={cfg.api_key}")
    cabeceras = cons._headers()
    # MEASURED: the `referer` has to carry the HOST that is called; `x-origin` and
    # the signature are still the ones of the CONSOLE origin.
    cabeceras["referer"] = f"https://{host}/static/proxy.html?usegapi=1"
    regional = {}
    try:
        data = Session().json("GET", url, cabeceras, None, timeout=60)
        agents = data.get("agents") or []
        regional = {"status": "ok", "host": host, "agents": len(agents),
                    "names": [a.get("displayName") for a in agents][:5]}
        print(f"  {host} -> OK, {len(agents)} agents: {regional['names']}")
    except Exception as exc:
        regional = {"status": "error", "host": host, "detail": str(exc)[:200]}
        print(f"  {host} -> ERROR {str(exc)[:150]}")
    # The global host does NOT work for another location:
    try:
        Session().json(
            "GET",
            f"https://{CONSOLE_HOST}/v3beta1/projects/{project}/locations/{loc}"
            f"/agents?key={cfg.api_key}",
            cons._headers(), None, timeout=60)
        global_ = {"status": "ok"}
    except Exception as exc:
        global_ = {"status": "error", "detail": str(exc)[:200]}
    print(f"  {CONSOLE_HOST} (global host, location={loc}) -> {global_['status']}")
    res["regional"] = {**regional, "host_global": global_}

    # ------------------------------------------------------ 3) field gaps
    print()
    print("=" * 78)
    print("3) FIELDS: live sweep of the agent")
    print("=" * 78)
    idx = load_schema_index(cfg.api_version)
    sweep = scan_agent(cons, agent, project, idx=idx)
    print(f"  collections in the discovery doc: {sweep['collections']}")
    print(f"  reached: {len(sweep['reachable_collections'])}"
          f" | not reached: {len(sweep['unreachable'])}")
    for s in sweep["unreachable"]:
        print(f"    - {s['collection']}: {s['reason'][:90]}")
    print(f"  grouped field gaps: {len(sweep['gaps'])}")
    res["sweep"] = sweep

    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
