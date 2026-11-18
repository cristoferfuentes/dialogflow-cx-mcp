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

    .venv/bin/python probes/exp_test_cases.py [--agente <uuid>] [--out /tmp/testcases.json]
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
from cxp.gaps import load_schema_index, scan_coleccion


def _esperar(cons: ConsoleApi, op: dict, segundos: int = 90) -> dict:
    """Follows an LRO until it finishes."""
    nombre = op.get("name")
    if not nombre or op.get("done") or "/operations/" not in nombre:
        return op
    for _ in range(segundos * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + nombre))
        if op.get("done"):
            return op
    return op


def _barrer(cons: ConsoleApi, idx, url: str, clave: str, etiqueta: str) -> dict:
    """GET -> gaps against the public schema of that collection.

    MEASURED: a listing is swept with the WRAPPER schema
    (`ListTestCaseResultsResponse`), not with the item one (`TestCaseResult`):
    the other way round, `testCaseResults` comes out as an undeclared field.
    """
    try:
        data = cons.request("GET", url)
    except Exception as exc:
        return {"estado": "error", "detalle": str(exc)[:200], "huecos": []}
    if isinstance(data, list):
        data = {clave.split(".")[-1]: data}
    barrido = scan_coleccion(data, idx, clave)
    return {
        "estado": "ok",
        "clave": clave,
        "detalle": etiqueta,
        "campos": sorted(data.keys()) if isinstance(data, dict) else [],
        "envoltorio": barrido["envoltorio"],
        "items": barrido["items"],
        "n_items": barrido["n_items"],
        "esquema_envoltorio": barrido["esquema_envoltorio"],
        "esquema_item": barrido["esquema_item"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agente", default="")
    ap.add_argument("--proyecto", default="")
    ap.add_argument("--out", default="/tmp/testcases.json")
    args = ap.parse_args()

    cfg = load_config()
    agente = args.agente or cfg.only_agent()
    proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{proyecto}/locations/{cfg.location}/agents/{agente}"
    res: dict = {"agente": agente, "mediciones": {}, "temporales": {}}

    # ------------------------------------------------- what there is right now
    try:
        lista = cons.request("GET", cons._url(f"{base}/testCases", pageSize="100"))
        existentes = [t.get("name") for t in (lista.get("testCases") or [])]
        print(f"existing testCases: {len(existentes)}")
        res["mediciones"]["testCases.lista"] = {"estado": "ok",
                                                "n": len(existentes)}
        if existentes:
            print("  (there are already test cases: no need to create a temporary one)")
            res["mediciones"]["testCases"] = _barrer(
                cons, idx, cons._url(f"{base}/testCases", pageSize="100"),
                "testCases", "listing")
            caso = existentes[0]
        else:
            caso = ""
    except Exception as exc:
        print("could not list testCases:", str(exc)[:200])
        existentes = []
        caso = ""

    # ------------------------------------------------------- create temporary
    if not caso:
        cuerpo = {
            "displayName": f"zz_tmp_barrido_{uuid.uuid4().hex[:6]}",
            "notes": "temporal: barrido de tipos",
            "testCaseConversationTurns": [
                # MEASURED: in v3beta1 `TextInput.text` is a STRING (not a
                # list: sending a list gives "Proto field is not repeating"), and the
                # `languageCode` goes in the `QueryInput`, not inside the text.
                {"userInput": {"input": {"text": {"text": "hola"},
                                          "languageCode": "es"}}},
            ],
        }
        try:
            creado = cons.request("POST", cons._url(f"{base}/testCases"), body=cuerpo)
            print("create ->", json.dumps(creado, ensure_ascii=False)[:300])
            nombre = creado.get("name") or ""
            if not nombre:
                print("  (without 'name': the create is not an LRO, it resolves directly)")
                nombre = ""
            caso = nombre
            res["temporales"]["testCase"] = caso
        except Exception as exc:
            print("ERROR creating the test case:", str(exc)[:300])
            res["mediciones"]["testCases.create"] = {"estado": "error",
                                                     "detalle": str(exc)[:300]}

    if caso:
        res["mediciones"]["testCases"] = _barrer(
            cons, idx, cons._url("/" + caso), "testCases", caso)
        print("testCases (1 test case):", json.dumps(res["mediciones"]["testCases"],
                                         ensure_ascii=False)[:700])

        # ------------------------------------------------------- run
        try:
            op = cons.request("POST", cons._url(f"/{caso}:run"), body={})
            print("run ->", json.dumps(op, ensure_ascii=False)[:200])
            op = _esperar(cons, op)
            print("run (final):", json.dumps(op.get("response") or op.get("error"),
                                             ensure_ascii=False)[:300])
            res["mediciones"]["testCases.run"] = {"estado": "ok",
                                                  "detalle": json.dumps(
                                                      op.get("response") or
                                                      op.get("error"))[:300]}
        except Exception as exc:
            print("ERROR running it:", str(exc)[:250])
            res["mediciones"]["testCases.run"] = {"estado": "error",
                                                  "detalle": str(exc)[:250]}

        # ------------------------------------------------------- results
        res["mediciones"]["testCases.results"] = _barrer(
            cons, idx, cons._url(f"/{caso}/results", pageSize="100"),
            "testCases.results", caso)
        print("testCases.results:", json.dumps(res["mediciones"]["testCases.results"],
                                        ensure_ascii=False)[:1200])

        # --------------------------------------------------------- delete
        try:
            cons.request("POST", cons._url(f"{base}/testCases:batchDelete"),
                         body={"names": [caso]})
            print("temporary test case deleted (batchDelete)")
        except Exception as exc:
            print("Could NOT delete the test case:", str(exc)[:200])
            # Fallback: direct DELETE (the discovery doc does not declare it, but
            # some backend methods do exist).
            try:
                cons.request("DELETE", cons._url("/" + caso))
                print("deleted with a direct DELETE")
            except Exception as exc2:
                print("not even with DELETE:", str(exc2)[:150])

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
