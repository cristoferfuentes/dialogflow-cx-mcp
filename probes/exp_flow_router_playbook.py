#!/usr/bin/env python3
"""SAFE EXPERIMENT: does the internal backend (first party) PERSIST
`targetPlaybook` in a FLOW-LEVEL transition route (the "router" of a flow)?

HARD RULE (2026-11-08): **never edit a playbook that is not our own copy
of the agent**. This script, therefore:

  * only works on the agent of `CXP_AGENT_READWRITE` (our copy);
  * does NOT create, does NOT edit and does NOT delete any playbook;
  * creates a TEMPORARY FLOW, deletes it ALWAYS (finally) and does not touch any
    existing flow.

Procedure:
  1. creates a temporary flow in our copy
  2. gives it a transition route targeting one of OUR playbooks
  3. verifies via GET that the `targetPlaybook` persisted
  4. deletes the temporary flow (finally) and checks that it is gone

    .venv/bin/python probes/exp_flow_router_playbook.py
"""
from __future__ import annotations

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
    agent_id = cfg.only_agent()
    # HARD RULE: this script can only touch our own copy.
    assert_writable(cfg.project, agent_id, cfg)
    print(f"working agent: {agent_id} (our copy, with write permission)")

    api = ConsoleApi(cfg)
    base = f"{api._base('')}/agents/{agent_id}"
    flow_id = None
    try:
        pbs = api.list_playbooks(agent_id)
        if not pbs:
            print("ABORT: our copy has no playbooks")
            return 1
        pb = pbs[0]
        pb_ref = f"projects/{cfg.project}/locations/{cfg.location}/agents/{agent_id}" \
                 f"/playbooks/{pb['name'].split('/')[-1]}"
        print(f"target playbook (OURS, not modified): {pb.get('displayName')}")

        # ------------------------------------------------ temporary flow
        created = api.request("POST", api._url(f"{base}/flows", alt="json"),
                              body={"displayName": TMP_FLOW,
                                    "description": "temporal - probe"})
        flow_id = created["name"].split("/")[-1]
        print(f"temporary flow created: {flow_id}")

        # ------------------------------------- PATCH with targetPlaybook
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
                print(f"\ntemporary flow DELETED: {flow_id}")
                vivos = [f for f in api.list_flows(agent_id)
                         if f["name"].split("/")[-1] == flow_id]
                print("   still in the agent?", bool(vivos))
            except HttpStatusError as e:
                print(f"\n!! COULD NOT DELETE the flow {flow_id}: HTTP {e.status}")


if __name__ == "__main__":
    raise SystemExit(main())
