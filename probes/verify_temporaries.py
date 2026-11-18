#!/usr/bin/env python3
"""Checks that no TEMPORARY resources are left in the working agent.

The sweeps create environments, test cases and temporary playbook/tool
versions; all of them are deleted, but it has to be VERIFIED (lesson 2026-11-13:
"the detector does not see it" -> you have to look).

    .venv/bin/python probes/verify_temporaries.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi


def main() -> int:
    cfg = load_config()
    cons = ConsoleApi(cfg)
    base = (f"/projects/{cfg.project}/locations/{cfg.location}"
            f"/agents/{cfg.only_agent()}")

    entornos = (cons.request("GET", cons._url(f"{base}/environments"))
                .get("environments") or [])
    cases = (cons.request("GET", cons._url(f"{base}/testCases", pageSize="100"))
             .get("testCases") or [])
    print(f"environments: {len(entornos)}")
    print(f"testCases: {len(cases)}")

    pb_con = []
    pbs = (cons.request("GET", cons._url(f"{base}/playbooks", pageSize="100"))
           .get("playbooks") or [])
    for p in pbs:
        v = (cons.request("GET", cons._url(f"/{p['name']}/versions", pageSize="10"))
             .get("playbookVersions") or [])
        if v:
            pb_con.append((p.get("displayName"), len(v)))
    print(f"playbooks with versions: {pb_con}")

    tool_con = []
    tools = (cons.request("GET", cons._url(f"{base}/tools", pageSize="100"))
             .get("tools") or [])
    for t in tools:
        try:
            v = (cons.request("GET", cons._url(f"/{t['name']}/versions",
                                               pageSize="10"))
                 .get("toolVersions") or [])
        except Exception:
            continue
        if v:
            tool_con.append((t.get("displayName"), len(v)))
    print(f"tools with versions: {tool_con}")

    flows = (cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
             .get("flows") or [])
    versions = 0
    for f in flows:
        fid = f["name"].rsplit("/", 1)[-1]
        versions += len(cons.request(
            "GET", cons._url(f"{base}/flows/{fid}/versions", pageSize="50"))
            .get("versions") or [])
    print(f"flow versions: {versions} in {len(flows)} flows")

    # Lab agents: there should not be any left with the name zz_tmp_lab
    all_agents = (cons.request("GET", cons._url(
        f"/projects/{cfg.project}/locations/{cfg.location}/agents", pageSize="100"))
        .get("agents") or [])
    labs = [a.get("displayName") for a in all_agents
            if str(a.get("displayName", "")).startswith("zz_tmp_lab")]
    print(f"lab agents left: {labs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
