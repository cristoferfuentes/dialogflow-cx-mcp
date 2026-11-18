#!/usr/bin/env python3
"""Looks for the REAL name of the field that includes the PLAYBOOKS in an environment.

MEASURED (2026-11-15):
  * Creating an environment requires the versions of **ALL** the flows (they
    reference each other: "Flow 'X' is referenced in version ... but not included in
    the environment").
  * And also the PLAYBOOKS that the routes reference:
    "Playbook 'P' is referenced in version ... but not included in the
    environment".
  * The public proto declares `EnvironmentVersionConfig = {version}`: the field
    that includes the playbooks **is not declared in any version**.

The server silently IGNORES the fields it does not know, so the way to discover
it is to measure the CHANGE in the error: if the field exists but the value is
wrong, the error changes; if the field does not exist, the error is the usual one.

This script tries candidates and **deletes any environment that gets created**.

    .venv/bin/python probes/exp_env_playbook_field.py
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

ERROR_BASE = ("not included in the environment", "referenced in version")


def wait_for(cons: ConsoleApi, op: dict, seconds: int = 120) -> dict:
    name = op.get("name")
    if not name or op.get("done") or "/operations/" not in name:
        return op
    for _ in range(seconds * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + name))
        if op.get("done"):
            return op
    return op


def playbooks_by_flow(cons: ConsoleApi, base: str) -> dict[str, list[str]]:
    """Flow -> playbooks that reference its routes (transitive closure)."""
    flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
    out: dict[str, list[str]] = {}
    for f in (flows.get("flows") or []):
        fid = f["name"].rsplit("/", 1)[-1]
        refs: set[str] = set()
        pages = cons.request("GET", cons._url(f"{base}/flows/{fid}/pages",
                                                pageSize="100"))
        for pg in (pages.get("pages") or []):
            for tr in (pg.get("transitionRoutes") or []):
                if tr.get("targetPlaybook"):
                    refs.add(tr["targetPlaybook"])
        fl = cons.request("GET", cons._url(f"{base}/flows/{fid}"))
        for tr in (fl.get("transitionRoutes") or []):
            if tr.get("targetPlaybook"):
                refs.add(tr["targetPlaybook"])
        queue = list(refs)
        while queue:
            ref_name = queue.pop()
            try:
                pb = cons.request("GET", cons._url("/" + ref_name))
            except Exception:
                continue
            for ref in (pb.get("referencedPlaybooks") or []):
                if ref and ref not in refs:
                    refs.add(ref)
                    queue.append(ref)
        out[fid] = sorted(refs)
    return out


def versions(cons: ConsoleApi, base: str) -> list[tuple[str, str]]:
    """[(flow_id, version_name)] with one version per flow (reuses if there is one)."""
    flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
    out = []
    for f in (flows.get("flows") or []):
        fid = f["name"].rsplit("/", 1)[-1]
        v = cons.request("GET", cons._url(f"{base}/flows/{fid}/versions",
                                         pageSize="5"))
        vs = v.get("versions") or []
        if vs:
            out.append((fid, vs[0]["name"]))
    return out


def candidates(playbooks: list[str]) -> list[tuple[str, object]]:
    """(name, field value) -> they are tried inside `versionConfigs[i]`."""
    return [
        ("playbookConfigs[{playbook}]", [{"playbook": p} for p in playbooks]),
        ("playbookConfigs[{name}]", [{"name": p} for p in playbooks]),
        ("playbookConfigs[{playbook,version}]",
         [{"playbook": p, "version": p} for p in playbooks]),
        ("playbooks[]", list(playbooks)),
        ("playbookNames[]", list(playbooks)),
        ("includedPlaybooks[]", list(playbooks)),
        ("playbookConfigs[{playbookId}]",
         [{"playbookId": p.rsplit("/", 1)[-1]} for p in playbooks]),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    _project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    base = f"/projects/{cfg.project}/locations/{cfg.location}/agents/{cfg.only_agent()}" \
        if args.project == "" else \
        f"/projects/{args.project}/locations/{cfg.location}/agents/{agent}"

    pairs = versions(cons, base)
    pbs = playbooks_by_flow(cons, base)
    print(f"flows with a version: {len(pairs)}")
    total = sorted({p for v in pbs.values() for p in v})
    print(f"referenced playbooks: {len(total)}")

    # The flow that references the playbooks is the one that brings them in:
    by_flow = {fid: pbs.get(fid, []) for fid, _ in pairs}

    errors: dict[str, str] = {}
    for field_name, value in candidates(total):
        vcs = []
        for fid, version in pairs:
            vc = {"version": version}
            pbs_flow = by_flow.get(fid) or []
            if pbs_flow:
                vc[field_name] = (value if field_name in
                                    ("playbooks[]", "playbookNames[]",
                                     "includedPlaybooks[]")
                                    else [{"playbook": p} if "playbook" in
                                          str(list(value)[:1]) else
                                          ({"name": p} if "name" in
                                           str(list(value)[:1]) else
                                           {"playbookId": p})
                                          for p in pbs_flow])
            vcs.append(vc)
        body = {"displayName": f"zz_tmp_env_{uuid.uuid4().hex[:6]}",
                  "description": "temporary: looking for the playbooks field",
                  "versionConfigs": vcs}
        try:
            op = wait_for(cons, cons.request(
                "POST", cons._url(f"{base}/environments"), body=body))
            response = op.get("response") or {}
            name = response.get("name") or ""
            if name:
                print(f"  {field_name:<32} -> CREATED ({name})")
                cons.request("DELETE", cons._url("/" + name))
                print("      (deleted)")
                errors[field_name] = "CREATED"
                continue
            err = json.dumps(op.get("error"), ensure_ascii=False)
        except Exception as exc:
            err = str(exc)
        if any(m in err for m in ERROR_BASE):
            errors[field_name] = "same error (field ignored)"
        else:
            errors[field_name] = "DIFFERENT ERROR: " + err[:200]
        print(f"  {field_name:<32} -> {errors[field_name][:150]}")

    print()
    for k, v in errors.items():
        print(f"  {k:<32} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
