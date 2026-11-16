#!/usr/bin/env python3
"""Test of the new tools: cx_gaps, cx_set_console_field and
cx_set_group_route_playbook. Everything on TEMPORARY resources of the writable agent.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

OK = 0
FAIL = 0


def check(nombre, cond, detalle=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {nombre}")
    else:
        FAIL += 1
        print(f"  FAIL {nombre} :: {detalle}")


def main() -> int:
    cons = server._api()
    cfg = server.CFG
    agent = cfg.default_agent
    project = cfg.project
    ab = f"/projects/{project}/locations/{cfg.location}"

    # ------------------------------------------------------------ temporary
    print("\n== temporary resources ==")
    flow = cons.request("POST", cons._url(f"{ab}/agents/{agent}/flows"),
                        {"displayName": "zz_tmp_nuevas_" + str(int(time.time()) % 100000),
                         "description": "temporal"})
    flow_id = flow["name"].rsplit("/", 1)[-1]
    page = cons.request("POST", cons._url(f"{ab}/agents/{agent}/flows/{flow_id}/pages"),
                        {"displayName": "Pagina probe",
                         "entryFulfillment": {"messages": [{"text": {"text": ["hola"]}}]}})
    page_name = page["name"]
    et = cons.request("POST", cons._url(f"{ab}/agents/{agent}/entityTypes"),
                      {"displayName": "zz_tmp_et_" + str(int(time.time()) % 100000),
                       "kind": "KIND_MAP",
                       "entities": [{"value": "uno", "synonyms": ["uno"]}]})
    et_name = et["name"]
    pbs = cons.list_playbooks(agent, project)
    pb_ref = f"projects/{project}/locations/{cfg.location}/agents/{agent}/playbooks/" + \
        (pbs[0].get("name") or "").rsplit("/", 1)[-1]
    # a route group needs a target on every route
    grp = cons.request("POST", cons._url(f"{ab}/agents/{agent}/transitionRouteGroups"),
                       {"displayName": "zz_tmp_grp_" + str(int(time.time()) % 100000),
                        "transitionRoutes": [{"condition": "true",
                                              "targetPlaybook": pb_ref}]})
    grp_name = grp["name"]

    print("\n== cx_gaps (with the temporary resources already created) ==")
    g = server.cx_gaps(agent=agent, project=project, incluir_paginas=True)
    check("cx_gaps answers", "error" not in g, json.dumps(g)[:300])
    campos = sorted({h["campo"] for h in g.get("huecos", [])})
    print("   detected fields:", ", ".join(campos[:20]))
    check("detects targetPlaybook", "targetPlaybook" in campos, str(campos))
    check("detects dtmfPattern", "dtmfPattern" in campos, str(campos))
    check("detects description (FormParameter)", "description" in campos, str(campos))
    check("detects webClientSettings", "webClientSettings" in campos, str(campos))

    try:
        print("\n== cx_set_console_field (dry-run) ==")
        d = server.cx_set_console_field(recurso=page_name, campo="speakerIdSettings",
                                        valor={"enableSpeakerId": True}, dry_run=True,
                                        agent=agent, project=project)
        check("dry-run does not write", d.get("modo") == "dry-run", json.dumps(d)[:200])
        check("dry-run returns the plan", "cambios" in d, json.dumps(d)[:200])

        print("\n== cx_set_console_field: entities[0].dtmfPattern ==")
        r = server.cx_set_console_field(recurso=et_name, campo="entities[0].dtmfPattern",
                                        valor="9", dry_run=False, agent=agent,
                                        project=project)
        check("dtmfPattern written and verified", r.get("verificado") is True,
              json.dumps(r)[:400])

        print("\n== cx_set_console_field: webClientSettings ==")
        agent_name = f"projects/{project}/locations/{cfg.location}/agents/{agent}"
        antes = cons.request("GET", cons._url("/" + agent_name))
        radio_antes = (antes.get("webClientSettings") or {}).get("boxBorderRadius")
        r = server.cx_set_console_field(recurso=agent_name,
                                        campo="webClientSettings.boxBorderRadius",
                                        valor="8", dry_run=False, agent=agent,
                                        project=project)
        check("boxBorderRadius written and verified", r.get("verificado") is True,
              json.dumps(r)[:400])
        # revert
        cons.patch_resource(agent_name,
                            {"name": agent_name,
                             "webClientSettings": {"boxBorderRadius": radio_antes}},
                            update_mask="webClientSettings")
        check("reverted", True)

        print("\n== cx_set_group_route_playbook ==")
        g3 = server.cx_set_group_route_playbook(agent=agent,
                                                group=grp_name.rsplit("/", 1)[-1],
                                                route="true",
                                                playbook=pbs[0].get("displayName", ""),
                                                dry_run=True, project=project)
        check("group: dry-run", g3.get("modo") == "dry-run", json.dumps(g3)[:300])
        g4 = server.cx_set_group_route_playbook(agent=agent,
                                                group=grp_name.rsplit("/", 1)[-1],
                                                route="true",
                                                playbook=pbs[0].get("displayName", ""),
                                                dry_run=False, project=project)
        check("group: written and verified", g4.get("verificado") is True,
              json.dumps(g4)[:400])
        g5 = server.cx_set_group_route_playbook(agent=agent,
                                                group=grp_name.rsplit("/", 1)[-1],
                                                project=project)
        check("group: read without route", g5.get("modo") == "lectura",
              json.dumps(g5)[:200])
    finally:
        for path in (f"{ab}/agents/{agent}/transitionRouteGroups/{grp_name.rsplit('/',1)[-1]}",
                     f"{ab}/agents/{agent}/entityTypes/{et_name.rsplit('/',1)[-1]}",
                     f"{ab}/agents/{agent}/flows/{flow_id}"):
            try:
                cons.request("DELETE", cons._url(path))
            except Exception as exc:
                print("   it could not be deleted", path, str(exc)[:100])

    print(f"\n== {OK} OK / {FAIL} FAIL ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
