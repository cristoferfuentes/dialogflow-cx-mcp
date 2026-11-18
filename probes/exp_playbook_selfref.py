#!/usr/bin/env python3
"""EXPERIMENT: can a self-referencing playbook be created in OUR COPY
(`${PLAYBOOK:itself}`), which the API rejects with "caused loop in playbook routes"?

It only works on our own copy (the agent with write permission). It creates
TEMPORARY playbooks and ALWAYS deletes them (finally). It does NOT touch any
existing playbook nor any other agent.

Variants tested:
  V1  self-referencing macro in `steps`      -> the loop error is expected
  V2  self-referencing macro in `guidelines` -> ESCAPE HATCH (it is not scanned)
  V3  PATCH of V2 moving the macro from guidelines to steps -> one-way door

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
    agent = cfg.only_agent()
    assert_writable(cfg.project, agent, cfg)
    api = ConsoleApi(cfg)
    base = f"{api._base('')}/agents/{agent}"
    creados: list[str] = []
    try:
        # ---------------------------------------------- V1: macro in steps
        name1 = "zz_tmp_selfref_steps"
        body1 = {
            "displayName": name1,
            "goal": "Playbook de prueba con autorreferencia en steps.",
            "instruction": {"guidelines": "",
                            "steps": [{"text": f"Delega en ${{PLAYBOOK:{name1}}}"}]},
        }
        print("=== V1: self-referencing macro in `steps` ===")
        try:
            pb = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                             body=body1, timeout=120.0)
            pid = pb["name"].split("/")[-1]
            creados.append(pid)
            print(f"   CREATED (unexpected): {pid}")
            print("   referencedPlaybooks:", pb.get("referencedPlaybooks"))
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")

        # ------------------------------------------ V2: macro in guidelines
        name2 = "zz_tmp_selfref_guidelines"
        body2 = {
            "displayName": name2,
            "goal": "Playbook de prueba con autorreferencia en guidelines.",
            "instruction": {"guidelines": f"Delega en ${{PLAYBOOK:{name2}}}",
                            "steps": [{"text": "Paso limpio sin macros."}]},
        }
        print("\n=== V2: self-referencing macro in `guidelines` ===")
        pid2 = None
        try:
            pb = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                             body=body2, timeout=120.0)
            pid2 = pb["name"].split("/")[-1]
            creados.append(pid2)
            print(f"   CREATED: {pid2}  ({pb.get('displayName')})")
            print(f"   referencedPlaybooks: {pb.get('referencedPlaybooks')}  <-- edge registered?")
            print(f"   instruction.guidelines: {(pb.get('instruction') or {}).get('guidelines')!r}")
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")

        # ------------------------- V3: PATCH moving the macro to steps
        if pid2:
            print("\n=== V3: PATCH moving the macro from guidelines to steps ===")
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
                print("   PATCH ACCEPTED (unexpected)")
            except HttpStatusError as e:
                print(f"   HTTP {e.status}: {e.body[:300]}")

            # V4: rename so that the macro (in guidelines) points to itself
            print("\n=== V4: the playbook is already named like its macro: does anything change? ===")
            after = api.request("GET", api._url(f"{base}/playbooks/{pid2}"))
            print("   guidelines:", repr((after.get("instruction") or {}).get("guidelines")))
            print("   referencedPlaybooks:", after.get("referencedPlaybooks"))
            print("   steps:", json.dumps((after.get("instruction") or {}).get("steps"),
                                          ensure_ascii=False)[:200])

    finally:
        print("\n=== cleanup ===")
        for pid in creados:
            try:
                api.request("DELETE", api._url(f"{base}/playbooks/{pid}", alt="json",
                                              force="true"))
                print(f"   playbook {pid} deleted")
            except HttpStatusError as e:
                print(f"   !! could not delete {pid}: HTTP {e.status} {e.body[:160]}")
        vivos = [p for p in api.list_playbooks(agent)
                 if (p.get("displayName") or "").startswith("zz_tmp_")]
        print("   zz_tmp_ playbooks left:", len(vivos))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
