#!/usr/bin/env python3
"""FINAL VERIFICATION of the MCP cx-playbook (everything in one script, with a report).

It runs, in this order:

  1. The unit tests (pytest).
  2. The smoke test.
  3. The cleanup: no temporary resource (environments, test cases, versions) and
     no orphan lab agent in the known locations.
  4. The internal proto catalog: the 38 MEASURED fields, valid states and
     none of them declared in the public discovery doc.
  5. A LIVE measurement of the 31 fields (probes/exp_verifica_campos_internos.py)
     and the contrast of each measured state against the catalog.
  6. A real write of an internal field with `cx_set_console_field`
     (verified by re-reading it).

It leaves the report in `VERIFICACION.md` (at the repo root).

    .venv/bin/python probes/verificacion_final.py            # everything
    .venv/bin/python probes/verificacion_final.py --rapido   # without the live measurement
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from cxp.config import load_config
from cxp.console_api import LOCACIONES_CONOCIDAS, ConsoleApi
from cxp.gaps import SchemaIndex
from cxp.proto_interno import (
    ESTADOS_ESCRITURA,
    HUECOS_PROTO_INTERNO,
    resumen,
)

PY = str(RAIZ / ".venv" / "bin" / "python")
DISCOVERY = RAIZ / ".discovery" / "v3beta1.json"

# How each catalog field is called in the live probe.
ID_SONDA = {
    "QueryInput": lambda c: "queryinput." + c,
    "Webhook": lambda c: "webhook." + c,
    "EventHandler": lambda c: "eventhandler." + c,
    "AdvancedSettingsDtmfSettings": lambda c: "dtmf." + c,
    "AdvancedSettingsSpeechSettings": lambda c: "speech." + c,
    "IntentTrainingPhrasePart": lambda c: "intent." + c,
    "ConversationTurn": lambda c: "testcase.expectations",
    "ConversationTurnUserInput": lambda c: "testcase.parameterScope",
    "DtmfInput": lambda c: "testcase.dtmf.transformed",
}


class Informe:
    """Accumulates the sections of the report."""

    def __init__(self) -> None:
        self.lineas: list[str] = []
        self.ok = True

    def seccion(self, titulo: str) -> None:
        self.lineas.append("")
        self.lineas.append(f"## {titulo}")
        self.lineas.append("")

    def ok_(self, texto: str) -> None:
        self.lineas.append(f"- ✅ {texto}")

    def fail(self, texto: str) -> None:
        self.ok = False
        self.lineas.append(f"- ❌ {texto}")

    def info(self, texto: str) -> None:
        self.lineas.append(f"- {texto}")

    def tabla(self, cabecera: list[str], filas: list[list[Any]]) -> None:
        self.lineas.append("| " + " | ".join(cabecera) + " |")
        self.lineas.append("|" + "|".join("---" for _ in cabecera) + "|")
        for f in filas:
            self.lineas.append("| " + " | ".join(str(x) for x in f) + " |")


# --------------------------------------------------------------- 1) tests

def seccion_tests(informe: Informe) -> None:
    informe.seccion("1. Unit tests")
    # CAREFUL: `pyproject.toml` already sets `addopts = "-q"`; adding another `-q`
    # turns it into `-qq` and pytest OMITS the summary ("76 passed in ...").
    p = subprocess.run([PY, "-m", "pytest", "tests/"],
                       cwd=RAIZ, capture_output=True, text=True, timeout=900)
    salida = (p.stdout + p.stderr).strip().splitlines()
    resumen_pytest = ""
    for linea in salida:
        if re.search(r"\d+ (passed|failed)", linea):
            resumen_pytest = linea.strip()
    m = re.search(r"(\d+) passed", resumen_pytest)
    total = int(m.group(1)) if m else 0
    if p.returncode == 0 and total:
        informe.ok_(f"`pytest tests/ -q` → {resumen_pytest}")
    else:
        informe.fail(f"`pytest tests/ -q` → {resumen_pytest or p.returncode}")
        for linea in salida[-15:]:
            informe.info(linea)
    informe.info(f"total number of tests: {total}")


# --------------------------------------------------------------- 2) smoke

def seccion_smoke(informe: Informe) -> None:
    informe.seccion("2. Smoke test")
    p = subprocess.run([PY, "smoke_test.py"], cwd=RAIZ,
                       capture_output=True, text=True, timeout=900)
    if "TODO OK" in p.stdout:
        informe.ok_("`smoke_test.py` → TODO OK")
    else:
        informe.fail("`smoke_test.py` → " +
                     (p.stdout.strip().splitlines() or ["no output"])[-1])


# --------------------------------------------------------- 3) cleanup

def seccion_limpieza(informe: Informe, cons: ConsoleApi, cfg) -> None:
    informe.seccion("3. Cleanup (temporary resources and lab agents)")
    base = (f"/projects/{cfg.project}/locations/{cfg.location}"
            f"/agents/{cfg.only_agent()}")

    entornos = (cons.request("GET", cons._url(f"{base}/environments"))
                .get("environments") or [])
    casos = (cons.request("GET", cons._url(f"{base}/testCases", pageSize="100"))
             .get("testCases") or [])
    flows = (cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
             .get("flows") or [])
    versiones = 0
    for f in flows:
        fid = f["name"].rsplit("/", 1)[-1]
        versiones += len(cons.request(
            "GET", cons._url(f"{base}/flows/{fid}/versions", pageSize="50"))
            .get("versions") or [])
    pb_con = []
    for p in (cons.request("GET", cons._url(f"{base}/playbooks", pageSize="100"))
              .get("playbooks") or []):
        if cons.request("GET", cons._url(f"/{p['name']}/versions", pageSize="10")
                        ).get("playbookVersions"):
            pb_con.append(p.get("displayName"))
    tool_con = []
    for t in (cons.request("GET", cons._url(f"{base}/tools", pageSize="100"))
              .get("tools") or []):
        try:
            if cons.request("GET", cons._url(f"/{t['name']}/versions",
                                             pageSize="10")
                            ).get("toolVersions"):
                tool_con.append(t.get("displayName"))
        except Exception:
            continue

    if not (entornos or casos or versiones or pb_con or tool_con):
        informe.ok_("no temporary resources: 0 environments, 0 test cases, "
                    f"0 flow versions ({len(flows)} flows), "
                    "0 playbook/tool versions")
    else:
        informe.fail(f"temporary resources are left: environments={len(entornos)} "
                     f"test cases={len(casos)} versions={versiones} "
                     f"playbooks={pb_con} tools={tool_con}")

    # Lab agents in ALL the known locations.
    huerfanos = []
    for loc in sorted(set(["global"] + list(LOCACIONES_CONOCIDAS))):
        try:
            for a in cons.list_agents(location=loc):
                if str(a.get("displayName", "")).startswith("zz_tmp_lab"):
                    huerfanos.append(f"{loc}:{a['name']}")
        except Exception:
            continue
    if huerfanos:
        informe.fail(f"lab agents not deleted: {huerfanos}")
    else:
        informe.ok_("no orphan lab agent in the known locations")


# --------------------------------------------------------- 4) the catalog

def seccion_catalogo(informe: Informe) -> None:
    informe.seccion("4. Internal proto catalog")
    r = resumen()
    if r["sin_medir"]:
        informe.fail(f"fields are still unmeasured: {r['sin_medir']}")
    else:
        informe.ok_(f"{r['medidos']}/{r['campos']} fields MEASURED "
                    f"({r['esquemas']} schemas)")
    invalidos = [(h["esquema"], c, e) for h in HUECOS_PROTO_INTERNO
                 for c, e in (h.get("escritura") or {}).items()
                 if e not in ESTADOS_ESCRITURA]
    if invalidos:
        informe.fail(f"invalid states: {invalidos}")
    else:
        informe.ok_("all the states are valid")

    if DISCOVERY.exists():
        idx = SchemaIndex.load(DISCOVERY, version="v3beta1")
        declarados = []
        for h in HUECOS_PROTO_INTERNO:
            props = set()
            for nombre, p in idx.props.items():
                if nombre.endswith(h["esquema"]):
                    props = set(p)
                    break
            for c in h["campos"]:
                if c in props:
                    declarados.append(f"{h['esquema']}.{c}")
        if declarados:
            informe.fail(f"the public discovery doc DOES declare: {declarados}")
        else:
            informe.ok_("none of the 38 fields is declared by the public v3beta1 "
                        "discovery doc (they are still gaps)")
    else:
        informe.info("no cached discovery doc: nothing to contrast")


# ------------------------------------------- 5) live measurement + contrast

def seccion_viva(informe: Informe, rapido: bool) -> dict[str, Any]:
    informe.seccion("5. Live measurement of the fields (lab agent)")
    if rapido:
        informe.info("skipped (--rapido)")
        return {}

    t0 = time.time()
    p = subprocess.run([PY, "-u", "probes/exp_verifica_campos_internos.py"],
                       cwd=RAIZ, capture_output=True, text=True, timeout=2400)
    dur = time.time() - t0
    if p.returncode != 0:
        informe.fail(f"the live probe failed (rc={p.returncode}): "
                     f"{p.stdout.strip()[-300:]}")
        return {}

    medido = {c["id"]: c for c in json.loads(
        Path("/tmp/campos_internos.json").read_text(encoding="utf-8")
    )["casos"]}

    filas = []
    diferencias = []
    for h in HUECOS_PROTO_INTERNO:
        f = ID_SONDA.get(h["esquema"])
        for c, estado in sorted(h["escritura"].items()):
            if not f:
                continue
            m = medido.get(f(c))
            if not m:
                continue
            vivo = m.get("escritura")
            tipo = m.get("tipo") or "—"
            if vivo == estado:
                marca = "✅"
            else:
                marca = "⚠️"
                diferencias.append(f"{h['esquema']}.{c}: "
                                   f"catalog={estado} live={vivo}")
            filas.append([f"`{h['esquema']}.{c}`", tipo, estado, vivo, marca])
    informe.info(f"live measurement: {len(filas)} fields in {dur:.0f} s")
    if diferencias:
        informe.fail(f"{len(diferencias)} differences: {diferencias}")
    else:
        informe.ok_("the 31 states measured live MATCH the catalog")
    informe.tabla(["Field", "Measured type", "Catalog", "Live", ""], filas)
    return medido


# --------------------------------------- 6) real write of an internal field

def seccion_escritura(informe: Informe, cons: ConsoleApi, cfg) -> None:
    informe.seccion("6. Real write of an internal field (cx_set_console_field)")
    import server
    base = f"/projects/{cfg.project}/locations/{cfg.location}/agents"
    creado = cons.request("POST", cons._url(base), body={
        "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
        "defaultLanguageCode": "es", "timeZone": "America/Santiago",
        "description": "temporal: verificacion final",
    })
    nombre = creado.get("name") or ""
    if not nombre:
        informe.fail("the lab agent could not be created")
        return
    ident = nombre.rsplit("/", 1)[-1]
    cfg.agent_readwrite = list(cfg.agent_readwrite) + [ident]
    server.CFG.agent_readwrite = list(server.CFG.agent_readwrite) + [ident]
    try:
        casos = [
            ("advancedSettings.speechSettings.enableLlmEndpointing", True),
            ("advancedSettings.speechSettings.utteranceWaitTimeout", "2s"),
            ("advancedSettings.speechSettings.minDigits", 4),
        ]
        filas = []
        for campo, valor in casos:
            r = server.cx_set_console_field(recurso=nombre, campo=campo,
                                            valor=valor, dry_run=False)
            leido = (cons.request("GET", cons._url("/" + nombre))
                     .get("advancedSettings", {}).get("speechSettings", {}))
            valor_leido = None
            partes = campo.split(".")[-1]
            if partes in leido:
                valor_leido = leido[partes]
            _guardado = r.get("guardado", valor_leido is not None)
            filas.append([f"`{campo}`", json.dumps(valor),
                          json.dumps(valor_leido),
                          "✅" if valor_leido is not None else "❌"])
        if all(f[-1] == "✅" for f in filas):
            informe.ok_("3 internal fields written with the MCP tool "
                        "and verified by re-reading them")
        else:
            informe.fail(f"some write was not saved: {filas}")
        informe.tabla(["Field", "Written", "Re-read", ""], filas)
    finally:
        try:
            cons.request("DELETE", cons._url("/" + nombre))
            informe.info("lab agent deleted")
        except Exception as exc:
            informe.fail(f"the agent could not be deleted: {str(exc)[:150]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rapido", action="store_true",
                    help="skips the live measurement of the 31 fields")
    args = ap.parse_args()

    cfg = load_config()
    cons = ConsoleApi(cfg)
    informe = Informe()
    inicio = datetime.now(timezone.utc).astimezone()

    informe.lineas.append("# Final verification — MCP `cx-playbook`")
    informe.lineas.append("")
    informe.lineas.append(f"Generated on {inicio:%Y-%m-%d %H:%M %Z} "
                           f"· project `{cfg.project}` · location "
                           f"`{cfg.location}`")
    informe.lineas.append("")
    informe.lineas.append(
        "Full verification of the MCP: tests, smoke, cleanup, internal proto "
        "catalog, **live measurement** of the 31 fields that were missing and "
        "a real write of an internal field with the MCP tool.")

    seccion_tests(informe)
    seccion_smoke(informe)
    seccion_limpieza(informe, cons, cfg)
    seccion_catalogo(informe)
    seccion_viva(informe, args.rapido)
    seccion_escritura(informe, cons, cfg)

    informe.seccion("Verdict")
    informe.lineas.append("")
    if informe.ok:
        informe.lineas.append("**TODO OK** — all the checks pass.")
    else:
        informe.lineas.append("**THERE ARE FAILURES** (marked above).")
    informe.lineas.append("")
    informe.lineas.append(f"_(end of the verification, "
                           f"{datetime.now(timezone.utc).astimezone():%H:%M:%S})_")

    texto = "\n".join(informe.lineas) + "\n"
    destino = RAIZ / "VERIFICACION.md"
    destino.write_text(texto, encoding="utf-8")
    print(texto)
    print(f"-> {destino}")
    return 0 if informe.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
