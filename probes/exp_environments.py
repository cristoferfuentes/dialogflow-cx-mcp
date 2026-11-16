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

    .venv/bin/python probes/exp_environments.py [--agente <uuid>] [--out /tmp/environments.json]
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


def barrer(cons: ConsoleApi, idx, url: str, clave: str, etiqueta: str) -> dict:
    """GET -> gaps against the public schema (wrapper + items)."""
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
    ap.add_argument("--agente", default="")
    ap.add_argument("--proyecto", default="")
    ap.add_argument("--out", default="/tmp/environments.json")
    args = ap.parse_args()

    cfg = load_config()
    agente = args.agente or cfg.default_agent
    proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    idx = load_schema_index(cfg.api_version)
    base = f"/projects/{proyecto}/locations/{cfg.location}/agents/{agente}"
    res: dict = {"agente": agente, "mediciones": {}, "temporales": {}}
    creados: list[str] = []      # temporary versions/resources to delete
    entorno = ""

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
                                       f"zz_tmp_barrido_{uuid.uuid4().hex[:6]}",
                                       "description": "temporal: barrido"})
                if r.get("name"):
                    vcs.append({"version": r["name"]})
        print(f"flow versions: {len(vcs)}")

        # ----------------------------- 2) playbook and tool closure
        playbooks: set[str] = set()
        for f in (flows.get("flows") or []):
            fid = f["name"].rsplit("/", 1)[-1]
            paginas = cons.request("GET", cons._url(f"{base}/flows/{fid}/pages",
                                                     pageSize="100"))
            for pg in (paginas.get("pages") or []):
                for tr in (pg.get("transitionRoutes") or []):
                    if tr.get("targetPlaybook"):
                        playbooks.add(tr["targetPlaybook"])
            fl = cons.request("GET", cons._url(f"{base}/flows/{fid}"))
            for tr in (fl.get("transitionRoutes") or []):
                if tr.get("targetPlaybook"):
                    playbooks.add(tr["targetPlaybook"])

        tools: set[str] = set()
        pendientes = list(playbooks)
        vistos: set[str] = set()
        while pendientes:
            pb = pendientes.pop()
            if pb in vistos:
                continue
            vistos.add(pb)
            try:
                d = cons.request("GET", cons._url("/" + pb))
            except Exception:
                continue
            for ref in (d.get("referencedPlaybooks") or []):
                if ref and ref not in vistos:
                    pendientes.append(ref)
            for ref in (d.get("referencedTools") or []):
                if ref:
                    tools.add(ref)
            for ref in (d.get("referencedFlows") or []):
                if ref:
                    print("  (referencedFlows ->", ref, ")")
        print(f"playbooks (closure): {len(vistos)} | tools: {len(tools)}")

        # --------------------------------- 3) playbook and tool versions
        def version_de(nombre: str, etiqueta: str) -> str:
            """Existing (or created) version of a playbook/tool.

            MEASURED: there are tools that a playbook references but that **are not
            resources of the agent** (the system ones: `df-code-interpreter-tool`,
            `google-search`...): `POST tools/{name}/versions` answers 404
            "Tool 'X' does not exist in the agent". Those get no version (the
            environment brings them by default) and are skipped.
            """
            hijo = "playbookVersions" if "playbooks/" in nombre else "toolVersions"
            try:
                ex = cons.request("GET", cons._url(f"/{nombre}/versions",
                                                   pageSize="5")).get(hijo) or []
            except Exception:
                return ""
            if ex:
                return ex[0]["name"]
            try:
                r = cons.request("POST", cons._url(f"/{nombre}/versions"),
                                 body={"description": "temporal: barrido"})
            except Exception as exc:
                print(f"  (tool not versionable: {nombre.rsplit('/', 1)[-1]}: "
                      f"{str(exc)[:80]})")
                return ""
            if r.get("name"):
                creados.append(r["name"])
                return r["name"]
            return ""

        for pb in sorted(vistos):
            v = version_de(pb, "playbook")
            if v:
                vcs.append({"version": v})
        for tl in sorted(tools):
            v = version_de(tl, "tool")
            if v:
                vcs.append({"version": v})
        print(f"total versionConfigs: {len(vcs)}")

        # ------------------------------------------------ 4) create environment
        cuerpo = {"displayName": f"zz_tmp_barrido_{uuid.uuid4().hex[:6]}",
                  "description": "temporal: barrido de entornos",
                  "versionConfigs": vcs}
        op = esperar(cons, cons.request("POST",
                                        cons._url(f"{base}/environments"),
                                        body=cuerpo))
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

        # -------------------------------------------------- 5) sweep everything
        res["mediciones"]["environments"] = barrer(
            cons, idx, cons._url("/" + entorno), "environments", entorno)
        print("environments:", json.dumps(res["mediciones"]["environments"],
                                           ensure_ascii=False)[:600])

        # deployment
        try:
            version_flow = vcs[0]["version"]
            op = esperar(cons, cons.request(
                "POST", cons._url(f"/{entorno}/deployments"),
                body={"flowVersion": version_flow}))
            resp = op.get("response") or {}
            dep = resp.get("name") or ""
            print("deployment:", dep or json.dumps(op.get("error"),
                                                   ensure_ascii=False)[:200])
            if dep:
                res["mediciones"]["environments.deployments"] = barrer(
                    cons, idx, cons._url("/" + dep), "environments.deployments",
                    dep)
                print("  deployments:", json.dumps(
                    res["mediciones"]["environments.deployments"],
                    ensure_ascii=False)[:500])
        except Exception as exc:
            print("ERROR in the deployment:", str(exc)[:250])
            res["mediciones"]["environments.deployments"] = {"estado": "error",
                                                              "detalle": str(exc)[:250]}

        # experiment
        try:
            op = esperar(cons, cons.request(
                "POST", cons._url(f"/{entorno}/experiments"),
                body={"displayName":
                                            f"zz_tmp_barrido_{uuid.uuid4().hex[:6]}",
                                            "description": "temporal: barrido",
                                            "definition": {"environment": entorno}}))
            resp = op.get("response") or {}
            exp = resp.get("name") or ""
            print("experiment:", exp or json.dumps(op.get("error"),
                                                   ensure_ascii=False)[:200])
            if exp:
                res["mediciones"]["environments.experiments"] = barrer(
                    cons, idx, cons._url("/" + exp), "environments.experiments", exp)
                print("  experiments:", json.dumps(
                    res["mediciones"]["environments.experiments"],
                    ensure_ascii=False)[:500])
                res["temporales"]["experiment"] = exp
        except Exception as exc:
            print("ERROR in the experiment:", str(exc)[:250])
            res["mediciones"]["environments.experiments"] = {"estado": "error",
                                                              "detalle": str(exc)[:250]}

        # continuous results
        res["mediciones"]["environments.continuousTestResults"] = barrer(
            cons, idx, cons._url(f"/{entorno}/continuousTestResults"),
            "environments.continuousTestResults", entorno)
        print("continuousTestResults:", json.dumps(
            res["mediciones"]["environments.continuousTestResults"],
            ensure_ascii=False)[:400])

        # session INSIDE the environment (here entityTypes can be created)
        sid = f"barrido-{uuid.uuid4().hex[:10]}"
        try:
            cons.request("POST",
                         cons._url(f"/{entorno}/sessions/{sid}:detectIntent"),
                         body={"queryInput": {"text": {"text": "ok"},
                                              "languageCode": "es"}})
            res["mediciones"]["environments.sessions.entityTypes"] = barrer(
                cons, idx, cons._url(f"/{entorno}/sessions/{sid}/entityTypes"),
                "environments.sessions.entityTypes", sid)
            print("sessions.entityTypes (in the environment):", json.dumps(
                res["mediciones"]["environments.sessions.entityTypes"],
                ensure_ascii=False)[:500])
            # create a real session entity override
            ets = cons.request("GET", cons._url(f"{base}/entityTypes",
                                                pageSize="100"))
            tipo = next((e for e in (ets.get("entityTypes") or [])
                         if e.get("displayName") == "Confirmacion_negacion"), None)
            if tipo:
                det = cons.request("GET", cons._url("/" + tipo["name"]))
                nombre_et = f"{entorno}/sessions/{sid}/entityTypes/{uuid.uuid4().hex[:8]}"
                r = cons.request("POST", cons._url(f"/{entorno}/sessions/{sid}"
                                                    f"/entityTypes"),
                                 body={"name": nombre_et,
                                       "entityOverrideMode":
                                           "ENTITY_OVERRIDE_MODE_UNSPECIFIED",
                                       "entities": (det.get("entities") or [])[:2]})
                print("create session entityType ->",
                      json.dumps(r, ensure_ascii=False)[:250])
                b = scan_coleccion(r, idx, "environments.sessions.entityTypes")
                res["mediciones"]["environments.sessions.entityTypes.create"] = {
                    "estado": "ok", "nombre": r.get("name"), **b}
                print("  sweep:", json.dumps(b, ensure_ascii=False)[:400])
        except Exception as exc:
            print("ERROR in the environment session:", str(exc)[:250])
            res["mediciones"]["environments.sessions.entityTypes"] = {
                "estado": "error", "detalle": str(exc)[:250]}

    finally:
        # ------------------------------------------------------- cleanup
        if res["temporales"].get("experiment"):
            try:
                cons.request("DELETE", cons._url("/" + res["temporales"]["experiment"]))
                print("experiment deleted")
            except Exception as exc:
                print("Could NOT delete the experiment:", str(exc)[:150])
        if entorno:
            try:
                cons.request("DELETE", cons._url("/" + entorno))
                print("temporary environment deleted")
            except Exception as exc:
                print("Could NOT delete the environment:", str(exc)[:200])
        for nombre in creados:
            try:
                cons.request("DELETE", cons._url("/" + nombre))
            except Exception:
                pass
        if creados:
            print(f"{len(creados)} temporary versions deleted")

    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
