#!/usr/bin/env python3
"""EXPERIMENT: where can the SELF-REFERENCING macro `${PLAYBOOK:itself}` live.

Measured so far (previous probe):
  * POST with the macro in `steps`      -> 400 (the playbook does not exist yet / loop)
  * POST with the macro in `guidelines` -> 201 (CREATED, text preserved)
  * PATCH with the macro in `steps`     -> 400 'caused loop in playbook routes'

What is still to be measured (and it decides the escape hatch design):
  * clean POST + PATCH `updateMask=instruction.steps`      -> ? (LEAF field
    mask: according to AGENTS.md 2026-11-04g it does NOT revalidate the refs)
  * clean POST + PATCH `updateMask=instruction.guidelines`  -> ?
  * clean POST + PATCH `updateMask=instruction`             -> 400 (already measured)

If the macro can end up in `steps` (its FUNCTIONAL place: the runtime expands
it), the escape hatch is better than hiding it in `guidelines` (which the
runtime might not expand).

It only works on OUR COPY (the agent with write permission). It creates
TEMPORARY playbooks (`zz_tmp_`) and ALWAYS deletes them (finally).

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


def _state(api: ConsoleApi, base: str, pid: str) -> dict:
    """GET of the playbook: text + registered references."""
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
    agent = cfg.only_agent()
    assert_writable(cfg.project, agent, cfg)
    api = ConsoleApi(cfg)
    base = f"{api._base('')}/agents/{agent}"
    created: list[str] = []
    results: dict[str, str] = {}
    try:
        # ------------------------------------------------------------------
        # V1: clean POST (no macros) -> base for the PATCHes
        # ------------------------------------------------------------------
        name = "zz_tmp_selfref_mask"
        body = {
            "displayName": name,
            "goal": "Test playbook to measure where the self-reference can live.",
            "instruction": {
                "guidelines": "",
                "steps": [{"text": "Clean step without macros."}],
            },
        }
        print("=== V1: clean POST (no macros) ===")
        pb = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                         body=body, timeout=120.0)
        pid = pb["name"].split("/")[-1]
        created.append(pid)
        print(f"   CREATED: {pid}  ({pb.get('displayName')})")
        print(f"   referencedPlaybooks: {pb.get('referencedPlaybooks')}")

        # ------------------------------------------------------------------
        # V2: PATCH updateMask=instruction.steps  (LEAF FIELD mask)
        #     -> puts the self-referencing macro in steps
        # ------------------------------------------------------------------
        print("\n=== V2: PATCH updateMask=instruction.steps (leaf) with self-ref macro ===")
        steps = [{"text": f"Delega en ${{PLAYBOOK:{name}}}"}]
        try:
            api.request("PATCH", api._url(f"{base}/playbooks/{pid}", alt="json"),
                        body={"instruction": {"steps": steps}},
                        query={"updateMask": "instruction.steps"}, timeout=120.0)
            est = _state(api, base, pid)
            print("   PATCH ACCEPTED")
            print(f"   steps:          {est['steps'][:160]}")
            print(f"   guidelines:     {est['guidelines']!r}")
            print(f"   refPlaybooks:   {est['referencedPlaybooks']}")
            results["instruction.steps"] = "ACCEPTED (macro in steps, no edge)"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            results["instruction.steps"] = f"HTTP {e.status}"

        # ------------------------------------------------------------------
        # V3: PATCH updateMask=instruction.guidelines -> macro in guidelines
        # ------------------------------------------------------------------
        print("\n=== V3: PATCH updateMask=instruction.guidelines with self-ref macro ===")
        try:
            api.request("PATCH", api._url(f"{base}/playbooks/{pid}", alt="json"),
                        body={"instruction": {"guidelines": f"Delega en ${{PLAYBOOK:{name}}}"}},
                        query={"updateMask": "instruction.guidelines"}, timeout=120.0)
            est = _state(api, base, pid)
            print("   PATCH ACCEPTED")
            print(f"   guidelines:     {est['guidelines']!r}")
            print(f"   steps:          {est['steps'][:120]}")
            print(f"   refPlaybooks:   {est['referencedPlaybooks']}")
            results["instruction.guidelines"] = "ACCEPTED"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            results["instruction.guidelines"] = f"HTTP {e.status}"

        # ------------------------------------------------------------------
        # V4: PATCH updateMask=instruction (TOP-LEVEL field)
        #     -> should fail with 'caused loop in playbook routes'
        # ------------------------------------------------------------------
        print("\n=== V4: PATCH updateMask=instruction (top level) with self-ref macro ===")
        try:
            api.request("PATCH", api._url(f"{base}/playbooks/{pid}", alt="json"),
                        body={"instruction": {"guidelines": "",
                                              "steps": [{"text": f"Delega en ${{PLAYBOOK:{name}}}"}]}},
                        query={"updateMask": "instruction"}, timeout=120.0)
            print("   PATCH ACCEPTED (unexpected)")
            results["instruction"] = "ACCEPTED (unexpected)"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            results["instruction"] = f"HTTP {e.status}"

        # ------------------------------------------------------------------
        # V5: POST with the macro in `goal` (the goal is ALSO scanned)
        # ------------------------------------------------------------------
        print("\n=== V5: POST with the self-referencing macro in `goal` ===")
        name5 = "zz_tmp_selfref_goal"
        body5 = {
            "displayName": name5,
            "goal": f"Do not invoke yourself: ${{PLAYBOOK:{name5}}}",
            "instruction": {"guidelines": "", "steps": [{"text": "Paso limpio."}]},
        }
        try:
            pb5 = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                              body=body5, timeout=120.0)
            pid5 = pb5["name"].split("/")[-1]
            created.append(pid5)
            print(f"   CREATED (unexpected): {pid5}")
            results["goal"] = "ACCEPTED (unexpected)"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            results["goal"] = f"HTTP {e.status}"

        # ------------------------------------------------------------------
        # V6: POST with the macro in `guidelines` (the known escape hatch)
        #     and with a NORMAL `goal`: checks that guidelines is not scanned
        # ------------------------------------------------------------------
        print("\n=== V6: POST with the self-referencing macro in `guidelines` ===")
        name6 = "zz_tmp_selfref_guide"
        body6 = {
            "displayName": name6,
            "goal": "Test playbook (macro in the guidelines).",
            "instruction": {
                "guidelines": f"Delega en ${{PLAYBOOK:{name6}}}",
                "steps": [{"text": "Clean step without macros."}],
            },
        }
        try:
            pb6 = api.request("POST", api._url(f"{base}/playbooks", alt="json"),
                              body=body6, timeout=120.0)
            pid6 = pb6["name"].split("/")[-1]
            created.append(pid6)
            est = _state(api, base, pid6)
            print(f"   CREATED: {pid6}")
            print(f"   guidelines:   {est['guidelines']!r}")
            print(f"   refPlaybooks: {est['referencedPlaybooks']}  <-- edge registered?")
            results["POST guidelines"] = "ACCEPTED (no edge)"
        except HttpStatusError as e:
            print(f"   HTTP {e.status}: {e.body[:300]}")
            results["POST guidelines"] = f"HTTP {e.status}"

        print("\n=== SUMMARY ===")
        for k, v in results.items():
            print(f"   {k:28s} -> {v}")

    finally:
        print("\n=== cleanup ===")
        for pid in created:
            try:
                api.request("DELETE", api._url(f"{base}/playbooks/{pid}", alt="json",
                                              force="true"))
                print(f"   playbook {pid} deleted")
            except HttpStatusError as e:
                print(f"   !! could not delete {pid}: HTTP {e.status} {e.body[:160]}")
        alive = [p for p in api.list_playbooks(agent)
                 if (p.get("displayName") or "").startswith("zz_tmp_")]
        print("   zz_tmp_ playbooks left:", len(alive))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
