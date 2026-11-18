#!/usr/bin/env python3
"""Discovers the environment's playbook field by sending a GARBAGE playbook.

The server silently ignores the fields it does not know. But if the field
EXISTS and the value does not, the error changes: it starts talking about the
garbage playbook ("Playbook 'NO_EXISTE' not found") instead of "not included in
the environment".

=> With a garbage value you can **tell "unknown field" from "known field"**
without creating any playbook and without creating any environment.

    .venv/bin/python probes/exp_env_playbook_disk.py
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


def wait_for(cons: ConsoleApi, op: dict, seconds: int = 90) -> dict:
    name = op.get("name")
    if not name or op.get("done") or "/operations/" not in name:
        return op
    for _ in range(seconds * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + name))
        if op.get("done"):
            return op
    return op


BASURA = ("projects/my-project/locations/global/agents/"
          "11111111-1111-4111-8111-111111111111/playbooks/NO_EXISTE_0000")

# (where, field name, value)
CANDIDATOS: list[tuple[str, str, object]] = [
    ("versionConfigs", "playbookConfigs", [{"playbook": BASURA}]),
    ("versionConfigs", "playbookConfigs", [{"name": BASURA}]),
    ("versionConfigs", "playbookConfigs", [{"playbook": BASURA, "version": BASURA}]),
    ("versionConfigs", "playbooks", [BASURA]),
    ("versionConfigs", "playbookNames", [BASURA]),
    ("versionConfigs", "includedPlaybooks", [BASURA]),
    ("versionConfigs", "referencedPlaybooks", [BASURA]),
    ("versionConfigs", "playbookVersions", [BASURA]),
    ("versionConfigs", "playbooksToInclude", [BASURA]),
    ("versionConfigs", "playbook", BASURA),
    ("root", "playbookConfigs", [{"playbook": BASURA}]),
    ("root", "playbooks", [BASURA]),
    ("root", "includedPlaybooks", [BASURA]),
    ("root", "playbookNames", [BASURA]),
    ("root", "playbookVersions", [BASURA]),
    ("root", "referencedPlaybooks", [BASURA]),
]


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

    flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
    pairs = []
    for f in (flows.get("flows") or []):
        fid = f["name"].rsplit("/", 1)[-1]
        v = cons.request("GET", cons._url(f"{base}/flows/{fid}/versions", pageSize="5"))
        vs = v.get("versions") or []
        if vs:
            pairs.append(vs[0]["name"])
    print(f"versions: {len(pairs)}")

    print()
    for where, field, value in CANDIDATOS:
        vcs = [{"version": v} for v in pairs]
        if where == "versionConfigs":
            vcs[0][field] = value
        body = {"displayName": f"zz_tmp_env_{uuid.uuid4().hex[:6]}",
                  "description": "temporary: looking for the field",
                  "versionConfigs": vcs}
        if where == "root":
            body[field] = value
        try:
            op = wait_for(cons, cons.request(
                "POST", cons._url(f"{base}/environments"), body=body))
            response = op.get("response") or {}
            if response.get("name"):
                print(f"  {where}.{field:<22} -> CREATED (valid field?)")
                cons.request("DELETE", cons._url("/" + response["name"]))
                print("      deleted")
                continue
            err = json.dumps(op.get("error"), ensure_ascii=False)
        except Exception as exc:
            err = str(exc)
        if "NO_EXISTE_0000" in err:
            mark = "  <== THE FIELD IS READ (the server names the garbage)"
        elif "not included in the environment" in err:
            mark = "  (field ignored: the usual error)"
        else:
            mark = "  <== DIFFERENT ERROR"
        print(f"  {where}.{field:<22} -> {err[:150]}{mark}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
