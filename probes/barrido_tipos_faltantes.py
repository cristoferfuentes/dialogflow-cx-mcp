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

    .venv/bin/python probes/barrido_tipos_faltantes.py [--agente <uuid>]
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
from cxp.gaps import agrupar_gaps, find_gaps, load_schema_index, norm_schema


def _es_operacion(nombre: str) -> bool:
    """An LRO has '/operations/' in its name; a resource does not."""
    return "/operations/" in (nombre or "")


def _esperar(cons: ConsoleApi, op: dict, segundos: int = 60) -> dict:
    """Follows an operation (LRO) until it finishes. Returns the operation."""
    nombre = op.get("name")
    if not nombre or op.get("done") or not _es_operacion(nombre):
        return op
    for _ in range(segundos * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + nombre))
        if op.get("done"):
            return op
    return op


def _scan(cons, idx, url, clave, etiqueta=""):
    """GET url -> gaps against the public schema of that collection."""
    try:
        data = cons.request("GET", url)
    except Exception as exc:
        return {"estado": "error", "detalle": str(exc)[:200], "huecos": []}
    if isinstance(data, list):
        data = {clave.split(".")[-1]: data}
    esquema = idx.esquema_de(clave)
    huecos = find_gaps(data, esquema, idx)
    return {
        "estado": "ok",
        "clave": clave,
        "esquema": norm_schema(esquema),
        "detalle": etiqueta,
        "campos": sorted(data.keys()) if isinstance(data, dict) else [],
        "huecos": agrupar_gaps(huecos),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agente", default="")
    ap.add_argument("--proyecto", default="")
    ap.add_argument("--out", default="/tmp/barrido_tipos.json")
    args = ap.parse_args()

    cfg = load_config()
    agente = args.agente or cfg.default_agent
    proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{proyecto}/locations/{cfg.location}/agents/{agente}"
    res: dict = {"agente": agente, "mediciones": {}, "temporales": {}}
    print(f"agent: {agente}")

    def anota(clave, r):
        res["mediciones"][clave] = r
        print(f"{clave}: {json.dumps(r, ensure_ascii=False)[:400]}")

    # ---------------------------------------------------- 1) flow version
    versiones: list[str] = []
    try:
        flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
        for flujo in (flows.get("flows") or []):
            fid = flujo.get("name", "").rsplit("/", 1)[-1]
            # If the flow already has versions, the first one is reused.
            # MEASURED (2026-11-15): flow versions **CAN** be deleted
            # (`DELETE flows/{f}/versions/{v}`); this script does it at the
            # end. (An earlier comment said they could not, and that was false.)
            existentes = cons.request("GET",
                                      cons._url(f"{base}/flows/{fid}/versions",
                                                pageSize="10"))
            previas = existentes.get("versions") or []
            if previas:
                versiones.append(previas[0].get("name"))
                continue
            op = cons.request("POST", cons._url(f"{base}/flows/{fid}/versions"),
                              body={"displayName": f"zz_tmp_barrido_{uuid.uuid4().hex[:6]}",
                                    "description": "temporal: barrido de tipos"})
            op = _esperar(cons, op)
            resp = op.get("response") or {}
            nombre = resp.get("name") or (op.get("name")
                                          if not _es_operacion(op.get("name", "")) else "")
            if nombre:
                versiones.append(nombre)
        res["temporales"]["versiones"] = versiones
        print(f"versions created: {len(versiones)}")
        anota("flows.versions", {"estado": "ok" if versiones else "error",
                                 "detalle": "", "versiones": versiones})
    except Exception as exc:
        anota("flows.versions", {"estado": "error", "detalle": str(exc)[:200]})
    version = versiones[0] if versiones else ""

    # --------------------------------------------------------- 2) environment
    entorno = ""
    if version:
        try:
            op = cons.request("POST", cons._url(f"{base}/environments"),
                              body={"displayName": f"zz_tmp_barrido_{uuid.uuid4().hex[:6]}",
                                    "description": "temporal: barrido de tipos",
                                    "versionConfigs": [{"version": v} for v in versiones]})
            op = _esperar(cons, op)
            resp = op.get("response") or {}
            entorno = resp.get("name") or ""
            res["temporales"]["entorno"] = entorno
            print(f"environment: {entorno} (error: {op.get('error')})")
            if entorno:
                anota("environments", _scan(cons, idx, cons._url("/" + entorno),
                                            "environments", entorno))
        except Exception as exc:
            anota("environments", {"estado": "error", "detalle": str(exc)[:200]})

    # -------------------------------------------- 3) deployment and experiment
    if entorno:
        try:
            op = cons.request("POST", cons._url(f"/{entorno}/deployments"),
                              body={"flowVersion": version})
            op = _esperar(cons, op)
            resp = op.get("response") or {}
            print(f"deployment: {resp.get('name')} (error: {op.get('error')})")
            if resp.get("name"):
                anota("environments.deployments",
                      _scan(cons, idx, cons._url("/" + resp["name"]),
                            "environments.deployments", resp["name"]))
        except Exception as exc:
            anota("environments.deployments", {"estado": "error", "detalle": str(exc)[:200]})

        try:
            op = cons.request("POST", cons._url(f"/{entorno}/experiments"),
                              body={"displayName": f"zz_tmp_barrido_{uuid.uuid4().hex[:6]}",
                                    "description": "temporal: barrido de tipos",
                                    "definition": {"environment": entorno}})
            op = _esperar(cons, op)
            resp = op.get("response") or {}
            print(f"experiment: {resp.get('name')} (error: {op.get('error')})")
            if resp.get("name"):
                anota("environments.experiments",
                      _scan(cons, idx, cons._url("/" + resp["name"]),
                            "environments.experiments", resp["name"]))
        except Exception as exc:
            anota("environments.experiments", {"estado": "error", "detalle": str(exc)[:200]})

        # The sessions of the environment
        try:
            d = cons.request("GET", cons._url(f"/{entorno}/sessions", pageSize="20"))
            print("environment sessions:", d)
        except Exception as exc:
            print("environment sessions: ERR", str(exc)[:120])

    # ------------------------------------------------------- 4) test case
    caso = ""
    try:
        op = cons.request("POST", cons._url(f"{base}/testCases"),
                          body={"displayName": f"zz_tmp_barrido_{uuid.uuid4().hex[:6]}",
                                "testCases": [{"displayName": "temporal",
                                               "input": {"text": "hola",
                                                         "languageCode": "es"}}]})
        op = _esperar(cons, op)
        resp = op.get("response") or {}
        caso = resp.get("name") or ""
        res["temporales"]["caso"] = caso
        print(f"testCase: {caso} (error: {op.get('error')})")
        if caso:
            anota("testCases", _scan(cons, idx, cons._url("/" + caso), "testCases", caso))
            # Run it -> results
            try:
                op = cons.request("POST", cons._url(f"/{caso}:run"), body={})
                op = _esperar(cons, op)
                print("run:", str(op.get("response") or op.get("error"))[:200])
            except Exception as exc:
                print("run: ERR", str(exc)[:150])
            anota("testCases.results",
                  _scan(cons, idx, cons._url(f"/{caso}/results", pageSize="20"),
                        "testCases.results", caso))
    except Exception as exc:
        anota("testCases", {"estado": "error", "detalle": str(exc)[:200]})

    # ------------------------------------------------------------ 5) session
    try:
        sid = f"barrido-{uuid.uuid4().hex[:10]}"
        # MEASURED: `queryInput.languageCode` (not `queryInput.text.languageCode`).
        cons.request("POST", cons._url(f"{base}/sessions/{sid}:detectIntent"),
                     body={"queryInput": {"text": {"text": "hola"},
                                          "languageCode": "es"}})
        print("sessionId:", sid)
        if sid:
            anota("sessions.entityTypes",
                  _scan(cons, idx, cons._url(f"{base}/sessions/{sid}/entityTypes"),
                        "sessions.entityTypes", sid))
    except Exception as exc:
        anota("sessions.entityTypes", {"estado": "error", "detalle": str(exc)[:200]})

    # ------------------------------------------------------------- cleanup
    for etiqueta, nombre in (("case", caso), ("environment", entorno)):
        if not nombre:
            continue
        try:
            cons.request("DELETE", cons._url("/" + nombre))
            print(f"temporary {etiqueta} deleted")
        except Exception as exc:
            print(f"Could NOT delete {etiqueta}: {str(exc)[:200]}")

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
