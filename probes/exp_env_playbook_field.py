#!/usr/bin/env python3
"""Looks for the REAL name of the field that includes the PLAYBOOKS in an environment.

MEASURED (2026-11-15):
  * Creating an environment requires the versions of **ALL** the flows (they
    reference each other: "Flow 'X' is referenced in version ... but not included in
    the environment").
  * And also the PLAYBOOKS that the routes reference:
    "Playbook 'P' is referenced in version ... but not included in the
    environment".
  * The public proto declares `EnvironmentVersionConfig = {version}`: the field
    that includes the playbooks **is not declared in any version**.

The server silently IGNORES the fields it does not know, so the way to discover
it is to measure the CHANGE in the error: if the field exists but the value is
wrong, the error changes; if the field does not exist, the error is the usual one.

This script tries candidates and **deletes any environment that gets created**.

    .venv/bin/python probes/exp_env_playbook_field.py
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

ERROR_BASE = ("not included in the environment", "referenced in version")


def esperar(cons: ConsoleApi, op: dict, segundos: int = 120) -> dict:
    nombre = op.get("name")
    if not nombre or op.get("done") or "/operations/" not in nombre:
        return op
    for _ in range(segundos * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + nombre))
        if op.get("done"):
            return op
    return op


def playbooks_por_flow(cons: ConsoleApi, base: str) -> dict[str, list[str]]:
    """Flow -> playbooks that reference its routes (transitive closure)."""
    flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
    out: dict[str, list[str]] = {}
    for f in (flows.get("flows") or []):
        fid = f["name"].rsplit("/", 1)[-1]
        refs: set[str] = set()
        paginas = cons.request("GET", cons._url(f"{base}/flows/{fid}/pages",
                                                pageSize="100"))
        for pg in (paginas.get("pages") or []):
            for tr in (pg.get("transitionRoutes") or []):
                if tr.get("targetPlaybook"):
                    refs.add(tr["targetPlaybook"])
        fl = cons.request("GET", cons._url(f"{base}/flows/{fid}"))
        for tr in (fl.get("transitionRoutes") or []):
            if tr.get("targetPlaybook"):
                refs.add(tr["targetPlaybook"])
        cola = list(refs)
        while cola:
            nom = cola.pop()
            try:
                pb = cons.request("GET", cons._url("/" + nom))
            except Exception:
                continue
            for ref in (pb.get("referencedPlaybooks") or []):
                if ref and ref not in refs:
                    refs.add(ref)
                    cola.append(ref)
        out[fid] = sorted(refs)
    return out


def versiones(cons: ConsoleApi, base: str) -> list[tuple[str, str]]:
    """[(flow_id, version_name)] with one version per flow (reuses if there is one)."""
    flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
    out = []
    for f in (flows.get("flows") or []):
        fid = f["name"].rsplit("/", 1)[-1]
        v = cons.request("GET", cons._url(f"{base}/flows/{fid}/versions",
                                         pageSize="5"))
        vs = v.get("versions") or []
        if vs:
            out.append((fid, vs[0]["name"]))
    return out


def candidatos(playbooks: list[str]) -> list[tuple[str, object]]:
    """(name, field value) -> they are tried inside `versionConfigs[i]`."""
    return [
        ("playbookConfigs[{playbook}]", [{"playbook": p} for p in playbooks]),
        ("playbookConfigs[{name}]", [{"name": p} for p in playbooks]),
        ("playbookConfigs[{playbook,version}]",
         [{"playbook": p, "version": p} for p in playbooks]),
        ("playbooks[]", list(playbooks)),
        ("playbookNames[]", list(playbooks)),
        ("includedPlaybooks[]", list(playbooks)),
        ("playbookConfigs[{playbookId}]",
         [{"playbookId": p.rsplit("/", 1)[-1]} for p in playbooks]),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agente", default="")
    ap.add_argument("--proyecto", default="")
    args = ap.parse_args()

    cfg = load_config()
    agente = args.agente or cfg.default_agent
    _proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    base = f"/projects/{cfg.project}/locations/{cfg.location}/agents/{cfg.default_agent}" \
        if args.proyecto == "" else \
        f"/projects/{args.proyecto}/locations/{cfg.location}/agents/{agente}"

    pares = versiones(cons, base)
    pbs = playbooks_por_flow(cons, base)
    print(f"flows with a version: {len(pares)}")
    total = sorted({p for v in pbs.values() for p in v})
    print(f"referenced playbooks: {len(total)}")

    # The flow that references the playbooks is the one that brings them in:
    por_flow = {fid: pbs.get(fid, []) for fid, _ in pares}

    errores: dict[str, str] = {}
    for nombre_campo, valor in candidatos(total):
        vcs = []
        for fid, version in pares:
            vc = {"version": version}
            pbs_flow = por_flow.get(fid) or []
            if pbs_flow:
                vc[nombre_campo] = (valor if nombre_campo in
                                    ("playbooks[]", "playbookNames[]",
                                     "includedPlaybooks[]")
                                    else [{"playbook": p} if "playbook" in
                                          str(list(valor)[:1]) else
                                          ({"name": p} if "name" in
                                           str(list(valor)[:1]) else
                                           {"playbookId": p})
                                          for p in pbs_flow])
            vcs.append(vc)
        cuerpo = {"displayName": f"zz_tmp_env_{uuid.uuid4().hex[:6]}",
                  "description": "temporal: buscar el campo de playbooks",
                  "versionConfigs": vcs}
        try:
            op = esperar(cons, cons.request(
                "POST", cons._url(f"{base}/environments"), body=cuerpo))
            resp = op.get("response") or {}
            nombre = resp.get("name") or ""
            if nombre:
                print(f"  {nombre_campo:<32} -> CREATED ({nombre})")
                cons.request("DELETE", cons._url("/" + nombre))
                print("      (deleted)")
                errores[nombre_campo] = "CREATED"
                continue
            err = json.dumps(op.get("error"), ensure_ascii=False)
        except Exception as exc:
            err = str(exc)
        if any(m in err for m in ERROR_BASE):
            errores[nombre_campo] = "same error (field ignored)"
        else:
            errores[nombre_campo] = "DIFFERENT ERROR: " + err[:200]
        print(f"  {nombre_campo:<32} -> {errores[nombre_campo][:150]}")

    print()
    for k, v in errores.items():
        print(f"  {k:<32} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
