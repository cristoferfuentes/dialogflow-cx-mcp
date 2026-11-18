#!/usr/bin/env python3
"""API vs CONSOLE parity, measured live.

Three questions, three measurements:

1. **METHOD**: which endpoints does the console use that are in NO discovery doc
   (public or internal)? They are compared against the discovery doc, not from
   memory. Plus: do the catalog ones answer TODAY?

2. **TRANSPORT**: the console calls the REGIONAL hosts
   (`{location}-dialogflow.clients6.google.com`) for the locations that are not
   `global`. Can the MCP? (measured rule: `x-origin` = the console origin,
   `referer` = `{HOST}/static/proxy.html?usegapi=1`, signature over the console
   origin).

3. **FIELD**: which fields does the backend expose that the public proto does NOT
   declare? (live sweep, reuses `cxp.gaps.scan_agent`).

    .venv/bin/python probes/paridad_consola.py [--agente <uuid>] [--json /tmp/paridad.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import CONSOLE_HOST, ConsoleApi
from cxp.console_endpoints import HUECOS_METODO
from cxp.gaps import load_schema_index, scan_agent
from cxp.httpclient import Session

# The endpoints the console calls that are NOT in any discovery doc.
# They come from the captured traffic (probes/captura_trafico_consola.py): the console
# catalog plus the one of projects, which was NOT in `cxp/console_endpoints.py`.
ENDPOINT_PROYECTOS = {
    "id": "projects.list",
    "version": "v3alpha1",
    "metodo": "GET",
    "path": "projects",
    "estado": "verificado",
    "para_que": "The project listing (what the console project selector "
                "paints). It calls it with `filter=id=\"<project>\"` and "
                "`view=FULL`. It is NOT in any discovery doc.",
    "respuesta": "{\"projects\":[{\"name\":\"projects/my-project\", ...}]}",
}


def _estado(cons: ConsoleApi, version: str, ruta: str, query: dict | None = None) -> dict:
    """Calls a console endpoint and summarizes the result."""
    url = f"https://{CONSOLE_HOST}/{version}/{ruta.lstrip('/')}?key={cons.cfg.api_key}"
    try:
        data = cons.request("GET", url, query=query)
    except Exception as exc:
        return {"estado": "error", "detalle": str(exc)[:200]}
    if isinstance(data, dict):
        claves = sorted(data.keys())
        vacio = not claves
    else:
        claves = []
        vacio = not data
    return {"estado": "ok_vacio" if vacio else "ok", "claves": claves}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agente", default="")
    ap.add_argument("--proyecto", default="")
    ap.add_argument("--location-extra", default="us-central1")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cfg = load_config()
    agente = args.agente or cfg.only_agent()
    proyecto = args.proyecto or cfg.project
    cons = ConsoleApi(cfg)
    res: dict = {"agente": agente, "proyecto": proyecto}

    # ------------------------------------------- 1) endpoints outside any doc
    print("=" * 78)
    print("1) CONSOLE ENDPOINTS THAT ARE NOT IN ANY DISCOVERY DOC")
    print("=" * 78)
    catalogo = list(HUECOS_METODO) + [ENDPOINT_PROYECTOS]
    metodos: list[dict] = []
    for h in catalogo:
        ruta = (h.get("path") or "").replace("{project}", proyecto) \
                                 .replace("{location}", cfg.location) \
                                 .replace("{agent}", agente)
        if not ruta.startswith("projects/"):
            ruta = "/" + ruta
        q = None
        if h["id"] == "projects.list":
            q = {"filter": f'id="{proyecto}"', "view": "FULL"}
        t0 = time.perf_counter()
        est = _estado(cons, h.get("version", "v3alpha1"), ruta, q)
        ms = (time.perf_counter() - t0) * 1000
        fila = {"id": h["id"], "version": h.get("version"), "ruta": ruta,
                "vivo": est, "ms": round(ms)}
        metodos.append(fila)
        print(f"  {h['id']:<28} {est['estado']:<9} {ms:>6.0f} ms  "
              f"{','.join(est.get('claves') or [])[:60]}")
    res["endpoints_fuera_de_descriptor"] = metodos

    # ------------------------------------------------ 2) regional hosts
    print()
    print("=" * 78)
    print("2) TRANSPORT: can the MCP reach a location that is not `global`?")
    print("=" * 78)
    loc = args.location_extra
    host = f"{loc}-{CONSOLE_HOST}"
    url = (f"https://{host}/v3alpha1/projects/{proyecto}/locations/{loc}/agents"
           f"?key={cfg.api_key}")
    cabeceras = cons._headers()
    # MEASURED: the `referer` has to carry the HOST that is called; `x-origin` and
    # the signature are still the ones of the CONSOLE origin.
    cabeceras["referer"] = f"https://{host}/static/proxy.html?usegapi=1"
    regional = {}
    try:
        data = Session().json("GET", url, cabeceras, None, timeout=60)
        agentes = data.get("agents") or []
        regional = {"estado": "ok", "host": host, "agentes": len(agentes),
                    "nombres": [a.get("displayName") for a in agentes][:5]}
        print(f"  {host} -> OK, {len(agentes)} agents: {regional['nombres']}")
    except Exception as exc:
        regional = {"estado": "error", "host": host, "detalle": str(exc)[:200]}
        print(f"  {host} -> ERROR {str(exc)[:150]}")
    # The global host does NOT work for another location:
    try:
        Session().json(
            "GET",
            f"https://{CONSOLE_HOST}/v3beta1/projects/{proyecto}/locations/{loc}"
            f"/agents?key={cfg.api_key}",
            cons._headers(), None, timeout=60)
        global_ = {"estado": "ok"}
    except Exception as exc:
        global_ = {"estado": "error", "detalle": str(exc)[:200]}
    print(f"  {CONSOLE_HOST} (global host, location={loc}) -> {global_['estado']}")
    res["regional"] = {**regional, "host_global": global_}

    # ------------------------------------------------------ 3) field gaps
    print()
    print("=" * 78)
    print("3) FIELDS: live sweep of the agent")
    print("=" * 78)
    idx = load_schema_index(cfg.api_version)
    barrido = scan_agent(cons, agente, proyecto, idx=idx)
    print(f"  collections in the discovery doc: {barrido['colecciones']}")
    print(f"  reached: {len(barrido['colecciones_alcanzables'])}"
          f" | not reached: {len(barrido['sin_alcanzar'])}")
    for s in barrido["sin_alcanzar"]:
        print(f"    - {s['coleccion']}: {s['motivo'][:90]}")
    print(f"  grouped field gaps: {len(barrido['huecos'])}")
    res["barrido"] = barrido

    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
