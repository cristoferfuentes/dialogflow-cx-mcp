#!/usr/bin/env python3
"""Temporary LAB agent to sweep the `environments*` collections.

Why: the working agent CANNOT have environments. MEASURED (2026-11-15):

  * The environment has to include the version of **all** the flows, and of every
    playbook/tool that the published graph references (transitive closure).
    `versionConfigs[].version` is a GENERIC resource name: it works for
    `flows/*/versions/*`, `playbooks/*/versions/*` and `tools/*/versions/*`.
  * The working agent references `df-code-interpreter-tool` (a SYSTEM tool):
    it cannot be versioned (`POST tools/.../versions` -> 404 "does not exist in
    the agent") and its name does not match the version template -> **the environment
    is IMPOSSIBLE to create on that agent**.

    Neither the name nor a canonical version works: the server replies
    "[ResourceName error] ... does not match template
    'projects/*/locations/*/agents/*/flows/*/versions/*'".

Solution: a lab agent with a minimal graph (one flow, one page,
no system tools), in which the environment CAN be created. Everything that is
created is DELETED at the end (the whole agent).

    .venv/bin/python probes/exp_lab_agent.py [--out /tmp/lab_agent.json]
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
from cxp.gaps import load_schema_index, scan_collection


def wait_for(cons: ConsoleApi, op: dict, seconds: int = 240) -> dict:
    name = op.get("name")
    if not name or op.get("done") or "/operations/" not in name:
        return op
    for _ in range(seconds * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + name))
        if op.get("done"):
            return op
    return op


def sweep(cons: ConsoleApi, idx, url: str, key: str, label: str) -> dict:
    try:
        data = cons.request("GET", url)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:250]}
    if isinstance(data, list):
        data = {key.split(".")[-1]: data}
    b = scan_collection(data, idx, key)
    return {"status": "ok", "key": key, "detail": label,
            "fields": sorted(data.keys()) if isinstance(data, dict) else [],
            "wrapper": b["wrapper"], "items": b["items"],
            "n_items": b["n_items"],
            "wrapper_schema": b["wrapper_schema"],
            "item_schema": b["item_schema"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--out", default="/tmp/lab_agent.json")
    args = ap.parse_args()

    cfg = load_config()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{project}/locations/{cfg.location}/agents"
    res: dict = {"project": project, "measurements": {}, "temporary": {}}
    agent = ""
    flow = ""
    environment = ""

    try:
        # ------------------------------------------------ 1) lab agent
        body = {"displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
                 "defaultLanguageCode": "es",
                 "timeZone": "America/Santiago",
                 "description": "temporary: environment sweep"}
        created = cons.request("POST", cons._url(base), body=body)
        agent = created.get("name") or ""
        if not agent:
            print("Could NOT create the agent:", json.dumps(created,
                                                           ensure_ascii=False)[:300])
            return 1
        res["temporary"]["agent"] = agent
        print("lab agent:", agent)
        # The guard only allows writes on allowlisted agents: this one is
        # new, so it is added IN MEMORY (the .env is not touched).
        cfg.agent_readwrite = list(cfg.agent_readwrite) + [agent.rsplit("/", 1)[-1]]
        abase = base + "/" + agent.rsplit("/", 1)[-1]

        # ------------------------------------------------------ 2) flow and page
        f = cons.request("POST", cons._url(f"{abase}/flows"),
                         body={"displayName": "lab_flow",
                               "transitionRoutes": [],
                               "eventHandlers": []})
        flow = f.get("name") or ""
        print("flow:", flow)
        res["temporary"]["flow"] = flow
        fid = flow.rsplit("/", 1)[-1]
        cons.request("POST", cons._url(f"{abase}/flows/{fid}/pages"),
                     body={"displayName": "lab_page",
                           "entryPoints": [{"languageCode": "es",
                                            "name": "lab.entry"}],
                           "transitionRoutes": [],
                           "eventHandlers": []})

        # ---------------------------------------------------- 3) versions
        # MEASURED: the environment requires a version of ALL the flows, and also of the
        # agent's "start flow": otherwise, "Version must be provided for start
        # flow resource".
        version = ""
        vcs: list[dict] = []
        flows = cons.request("GET", cons._url(f"{abase}/flows", pageSize="100"))
        for fl in (flows.get("flows") or []):
            flow_id = fl["name"].rsplit("/", 1)[-1]
            op = wait_for(cons, cons.request(
                "POST", cons._url(f"{abase}/flows/{flow_id}/versions"),
                body={"displayName": f"lab_version_{uuid.uuid4().hex[:4]}",
                      "description": "temporary: sweep"}))
            v = (op.get("response") or {}).get("name") or ""
            if v:
                vcs.append({"version": v})
                if not version:
                    version = v
        print("versions:", len(vcs))
        res["temporary"]["version"] = version
        if not vcs:
            return 1

        # ------------------------------------------------------ 4) environment
        op = wait_for(cons, cons.request(
            "POST", cons._url(f"{abase}/environments"),
            body={"displayName": "lab_environment", "description": "temporary",
                  "versionConfigs": vcs}))
        response = op.get("response") or {}
        environment = response.get("name") or ""
        if not environment:
            print("Could NOT create the environment:",
                  json.dumps(op.get("error"), ensure_ascii=False)[:400])
            res["measurements"]["environments.create"] = {
                "status": "error",
                "detail": json.dumps(op.get("error"), ensure_ascii=False)[:400]}
            return 1
        res["temporary"]["environment"] = environment
        print("environment:", environment)

        # ------------------------------------------------------- 5) sweep
        res["measurements"]["environments"] = sweep(
            cons, idx, cons._url("/" + environment), "environments", environment)
        print("environments:", json.dumps(res["measurements"]["environments"],
                                           ensure_ascii=False)[:700])

        # deployment
        op = wait_for(cons, cons.request(
            "POST", cons._url(f"/{environment}/deployments"),
            body={"flowVersion": version}))
        deployment = (op.get("response") or {}).get("name") or op.get("name") or ""
        print("deployment:", deployment or json.dumps(op.get("error"),
                                               ensure_ascii=False)[:200])
        print("  raw op:", json.dumps(op, ensure_ascii=False)[:400])
        if deployment:
            res["measurements"]["environments.deployments"] = sweep(
                cons, idx, cons._url("/" + deployment), "environments.deployments", deployment)
            print("  deployments:", json.dumps(
                res["measurements"]["environments.deployments"],
                ensure_ascii=False)[:600])

        # experiment
        op = wait_for(cons, cons.request(
            "POST", cons._url(f"/{environment}/experiments"),
            body={"displayName": "lab_experiment", "description": "temporary",
                  "definition": {"environment": environment}}))
        experiment = (op.get("response") or {}).get("name") or op.get("name") or ""
        print("experiment:", experiment or json.dumps(op.get("error"),
                                               ensure_ascii=False)[:200])
        print("  raw op:", json.dumps(op, ensure_ascii=False)[:400])
        if experiment:
            res["temporary"]["experiment"] = experiment
            res["measurements"]["environments.experiments"] = sweep(
                cons, idx, cons._url("/" + experiment), "environments.experiments", experiment)
            print("  experiments:", json.dumps(
                res["measurements"]["environments.experiments"],
                ensure_ascii=False)[:600])

        # continuous results
        res["measurements"]["environments.continuousTestResults"] = sweep(
            cons, idx, cons._url(f"/{environment}/continuousTestResults"),
            "environments.continuousTestResults", environment)
        print("continuousTestResults:", json.dumps(
            res["measurements"]["environments.continuousTestResults"],
            ensure_ascii=False)[:500])

        # session inside the environment
        sid = f"lab-{uuid.uuid4().hex[:8]}"
        try:
            cons.request("POST", cons._url(f"/{environment}/sessions/{sid}:detectIntent"),
                         body={"queryInput": {"text": {"text": "hola"},
                                              "languageCode": "es"}})
            res["measurements"]["environments.sessions.entityTypes"] = sweep(
                cons, idx, cons._url(f"/{environment}/sessions/{sid}/entityTypes"),
                "environments.sessions.entityTypes", sid)
            print("sessions.entityTypes:", json.dumps(
                res["measurements"]["environments.sessions.entityTypes"],
                ensure_ascii=False)[:500])
        except Exception as exc:
            print("ERROR in the session:", str(exc)[:200])
            res["measurements"]["environments.sessions.entityTypes"] = {
                "status": "error", "detail": str(exc)[:250]}

    finally:
        # ------------------------------------------------------- cleanup
        if res["temporary"].get("experiment"):
            try:
                cons.request("DELETE",
                             cons._url("/" + res["temporary"]["experiment"]))
                print("experiment deleted")
            except Exception as exc:
                print("Could NOT delete the experiment:", str(exc)[:150])
        if environment:
            try:
                cons.request("DELETE", cons._url("/" + environment))
                print("environment deleted")
            except Exception as exc:
                print("Could NOT delete the environment:", str(exc)[:200])
        if agent:
            try:
                cons.request("DELETE", cons._url("/" + agent))
                print("lab agent deleted")
            except Exception as exc:
                print("Could NOT delete the agent:", str(exc)[:250])

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
