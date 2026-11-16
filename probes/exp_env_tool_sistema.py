#!/usr/bin/env python3
"""Can the SYSTEM TOOL be included in an environment?

MEASURED (2026-11-15): the tool `df-code-interpreter-tool` (code-interpreter) is
in the agent and a playbook references it, but:

  * `POST tools/df-code-interpreter-tool/versions` -> 404 "Tool does not exist
    in the agent" (system tools are NOT versioned);
  * `GET  tools/df-code-interpreter-tool/versions` -> `{}` (it has none);
  * `versionConfigs[].version = ".../tools/<id>"` (the tool NAME) ->
    "[ResourceName error] ... does not match template
     'projects/*/locations/*/agents/*/flows/*/versions/*'".

=> The environment cannot be created if the published graph includes a system
tool. This script tests whether there is a way to include it (canonical version,
name with another template...) and, if not, leaves a record.

It creates all the temporary versions it needs, tests and **deletes everything**.

    .venv/bin/python probes/exp_env_tool_sistema.py
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


def esperar(cons: ConsoleApi, op: dict, segundos: int = 180) -> dict:
    nombre = op.get("name")
    if not nombre or op.get("done") or "/operations/" not in nombre:
        return op
    for _ in range(segundos * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + nombre))
        if op.get("done"):
            return op
    return op


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agente", default="")
    ap.add_argument("--proyecto", default="")
    args = ap.parse_args()

    cfg = load_config()
    agente = args.agente or cfg.default_agent
    proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    base = f"/projects/{proyecto}/locations/{cfg.location}/agents/{agente}"
    creados: list[str] = []

    try:
        flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
        vcs: list[dict] = []
        for f in (flows.get("flows") or []):
            fid = f["name"].rsplit("/", 1)[-1]
            v = cons.request("GET", cons._url(f"{base}/flows/{fid}/versions",
                                               pageSize="5"))
            vs = v.get("versions") or []
            if vs:
                vcs.append({"version": vs[0]["name"]})

        cola: set[str] = set()
        for f in (flows.get("flows") or []):
            fid = f["name"].rsplit("/", 1)[-1]
            for pg in (cons.request("GET", cons._url(f"{base}/flows/{fid}/pages",
                                                     pageSize="100")).get("pages") or []):
                for tr in (pg.get("transitionRoutes") or []):
                    if tr.get("targetPlaybook"):
                        cola.add(tr["targetPlaybook"])
        playbooks: set[str] = set()
        while cola:
            pb = cola.pop()
            if pb in playbooks:
                continue
            playbooks.add(pb)
            try:
                d = cons.request("GET", cons._url("/" + pb))
            except Exception:
                continue
            for r in (d.get("referencedPlaybooks") or []):
                if r:
                    cola.add(r)
        tools: set[str] = set()
        for pb in playbooks:
            try:
                d = cons.request("GET", cons._url("/" + pb))
            except Exception:
                continue
            for r in (d.get("referencedTools") or []):
                if r:
                    tools.add(r)

        def version_de(nombre: str) -> str:
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
            except Exception:
                return ""
            if r.get("name"):
                creados.append(r["name"])
                return r["name"]
            return ""

        for pb in sorted(playbooks):
            v = version_de(pb)
            if v:
                vcs.append({"version": v})
        for tl in sorted(tools):
            v = version_de(tl)
            if v:
                vcs.append({"version": v})
        print(f"versionConfigs ready: {len(vcs)} "
              f"({len(playbooks)} playbooks, {len(tools)} tools)")

        prefijo = f"{base}/tools/df-code-interpreter-tool"
        intentos = [
            ("versions/1", f"{prefijo}/versions/1"),
            ("versions/0", f"{prefijo}/versions/0"),
            ("tool name", prefijo),
            ("versions/latest", f"{prefijo}/versions/latest"),
        ]
        for etiqueta, valor in intentos:
            cuerpo = {"displayName": f"zz_tmp_{uuid.uuid4().hex[:6]}",
                      "description": "temporal",
                      "versionConfigs": vcs + [{"version": valor}]}
            try:
                op = esperar(cons, cons.request(
                    "POST", cons._url(f"{base}/environments"), body=cuerpo))
                resp = op.get("response") or {}
                nombre = resp.get("name") or ""
            except Exception as exc:
                nombre = ""
                err = str(exc)
            else:
                err = json.dumps(op.get("error"), ensure_ascii=False)
            if nombre:
                print(f"  {etiqueta:<20} -> CREATED ({nombre})")
                cons.request("DELETE", cons._url("/" + nombre))
                print("      deleted")
                break
            print(f"  {etiqueta:<20} -> {err[:190]}")
    finally:
        for nombre in creados:
            try:
                cons.request("DELETE", cons._url("/" + nombre))
            except Exception:
                pass
        print(f"{len(creados)} temporary versions deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
