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

    .venv/bin/python probes/exp_sessions_entity_types.py [--agent <uuid>]
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
from cxp.gaps import load_schema_index, scan_collection


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--text", dest="text", default="ok")
    ap.add_argument("--out", default="/tmp/sessions.json")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{project}/locations/{cfg.location}/agents/{agent}"
    res: dict = {"agent": agent, "measurements": {}}

    # ------------------------------------------------------ 1) a real session
    sid = f"sweep-{uuid.uuid4().hex[:10]}"
    body = {"queryInput": {"text": {"text": args.text}, "languageCode": "es"}}
    det = cons.request("POST", cons._url(f"{base}/sessions/{sid}:detectIntent"),
                       body=body)
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
    res["measurements"]["detectIntent"] = {
        "session": det.get("session"),
        "intents": [i.get("displayName") for i in (qr.get("intents") or [])],
        "parameters": [{"type": e.get("entityType"),
                        "value": e.get("resolvedValue")} for e in params],
    }

    # ------------------------------------------- 2) list session entityTypes
    try:
        data = cons.request("GET", cons._url(f"{base}/sessions/{sid}/entityTypes"))
        sweep = scan_collection(data, idx, "sessions.entityTypes")
        print("session entityTypes ->", json.dumps(sweep, ensure_ascii=False)[:500])
        res["measurements"]["sessions.entityTypes"] = {"status": "ok", **sweep}
    except Exception as exc:
        print("ERROR listing entityTypes:", str(exc)[:200])
        res["measurements"]["sessions.entityTypes"] = {"status": "error",
                                                     "detail": str(exc)[:200]}

    # -------------------------------- 3) create a session entity override
    # (to have a real SessionEntityType to sweep)
    entity_types = cons.request("GET", cons._url(f"{base}/entityTypes", pageSize="100"))
    entity_type = next((e for e in (entity_types.get("entityTypes") or [])
                        if e.get("displayName") == "Confirmacion_negacion"), None)
    if entity_type:
        entity_detail = cons.request("GET", cons._url("/" + entity_type["name"]))
        entities = (entity_detail.get("entities") or [])[:2]
        name = f"{base}/sessions/{sid}/entityTypes/{uuid.uuid4().hex[:8]}"
        entity_body = {
            "name": name,
            "entityOverrideMode": "ENTITY_OVERRIDE_MODE_UNSPECIFIED",
            "entities": entities,
        }
        try:
            created = cons.request("POST",
                                  cons._url(f"{base}/sessions/{sid}/entityTypes"),
                                  body=entity_body)
            print("create session entityType ->", json.dumps(created,
                                                              ensure_ascii=False)[:300])
            real = created.get("name") or name
            sweep = scan_collection(created, idx, "sessions.entityTypes")
            print("  sweep:", json.dumps(sweep, ensure_ascii=False)[:500])
            res["measurements"]["sessions.entityTypes.create"] = {
                "status": "ok", "name": real, **sweep}
            try:
                cons.request("DELETE", cons._url("/" + real))
                print("  session entityType deleted")
            except Exception as exc:
                print("  Could NOT delete:", str(exc)[:150])
        except Exception as exc:
            print("ERROR creating the session entityType:", str(exc)[:250])
            res["measurements"]["sessions.entityTypes.create"] = {
                "status": "error", "detail": str(exc)[:250]}

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
