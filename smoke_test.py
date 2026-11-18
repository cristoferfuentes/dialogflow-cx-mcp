#!/usr/bin/env python3
"""Smoke test del MCP: llama a las herramientas de verdad, por el protocolo MCP.

Es **generico**: no hay ningun agente, flow, pagina ni playbook cableado. Todo
se descubre en los agentes de la lista blanca del `.env` (`CXP_AGENT_READ` /
`CXP_AGENT_READWRITE`), y lo que se escribe se hace sobre recursos
**TEMPORALES** que se borran al final.

    python smoke_test.py            # usa el agente con escritura de la lista blanca
    CXP_AGENT_READWRITE=<uuid> python smoke_test.py

REGLA DURA: solo se escribe en un agente de `CXP_AGENT_READWRITE` (nuestra
copia). Nunca en un agente de referencia: se romperia la paridad con la
consola.
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


def check(nombre: str, ok: bool, detalle: str = "") -> None:
    print(f"   [{'OK ' if ok else 'FALLO'}] {nombre}" + (f" -- {detalle}" if detalle else ""))
    if not ok:
        FAIL.append(nombre)


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
    # El SDK de MCP NO hereda el entorno del padre (usa un subconjunto seguro),
    # asi que hay que pasarlo explicito.
    env = dict(os.environ)
    params = StdioServerParameters(command=PY, args=[SERVER], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
            print(f"== servidor: {getattr(info, 'name', '?')}")
            tools = await session.list_tools()
            nombres = {t.name for t in tools.tools}
            print(f"== herramientas ({len(nombres)}): {', '.join(sorted(nombres))}")
            for esperadas in ("cx_gaps", "cx_set_console_field",
                              "cx_set_group_route_playbook", "cx_set_route_playbook",
                              "cx_set_flow_route_playbook", "cx_limites_api"):
                check(f"existe la herramienta {esperadas}", esperadas in nombres)

            # ---------------------------------------------------------- status
            print("\n== cx_status ==")
            st = _first(await session.call_tool("cx_status", {}))
            check("credenciales_ok", st.get("credenciales_ok") is True)
            check("sesion_aceptada", st.get("sesion_aceptada") is True)
            check("api_version v3beta1", st.get("api_version") == "v3beta1")
            check("tabla de permisos", len(st.get("permisos") or []) >= 1,
                  f"n={len(st.get('permisos') or [])}")

            # The agents of the allowlist (the MCP has no default agent):
            # one to READ and one to WRITE. They may live in different
            # projects, so each one carries its own.
            visibles = st.get("agentes_visibles") or []
            con_escritura = st.get("agentes_con_escritura") or []
            permisos = st.get("permisos") or []

            def _id(a):
                return a.get("id", "") if isinstance(a, dict) else str(a)

            agente = _id(visibles[0]) if visibles else ""
            agente_escritura = _id(con_escritura[0]) if con_escritura else ""
            if not agente and not agente_escritura:
                print("   no allowed agent: put one in CXP_AGENT_READ or "
                      "CXP_AGENT_READWRITE")
                return 1
            # The project of the writable agent, when the allowlist declares it
            # ('<project>/<uuid>').
            proyecto = st.get("proyecto") or ""
            for p_ in permisos:
                if p_.get("agente") == agente_escritura and \
                        p_.get("proyecto") not in (None, "", "(cualquiera)"):
                    proyecto = p_["proyecto"]
            print(f"   agente (lectura): {agente}")
            print(f"   agente (escritura): {agente_escritura} | proyecto: {proyecto}")
            # Does the writable agent really exist? An allowlist entry can
            # point to an agent that is not there any more.
            comprobados = {f.get("agente"): f for f in (st.get("agentes_comprobados") or [])}
            if not agente_escritura:
                print("   no hay agente con escritura: se saltan las pruebas de escritura")
                return 0
            if not (comprobados.get(agente_escritura) or {}).get("accesible"):
                # The allowlist points to an agent that is not there: it is a
                # configuration problem, not a failure of the MCP.
                print(f"   AVISO: el agente con escritura {agente_escritura} no existe "
                      f"(404): revisa CXP_AGENT_READWRITE (el MCP lo comprueba con "
                      f"un GET real). Se saltan las pruebas de escritura.")
                return 0

            # -------------------------------------------------------- discover
            print("\n== cx_discover ==")
            d = _first(await session.call_tool("cx_discover", {}))
            check("el mapa de la API tiene metodos", (d.get("total_metodos") or 0) > 100,
                  f"total={d.get('total_metodos')}")
            det = _first(await session.call_tool("cx_discover",
                                                 {"method": "agents.flows.train"}))
            check("detalle de un metodo", det.get("metodo", {}).get("http") == "POST")

            # --------------------------------------------------------- lecturas
            print("\n== lecturas ==")
            ga = _first(await session.call_tool("cx_get_agent", {"agent": agente}))
            check("GET agent", str(ga.get("datos", {}).get("name", "")).endswith(agente),
                  str(ga.get("error") or "")[:120])
            fl = _first(await session.call_tool("cx_list_flows", {"agent": agente}))
            flows = fl.get("flows") or []
            check("el agente tiene flows", len(flows) > 0, f"n={len(flows)}")
            pb = _first(await session.call_tool("cx_list_playbooks", {"agent": agente}))
            playbooks = pb.get("playbooks") or []
            check("el agente tiene playbooks", len(playbooks) > 0, f"n={len(playbooks)}")
            if not playbooks:
                print("   sin playbooks: se saltan las pruebas de destino a playbook")
                return 1
            pb_nombre = playbooks[0].get("displayName") or playbooks[0].get("name")
            pb_id = (playbooks[0].get("id")
                     or (playbooks[0].get("name") or "").rsplit("/", 1)[-1])

            # --------------------------------------------------------- huecos
            print("\n== cx_gaps (los huecos de la API publica) ==")
            g = _first(await session.call_tool("cx_gaps", {"agent": agente,
                                                           "incluir_paginas": False}))
            check("cx_gaps responde", "error" not in g, str(g.get("error"))[:150])
            check("cx_gaps detecta huecos", (g.get("total_huecos") or 0) > 0,
                  f"total={g.get('total_huecos')}")
            check("cx_gaps trae el catalogo medido",
                  len(g.get("catalogo") or []) > 0)
            campos = {h.get("campo") for h in (g.get("huecos") or [])}
            check("detecta un campo fuera de esquema", len(campos) > 0,
                  ", ".join(sorted(c for c in campos if c)[:8]))
            g2 = _first(await session.call_tool("cx_gaps", {"agent": agente,
                                                            "scope": "Tool",
                                                            "incluir_paginas": False}))
            check("cx_gaps filtra por scope", "error" not in g2, str(g2)[:120])
            cob = g.get("cobertura") or {}
            check("cx_gaps dice la cobertura",
                  (cob.get("colecciones_del_descriptor") or 0) > 0
                  and (cob.get("colecciones_alcanzadas") or 0) > 0,
                  str(cob))
            check("cx_gaps trae los huecos de METODO",
                  len(g.get("huecos_de_metodo") or []) > 0)

            # ------------------------------------------- huecos de METODO
            print("\n== cx_endpoints_consola (endpoints fuera del descriptor) ==")
            ec = _first(await session.call_tool("cx_endpoints_consola", {}))
            check("cx_endpoints_consola devuelve el catalogo",
                  (ec.get("total") or 0) >= 8, f"n={ec.get('total')}")
            sys_types = _first(await session.call_tool("cx_endpoints_consola",
                                                       {"endpoint": "systemEntityTypes.list"}))
            check("llama a un endpoint fuera del descriptor",
                  sys_types.get("estado") == "ok"
                  and bool((sys_types.get("datos") or {}).get("systemEntityTypes")),
                  str(sys_types.get("error"))[:150])

            # ------------------------------- cx_api con version y rutas absolutas
            print("\n== cx_api: version y rutas absolutas ==")
            v3a = _first(await session.call_tool("cx_api", {
                "method": "GET",
                "path": "projects/{project}/locations/{location}/agents/{agent}/currentUsers",
                "params": {"project": st.get("proyecto"), "location": st.get("location"),
                           "agent": agente},
                "version": "v3alpha1"}))
            check("cx_api alcanza un endpoint de v3alpha1",
                  "error" not in v3a and bool((v3a.get("datos") or {}).get("currentUsers")),
                  str(v3a.get("error"))[:150])
            raiz = _first(await session.call_tool("cx_api", {
                "method": "GET", "path": "/users/me/userSettings/recentProjects",
                "version": "v3alpha1"}))
            check("cx_api alcanza una ruta ABSOLUTA desde la raiz",
                  "error" not in raiz and bool((raiz.get("datos") or {}).get("projects")),
                  str(raiz.get("error"))[:150])

            # ------------------------------------------------- recursos temporales
            print("\n== recursos temporales ==")
            marca = str(int(time.time()) % 100000)
            flow = _first(await session.call_tool("cx_api", {
                "method": "POST", "path": "agents/{agent}/flows",
                "agent": agente_escritura, "project": proyecto,
                "body": {"displayName": f"zz_tmp_smoke_{marca}", "description": "temporal"}}))
            flow_id = (flow.get("datos") or {}).get("name", "").rsplit("/", 1)[-1]
            check("flow temporal creado", bool(flow_id), str(flow.get("error"))[:120])
            if not flow_id:
                return 1

            try:
                pg = _first(await session.call_tool("cx_api", {
                    "method": "POST", "path": "agents/{agent}/flows/{flow}/pages",
                    "agent": agente_escritura, "project": proyecto,
                    "params": {"flow": flow_id},
                    "body": {"displayName": "Pagina smoke",
                             "entryFulfillment": {"messages": [
                                 {"text": {"text": ["hola"]}}]}}}))
                page_name = (pg.get("datos") or {}).get("name", "")
                check("pagina temporal creada", bool(page_name), str(pg.get("error"))[:160])

                et = _first(await session.call_tool("cx_api", {
                    "method": "POST", "path": "agents/{agent}/entityTypes",
                    "agent": agente_escritura, "project": proyecto,
                    "body": {"displayName": f"zz_tmp_smoke_et_{marca}", "kind": "KIND_MAP",
                             "entities": [{"value": "uno", "synonyms": ["uno"]}]}}))
                et_name = (et.get("datos") or {}).get("name", "")
                check("entityType temporal creado", bool(et_name), str(et.get("error"))[:160])

                # Una ruta con destino a playbook: sin ella no hay nada que
                # probar en los tres niveles (pagina, flow, grupo de rutas).
                pb_ref = (f"projects/{proyecto}/locations/global/agents/{agente_escritura}"
                          f"/playbooks/{pb_id}")
                ruta = [{"condition": "true", "targetPlaybook": pb_ref}]

                pflow = _first(await session.call_tool("cx_api", {
                    "method": "PATCH", "path": "agents/{agent}/flows/{flow}",
                    "agent": agente_escritura, "project": proyecto,
                    "params": {"flow": flow_id},
                    "body": {"name": f"projects/{proyecto}/locations/global/agents/{agente_escritura}"
                                     f"/flows/{flow_id}", "transitionRoutes": ruta},
                    "query": {"updateMask": "transitionRoutes"}}))
                check("ruta con playbook en el flow temporal",
                      bool(pflow.get("datos", {}).get("transitionRoutes")),
                      str(pflow.get("error"))[:140])

                ppage = _first(await session.call_tool("cx_api", {
                    "method": "PATCH", "path": "agents/{agent}/flows/{flow}/pages/{page}",
                    "agent": agente_escritura, "project": proyecto,
                    "params": {"flow": flow_id,
                                                "page": page_name.rsplit("/", 1)[-1]},
                    "body": {"name": page_name, "transitionRoutes": ruta},
                    "query": {"updateMask": "transitionRoutes"}}))
                check("ruta con playbook en la pagina temporal",
                      bool(ppage.get("datos", {}).get("transitionRoutes")),
                      str(ppage.get("error"))[:140])

                grp = _first(await session.call_tool("cx_api", {
                    "method": "POST", "path": "agents/{agent}/transitionRouteGroups",
                    "agent": agente_escritura, "project": proyecto,
                    "body": {"displayName": f"zz_tmp_smoke_grp_{marca}",
                             "transitionRoutes": ruta}}))
                group_name = (grp.get("datos") or {}).get("name", "")
                check("grupo de rutas temporal creado", bool(group_name),
                      str(grp.get("error"))[:160])

                # ------------------------------- destino a playbook (3 niveles)
                print("\n== destino a playbook (los 3 niveles) ==")
                r = _first(await session.call_tool("cx_set_route_playbook", {
                    "agent": agente_escritura, "project": proyecto, "flow": flow_id,
                    "page": page_name.rsplit("/", 1)[-1],
                    "route": "true", "playbook": pb_nombre, "dry_run": True}))
                check("nivel pagina: dry-run", r.get("modo") == "dry-run",
                      str(r.get("error") or r.get("modo"))[:140])

                fr = _first(await session.call_tool("cx_set_flow_route_playbook", {
                    "agent": agente_escritura, "project": proyecto, "flow": flow_id,
                    "route": "true", "playbook": pb_nombre, "dry_run": True}))
                check("nivel flow: dry-run", fr.get("modo") == "dry-run",
                      str(fr.get("error") or fr.get("modo"))[:140])

                gr = _first(await session.call_tool("cx_set_group_route_playbook", {
                    "agent": agente_escritura, "project": proyecto,
                    "group": group_name.rsplit("/", 1)[-1],
                    "route": "true", "playbook": pb_nombre, "dry_run": True}))
                check("nivel grupo de rutas: dry-run", gr.get("modo") == "dry-run",
                      str(gr.get("error") or gr.get("modo"))[:140])

                # ------------------------------- escritura de un campo fuera de esquema
                print("\n== cx_set_console_field (fuera de esquema) ==")
                dry = _first(await session.call_tool("cx_set_console_field", {
                    "recurso": et_name, "campo": "entities[0].dtmfPattern",
                    "valor": "9", "dry_run": True, "agent": agente_escritura,
                    "project": proyecto}))
                check("dry-run no escribe", dry.get("modo") == "dry-run",
                      str(dry.get("error"))[:120])
                wri = _first(await session.call_tool("cx_set_console_field", {
                    "recurso": et_name, "campo": "entities[0].dtmfPattern",
                    "valor": "9", "dry_run": False, "agent": agente_escritura,
                    "project": proyecto}))
                check("dtmfPattern escrito y verificado", wri.get("verificado") is True,
                      str(wri)[:200])

                # ------------------------------- limites (la memoria del MCP)
                print("\n== cx_limites_api ==")
                lim = _first(await session.call_tool("cx_limites_api", {}))
                ids = {x.get("id") for x in (lim.get("limites") or [])}
                check("cx_limites_api devuelve limites", len(ids) > 5, f"n={len(ids)}")
                check("incluye el limite de los campos fuera de esquema",
                      "campos_fuera_de_esquema" in ids, ", ".join(sorted(ids))[:120])
            finally:
                print("\n== limpieza ==")
                for path, params in (
                        (f"agents/{{agent}}/transitionRouteGroups/{group_name.rsplit('/', 1)[-1]}", {}),
                        (f"agents/{{agent}}/entityTypes/{et_name.rsplit('/', 1)[-1]}", {}),
                        (f"agents/{{agent}}/flows/{flow_id}", {})):
                    if "{" in path and path.split("/")[2] == "":
                        continue
                    rev = _first(await session.call_tool("cx_api", {
                        "method": "DELETE", "path": path, "agent": agente_escritura,
                        "project": proyecto, "params": params}))
                    print(f"   borrado {path}: {rev.get('error') or 'ok'}")

            # ------------------------------------------------------- guardas
            print("\n== guardas ==")
            den = _first(await session.call_tool("cx_list_flows", {
                "agent": "00000000-0000-0000-0000-000000000000"}))
            check("agente sin permiso: denegado", "error" in den, str(den)[:120])

    print(f"\n== {'TODO OK' if not FAIL else 'FALLOS: ' + ', '.join(FAIL)} ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
