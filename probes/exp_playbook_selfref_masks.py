#!/usr/bin/env python3
"""EXPERIMENTO: donde puede vivir la macro AUTORREFERENTE `${PLAYBOOK:si_mismo}`.

Medido hasta ahora (probe anterior):
  * POST con la macro en `steps`      -> 400 (el playbook aun no existe / bucle)
  * POST con la macro en `guidelines` -> 201 (CREADO, texto conservado)
  * PATCH con la macro en `steps`     -> 400 'caused loop in playbook routes'

Lo que falta por medir (y decide el diseno del escape hatch):
  * POST limpio + PATCH `updateMask=instruction.steps`      -> ? (mascara de
    campo HOJA: segun AGENTS.md 2026-11-04g NO revalida las refs)
  * POST limpio + PATCH `updateMask=instruction.guidelines`  -> ?
  * POST limpio + PATCH `updateMask=instruction`             -> 400 (ya medido)

Si la macro puede acabar en `steps` (su posicion FUNCIONAL: el runtime la
expande), el escape hatch es mejor que esconderla en `guidelines` (que el
runtime podria no expandir).

Solo trabaja sobre NUESTRA COPIA (agente con permiso de escritura). Crea
playbooks TEMPORALES (`zz_tmp_`) y los borra SIEMPRE (finally).

    .venv/bin/python probes/exp_playbook_selfref_masks.py
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


def _estado(api: ConsoleApi, base: str, pid: str) -> dict:
    """GET del playbook: texto + referencias registradas."""
    pb = api.request("GET", api._url(f"{base}/playbooks/{pid}"))
    instr = pb.get("instruction") or {}
    return {
        "goal": pb.get("goal"),
        "guidelines": instr.get("guidelines"),
        "steps": json.dumps(instr.get("steps"), ensure_ascii=False),
        "referencedPlaybooks": pb.get("referencedPlaybooks"),
        "referencedTools": pb.get("referencedTools"),
        "referencedFlows": pb.get("referencedFlows"),
    }


def main() -> int:
    cfg = load_config()
    agent = cfg.default_agent
    assert_writable(cfg.project, agent, cfg)
    api = ConsoleApi(cfg)
    base = f"{api._base('')}/agents/{agent}"
    creados: list[str] = []
    resultados: dict[str, str] = {}
    try:
        # ------------------------------------------------------------------
        # V1: POST limpio (sin macros) -> base para los PATCH
        # ------------------------------------------------------------------
        name = "zz_tmp_selfref_mask"
        body = {
            "displayName": name,
            "goal": "Playbook de prueba para medir donde puede vivir la autorreferencia.",
            "instruction": {
                "guidelines": "",
                "steps": [{"text": "Paso limpio sin macros."}],
            },
        }
        print("=== V1: POST limpio (sin macros) ===")
        pb = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                         body=body, timeout=120.0)
        pid = pb["name"].split("/")[-1]
        creados.append(pid)
        print(f"   CREADO: {pid}  ({pb.get('displayName')})")
        print(f"   referencedPlaybooks: {pb.get('referencedPlaybooks')}")

        # ------------------------------------------------------------------
        # V2: PATCH updateMask=instruction.steps  (mascara de CAMPO HOJA)
        #     -> mete la macro autorreferente en steps
        # ------------------------------------------------------------------
        print("\n=== V2: PATCH updateMask=instruction.steps (hoja) con macro autorref ===")
        steps = [{"text": f"Delega en ${{PLAYBOOK:{name}}}"}]
        try:
            api.request("PATCH", api._url(f"{base}/playbooks/{pid}", alt="json"),
                        body={"instruction": {"steps": steps}},
                        query={"updateMask": "instruction.steps"}, timeout=120.0)
            est = _estado(api, base, pid)
            print("   PATCH ACEPTADO")
            print(f"   steps:          {est['steps'][:160]}")
            print(f"   guidelines:     {est['guidelines']!r}")
            print(f"   refPlaybooks:   {est['referencedPlaybooks']}")
            resultados["instruction.steps"] = "ACEPTADO (macro en steps, sin arista)"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            resultados["instruction.steps"] = f"HTTP {e.status}"

        # ------------------------------------------------------------------
        # V3: PATCH updateMask=instruction.guidelines -> macro en guidelines
        # ------------------------------------------------------------------
        print("\n=== V3: PATCH updateMask=instruction.guidelines con macro autorref ===")
        try:
            api.request("PATCH", api._url(f"{base}/playbooks/{pid}", alt="json"),
                        body={"instruction": {"guidelines": f"Delega en ${{PLAYBOOK:{name}}}"}},
                        query={"updateMask": "instruction.guidelines"}, timeout=120.0)
            est = _estado(api, base, pid)
            print("   PATCH ACEPTADO")
            print(f"   guidelines:     {est['guidelines']!r}")
            print(f"   steps:          {est['steps'][:120]}")
            print(f"   refPlaybooks:   {est['referencedPlaybooks']}")
            resultados["instruction.guidelines"] = "ACEPTADO"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            resultados["instruction.guidelines"] = f"HTTP {e.status}"

        # ------------------------------------------------------------------
        # V4: PATCH updateMask=instruction (campo de NIVEL SUPERIOR)
        #     -> deberia fallar con 'caused loop in playbook routes'
        # ------------------------------------------------------------------
        print("\n=== V4: PATCH updateMask=instruction (nivel superior) con macro autorref ===")
        try:
            api.request("PATCH", api._url(f"{base}/playbooks/{pid}", alt="json"),
                        body={"instruction": {"guidelines": "",
                                              "steps": [{"text": f"Delega en ${{PLAYBOOK:{name}}}"}]}},
                        query={"updateMask": "instruction"}, timeout=120.0)
            print("   PATCH ACEPTADO (inesperado)")
            resultados["instruction"] = "ACEPTADO (inesperado)"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            resultados["instruction"] = f"HTTP {e.status}"

        # ------------------------------------------------------------------
        # V5: POST con la macro en `goal` (el goal TAMBIEN se escanea)
        # ------------------------------------------------------------------
        print("\n=== V5: POST con la macro autorreferente en `goal` ===")
        name5 = "zz_tmp_selfref_goal"
        body5 = {
            "displayName": name5,
            "goal": f"No te invoques a ti mismo: ${{PLAYBOOK:{name5}}}",
            "instruction": {"guidelines": "", "steps": [{"text": "Paso limpio."}]},
        }
        try:
            pb5 = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                              body=body5, timeout=120.0)
            pid5 = pb5["name"].split("/")[-1]
            creados.append(pid5)
            print(f"   CREADO (inesperado): {pid5}")
            resultados["goal"] = "ACEPTADO (inesperado)"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            resultados["goal"] = f"HTTP {e.status}"

        # ------------------------------------------------------------------
        # V6: POST con la macro en `guidelines` (el escape hatch conocido)
        #     y con un `goal` NORMAL: comprueba que guidelines no se escanea
        # ------------------------------------------------------------------
        print("\n=== V6: POST con la macro autorreferente en `guidelines` ===")
        name6 = "zz_tmp_selfref_guide"
        body6 = {
            "displayName": name6,
            "goal": "Playbook de prueba (macro en guidelines).",
            "instruction": {
                "guidelines": f"Delega en ${{PLAYBOOK:{name6}}}",
                "steps": [{"text": "Paso limpio sin macros."}],
            },
        }
        try:
            pb6 = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                              body=body6, timeout=120.0)
            pid6 = pb6["name"].split("/")[-1]
            creados.append(pid6)
            est = _estado(api, base, pid6)
            print(f"   CREADO: {pid6}")
            print(f"   guidelines:   {est['guidelines']!r}")
            print(f"   refPlaybooks: {est['referencedPlaybooks']}  <-- arista registrada?")
            resultados["POST guidelines"] = "ACEPTADO (sin arista)"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            resultados["POST guidelines"] = f"HTTP {e.status}"

        print("\n=== RESUMEN ===")
        for k, v in resultados.items():
            print(f"   {k:28s} -> {v}")

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
