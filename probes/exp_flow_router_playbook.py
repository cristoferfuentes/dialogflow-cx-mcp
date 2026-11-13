#!/usr/bin/env python3
"""EXPERIMENTO SEGURO: ¿el backend interno (primera parte) PERSISTE
`targetPlaybook` en una transition route de NIVEL FLOW (el "router" de un flow)?

REGLA DURA (2026-11-08): **nunca se edita un playbook que no sea nuestra copia
del agente**. Este script, por tanto:

  * solo trabaja sobre el agente de `CXP_AGENT_READWRITE` (nuestra copia);
  * NO crea, NO edita y NO borra ningun playbook;
  * crea un FLOW TEMPORAL, lo borra SIEMPRE (finally) y no toca ningun flow
    existente.

Procedimiento:
  1. crea un flow temporal en nuestra copia
  2. le pone una transition route con destino a un playbook NUESTRO
  3. verifica por GET que el `targetPlaybook` persistio
  4. borra el flow temporal (finally) y comprueba que ya no existe

    .venv/bin/python probes/exp_flow_router_playbook.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi
from cxp.guard import assert_writable
from cxp.httpclient import HttpStatusError

TMP_FLOW = "zz_tmp_probe_router_playbook"


def main() -> int:
    cfg = load_config()
    agent_id = cfg.default_agent
    # REGLA DURA: este script solo puede tocar nuestra copia.
    assert_writable(cfg.project, agent_id, cfg)
    print(f"agente de trabajo: {agent_id} (nuestra copia, con permiso de escritura)")

    api = ConsoleApi(cfg)
    base = f"{api._base('')}/agents/{agent_id}"
    flow_id = None
    try:
        pbs = api.list_playbooks(agent_id)
        if not pbs:
            print("ABORTO: nuestra copia no tiene playbooks")
            return 1
        pb = pbs[0]
        pb_ref = f"projects/{cfg.project}/locations/{cfg.location}/agents/{agent_id}" \
                 f"/playbooks/{pb['name'].split('/')[-1]}"
        print(f"playbook destino (NUESTRO, no se modifica): {pb.get('displayName')}")

        # ------------------------------------------------ flow temporal
        created = api.request("POST", api._url(f"{base}/flows", alt="json"),
                              body={"displayName": TMP_FLOW,
                                    "description": "temporal - probe"})
        flow_id = created["name"].split("/")[-1]
        print(f"flow temporal creado: {flow_id}")

        # ------------------------------------- PATCH con targetPlaybook
        flow_body = {
            "name": created["name"],
            "displayName": created.get("displayName"),
            "description": created.get("description"),
            "transitionRoutes": [{
                "condition": "true",
                "targetPlaybook": pb_ref,
            }],
        }
        for ver in ("v3alpha1", "v3beta1"):
            api.cfg.api_version = ver
            try:
                api.patch_flow(agent_id, flow_id, flow_body,
                               update_mask="transitionRoutes")
                print(f"[{ver}] PATCH -> OK")
            except HttpStatusError as e:
                print(f"[{ver}] PATCH -> HTTP {e.status}: {e.body[:200]}")
                continue
            for gv in ("v3alpha1", "v3beta1"):
                api.cfg.api_version = gv
                got = api.get_flow(agent_id, flow_id)
                tps = [r.get("targetPlaybook") for r in (got.get("transitionRoutes") or [])]
                print(f"    GET[{gv}] targetPlaybook={tps}")
        api.cfg.api_version = cfg.api_version
        return 0
    finally:
        if flow_id:
            try:
                api.request("DELETE", api._url(f"{base}/flows/{flow_id}", alt="json"))
                print(f"\nflow temporal BORRADO: {flow_id}")
                vivos = [f for f in api.list_flows(agent_id)
                         if f["name"].split("/")[-1] == flow_id]
                print("   sigue en el agente?", bool(vivos))
            except HttpStatusError as e:
                print(f"\n!! NO SE PUDO BORRAR el flow {flow_id}: HTTP {e.status}")


if __name__ == "__main__":
    raise SystemExit(main())
