#!/usr/bin/env python3
"""Closes the `sessions.entityTypes` collection of the sweep.

Why: `agents/{a}/sessions` has **no `list`** in the discovery doc (only
`detectIntent`, `matchIntent`, `fulfillIntent`...). The generic sweep cannot
reach `sessions.entityTypes` because it needs a **sessionId**, and that comes from
a `detectIntent`.

MEASURED (2026-11-15):
  * `GET sessions/{id}/entityTypes` returns `{}` if the session has not detected
    entities: you have to send a text that triggers an entity of the agent.
  * `sessions.entityTypes` has `create`/`patch`/`delete`: you can create a
    **session** entity override, sweep it and delete it.

    .venv/bin/python probes/exp_sessions_entity_types.py [--agente <uuid>]
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi
from cxp.gaps import load_schema_index, scan_coleccion


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agente", default="")
    ap.add_argument("--proyecto", default="")
    ap.add_argument("--texto", default="ok")
    ap.add_argument("--out", default="/tmp/sessions.json")
    args = ap.parse_args()

    cfg = load_config()
    agente = args.agente or cfg.only_agent()
    proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{proyecto}/locations/{cfg.location}/agents/{agente}"
    res: dict = {"agente": agente, "mediciones": {}}

    # ------------------------------------------------------ 1) a real session
    sid = f"barrido-{uuid.uuid4().hex[:10]}"
    cuerpo = {"queryInput": {"text": {"text": args.texto}, "languageCode": "es"}}
    det = cons.request("POST", cons._url(f"{base}/sessions/{sid}:detectIntent"),
                       body=cuerpo)
    print("detectIntent ->", json.dumps(
        {k: v for k, v in (det or {}).items()
         if k in ("session", "queryResult", "fulfillmentText")},
        ensure_ascii=False)[:300])
    qr = det.get("queryResult") or {}
    params = qr.get("parameters") or []
    # MEASURED: in the live response `parameters` is a MAP (str->str), not a
    # list of objects (the v3beta1 discovery doc declares it as `additionalProperties`).
    if isinstance(params, dict):
        params = [{"entityType": k, "resolvedValue": v}
                  for k, v in params.items()]
    print("  intent:", (qr.get("intent") or {}).get("displayName"),
          "| entities:", [(e.get("entityType"), e.get("resolvedValue"))
                          for e in params][:5])
    res["mediciones"]["detectIntent"] = {
        "session": det.get("session"),
        "intents": [i.get("displayName") for i in (qr.get("intents") or [])],
        "parameters": [{"tipo": e.get("entityType"),
                        "valor": e.get("resolvedValue")} for e in params],
    }

    # ------------------------------------------- 2) list session entityTypes
    try:
        data = cons.request("GET", cons._url(f"{base}/sessions/{sid}/entityTypes"))
        barrido = scan_coleccion(data, idx, "sessions.entityTypes")
        print("session entityTypes ->", json.dumps(barrido, ensure_ascii=False)[:500])
        res["mediciones"]["sessions.entityTypes"] = {"estado": "ok", **barrido}
    except Exception as exc:
        print("ERROR listing entityTypes:", str(exc)[:200])
        res["mediciones"]["sessions.entityTypes"] = {"estado": "error",
                                                     "detalle": str(exc)[:200]}

    # -------------------------------- 3) create a session entity override
    # (to have a real SessionEntityType to sweep)
    ets = cons.request("GET", cons._url(f"{base}/entityTypes", pageSize="100"))
    tipo = next((e for e in (ets.get("entityTypes") or [])
                 if e.get("displayName") == "Confirmacion_negacion"), None)
    if tipo:
        det_tipo = cons.request("GET", cons._url("/" + tipo["name"]))
        entidades = (det_tipo.get("entities") or [])[:2]
        nombre = f"{base}/sessions/{sid}/entityTypes/{uuid.uuid4().hex[:8]}"
        cuerpo_et = {
            "name": nombre,
            "entityOverrideMode": "ENTITY_OVERRIDE_MODE_UNSPECIFIED",
            "entities": entidades,
        }
        try:
            creado = cons.request("POST",
                                  cons._url(f"{base}/sessions/{sid}/entityTypes"),
                                  body=cuerpo_et)
            print("create session entityType ->", json.dumps(creado,
                                                              ensure_ascii=False)[:300])
            real = creado.get("name") or nombre
            barrido = scan_coleccion(creado, idx, "sessions.entityTypes")
            print("  sweep:", json.dumps(barrido, ensure_ascii=False)[:500])
            res["mediciones"]["sessions.entityTypes.create"] = {
                "estado": "ok", "nombre": real, **barrido}
            try:
                cons.request("DELETE", cons._url("/" + real))
                print("  session entityType deleted")
            except Exception as exc:
                print("  Could NOT delete:", str(exc)[:150])
        except Exception as exc:
            print("ERROR creating the session entityType:", str(exc)[:250])
            res["mediciones"]["sessions.entityTypes.create"] = {
                "estado": "error", "detalle": str(exc)[:250]}

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
