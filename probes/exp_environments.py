#!/usr/bin/env python3
"""Closes the `environments*` collections of the sweep: creates a temporary ENVIRONMENT.

Why: `cxp.gaps.scan_agent` cannot reach `environments.deployments`,
`environments.experiments`, `environments.continuousTestResults` nor
`environments.sessions.entityTypes` because the agent has no environment.
One has to be CREATED.

MEASURED (2026-11-15) - THE THREE ENVIRONMENT RULES:

1. The environment has to include the version of **ALL** the flows:
   "Flow 'X' is referenced in version ... but not included in the environment".

2. And the version of **ALL the playbooks** that reference (directly or
   indirectly) the routes of those flows:
   "Playbook 'P' is referenced in version ... but not included in the environment".

   CAREFUL: `versionConfigs[].version` is not only a flow: it also accepts the
   name of a **playbook version**
   (`.../playbooks/{id}/versions/{v}`). That is what makes the error
   move on to the next level.

3. And the version of the **TOOLS** that those playbooks reference
   ("Tool 'T' is referenced in version ... of playbook 'P', but not included").
   Same trick: `.../tools/{id}/versions/{v}`.

   The closure is TRANSITIVE: playbook -> playbook -> tool.

None of those extra fields is in the public discovery doc: `versionConfigs`
only declares `version`, and `version` turns out to be a GENERIC resource name
(it works for flows, playbooks and tools).

The script creates what is missing (temporary versions of playbooks/tools), sweeps
the environment and its sub-resources, and **deletes everything** in a `finally`.

    .venv/bin/python probes/exp_environments.py [--agent <uuid>] [--out /tmp/environments.json]
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


def sweep(cons: ConsoleApi, idx, url: str, key: str, label: str) -> dict:
    """GET -> gaps against the public schema (wrapper + items)."""
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
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--out", default="/tmp/environments.json")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{project}/locations/{cfg.location}/agents/{agent}"
    res: dict = {"agent": agent, "measurements": {}, "temporary": {}}
    created: list[str] = []      # temporary versions/resources to delete
    environment = ""

    try:
        # ---------------------------------------------- 1) flow versions
        flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
        vcs: list[dict] = []
        for f in (flows.get("flows") or []):
            fid = f["name"].rsplit("/", 1)[-1]
            v = cons.request("GET", cons._url(f"{base}/flows/{fid}/versions",
                                               pageSize="5"))
            vs = v.get("versions") or []
            if vs:
                vcs.append({"version": vs[0]["name"]})
            else:
                r = cons.request("POST", cons._url(f"{base}/flows/{fid}/versions"),
                                 body={"displayName":
                                       f"zz_tmp_sweep_{uuid.uuid4().hex[:6]}",
                                       "description": "temporary: sweep"})
                if r.get("name"):
                    vcs.append({"version": r["name"]})
        print(f"flow versions: {len(vcs)}")

        # ----------------------------- 2) playbook and tool closure
        playbooks: set[str] = set()
        for f in (flows.get("flows") or []):
            fid = f["name"].rsplit("/", 1)[-1]
            pages = cons.request("GET", cons._url(f"{base}/flows/{fid}/pages",
                                                     pageSize="100"))
            for pg in (pages.get("pages") or []):
                for tr in (pg.get("transitionRoutes") or []):
                    if tr.get("targetPlaybook"):
                        playbooks.add(tr["targetPlaybook"])
            fl = cons.request("GET", cons._url(f"{base}/flows/{fid}"))
            for tr in (fl.get("transitionRoutes") or []):
                if tr.get("targetPlaybook"):
                    playbooks.add(tr["targetPlaybook"])

        tools: set[str] = set()
        pending = list(playbooks)
        seen: set[str] = set()
        while pending:
            pb = pending.pop()
            if pb in seen:
                continue
            seen.add(pb)
            try:
                d = cons.request("GET", cons._url("/" + pb))
            except Exception:
                continue
            for ref in (d.get("referencedPlaybooks") or []):
                if ref and ref not in seen:
                    pending.append(ref)
            for ref in (d.get("referencedTools") or []):
                if ref:
                    tools.add(ref)
            for ref in (d.get("referencedFlows") or []):
                if ref:
                    print("  (referencedFlows ->", ref, ")")
        print(f"playbooks (closure): {len(seen)} | tools: {len(tools)}")

        # --------------------------------- 3) playbook and tool versions
        def version_of(name: str, label: str) -> str:
            """Existing (or created) version of a playbook/tool.

            MEASURED: there are tools that a playbook references but that **are not
            resources of the agent** (the system ones: `df-code-interpreter-tool`,
            `google-search`...): `POST tools/{name}/versions` answers 404
            "Tool 'X' does not exist in the agent". Those get no version (the
            environment brings them by default) and are skipped.
            """
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
            except Exception as exc:
                print(f"  (tool not versionable: {name.rsplit('/', 1)[-1]}: "
                      f"{str(exc)[:80]})")
                return ""
            if r.get("name"):
                created.append(r["name"])
                return r["name"]
            return ""

        for pb in sorted(seen):
            v = version_of(pb, "playbook")
            if v:
                vcs.append({"version": v})
        for tl in sorted(tools):
            v = version_of(tl, "tool")
            if v:
                vcs.append({"version": v})
        print(f"total versionConfigs: {len(vcs)}")

        # ------------------------------------------------ 4) create environment
        body = {"displayName": f"zz_tmp_sweep_{uuid.uuid4().hex[:6]}",
                  "description": "temporary: environment sweep",
                  "versionConfigs": vcs}
        op = wait_for(cons, cons.request("POST",
                                        cons._url(f"{base}/environments"),
                                        body=body))
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

        # -------------------------------------------------- 5) sweep everything
        res["measurements"]["environments"] = sweep(
            cons, idx, cons._url("/" + environment), "environments", environment)
        print("environments:", json.dumps(res["measurements"]["environments"],
                                           ensure_ascii=False)[:600])

        # deployment
        try:
            version_flow = vcs[0]["version"]
            op = wait_for(cons, cons.request(
                "POST", cons._url(f"/{environment}/deployments"),
                body={"flowVersion": version_flow}))
            response = op.get("response") or {}
            dep = response.get("name") or ""
            print("deployment:", dep or json.dumps(op.get("error"),
                                                   ensure_ascii=False)[:200])
            if dep:
                res["measurements"]["environments.deployments"] = sweep(
                    cons, idx, cons._url("/" + dep), "environments.deployments",
                    dep)
                print("  deployments:", json.dumps(
                    res["measurements"]["environments.deployments"],
                    ensure_ascii=False)[:500])
        except Exception as exc:
            print("ERROR in the deployment:", str(exc)[:250])
            res["measurements"]["environments.deployments"] = {"status": "error",
                                                              "detail": str(exc)[:250]}

        # experiment
        try:
            op = wait_for(cons, cons.request(
                "POST", cons._url(f"/{environment}/experiments"),
                body={"displayName":
                                            f"zz_tmp_sweep_{uuid.uuid4().hex[:6]}",
                                            "description": "temporary: sweep",
                                            "definition": {"environment": environment}}))
            response = op.get("response") or {}
            exp = response.get("name") or ""
            print("experiment:", exp or json.dumps(op.get("error"),
                                                   ensure_ascii=False)[:200])
            if exp:
                res["measurements"]["environments.experiments"] = sweep(
                    cons, idx, cons._url("/" + exp), "environments.experiments", exp)
                print("  experiments:", json.dumps(
                    res["measurements"]["environments.experiments"],
                    ensure_ascii=False)[:500])
                res["temporary"]["experiment"] = exp
        except Exception as exc:
            print("ERROR in the experiment:", str(exc)[:250])
            res["measurements"]["environments.experiments"] = {"status": "error",
                                                              "detail": str(exc)[:250]}

        # continuous results
        res["measurements"]["environments.continuousTestResults"] = sweep(
            cons, idx, cons._url(f"/{environment}/continuousTestResults"),
            "environments.continuousTestResults", environment)
        print("continuousTestResults:", json.dumps(
            res["measurements"]["environments.continuousTestResults"],
            ensure_ascii=False)[:400])

        # session INSIDE the environment (here entityTypes can be created)
        sid = f"sweep-{uuid.uuid4().hex[:10]}"
        try:
            cons.request("POST",
                         cons._url(f"/{environment}/sessions/{sid}:detectIntent"),
                         body={"queryInput": {"text": {"text": "ok"},
                                              "languageCode": "es"}})
            res["measurements"]["environments.sessions.entityTypes"] = sweep(
                cons, idx, cons._url(f"/{environment}/sessions/{sid}/entityTypes"),
                "environments.sessions.entityTypes", sid)
            print("sessions.entityTypes (in the environment):", json.dumps(
                res["measurements"]["environments.sessions.entityTypes"],
                ensure_ascii=False)[:500])
            # create a real session entity override
            entity_types = cons.request("GET", cons._url(f"{base}/entityTypes",
                                                pageSize="100"))
            type = next((e for e in (entity_types.get("entityTypes") or [])
                         if e.get("displayName") == "Confirmacion_negacion"), None)
            if type:
                det = cons.request("GET", cons._url("/" + type["name"]))
                entity_type_name = f"{environment}/sessions/{sid}/entityTypes/{uuid.uuid4().hex[:8]}"
                r = cons.request("POST", cons._url(f"/{environment}/sessions/{sid}"
                                                    f"/entityTypes"),
                                 body={"name": entity_type_name,
                                       "entityOverrideMode":
                                           "ENTITY_OVERRIDE_MODE_UNSPECIFIED",
                                       "entities": (det.get("entities") or [])[:2]})
                print("create session entityType ->",
                      json.dumps(r, ensure_ascii=False)[:250])
                b = scan_collection(r, idx, "environments.sessions.entityTypes")
                res["measurements"]["environments.sessions.entityTypes.create"] = {
                    "status": "ok", "name": r.get("name"), **b}
                print("  sweep:", json.dumps(b, ensure_ascii=False)[:400])
        except Exception as exc:
            print("ERROR in the environment session:", str(exc)[:250])
            res["measurements"]["environments.sessions.entityTypes"] = {
                "status": "error", "detail": str(exc)[:250]}

    finally:
        # ------------------------------------------------------- cleanup
        if res["temporary"].get("experiment"):
            try:
                cons.request("DELETE", cons._url("/" + res["temporary"]["experiment"]))
                print("experiment deleted")
            except Exception as exc:
                print("Could NOT delete the experiment:", str(exc)[:150])
        if environment:
            try:
                cons.request("DELETE", cons._url("/" + environment))
                print("temporary environment deleted")
            except Exception as exc:
                print("Could NOT delete the environment:", str(exc)[:200])
        for name in created:
            try:
                cons.request("DELETE", cons._url("/" + name))
            except Exception:
                pass
        if created:
            print(f"{len(created)} temporary versions deleted")

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
