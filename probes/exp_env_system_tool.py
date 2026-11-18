#!/usr/bin/env python3
"""Can the SYSTEM TOOL be included in an environment?

MEASURED (2026-11-15): the tool `df-code-interpreter-tool` (code-interpreter) is
in the agent and a playbook references it, but:

  * `POST tools/df-code-interpreter-tool/versions` -> 404 "Tool does not exist
    in the agent" (system tools are NOT versioned);
  * `GET  tools/df-code-interpreter-tool/versions` -> `{}` (it has none);
  * `versionConfigs[].version = ".../tools/<id>"` (the tool NAME) ->
    "[ResourceName error] ... does not match template
     'projects/*/locations/*/agents/*/flows/*/versions/*'".

=> The environment cannot be created if the published graph includes a system
tool. This script tests whether there is a way to include it (canonical version,
name with another template...) and, if not, leaves a record.

It creates all the temporary versions it needs, tests and **deletes everything**.

    .venv/bin/python probes/exp_env_system_tool.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi


def wait_for(cons: ConsoleApi, op: dict, seconds: int = 180) -> dict:
    name = op.get("name")
    if not name or op.get("done") or "/operations/" not in name:
        return op
    for _ in range(seconds * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + name))
        if op.get("done"):
            return op
    return op


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    base = f"/projects/{project}/locations/{cfg.location}/agents/{agent}"
    created: list[str] = []

    try:
        flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
        vcs: list[dict] = []
        for f in (flows.get("flows") or []):
            fid = f["name"].rsplit("/", 1)[-1]
            v = cons.request("GET", cons._url(f"{base}/flows/{fid}/versions",
                                               pageSize="5"))
            vs = v.get("versions") or []
            if vs:
                vcs.append({"version": vs[0]["name"]})

        queue: set[str] = set()
        for f in (flows.get("flows") or []):
            fid = f["name"].rsplit("/", 1)[-1]
            for pg in (cons.request("GET", cons._url(f"{base}/flows/{fid}/pages",
                                                     pageSize="100")).get("pages") or []):
                for tr in (pg.get("transitionRoutes") or []):
                    if tr.get("targetPlaybook"):
                        queue.add(tr["targetPlaybook"])
        playbooks: set[str] = set()
        while queue:
            pb = queue.pop()
            if pb in playbooks:
                continue
            playbooks.add(pb)
            try:
                d = cons.request("GET", cons._url("/" + pb))
            except Exception:
                continue
            for r in (d.get("referencedPlaybooks") or []):
                if r:
                    queue.add(r)
        tools: set[str] = set()
        for pb in playbooks:
            try:
                d = cons.request("GET", cons._url("/" + pb))
            except Exception:
                continue
            for r in (d.get("referencedTools") or []):
                if r:
                    tools.add(r)

        def version_of(name: str) -> str:
            child = "playbookVersions" if "playbooks/" in name else "toolVersions"
            try:
                ex = cons.request("GET", cons._url(f"/{name}/versions",
                                                   pageSize="5")).get(child) or []
            except Exception:
                return ""
            if ex:
                return ex[0]["name"]
            try:
                r = cons.request("POST", cons._url(f"/{name}/versions"),
                                 body={"description": "temporary: sweep"})
            except Exception:
                return ""
            if r.get("name"):
                created.append(r["name"])
                return r["name"]
            return ""

        for pb in sorted(playbooks):
            v = version_of(pb)
            if v:
                vcs.append({"version": v})
        for tl in sorted(tools):
            v = version_of(tl)
            if v:
                vcs.append({"version": v})
        print(f"versionConfigs ready: {len(vcs)} "
              f"({len(playbooks)} playbooks, {len(tools)} tools)")

        prefix = f"{base}/tools/df-code-interpreter-tool"
        attempts = [
            ("versions/1", f"{prefix}/versions/1"),
            ("versions/0", f"{prefix}/versions/0"),
            ("tool name", prefix),
            ("versions/latest", f"{prefix}/versions/latest"),
        ]
        for label, value in attempts:
            body = {"displayName": f"zz_tmp_{uuid.uuid4().hex[:6]}",
                      "description": "temporary",
                      "versionConfigs": vcs + [{"version": value}]}
            try:
                op = wait_for(cons, cons.request(
                    "POST", cons._url(f"{base}/environments"), body=body))
                response = op.get("response") or {}
                name = response.get("name") or ""
            except Exception as exc:
                name = ""
                err = str(exc)
            else:
                err = json.dumps(op.get("error"), ensure_ascii=False)
            if name:
                print(f"  {label:<20} -> CREATED ({name})")
                cons.request("DELETE", cons._url("/" + name))
                print("      deleted")
                break
            print(f"  {label:<20} -> {err[:190]}")
    finally:
        for name in created:
            try:
                cons.request("DELETE", cons._url("/" + name))
            except Exception:
                pass
        print(f"{len(created)} temporary versions deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
