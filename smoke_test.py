#!/usr/bin/env python3
"""Smoke test of the MCP: it calls the real tools, over the MCP protocol.

It is **generic**: there is no agent, flow, page or playbook hard-wired. Everything
is discovered in the agents of the allowlist of the `.env` (`CXP_AGENT_READ` /
`CXP_AGENT_READWRITE`), and what is written is done on **TEMPORARY** resources
that are deleted at the end.

    python smoke_test.py            # uses the writable agent of the allowlist
    CXP_AGENT_READWRITE=<uuid> python smoke_test.py

HARD RULE: it only writes on an agent of `CXP_AGENT_READWRITE` (our
copy). Never on a reference agent: that would break the parity with the
console.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
PY = sys.executable or str(HERE / ".venv" / "bin" / "python")
SERVER = str(HERE / "server.py")

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"   [{'OK ' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


def _first(res):
    for item in res.content:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return res


async def main() -> int:
    # The MCP SDK does NOT inherit the environment of the parent (it uses a safe
    # subset), so it has to be passed explicitly.
    env = dict(os.environ)
    params = StdioServerParameters(command=PY, args=[SERVER], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
            print(f"== server: {getattr(info, 'name', '?')}")
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            print(f"== tools ({len(names)}): {', '.join(sorted(names))}")
            for expected in ("cx_gaps", "cx_set_console_field",
                             "cx_set_group_route_playbook", "cx_set_route_playbook",
                             "cx_set_flow_route_playbook", "cx_limites_api"):
                check(f"the tool {expected} exists", expected in names)

            # ---------------------------------------------------------- status
            print("\n== cx_status ==")
            st = _first(await session.call_tool("cx_status", {}))
            check("credentials_ok", st.get("credentials_ok") is True)
            check("session_accepted", st.get("session_accepted") is True)
            check("api_version v3beta1", st.get("api_version") == "v3beta1")
            check("permission table", len(st.get("permissions") or []) >= 1,
                  f"n={len(st.get('permissions') or [])}")

            # The agents of the allowlist (the MCP has no default agent):
            # one to READ and one to WRITE. They may live in different
            # projects, so each one carries its own.
            visible = st.get("visible_agents") or []
            writable = st.get("writable_agents") or []
            permissions = st.get("permissions") or []

            def _id(a):
                return a.get("id", "") if isinstance(a, dict) else str(a)

            agent = _id(visible[0]) if visible else ""
            write_agent = _id(writable[0]) if writable else ""
            if not agent and not write_agent:
                print("   no allowed agent: put one in CXP_AGENT_READ or "
                      "CXP_AGENT_READWRITE")
                return 1
            # The project of the writable agent, when the allowlist declares it
            # ('<project>/<uuid>').
            project = st.get("project") or ""
            for p_ in permissions:
                if p_.get("agent") == write_agent and \
                        p_.get("project") not in (None, "", "(any)"):
                    project = p_["project"]
            print(f"   agent (read): {agent}")
            print(f"   agent (write): {write_agent} | project: {project}")
            # Does the writable agent really exist? An allowlist entry can
            # point to an agent that is not there any more.
            checked = {f.get("agent"): f for f in (st.get("checked_agents") or [])}
            if not write_agent:
                print("   no writable agent: the write tests are skipped")
                return 0
            if not (checked.get(write_agent) or {}).get("accessible"):
                # The allowlist points to an agent that is not there: it is a
                # configuration problem, not a failure of the MCP.
                print(f"   WARNING: the writable agent {write_agent} does not exist "
                      f"(404): check CXP_AGENT_READWRITE (the MCP checks it with "
                      f"a real GET). The write tests are skipped.")
                return 0

            # -------------------------------------------------------- discover
            print("\n== cx_discover ==")
            d = _first(await session.call_tool("cx_discover", {}))
            check("the API map has methods", (d.get("total_methods") or 0) > 100,
                  f"total={d.get('total_methods')}")
            det = _first(await session.call_tool("cx_discover",
                                                 {"method": "agents.flows.train"}))
            check("detail of one method", det.get("method", {}).get("http") == "POST")

            # --------------------------------------------------------- reads
            print("\n== reads ==")
            ga = _first(await session.call_tool("cx_get_agent", {"agent": agent}))
            check("GET agent", str(ga.get("data", {}).get("name", "")).endswith(agent),
                  str(ga.get("error") or "")[:120])
            fl = _first(await session.call_tool("cx_list_flows", {"agent": agent}))
            flows = fl.get("flows") or []
            check("the agent has flows", len(flows) > 0, f"n={len(flows)}")
            pb = _first(await session.call_tool("cx_list_playbooks", {"agent": agent}))
            playbooks = pb.get("playbooks") or []
            check("the agent has playbooks", len(playbooks) > 0, f"n={len(playbooks)}")
            if not playbooks:
                print("   no playbooks: the playbook-target tests are skipped")
                return 1
            pb_name = playbooks[0].get("displayName") or playbooks[0].get("name")
            pb_id = (playbooks[0].get("id")
                     or (playbooks[0].get("name") or "").rsplit("/", 1)[-1])

            # --------------------------------------------------------- gaps
            print("\n== cx_gaps (the gaps of the public API) ==")
            g = _first(await session.call_tool("cx_gaps", {"agent": agent,
                                                           "include_pages": False}))
            check("cx_gaps answers", "error" not in g, str(g.get("error"))[:150])
            check("cx_gaps detects gaps", (g.get("total_gaps") or 0) > 0,
                  f"total={g.get('total_gaps')}")
            check("cx_gaps brings the measured catalogue",
                  len(g.get("catalogue") or []) > 0)
            fields = {h.get("field") for h in (g.get("gaps") or [])}
            check("it detects a field outside the schema", len(fields) > 0,
                  ", ".join(sorted(c for c in fields if c)[:8]))
            g2 = _first(await session.call_tool("cx_gaps", {"agent": agent,
                                                            "scope": "Tool",
                                                            "include_pages": False}))
            check("cx_gaps filters by scope", "error" not in g2, str(g2)[:120])
            cob = g.get("coverage") or {}
            check("cx_gaps reports the coverage",
                  (cob.get("descriptor_collections") or 0) > 0
                  and (cob.get("reachable_collections") or 0) > 0,
                  str(cob))
            check("cx_gaps brings the METHOD gaps",
                  len(g.get("method_gaps") or []) > 0)

            # ------------------------------------------- method gaps
            print("\n== cx_endpoints_consola (endpoints outside the descriptor) ==")
            ec = _first(await session.call_tool("cx_endpoints_consola", {}))
            check("cx_endpoints_consola returns the catalogue",
                  (ec.get("total") or 0) >= 8, f"n={ec.get('total')}")
            sys_types = _first(await session.call_tool("cx_endpoints_consola",
                                                       {"endpoint": "systemEntityTypes.list"}))
            check("it calls an endpoint outside the descriptor",
                  sys_types.get("status") == "ok"
                  and bool((sys_types.get("data") or {}).get("systemEntityTypes")),
                  str(sys_types.get("error"))[:150])

            # ------------------------------- cx_api with version and absolute routes
            print("\n== cx_api: version and absolute routes ==")
            v3a = _first(await session.call_tool("cx_api", {
                "method": "GET",
                "path": "projects/{project}/locations/{location}/agents/{agent}/currentUsers",
                "params": {"project": st.get("project"), "location": st.get("location"),
                           "agent": agent},
                "version": "v3alpha1"}))
            check("cx_api reaches a v3alpha1 endpoint",
                  "error" not in v3a and bool((v3a.get("data") or {}).get("currentUsers")),
                  str(v3a.get("error"))[:150])
            root = _first(await session.call_tool("cx_api", {
                "method": "GET", "path": "/users/me/userSettings/recentProjects",
                "version": "v3alpha1"}))
            check("cx_api reaches an ABSOLUTE route from the root",
                  "error" not in root and bool((root.get("data") or {}).get("projects")),
                  str(root.get("error"))[:150])

            # ------------------------------------------------- temporary resources
            print("\n== temporary resources ==")
            mark = str(int(time.time()) % 100000)
            flow = _first(await session.call_tool("cx_api", {
                "method": "POST", "path": "agents/{agent}/flows",
                "agent": write_agent, "project": project,
                "body": {"displayName": f"zz_tmp_smoke_{mark}", "description": "temporary"}}))
            flow_id = (flow.get("data") or {}).get("name", "").rsplit("/", 1)[-1]
            check("temporary flow created", bool(flow_id), str(flow.get("error"))[:120])
            if not flow_id:
                return 1

            try:
                pg = _first(await session.call_tool("cx_api", {
                    "method": "POST", "path": "agents/{agent}/flows/{flow}/pages",
                    "agent": write_agent, "project": project,
                    "params": {"flow": flow_id},
                    "body": {"displayName": "Smoke page",
                             "entryFulfillment": {"messages": [
                                 {"text": {"text": ["hello"]}}]}}}))
                page_name = (pg.get("data") or {}).get("name", "")
                check("temporary page created", bool(page_name), str(pg.get("error"))[:160])

                et = _first(await session.call_tool("cx_api", {
                    "method": "POST", "path": "agents/{agent}/entityTypes",
                    "agent": write_agent, "project": project,
                    "body": {"displayName": f"zz_tmp_smoke_et_{mark}", "kind": "KIND_MAP",
                             "entities": [{"value": "one", "synonyms": ["one"]}]}}))
                et_name = (et.get("data") or {}).get("name", "")
                check("temporary entityType created", bool(et_name), str(et.get("error"))[:160])

                # A route with a playbook target: without it there is nothing to
                # test at the three levels (page, flow, route group).
                pb_ref = (f"projects/{project}/locations/global/agents/{write_agent}"
                          f"/playbooks/{pb_id}")
                routes = [{"condition": "true", "targetPlaybook": pb_ref}]

                pflow = _first(await session.call_tool("cx_api", {
                    "method": "PATCH", "path": "agents/{agent}/flows/{flow}",
                    "agent": write_agent, "project": project,
                    "params": {"flow": flow_id},
                    "body": {"name": f"projects/{project}/locations/global/agents/{write_agent}"
                                     f"/flows/{flow_id}", "transitionRoutes": routes},
                    "query": {"updateMask": "transitionRoutes"}}))
                check("route with a playbook in the temporary flow",
                      bool(pflow.get("data", {}).get("transitionRoutes")),
                      str(pflow.get("error"))[:140])

                ppage = _first(await session.call_tool("cx_api", {
                    "method": "PATCH", "path": "agents/{agent}/flows/{flow}/pages/{page}",
                    "agent": write_agent, "project": project,
                    "params": {"flow": flow_id,
                                                "page": page_name.rsplit("/", 1)[-1]},
                    "body": {"name": page_name, "transitionRoutes": routes},
                    "query": {"updateMask": "transitionRoutes"}}))
                check("route with a playbook in the temporary page",
                      bool(ppage.get("data", {}).get("transitionRoutes")),
                      str(ppage.get("error"))[:140])

                grp = _first(await session.call_tool("cx_api", {
                    "method": "POST", "path": "agents/{agent}/transitionRouteGroups",
                    "agent": write_agent, "project": project,
                    "body": {"displayName": f"zz_tmp_smoke_grp_{mark}",
                             "transitionRoutes": routes}}))
                group_name = (grp.get("data") or {}).get("name", "")
                check("temporary route group created", bool(group_name),
                      str(grp.get("error"))[:160])

                # ------------------------------- playbook target (3 levels)
                print("\n== playbook target (the 3 levels) ==")
                r = _first(await session.call_tool("cx_set_route_playbook", {
                    "agent": write_agent, "project": project, "flow": flow_id,
                    "page": page_name.rsplit("/", 1)[-1],
                    "route": "true", "playbook": pb_name, "dry_run": True}))
                check("page level: dry-run", r.get("mode") == "dry-run",
                      str(r.get("error") or r.get("mode"))[:140])

                fr = _first(await session.call_tool("cx_set_flow_route_playbook", {
                    "agent": write_agent, "project": project, "flow": flow_id,
                    "route": "true", "playbook": pb_name, "dry_run": True}))
                check("flow level: dry-run", fr.get("mode") == "dry-run",
                      str(fr.get("error") or fr.get("mode"))[:140])

                gr = _first(await session.call_tool("cx_set_group_route_playbook", {
                    "agent": write_agent, "project": project,
                    "group": group_name.rsplit("/", 1)[-1],
                    "route": "true", "playbook": pb_name, "dry_run": True}))
                check("route group level: dry-run", gr.get("mode") == "dry-run",
                      str(gr.get("error") or gr.get("mode"))[:140])

                # ------------------------------- writing a field outside the schema
                print("\n== cx_set_console_field (outside the schema) ==")
                dry = _first(await session.call_tool("cx_set_console_field", {
                    "resource": et_name, "field": "entities[0].dtmfPattern",
                    "value": "9", "dry_run": True, "agent": write_agent,
                    "project": project}))
                check("dry-run does not write", dry.get("mode") == "dry-run",
                      str(dry.get("error"))[:120])
                wri = _first(await session.call_tool("cx_set_console_field", {
                    "resource": et_name, "field": "entities[0].dtmfPattern",
                    "value": "9", "dry_run": False, "agent": write_agent,
                    "project": project}))
                check("dtmfPattern written and verified", wri.get("verified") is True,
                      str(wri)[:200])

                # ------------------------------- limits (the memory of the MCP)
                print("\n== cx_limites_api ==")
                lim = _first(await session.call_tool("cx_limites_api", {}))
                ids = {x.get("id") for x in (lim.get("limits") or [])}
                check("cx_limites_api returns the limits", len(ids) > 5, f"n={len(ids)}")
                check("it includes the limit of the fields outside the schema",
                      "fields_outside_the_schema" in ids, ", ".join(sorted(ids))[:120])
            finally:
                print("\n== cleanup ==")
                for path, params in (
                        (f"agents/{{agent}}/transitionRouteGroups/{group_name.rsplit('/', 1)[-1]}", {}),
                        (f"agents/{{agent}}/entityTypes/{et_name.rsplit('/', 1)[-1]}", {}),
                        (f"agents/{{agent}}/flows/{flow_id}", {})):
                    if "{" in path and path.split("/")[2] == "":
                        continue
                    rev = _first(await session.call_tool("cx_api", {
                        "method": "DELETE", "path": path, "agent": write_agent,
                        "project": project, "params": params}))
                    print(f"   deleted {path}: {rev.get('error') or 'ok'}")

            # ------------------------------------------------------- guards
            print("\n== guards ==")
            den = _first(await session.call_tool("cx_list_flows", {
                "agent": "00000000-0000-0000-0000-000000000000"}))
            check("agent without permission: denied", "error" in den, str(den)[:120])

    print(f"\n== {'ALL OK' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)} ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
