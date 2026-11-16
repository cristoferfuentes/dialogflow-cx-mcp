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

    .venv/bin/python probes/exp_agente_lab.py [--out /tmp/agente_lab.json]
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


def esperar(cons: ConsoleApi, op: dict, segundos: int = 240) -> dict:
    nombre = op.get("name")
    if not nombre or op.get("done") or "/operations/" not in nombre:
        return op
    for _ in range(segundos * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + nombre))
        if op.get("done"):
            return op
    return op


def barrer(cons: ConsoleApi, idx, url: str, clave: str, etiqueta: str) -> dict:
    try:
        data = cons.request("GET", url)
    except Exception as exc:
        return {"estado": "error", "detalle": str(exc)[:250]}
    if isinstance(data, list):
        data = {clave.split(".")[-1]: data}
    b = scan_coleccion(data, idx, clave)
    return {"estado": "ok", "clave": clave, "detalle": etiqueta,
            "campos": sorted(data.keys()) if isinstance(data, dict) else [],
            "envoltorio": b["envoltorio"], "items": b["items"],
            "n_items": b["n_items"],
            "esquema_envoltorio": b["esquema_envoltorio"],
            "esquema_item": b["esquema_item"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proyecto", default="")
    ap.add_argument("--out", default="/tmp/agente_lab.json")
    args = ap.parse_args()

    cfg = load_config()
    proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{proyecto}/locations/{cfg.location}/agents"
    res: dict = {"proyecto": proyecto, "mediciones": {}, "temporales": {}}
    agente = ""
    flow = ""
    entorno = ""

    try:
        # ------------------------------------------------ 1) lab agent
        cuerpo = {"displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
                  "defaultLanguageCode": "es",
                  "timeZone": "America/Santiago",
                  "description": "temporal: barrido de entornos"}
        creado = cons.request("POST", cons._url(base), body=cuerpo)
        agente = creado.get("name") or ""
        if not agente:
            print("Could NOT create the agent:", json.dumps(creado,
                                                           ensure_ascii=False)[:300])
            return 1
        res["temporales"]["agente"] = agente
        print("lab agent:", agente)
        # The guard only allows writes on allowlisted agents: this one is
        # new, so it is added IN MEMORY (the .env is not touched).
        cfg.agent_readwrite = list(cfg.agent_readwrite) + [agente.rsplit("/", 1)[-1]]
        abase = base + "/" + agente.rsplit("/", 1)[-1]

        # ------------------------------------------------------ 2) flow and page
        f = cons.request("POST", cons._url(f"{abase}/flows"),
                         body={"displayName": "lab_flow",
                               "transitionRoutes": [],
                               "eventHandlers": []})
        flow = f.get("name") or ""
        print("flow:", flow)
        res["temporales"]["flow"] = flow
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
            ffl = fl["name"].rsplit("/", 1)[-1]
            op = esperar(cons, cons.request(
                "POST", cons._url(f"{abase}/flows/{ffl}/versions"),
                body={"displayName": f"lab_version_{uuid.uuid4().hex[:4]}",
                      "description": "temporal: barrido"}))
            v = (op.get("response") or {}).get("name") or ""
            if v:
                vcs.append({"version": v})
                if not version:
                    version = v
        print("versions:", len(vcs))
        res["temporales"]["version"] = version
        if not vcs:
            return 1

        # ------------------------------------------------------ 4) environment
        op = esperar(cons, cons.request(
            "POST", cons._url(f"{abase}/environments"),
            body={"displayName": "lab_entorno", "description": "temporal",
                  "versionConfigs": vcs}))
        resp = op.get("response") or {}
        entorno = resp.get("name") or ""
        if not entorno:
            print("Could NOT create the environment:",
                  json.dumps(op.get("error"), ensure_ascii=False)[:400])
            res["mediciones"]["environments.create"] = {
                "estado": "error",
                "detalle": json.dumps(op.get("error"), ensure_ascii=False)[:400]}
            return 1
        res["temporales"]["entorno"] = entorno
        print("environment:", entorno)

        # ------------------------------------------------------- 5) sweep
        res["mediciones"]["environments"] = barrer(
            cons, idx, cons._url("/" + entorno), "environments", entorno)
        print("environments:", json.dumps(res["mediciones"]["environments"],
                                           ensure_ascii=False)[:700])

        # deployment
        op = esperar(cons, cons.request(
            "POST", cons._url(f"/{entorno}/deployments"),
            body={"flowVersion": version}))
        dep = (op.get("response") or {}).get("name") or op.get("name") or ""
        print("deployment:", dep or json.dumps(op.get("error"),
                                               ensure_ascii=False)[:200])
        print("  raw op:", json.dumps(op, ensure_ascii=False)[:400])
        if dep:
            res["mediciones"]["environments.deployments"] = barrer(
                cons, idx, cons._url("/" + dep), "environments.deployments", dep)
            print("  deployments:", json.dumps(
                res["mediciones"]["environments.deployments"],
                ensure_ascii=False)[:600])

        # experiment
        op = esperar(cons, cons.request(
            "POST", cons._url(f"/{entorno}/experiments"),
            body={"displayName": "lab_experiment", "description": "temporal",
                  "definition": {"environment": entorno}}))
        exp = (op.get("response") or {}).get("name") or op.get("name") or ""
        print("experiment:", exp or json.dumps(op.get("error"),
                                               ensure_ascii=False)[:200])
        print("  raw op:", json.dumps(op, ensure_ascii=False)[:400])
        if exp:
            res["temporales"]["experiment"] = exp
            res["mediciones"]["environments.experiments"] = barrer(
                cons, idx, cons._url("/" + exp), "environments.experiments", exp)
            print("  experiments:", json.dumps(
                res["medications"], ensure_ascii=False)[:600]
                if "medications" in res else json.dumps(
                res["mediciones"]["environments.experiments"],
                ensure_ascii=False)[:600])

        # continuous results
        res["mediciones"]["environments.continuousTestResults"] = barrer(
            cons, idx, cons._url(f"/{entorno}/continuousTestResults"),
            "environments.continuousTestResults", entorno)
        print("continuousTestResults:", json.dumps(
            res["mediciones"]["environments.continuousTestResults"],
            ensure_ascii=False)[:500])

        # session inside the environment
        sid = f"lab-{uuid.uuid4().hex[:8]}"
        try:
            cons.request("POST", cons._url(f"/{entorno}/sessions/{sid}:detectIntent"),
                         body={"queryInput": {"text": {"text": "hola"},
                                              "languageCode": "es"}})
            res["mediciones"]["environments.sessions.entityTypes"] = barrer(
                cons, idx, cons._url(f"/{entorno}/sessions/{sid}/entityTypes"),
                "environments.sessions.entityTypes", sid)
            print("sessions.entityTypes:", json.dumps(
                res["mediciones"]["environments.sessions.entityTypes"],
                ensure_ascii=False)[:500])
        except Exception as exc:
            print("ERROR in the session:", str(exc)[:200])
            res["mediciones"]["environments.sessions.entityTypes"] = {
                "estado": "error", "detalle": str(exc)[:250]}

    finally:
        # ------------------------------------------------------- cleanup
        if res["temporales"].get("experiment"):
            try:
                cons.request("DELETE",
                             cons._url("/" + res["temporales"]["experiment"]))
                print("experiment deleted")
            except Exception as exc:
                print("Could NOT delete the experiment:", str(exc)[:150])
        if entorno:
            try:
                cons.request("DELETE", cons._url("/" + entorno))
                print("environment deleted")
            except Exception as exc:
                print("Could NOT delete the environment:", str(exc)[:200])
        if agente:
            try:
                cons.request("DELETE", cons._url("/" + agente))
                print("lab agent deleted")
            except Exception as exc:
                print("Could NOT delete the agent:", str(exc)[:250])

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
