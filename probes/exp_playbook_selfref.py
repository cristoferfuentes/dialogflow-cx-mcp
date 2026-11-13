#!/usr/bin/env python3
"""EXPERIMENTO: ¿se puede crear en NUESTRA COPIA un playbook autorreferenciado
(`${PLAYBOOK:si_mismo}`), que la API rechaza con "caused loop in playbook routes"?

Solo trabaja sobre nuestra copia (agente con permiso de escritura). Crea
playbooks TEMPORALES y los borra SIEMPRE (finally). NO toca ningun playbook
existente ni ningun otro agente.

Variantes probadas:
  V1  macro autorreferente en `steps`      -> se espera el error de bucle
  V2  macro autorreferente en `guidelines` -> ESCAPE HATCH (no se escanea)
  V3  PATCH de V2 moviendo la macro de guidelines a steps -> puerta de un solo sentido

    .venv/bin/python probes/exp_playbook_selfref.py
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


def main() -> int:
    cfg = load_config()
    agent = cfg.default_agent
    assert_writable(cfg.project, agent, cfg)
    api = ConsoleApi(cfg)
    base = f"{api._base('')}/agents/{agent}"
    creados: list[str] = []
    try:
        # ---------------------------------------------- V1: macro en steps
        name1 = "zz_tmp_selfref_steps"
        body1 = {
            "displayName": name1,
            "goal": "Playbook de prueba con autorreferencia en steps.",
            "instruction": {"guidelines": "",
                            "steps": [{"text": f"Delega en ${{PLAYBOOK:{name1}}}"}]},
        }
        print("=== V1: macro autorreferente en `steps` ===")
        try:
            pb = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                             body=body1, timeout=120.0)
            pid = pb["name"].split("/")[-1]
            creados.append(pid)
            print(f"   CREADO (inesperado): {pid}")
            print("   referencedPlaybooks:", pb.get("referencedPlaybooks"))
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")

        # ------------------------------------------ V2: macro en guidelines
        name2 = "zz_tmp_selfref_guidelines"
        body2 = {
            "displayName": name2,
            "goal": "Playbook de prueba con autorreferencia en guidelines.",
            "instruction": {"guidelines": f"Delega en ${{PLAYBOOK:{name2}}}",
                            "steps": [{"text": "Paso limpio sin macros."}]},
        }
        print("\n=== V2: macro autorreferente en `guidelines` ===")
        pid2 = None
        try:
            pb = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                             body=body2, timeout=120.0)
            pid2 = pb["name"].split("/")[-1]
            creados.append(pid2)
            print(f"   CREADO: {pid2}  ({pb.get('displayName')})")
            print(f"   referencedPlaybooks: {pb.get('referencedPlaybooks')}  <-- arista registrada?")
            print(f"   instruction.guidelines: {(pb.get('instruction') or {}).get('guidelines')!r}")
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")

        # ------------------------- V3: PATCH moviendo la macro a steps
        if pid2:
            print("\n=== V3: PATCH moviendo la macro de guidelines a steps ===")
            fresh = api.request("GET", api._url(f"{base}/playbooks/{pid2}"))
            instr = dict(fresh.get("instruction") or {})
            instr["guidelines"] = ""
            instr["steps"] = [{"text": f"Delega en ${{PLAYBOOK:{name2}}}"}]
            patch_body = {k: v for k, v in fresh.items()
                          if k in ("name", "displayName", "description", "goal",
                                   "instruction", "llmModelSettings", "codeBlock")}
            patch_body["instruction"] = instr
            try:
                api.request("PATCH", api._url(f"{base}/playbooks/{pid2}", alt="json"),
                            body=patch_body,
                            query={"updateMask": "instruction"}, timeout=120.0)
                print("   PATCH ACEPTADO (inesperado)")
            except HttpStatusError as e:
                print(f"   HTTP {e.status}: {e.body[:300]}")

            # V4: renombrar para que la macro (en guidelines) apunte a si mismo
            print("\n=== V4: el playbook ya se llama como su macro: ¿cambia algo? ===")
            after = api.request("GET", api._url(f"{base}/playbooks/{pid2}"))
            print("   guidelines:", repr((after.get("instruction") or {}).get("guidelines")))
            print("   referencedPlaybooks:", after.get("referencedPlaybooks"))
            print("   steps:", json.dumps((after.get("instruction") or {}).get("steps"),
                                          ensure_ascii=False)[:200])

    finally:
        print("\n=== limpieza ===")
        for pid in creados:
            try:
                api.request("DELETE", api._url(f"{base}/playbooks/{pid}", alt="json",
                                              force="true"))
                print(f"   playbook {pid} borrado")
            except HttpStatusError as e:
                print(f"   !! no se pudo borrar {pid}: HTTP {e.status} {e.body[:160]}")
        vivos = [p for p in api.list_playbooks(agent)
                 if (p.get("displayName") or "").startswith("zz_tmp_")]
        print("   playbooks zz_tmp_ restantes:", len(vivos))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
