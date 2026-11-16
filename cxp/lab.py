"""The LAB agent: the only way to sweep the `environments*` collections.

Why it is needed
----------------
`cxp.gaps.scan_agent` walks the 24 collections declared by the v3beta1 descriptor,
but there is one family that **cannot be reached in a normal agent**:

    environments, environments.deployments, environments.experiments,
    environments.continuousTestResults, environments.sessions.entityTypes

because they all hang off an **environment**, and an environment must be created.

And an environment CANNOT be created in the working agent. MEASURED (2026-11-15),
the three rules (see also `probes/exp_environments.py`):

1. The environment must include the version of **ALL** the agent's flows
   (flows reference one another):
   "Flow 'X' is referenced in version ... but not included in the environment".

2. And the version of **ALL** the playbooks that the published graph references
   (directly or indirectly):
   "Playbook 'P' is referenced in version ... but not included in the environment".

   Trap: `versionConfigs[].version` **is not just a flow**. It is a GENERIC
   resource name: it also accepts
   `.../playbooks/{id}/versions/{v}` and `.../tools/{id}/versions/{v}`.

3. And the version of the **TOOLS** referenced by those playbooks:
   "Tool 'T' is referenced in version ... of playbook 'P', but not included".

The closure is transitive (playbook -> playbook -> tool) and the template the
server accepts for `version` is
`projects/*/locations/*/agents/*/flows/*/versions/*`.

The working agent's blockage
-----------------------------
Its graph references `df-code-interpreter-tool` (the system code-interpreter),
and that tool:

  * **cannot be versioned**: `POST tools/df-code-interpreter-tool/versions`
    -> 404 "Tool 'df-code-interpreter-tool' does not exist in the agent";
  * **has no versions**: `GET .../versions` -> `{}`;
  * and its NAME does not match the version template ("[ResourceName error]
    ... does not match template 'projects/*/.../flows/*/versions/*'").

=> With that tool in the graph, **the environment is IMPOSSIBLE to create via API**.
That is why this module creates a **lab agent** with a minimal graph
(one flow, one page, no system tools), sweeps the `environments*` family
in it and DELETES it entirely at the end."""
from __future__ import annotations

import time
import uuid
from typing import Any

from .config import Config
from .console_api import ConsoleApi
from .gaps import SchemaIndex, scan_coleccion


def _esperar(cons: ConsoleApi, op: dict, segundos: int = 240) -> dict:
    """Follow a LRO until it ends."""
    nombre = op.get("name")
    if not nombre or op.get("done") or "/operations/" not in nombre:
        return op
    for _ in range(segundos * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + nombre))
        if op.get("done"):
            return op
    return op


def _barrer(cons: ConsoleApi, idx: SchemaIndex, url: str, clave: str,
            etiqueta: str) -> dict[str, Any]:
    """GET -> gaps against the public schema (wrapper + items)."""
    try:
        data = cons.request("GET", url)
    except Exception as exc:
        return {"estado": "error", "detalle": str(exc)[:250]}
    if isinstance(data, list):
        data = {clave.split(".")[-1]: data}
    b = scan_coleccion(data, idx, clave)
    return {"estado": "ok", "clave": clave, "detalle": etiqueta,
            "campos": sorted(data.keys()) if isinstance(data, dict) else [],
            "envoltorio": b["envoltorio"], "items": b["items"],
            "n_items": b["n_items"],
            "esquema_envoltorio": b["esquema_envoltorio"],
            "esquema_item": b["esquema_item"]}


def barrer_entornos(cons: ConsoleApi, cfg: Config, idx: SchemaIndex,
                    project: str = "") -> dict[str, Any]:
    """Sweeps `environments*` on a lab agent and deletes it.

    Returns {"agente": ..., "mediciones": {...}, "temporales": {...}, "errores": [...]}. Never leaves the agent standing: if anything fails, it
    deletes it anyway."""
    proyecto = project or cfg.project
    base = f"/projects/{proyecto}/locations/{cfg.location}/agents"
    res: dict[str, Any] = {"agente": "", "mediciones": {}, "temporales": {},
                           "errores": []}
    agente = ""
    entorno = ""

    try:
        # -------------------------------------------- 1) lab agent
        creado = cons.request("POST", cons._url(base), body={
            "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
            "defaultLanguageCode": "es",
            "timeZone": "America/Santiago",
            "description": "temporal: barrido de entornos",
        })
        agente = creado.get("name") or ""
        if not agente:
            res["errores"].append("no se pudo crear el agente de laboratorio")
            return res
        res["agente"] = agente
        # The guard only allows writes to allowlisted agents: this one is
        # new, so it is added IN MEMORY (`.env` is left untouched).
        cfg.agent_readwrite = list(cfg.agent_readwrite) + [agente.rsplit("/", 1)[-1]]
        abase = base + "/" + agente.rsplit("/", 1)[-1]

        # ------------------------------------- 2) minimal flow + its versions
        f = cons.request("POST", cons._url(f"{abase}/flows"),
                         body={"displayName": "lab_flow",
                               "transitionRoutes": [], "eventHandlers": []})
        flow = f.get("name") or ""
        res["temporales"]["flow"] = flow
        fid = flow.rsplit("/", 1)[-1]
        if flow:
            cons.request("POST", cons._url(f"{abase}/flows/{fid}/pages"),
                         body={"displayName": "lab_page",
                               "entryPoints": [{"languageCode": "es",
                                                "name": "lab.entry"}],
                               "transitionRoutes": [], "eventHandlers": []})

        # MEASURED: the environment requires a version for ALL flows, and also the
        # "start flow" of the agent (otherwise: "Version must be provided for start
        # flow resource").
        vcs: list[dict[str, str]] = []
        flows = cons.request("GET", cons._url(f"{abase}/flows", pageSize="100"))
        for fl in (flows.get("flows") or []):
            ffl = fl["name"].rsplit("/", 1)[-1]
            op = _esperar(cons, cons.request(
                "POST", cons._url(f"{abase}/flows/{ffl}/versions"),
                body={"displayName": f"lab_version_{uuid.uuid4().hex[:4]}",
                      "description": "temporal: barrido"}))
            v = (op.get("response") or {}).get("name") or ""
            if v:
                vcs.append({"version": v})
        res["temporales"]["versions"] = [v["version"] for v in vcs]

        # ------------------------------------------------------- 3) environment
        op = _esperar(cons, cons.request(
            "POST", cons._url(f"{abase}/environments"),
            body={"displayName": "lab_entorno", "description": "temporal",
                  "versionConfigs": vcs}))
        entorno = (op.get("response") or {}).get("name") or ""
        if not entorno:
            res["errores"].append(
                "no se pudo crear el entorno: " +
                str(op.get("error"))[:300])
            return res
        res["temporales"]["entorno"] = entorno

        # ------------------------------------------------------- 4) sweep
        res["mediciones"]["environments"] = _barrer(
            cons, idx, cons._url("/" + entorno), "environments", entorno)

        op = _esperar(cons, cons.request(
            "POST", cons._url(f"/{entorno}/deployments"),
            body={"flowVersion": vcs[0]["version"] if vcs else ""}))
        dep = (op.get("response") or {}).get("name") or op.get("name") or ""
        if dep:
            res["mediciones"]["environments.deployments"] = _barrer(
                cons, idx, cons._url("/" + dep), "environments.deployments", dep)

        op = _esperar(cons, cons.request(
            "POST", cons._url(f"/{entorno}/experiments"),
            body={"displayName": "lab_experiment", "description": "temporal",
                  "definition": {"environment": entorno}}))
        exp = (op.get("response") or {}).get("name") or op.get("name") or ""
        if exp:
            res["temporales"]["experiment"] = exp
            res["mediciones"]["environments.experiments"] = _barrer(
                cons, idx, cons._url("/" + exp), "environments.experiments", exp)

        res["mediciones"]["environments.continuousTestResults"] = _barrer(
            cons, idx, cons._url(f"/{entorno}/continuousTestResults"),
            "environments.continuousTestResults", entorno)

        # Environment session INSIDE (agent-level does not support entityTypes:
        # the internal name requires `environments/{id}/sessions/{sid}/...`).
        sid = f"lab-{uuid.uuid4().hex[:8]}"
        try:
            cons.request("POST",
                         cons._url(f"/{entorno}/sessions/{sid}:detectIntent"),
                         body={"queryInput": {"text": {"text": "hola"},
                                              "languageCode": "es"}})
            res["mediciones"]["environments.sessions.entityTypes"] = _barrer(
                cons, idx, cons._url(f"/{entorno}/sessions/{sid}/entityTypes"),
                "environments.sessions.entityTypes", sid)
        except Exception as exc:
            res["errores"].append(f"sesion del entorno: {str(exc)[:200]}")

        # ------------------------- 5) testCases and testCases.results
        # MEASURED: a case's `create` is NOT `{"testCases": [...]}` (the
        # server silently ignores it); it is a `TestCase` with
        # `testCaseConversationTurns`, and v3beta1's `text` is a STRING
        # (not a list) with the `languageCode` in the `QueryInput`.
        caso = ""
        try:
            creado = cons.request("POST", cons._url(f"{abase}/testCases"), body={
                "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
                "notes": "temporal: barrido",
                "testCaseConversationTurns": [
                    {"userInput": {"input": {"text": {"text": "hola"},
                                              "languageCode": "es"}}},
                ],
            })
            caso = creado.get("name") or ""
            if caso:
                res["mediciones"]["testCases"] = _barrer(
                    cons, idx, cons._url("/" + caso), "testCases", caso)
                op = _esperar(cons, cons.request(
                    "POST", cons._url(f"/{caso}:run"), body={}))
                res["mediciones"]["testCases.results"] = _barrer(
                    cons, idx, cons._url(f"/{caso}/results", pageSize="100"),
                    "testCases.results", caso)
                res["temporales"]["testCase"] = caso
        except Exception as exc:
            res["errores"].append(f"testCases: {str(exc)[:200]}")
        finally:
            if caso:
                # MEASURED: `DELETE testCases/{id}` does NOT exist (404): deletion
                # is `POST testCases:batchDelete` with `{"names": [...]}`.
                try:
                    cons.request("POST",
                                 cons._url(f"{abase}/testCases:batchDelete"),
                                 body={"names": [caso]})
                except Exception:
                    pass

    except Exception as exc:
        res["errores"].append(str(exc)[:300])
    finally:
        # ------------------------------------------------------- cleanup
        if res["temporales"].get("experiment"):
            try:
                cons.request("DELETE",
                             cons._url("/" + res["temporales"]["experiment"]))
            except Exception:
                pass
        if entorno:
            try:
                cons.request("DELETE", cons._url("/" + entorno))
            except Exception as exc:
                res["errores"].append(f"no se pudo borrar el entorno: {str(exc)[:150]}")
        if agente:
            try:
                cons.request("DELETE", cons._url("/" + agente))
            except Exception as exc:
                res["errores"].append(f"no se pudo borrar el agente: {str(exc)[:200]}")
    return res
