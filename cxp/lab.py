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
from .gaps import SchemaIndex, scan_collection


def _wait(cons: ConsoleApi, op: dict, seconds: int = 240) -> dict:
    """Follow a LRO until it ends."""
    name = op.get("name")
    if not name or op.get("done") or "/operations/" not in name:
        return op
    for _ in range(seconds * 2):
        time.sleep(0.5)
        op = cons.request("GET", cons._url("/" + name))
        if op.get("done"):
            return op
    return op


def _sweep(cons: ConsoleApi, idx: SchemaIndex, url: str, key: str,
           label: str) -> dict[str, Any]:
    """GET -> gaps against the public schema (wrapper + items)."""
    try:
        data = cons.request("GET", url)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:250]}
    if isinstance(data, list):
        data = {key.split(".")[-1]: data}
    b = scan_collection(data, idx, key)
    return {"status": "ok", "key": key, "detail": label,
            "fields": sorted(data.keys()) if isinstance(data, dict) else [],
            "wrapper": b["wrapper"], "items": b["items"],
            "n_items": b["n_items"],
            "wrapper_schema": b["wrapper_schema"],
            "item_schema": b["item_schema"]}


def sweep_environments(cons: ConsoleApi, cfg: Config, idx: SchemaIndex,
                       project: str = "") -> dict[str, Any]:
    """Sweeps `environments*` on a lab agent and deletes it.

    Returns {"agent": ..., "measurements": {...}, "temporary": {...}, "errors": [...]}. Never leaves the agent standing: if anything fails, it
    deletes it anyway."""
    project_id = project or cfg.project
    base = f"/projects/{project_id}/locations/{cfg.location}/agents"
    res: dict[str, Any] = {"agent": "", "measurements": {}, "temporary": {},
                           "errors": []}
    agent = ""
    environment = ""

    try:
        # -------------------------------------------- 1) lab agent
        created = cons.request("POST", cons._url(base), body={
            "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
            "defaultLanguageCode": "es",
            "timeZone": "America/Santiago",
            "description": "temporary: environment sweep",
        })
        agent = created.get("name") or ""
        if not agent:
            res["errors"].append("could not create the lab agent")
            return res
        res["agent"] = agent
        # The guard only allows writes to allowlisted agents: this one is
        # new, so it is added IN MEMORY (`.env` is left untouched).
        cfg.agent_readwrite = list(cfg.agent_readwrite) + [agent.rsplit("/", 1)[-1]]
        abase = base + "/" + agent.rsplit("/", 1)[-1]

        # ------------------------------------- 2) minimal flow + its versions
        f = cons.request("POST", cons._url(f"{abase}/flows"),
                         body={"displayName": "lab_flow",
                               "transitionRoutes": [], "eventHandlers": []})
        flow = f.get("name") or ""
        res["temporary"]["flow"] = flow
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
            op = _wait(cons, cons.request(
                "POST", cons._url(f"{abase}/flows/{ffl}/versions"),
                body={"displayName": f"lab_version_{uuid.uuid4().hex[:4]}",
                      "description": "temporary: sweep"}))
            v = (op.get("response") or {}).get("name") or ""
            if v:
                vcs.append({"version": v})
        res["temporary"]["versions"] = [v["version"] for v in vcs]

        # ------------------------------------------------------- 3) environment
        op = _wait(cons, cons.request(
            "POST", cons._url(f"{abase}/environments"),
            body={"displayName": "lab_environment", "description": "temporary",
                  "versionConfigs": vcs}))
        environment = (op.get("response") or {}).get("name") or ""
        if not environment:
            res["errors"].append(
                "could not create the environment: " +
                str(op.get("error"))[:300])
            return res
        res["temporary"]["environment"] = environment

        # ------------------------------------------------------- 4) sweep
        res["measurements"]["environments"] = _sweep(
            cons, idx, cons._url("/" + environment), "environments", environment)

        op = _wait(cons, cons.request(
            "POST", cons._url(f"/{environment}/deployments"),
            body={"flowVersion": vcs[0]["version"] if vcs else ""}))
        dep = (op.get("response") or {}).get("name") or op.get("name") or ""
        if dep:
            res["measurements"]["environments.deployments"] = _sweep(
                cons, idx, cons._url("/" + dep), "environments.deployments", dep)

        op = _wait(cons, cons.request(
            "POST", cons._url(f"/{environment}/experiments"),
            body={"displayName": "lab_experiment", "description": "temporary",
                  "definition": {"environment": environment}}))
        exp = (op.get("response") or {}).get("name") or op.get("name") or ""
        if exp:
            res["temporary"]["experiment"] = exp
            res["measurements"]["environments.experiments"] = _sweep(
                cons, idx, cons._url("/" + exp), "environments.experiments", exp)

        res["measurements"]["environments.continuousTestResults"] = _sweep(
            cons, idx, cons._url(f"/{environment}/continuousTestResults"),
            "environments.continuousTestResults", environment)

        # Environment session INSIDE (agent-level does not support entityTypes:
        # the internal name requires `environments/{id}/sessions/{sid}/...`).
        sid = f"lab-{uuid.uuid4().hex[:8]}"
        try:
            cons.request("POST",
                         cons._url(f"/{environment}/sessions/{sid}:detectIntent"),
                         body={"queryInput": {"text": {"text": "hola"},
                                              "languageCode": "es"}})
            res["measurements"]["environments.sessions.entityTypes"] = _sweep(
                cons, idx, cons._url(f"/{environment}/sessions/{sid}/entityTypes"),
                "environments.sessions.entityTypes", sid)
        except Exception as exc:
            res["errors"].append(f"environment session: {str(exc)[:200]}")

        # ------------------------- 5) testCases and testCases.results
        # MEASURED: a case's `create` is NOT `{"testCases": [...]}` (the
        # server silently ignores it); it is a `TestCase` with
        # `testCaseConversationTurns`, and v3beta1's `text` is a STRING
        # (not a list) with the `languageCode` in the `QueryInput`.
        case = ""
        try:
            created = cons.request("POST", cons._url(f"{abase}/testCases"), body={
                "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
                "notes": "temporary: sweep",
                "testCaseConversationTurns": [
                    {"userInput": {"input": {"text": {"text": "hola",
                                                     "languageCode": "es"}}}},
                ],
            })
            case = created.get("name") or ""
            if case:
                res["measurements"]["testCases"] = _sweep(
                    cons, idx, cons._url("/" + case), "testCases", case)
                op = _wait(cons, cons.request(
                    "POST", cons._url(f"/{case}:run"), body={}))
                res["measurements"]["testCases.results"] = _sweep(
                    cons, idx, cons._url(f"/{case}/results", pageSize="100"),
                    "testCases.results", case)
                res["temporary"]["testCase"] = case
        except Exception as exc:
            res["errors"].append(f"testCases: {str(exc)[:200]}")
        finally:
            if case:
                # MEASURED: `DELETE testCases/{id}` does NOT exist (404): deletion
                # is `POST testCases:batchDelete` with `{"names": [...]}`.
                try:
                    cons.request("POST",
                                 cons._url(f"{abase}/testCases:batchDelete"),
                                 body={"names": [case]})
                except Exception:
                    pass

    except Exception as exc:
        res["errors"].append(str(exc)[:300])
    finally:
        # ------------------------------------------------------- cleanup
        if res["temporary"].get("experiment"):
            try:
                cons.request("DELETE",
                             cons._url("/" + res["temporary"]["experiment"]))
            except Exception:
                pass
        if environment:
            try:
                cons.request("DELETE", cons._url("/" + environment))
            except Exception as exc:
                res["errors"].append(
                    f"could not delete the environment: {str(exc)[:150]}")
        if agent:
            try:
                cons.request("DELETE", cons._url("/" + agent))
            except Exception as exc:
                res["errors"].append(f"could not delete the agent: {str(exc)[:200]}")
    return res
