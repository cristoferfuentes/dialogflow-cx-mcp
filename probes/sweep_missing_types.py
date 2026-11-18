#!/usr/bin/env python3
"""Measures the resource types that the sweep does NOT reach in a normal agent.

The generic sweep (`cxp.gaps.scan_agent`) lists ALL the collections that the
discovery doc declares, but there are types that hang off a resource a "normal"
agent does not have:

  * `environments`              -> you have to CREATE an environment (temporary).
  * `environments.deployments`  -> you have to deploy a version.
  * `environments.experiments`  -> you have to create an experiment.
  * `environments.sessions.entityTypes` -> you need a session id.
  * `testCases.results`         -> you have to create a test case and run it.
  * `sessions.entityTypes`      -> sessions are not listed: you have to do a
                                   `detectIntent` to get a sessionId.

This script writes **only to the given agent** (by default, the one from the
`.env`) and deletes at the end whatever can be deleted.

    .venv/bin/python probes/sweep_missing_types.py [--agent <uuid>]
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
from cxp.gaps import find_gaps, group_gaps, load_schema_index, norm_schema


def _is_operation(name: str) -> bool:
    """An LRO has '/operations/' in its name; a resource does not."""
    return "/operations/" in (name or "")


def _wait_for(cons: ConsoleApi, op: dict, seconds: int = 60) -> dict:
    """Follows an operation (LRO) until it finishes. Returns the operation."""
    name = op.get("name")
    if not name or op.get("done") or not _is_operation(name):
        return op
    for _ in range(seconds * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + name))
        if op.get("done"):
            return op
    return op


def _scan(cons, idx, url, key, label=""):
    """GET url -> gaps against the public schema of that collection."""
    try:
        data = cons.request("GET", url)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200], "gaps": []}
    if isinstance(data, list):
        data = {key.split(".")[-1]: data}
    schema = idx.schema_of(key)
    gaps = find_gaps(data, schema, idx)
    return {
        "status": "ok",
        "key": key,
        "schema": norm_schema(schema),
        "detail": label,
        "fields": sorted(data.keys()) if isinstance(data, dict) else [],
        "gaps": group_gaps(gaps),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--out", default="/tmp/sweep_missing_types.json")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{project}/locations/{cfg.location}/agents/{agent}"
    res: dict = {"agent": agent, "measurements": {}, "temporary": {}}
    print(f"agent: {agent}")

    def record(key, r):
        res["measurements"][key] = r
        print(f"{key}: {json.dumps(r, ensure_ascii=False)[:400]}")

    # ---------------------------------------------------- 1) flow version
    versions: list[str] = []
    try:
        flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
        for flow in (flows.get("flows") or []):
            fid = flow.get("name", "").rsplit("/", 1)[-1]
            # If the flow already has versions, the first one is reused.
            # MEASURED (2026-11-15): flow versions **CAN** be deleted
            # (`DELETE flows/{f}/versions/{v}`); this script does it at the
            # end. (An earlier comment said they could not, and that was false.)
            existing = cons.request("GET",
                                      cons._url(f"{base}/flows/{fid}/versions",
                                                pageSize="10"))
            existing_versions = existing.get("versions") or []
            if existing_versions:
                versions.append(existing_versions[0].get("name"))
                continue
            op = cons.request("POST", cons._url(f"{base}/flows/{fid}/versions"),
                              body={"displayName": f"zz_tmp_sweep_{uuid.uuid4().hex[:6]}",
                                    "description": "temporary: type sweep"})
            op = _wait_for(cons, op)
            response = op.get("response") or {}
            name = response.get("name") or (op.get("name")
                                          if not _is_operation(op.get("name", "")) else "")
            if name:
                versions.append(name)
        res["temporary"]["versions"] = versions
        print(f"versions created: {len(versions)}")
        record("flows.versions", {"status": "ok" if versions else "error",
                                 "detail": "", "versions": versions})
    except Exception as exc:
        record("flows.versions", {"status": "error", "detail": str(exc)[:200]})
    version = versions[0] if versions else ""

    # --------------------------------------------------------- 2) environment
    environment = ""
    if version:
        try:
            op = cons.request("POST", cons._url(f"{base}/environments"),
                              body={"displayName": f"zz_tmp_sweep_{uuid.uuid4().hex[:6]}",
                                    "description": "temporary: type sweep",
                                    "versionConfigs": [{"version": v} for v in versions]})
            op = _wait_for(cons, op)
            response = op.get("response") or {}
            environment = response.get("name") or ""
            res["temporary"]["environment"] = environment
            print(f"environment: {environment} (error: {op.get('error')})")
            if environment:
                record("environments", _scan(cons, idx, cons._url("/" + environment),
                                            "environments", environment))
        except Exception as exc:
            record("environments", {"status": "error", "detail": str(exc)[:200]})

    # -------------------------------------------- 3) deployment and experiment
    if environment:
        try:
            op = cons.request("POST", cons._url(f"/{environment}/deployments"),
                              body={"flowVersion": version})
            op = _wait_for(cons, op)
            response = op.get("response") or {}
            print(f"deployment: {response.get('name')} (error: {op.get('error')})")
            if response.get("name"):
                record("environments.deployments",
                      _scan(cons, idx, cons._url("/" + response["name"]),
                            "environments.deployments", response["name"]))
        except Exception as exc:
            record("environments.deployments", {"status": "error", "detail": str(exc)[:200]})

        try:
            op = cons.request("POST", cons._url(f"/{environment}/experiments"),
                              body={"displayName": f"zz_tmp_sweep_{uuid.uuid4().hex[:6]}",
                                    "description": "temporary: type sweep",
                                    "definition": {"environment": environment}})
            op = _wait_for(cons, op)
            response = op.get("response") or {}
            print(f"experiment: {response.get('name')} (error: {op.get('error')})")
            if response.get("name"):
                record("environments.experiments",
                      _scan(cons, idx, cons._url("/" + response["name"]),
                            "environments.experiments", response["name"]))
        except Exception as exc:
            record("environments.experiments", {"status": "error", "detail": str(exc)[:200]})

        # The sessions of the environment
        try:
            d = cons.request("GET", cons._url(f"/{environment}/sessions", pageSize="20"))
            print("environment sessions:", d)
        except Exception as exc:
            print("environment sessions: ERR", str(exc)[:120])

    # ------------------------------------------------------- 4) test case
    case = ""
    try:
        op = cons.request("POST", cons._url(f"{base}/testCases"),
                          body={"displayName": f"zz_tmp_sweep_{uuid.uuid4().hex[:6]}",
                                "testCases": [{"displayName": "temporary",
                                               "input": {"text": "hola",
                                                         "languageCode": "es"}}]})
        op = _wait_for(cons, op)
        response = op.get("response") or {}
        case = response.get("name") or ""
        res["temporary"]["case"] = case
        print(f"testCase: {case} (error: {op.get('error')})")
        if case:
            record("testCases", _scan(cons, idx, cons._url("/" + case), "testCases", case))
            # Run it -> results
            try:
                op = cons.request("POST", cons._url(f"/{case}:run"), body={})
                op = _wait_for(cons, op)
                print("run:", str(op.get("response") or op.get("error"))[:200])
            except Exception as exc:
                print("run: ERR", str(exc)[:150])
            record("testCases.results",
                  _scan(cons, idx, cons._url(f"/{case}/results", pageSize="20"),
                        "testCases.results", case))
    except Exception as exc:
        record("testCases", {"status": "error", "detail": str(exc)[:200]})

    # ------------------------------------------------------------ 5) session
    try:
        sid = f"sweep-{uuid.uuid4().hex[:10]}"
        # MEASURED: `queryInput.languageCode` (not `queryInput.text.languageCode`).
        cons.request("POST", cons._url(f"{base}/sessions/{sid}:detectIntent"),
                     body={"queryInput": {"text": {"text": "hola"},
                                          "languageCode": "es"}})
        print("sessionId:", sid)
        if sid:
            record("sessions.entityTypes",
                  _scan(cons, idx, cons._url(f"{base}/sessions/{sid}/entityTypes"),
                        "sessions.entityTypes", sid))
    except Exception as exc:
        record("sessions.entityTypes", {"status": "error", "detail": str(exc)[:200]})

    # ------------------------------------------------------------- cleanup
    for label, name in (("case", case), ("environment", environment)):
        if not name:
            continue
        try:
            cons.request("DELETE", cons._url("/" + name))
            print(f"temporary {label} deleted")
        except Exception as exc:
            print(f"Could NOT delete {label}: {str(exc)[:200]}")

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
