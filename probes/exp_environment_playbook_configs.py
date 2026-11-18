#!/usr/bin/env python3
"""Experiment: create a temporary ENVIRONMENT with `playbookConfigs`.

Why: the public v3beta1 schema declares `EnvironmentVersionConfig` with ONE
single field (`version`). But the internal backend REQUIRES that the playbooks that
the flow's routes reference are declared:

    Playbook 'pyb_public_playbook' is referenced in version 'X' of flow
    'Flujo_Saludo_botonera', but not included in the environment.

=> `versionConfigs[].playbookConfigs[]` is a FIELD GAP of the public API:
no discovery doc declares it (v3, v3beta1, v3alpha1) and without it an
environment cannot be created.

This script:
  1. Reuses (or creates) one version per flow.
  2. Computes the transitive closure of the playbooks referenced by each flow
     (`transitionRoutes[].targetPlaybook` of its PAGES + `referencedPlaybooks`
     of those playbooks).
  3. Creates the environment with `versionConfigs[].playbookConfigs`.
  4. DELETES it at the end (in a `finally`).

    .venv/bin/python probes/exp_environment_playbook_configs.py [--agent <uuid>]
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


def wait_for(cons: ConsoleApi, op: dict, seconds: int = 120) -> dict:
    """Follows an LRO until it finishes."""
    name = op.get("name")
    if not name or op.get("done") or "/operations/" not in name:
        return op
    for _ in range(seconds * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + name))
        if op.get("done"):
            return op
    return op


def playbooks_de_flow(cons: ConsoleApi, base: str, fid: str) -> list[str]:
    """Playbooks referenced by a flow (pages) + transitive closure."""
    refs: set[str] = set()
    # (a) the PAGES of the flow
    pages = cons.request("GET", cons._url(f"{base}/flows/{fid}/pages",
                                            pageSize="100"))
    for pg in (pages.get("pages") or []):
        for tr in (pg.get("transitionRoutes") or []):
            tp = tr.get("targetPlaybook")
            if tp:
                refs.add(tp)
    # (b) the routes of the flow itself
    fl = cons.request("GET", cons._url(f"{base}/flows/{fid}"))
    for tr in (fl.get("transitionRoutes") or []):
        tp = tr.get("targetPlaybook")
        if tp:
            refs.add(tp)
    # (c) transitive closure: one playbook can reference another.
    queue = list(refs)
    while queue:
        name = queue.pop()
        try:
            pb = cons.request("GET", cons._url("/" + name))
        except Exception:
            continue
        for ref in (pb.get("referencedPlaybooks") or []):
            if ref and ref not in refs:
                refs.add(ref)
                queue.append(ref)
    return sorted(refs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--only-playbooks", action="store_true",
                    help="does not create the environment: only measures the playbook closure")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    base = f"/projects/{project}/locations/{cfg.location}/agents/{agent}"

    flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
    lista = flows.get("flows") or []
    print(f"agent {agent}: {len(lista)} flows")

    # ------------------------------------------------- versions + playbooks
    plan: list[dict] = []
    for f in lista:
        fid = f["name"].rsplit("/", 1)[-1]
        existing = cons.request("GET", cons._url(f"{base}/flows/{fid}/versions",
                                                   pageSize="10"))
        existing_versions = existing.get("versions") or []
        if existing_versions:
            version = existing_versions[0]["name"]
        else:
            op = wait_for(cons, cons.request(
                "POST", cons._url(f"{base}/flows/{fid}/versions"),
                body={"displayName": f"zz_tmp_env_{uuid.uuid4().hex[:6]}",
                      "description": "temporary: environment experiment"}))
            response = op.get("response") or {}
            version = response.get("name") or ""
            if not version:
                print(f"  {f.get('displayName')}: could not be versioned "
                      f"({op.get('error')})")
                continue
        refs = playbooks_de_flow(cons, base, fid)
        plan.append({"flow": f.get("displayName"), "version": version,
                     "playbooks": refs})
        mark = f"  {f.get('displayName')}: version ok, {len(refs)} playbook(s)"
        if refs:
            mark += f" -> {[r.rsplit('/', 1)[-1][:8] for r in refs]}"
        print(mark)

    total_playbooks = {p for v in plan for p in v["playbooks"]}
    print(f"\nreferenced playbooks in total: {len(total_playbooks)}")
    if args.only_playbooks:
        return 0

    # ------------------------------------------------------------- environment
    version_configs = [{"version": v["version"]} for v in plan]
    for vc, v in zip(version_configs, plan, strict=True):
        if v["playbooks"]:
            vc["playbookConfigs"] = [{"playbook": p} for p in v["playbooks"]]
    body = {"displayName": f"zz_tmp_env_{uuid.uuid4().hex[:6]}",
              "description": "temporary: environment experiment",
              "versionConfigs": version_configs}
    print("\nversionConfigs[0]:", json.dumps(version_configs[0], ensure_ascii=False))

    op = wait_for(cons, cons.request("POST", cons._url(f"{base}/environments"),
                                     body=body))
    response = op.get("response") or {}
    environment = response.get("name") or ""
    if not environment:
        print("Could NOT create the environment:", json.dumps(op.get("error"),
                                                                ensure_ascii=False)[:500])
        return 1
    print("environment:", environment)

    try:
        ent = cons.request("GET", cons._url("/" + environment))
        print("environment fields:", sorted(ent.keys()))
        print(json.dumps(ent.get("versionConfigs"), ensure_ascii=False)[:600])
    finally:
        try:
            cons.request("DELETE", cons._url("/" + environment))
            print("temporary environment deleted")
        except Exception as exc:
            print("Could NOT delete the environment:", str(exc)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
