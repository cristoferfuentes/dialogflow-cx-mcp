#!/usr/bin/env python3
"""Discovers the environment's playbook field by sending a GARBAGE playbook.

The server silently ignores the fields it does not know. But if the field
EXISTS and the value does not, the error changes: it starts talking about the
garbage playbook ("Playbook 'NO_EXISTE' not found") instead of "not included in
the environment".

=> With a garbage value you can **tell "unknown field" from "known field"**
without creating any playbook and without creating any environment.

    .venv/bin/python probes/exp_env_playbook_disco.py
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


def esperar(cons: ConsoleApi, op: dict, segundos: int = 90) -> dict:
    nombre = op.get("name")
    if not nombre or op.get("done") or "/operations/" not in nombre:
        return op
    for _ in range(segundos * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + nombre))
        if op.get("done"):
            return op
    return op


BASURA = ("projects/my-project/locations/global/agents/"
          "11111111-1111-4111-8111-111111111111/playbooks/NO_EXISTE_0000")

# (where, field name, value)
CANDIDATOS: list[tuple[str, str, object]] = [
    ("versionConfigs", "playbookConfigs", [{"playbook": BASURA}]),
    ("versionConfigs", "playbookConfigs", [{"name": BASURA}]),
    ("versionConfigs", "playbookConfigs", [{"playbook": BASURA, "version": BASURA}]),
    ("versionConfigs", "playbooks", [BASURA]),
    ("versionConfigs", "playbookNames", [BASURA]),
    ("versionConfigs", "includedPlaybooks", [BASURA]),
    ("versionConfigs", "referencedPlaybooks", [BASURA]),
    ("versionConfigs", "playbookVersions", [BASURA]),
    ("versionConfigs", "playbooksToInclude", [BASURA]),
    ("versionConfigs", "playbook", BASURA),
    ("raiz", "playbookConfigs", [{"playbook": BASURA}]),
    ("raiz", "playbooks", [BASURA]),
    ("raiz", "includedPlaybooks", [BASURA]),
    ("raiz", "playbookNames", [BASURA]),
    ("raiz", "playbookVersions", [BASURA]),
    ("raiz", "referencedPlaybooks", [BASURA]),
]


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

    flows = cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
    pares = []
    for f in (flows.get("flows") or []):
        fid = f["name"].rsplit("/", 1)[-1]
        v = cons.request("GET", cons._url(f"{base}/flows/{fid}/versions", pageSize="5"))
        vs = v.get("versions") or []
        if vs:
            pares.append(vs[0]["name"])
    print(f"versions: {len(pares)}")

    print()
    for donde, campo, valor in CANDIDATOS:
        vcs = [{"version": v} for v in pares]
        if donde == "versionConfigs":
            vcs[0][campo] = valor
        cuerpo = {"displayName": f"zz_tmp_env_{uuid.uuid4().hex[:6]}",
                  "description": "temporal: buscar el campo",
                  "versionConfigs": vcs}
        if donde == "raiz":
            cuerpo[campo] = valor
        try:
            op = esperar(cons, cons.request(
                "POST", cons._url(f"{base}/environments"), body=cuerpo))
            resp = op.get("response") or {}
            if resp.get("name"):
                print(f"  {donde}.{campo:<22} -> CREATED (valid field?)")
                cons.request("DELETE", cons._url("/" + resp["name"]))
                print("      deleted")
                continue
            err = json.dumps(op.get("error"), ensure_ascii=False)
        except Exception as exc:
            err = str(exc)
        if "NO_EXISTE_0000" in err:
            marca = "  <== THE FIELD IS READ (the server names the garbage)"
        elif "not included in the environment" in err:
            marca = "  (field ignored: the usual error)"
        else:
            marca = "  <== DIFFERENT ERROR"
        print(f"  {donde}.{campo:<22} -> {err[:150]}{marca}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
