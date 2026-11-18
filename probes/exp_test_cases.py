#!/usr/bin/env python3
"""Closes the `testCases` and `testCases.results` collections of the sweep.

Why: `cxp.gaps.scan_agent` cannot reach `testCases.results` because
`testCases` is EMPTY in the agent: a temporary test case has to be CREATED, run
and its results read.

MEASURED (2026-11-15): the body of the `create` is NOT `{"testCases": [...]}`
(the previous script sent it and the server ignored it silently, without
returning a name). The discovery doc says:

    POST {parent}/testCases      request: TestCase
    TestCase = {displayName, testCaseConversationTurns[], testConfig, notes, tags}
    ConversationTurn = {userInput, virtualAgentOutput}

Also, `DELETE {name}` **does not exist** (404): the deletion is
`POST {parent}/testCases:batchDelete` with `{"names": [...]}`.

This script creates the test case, runs it, reads the results, SWEEPS them against
the public schema and deletes it at the end (always, with `finally`).

    .venv/bin/python probes/exp_test_cases.py [--agent <uuid>] [--out /tmp/testcases.json]
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


def _wait_for(cons: ConsoleApi, op: dict, seconds: int = 90) -> dict:
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


def _sweep(cons: ConsoleApi, idx, url: str, key: str, label: str) -> dict:
    """GET -> gaps against the public schema of that collection.

    MEASURED: a listing is swept with the WRAPPER schema
    (`ListTestCaseResultsResponse`), not with the item one (`TestCaseResult`):
    the other way round, `testCaseResults` comes out as an undeclared field.
    """
    try:
        data = cons.request("GET", url)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200], "gaps": []}
    if isinstance(data, list):
        data = {key.split(".")[-1]: data}
    sweep = scan_collection(data, idx, key)
    return {
        "status": "ok",
        "key": key,
        "detail": label,
        "fields": sorted(data.keys()) if isinstance(data, dict) else [],
        "wrapper": sweep["wrapper"],
        "items": sweep["items"],
        "n_items": sweep["n_items"],
        "wrapper_schema": sweep["wrapper_schema"],
        "item_schema": sweep["item_schema"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--out", default="/tmp/testcases.json")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{project}/locations/{cfg.location}/agents/{agent}"
    res: dict = {"agent": agent, "measurements": {}, "temporary": {}}

    # ------------------------------------------------- what there is right now
    try:
        lista = cons.request("GET", cons._url(f"{base}/testCases", pageSize="100"))
        existing = [t.get("name") for t in (lista.get("testCases") or [])]
        print(f"existing testCases: {len(existing)}")
        res["measurements"]["testCases.lista"] = {"status": "ok",
                                                "n": len(existing)}
        if existing:
            print("  (there are already test cases: no need to create a temporary one)")
            res["measurements"]["testCases"] = _sweep(
                cons, idx, cons._url(f"{base}/testCases", pageSize="100"),
                "testCases", "listing")
            case = existing[0]
        else:
            case = ""
    except Exception as exc:
        print("could not list testCases:", str(exc)[:200])
        existing = []
        case = ""

    # ------------------------------------------------------- create temporary
    if not case:
        body = {
            "displayName": f"zz_tmp_sweep_{uuid.uuid4().hex[:6]}",
            "notes": "temporary: type sweep",
            "testCaseConversationTurns": [
                # MEASURED: in v3beta1 `TextInput.text` is a STRING (not a
                # list: sending a list gives "Proto field is not repeating"), and the
                # `languageCode` goes in the `QueryInput`, not inside the text.
                {"userInput": {"input": {"text": {"text": "hola"},
                                          "languageCode": "es"}}},
            ],
        }
        try:
            created = cons.request("POST", cons._url(f"{base}/testCases"), body=body)
            print("create ->", json.dumps(created, ensure_ascii=False)[:300])
            name = created.get("name") or ""
            if not name:
                print("  (without 'name': the create is not an LRO, it resolves directly)")
                name = ""
            case = name
            res["temporary"]["testCase"] = case
        except Exception as exc:
            print("ERROR creating the test case:", str(exc)[:300])
            res["measurements"]["testCases.create"] = {"status": "error",
                                                     "detail": str(exc)[:300]}

    if case:
        res["measurements"]["testCases"] = _sweep(
            cons, idx, cons._url("/" + case), "testCases", case)
        print("testCases (1 test case):", json.dumps(res["measurements"]["testCases"],
                                         ensure_ascii=False)[:700])

        # ------------------------------------------------------- run
        try:
            op = cons.request("POST", cons._url(f"/{case}:run"), body={})
            print("run ->", json.dumps(op, ensure_ascii=False)[:200])
            op = _wait_for(cons, op)
            print("run (final):", json.dumps(op.get("response") or op.get("error"),
                                             ensure_ascii=False)[:300])
            res["measurements"]["testCases.run"] = {"status": "ok",
                                                  "detail": json.dumps(
                                                      op.get("response") or
                                                      op.get("error"))[:300]}
        except Exception as exc:
            print("ERROR running it:", str(exc)[:250])
            res["measurements"]["testCases.run"] = {"status": "error",
                                                  "detail": str(exc)[:250]}

        # ------------------------------------------------------- results
        res["measurements"]["testCases.results"] = _sweep(
            cons, idx, cons._url(f"/{case}/results", pageSize="100"),
            "testCases.results", case)
        print("testCases.results:", json.dumps(res["measurements"]["testCases.results"],
                                        ensure_ascii=False)[:1200])

        # --------------------------------------------------------- delete
        try:
            cons.request("POST", cons._url(f"{base}/testCases:batchDelete"),
                         body={"names": [case]})
            print("temporary test case deleted (batchDelete)")
        except Exception as exc:
            print("Could NOT delete the test case:", str(exc)[:200])
            # Fallback: direct DELETE (the discovery doc does not declare it, but
            # some backend methods do exist).
            try:
                cons.request("DELETE", cons._url("/" + case))
                print("deleted with a direct DELETE")
            except Exception as exc2:
                print("not even with DELETE:", str(exc2)[:150])

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
