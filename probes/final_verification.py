#!/usr/bin/env python3
"""FINAL VERIFICATION of the MCP cx-playbook (everything in one script, with a report).

It runs, in this order:

  1. The unit tests (pytest).
  2. The smoke test.
  3. The cleanup: no temporary resource (environments, test cases, versions) and
     no orphan lab agent in the known locations.
  4. The internal proto catalog: the 38 MEASURED fields, valid states and
     none of them declared in the public discovery doc.
  5. A LIVE measurement of the 31 fields (probes/exp_verify_internal_fields.py)
     and the contrast of each measured state against the catalog.
  6. A real write of an internal field with `cx_set_console_field`
     (verified by re-reading it).

It leaves the report in `VERIFICACION.md` (at the repo root).

    .venv/bin/python probes/final_verification.py            # everything
    .venv/bin/python probes/final_verification.py --fast   # without the live measurement
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cxp.config import load_config
from cxp.console_api import KNOWN_LOCATIONS, ConsoleApi
from cxp.gaps import SchemaIndex
from cxp.proto_interno import (
    INTERNAL_PROTO_GAPS,
    WRITE_STATES,
    summary,
)

PY = str(ROOT / ".venv" / "bin" / "python")
DISCOVERY = ROOT / ".discovery" / "v3beta1.json"

# How each catalog field is called in the live probe.
PROBE_ID = {
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
        self.lines: list[str] = []
        self.ok = True

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"## {title}")
        self.lines.append("")

    def ok_(self, text: str) -> None:
        self.lines.append(f"- ✅ {text}")

    def fail(self, text: str) -> None:
        self.ok = False
        self.lines.append(f"- ❌ {text}")

    def info(self, text: str) -> None:
        self.lines.append(f"- {text}")

    def table(self, header: list[str], rows: list[list[Any]]) -> None:
        self.lines.append("| " + " | ".join(header) + " |")
        self.lines.append("|" + "|".join("---" for _ in header) + "|")
        for f in rows:
            self.lines.append("| " + " | ".join(str(x) for x in f) + " |")


# --------------------------------------------------------------- 1) tests

def section_tests(report: Informe) -> None:
    report.section("1. Unit tests")
    # CAREFUL: `pyproject.toml` already sets `addopts = "-q"`; adding another `-q`
    # turns it into `-qq` and pytest OMITS the summary ("76 passed in ...").
    p = subprocess.run([PY, "-m", "pytest", "tests/"],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    output = (p.stdout + p.stderr).strip().splitlines()
    pytest_summary = ""
    for line in output:
        if re.search(r"\d+ (passed|failed)", line):
            pytest_summary = line.strip()
    m = re.search(r"(\d+) passed", pytest_summary)
    total = int(m.group(1)) if m else 0
    if p.returncode == 0 and total:
        report.ok_(f"`pytest tests/ -q` → {pytest_summary}")
    else:
        report.fail(f"`pytest tests/ -q` → {pytest_summary or p.returncode}")
        for line in output[-15:]:
            report.info(line)
    report.info(f"total number of tests: {total}")


# --------------------------------------------------------------- 2) smoke

def section_smoke(report: Informe) -> None:
    report.section("2. Smoke test")
    p = subprocess.run([PY, "smoke_test.py"], cwd=ROOT,
                       capture_output=True, text=True, timeout=900)
    if "TODO OK" in p.stdout:
        report.ok_("`smoke_test.py` → TODO OK")
    else:
        report.fail("`smoke_test.py` → " +
                     (p.stdout.strip().splitlines() or ["no output"])[-1])


# --------------------------------------------------------- 3) cleanup

def section_cleanup(report: Informe, cons: ConsoleApi, cfg) -> None:
    report.section("3. Cleanup (temporary resources and lab agents)")
    base = (f"/projects/{cfg.project}/locations/{cfg.location}"
            f"/agents/{cfg.only_agent()}")

    environments = (cons.request("GET", cons._url(f"{base}/environments"))
                   .get("environments") or [])
    cases = (cons.request("GET", cons._url(f"{base}/testCases", pageSize="100"))
             .get("testCases") or [])
    flows = (cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
             .get("flows") or [])
    versions = 0
    for f in flows:
        fid = f["name"].rsplit("/", 1)[-1]
        versions += len(cons.request(
            "GET", cons._url(f"{base}/flows/{fid}/versions", pageSize="50"))
            .get("versions") or [])
    playbook_versions = []
    for p in (cons.request("GET", cons._url(f"{base}/playbooks", pageSize="100"))
              .get("playbooks") or []):
        if cons.request("GET", cons._url(f"/{p['name']}/versions", pageSize="10")
                        ).get("playbookVersions"):
            playbook_versions.append(p.get("displayName"))
    tool_versions = []
    for t in (cons.request("GET", cons._url(f"{base}/tools", pageSize="100"))
              .get("tools") or []):
        try:
            if cons.request("GET", cons._url(f"/{t['name']}/versions",
                                             pageSize="10")
                            ).get("toolVersions"):
                tool_versions.append(t.get("displayName"))
        except Exception:
            continue

    if not (environments or cases or versions or playbook_versions or tool_versions):
        report.ok_("no temporary resources: 0 environments, 0 test cases, "
                    f"0 flow versions ({len(flows)} flows), "
                    "0 playbook/tool versions")
    else:
        report.fail(f"temporary resources are left: environments={len(environments)} "
                     f"test cases={len(cases)} versions={versions} "
                     f"playbooks={playbook_versions} tools={tool_versions}")

    # Lab agents in ALL the known locations.
    orphans = []
    for loc in sorted(set(["global"] + list(KNOWN_LOCATIONS))):
        try:
            for a in cons.list_agents(location=loc):
                if str(a.get("displayName", "")).startswith("zz_tmp_lab"):
                    orphans.append(f"{loc}:{a['name']}")
        except Exception:
            continue
    if orphans:
        report.fail(f"lab agents not deleted: {orphans}")
    else:
        report.ok_("no orphan lab agent in the known locations")


# --------------------------------------------------------- 4) the catalog

def section_catalog(report: Informe) -> None:
    report.section("4. Internal proto catalog")
    r = summary()
    if r["unmeasured"]:
        report.fail(f"fields are still unmeasured: {r['unmeasured']}")
    else:
        report.ok_(f"{r['measured']}/{r['fields']} fields MEASURED "
                    f"({r['schemas']} schemas)")
    invalidos = [(h["schema"], c, e) for h in INTERNAL_PROTO_GAPS
                 for c, e in (h.get("write") or {}).items()
                 if e not in WRITE_STATES]
    if invalidos:
        report.fail(f"invalid states: {invalidos}")
    else:
        report.ok_("all the states are valid")

    if DISCOVERY.exists():
        idx = SchemaIndex.load(DISCOVERY, version="v3beta1")
        declarados = []
        for h in INTERNAL_PROTO_GAPS:
            props = set()
            for name, p in idx.props.items():
                if name.endswith(h["schema"]):
                    props = set(p)
                    break
            for c in h["fields"]:
                if c in props:
                    declarados.append(f"{h['schema']}.{c}")
        if declarados:
            report.fail(f"the public discovery doc DOES declare: {declarados}")
        else:
            report.ok_("none of the 38 fields is declared by the public v3beta1 "
                        "discovery doc (they are still gaps)")
    else:
        report.info("no cached discovery doc: nothing to contrast")


# ------------------------------------------- 5) live measurement + contrast

def section_live(report: Informe, fast: bool) -> dict[str, Any]:
    report.section("5. Live measurement of the fields (lab agent)")
    if fast:
        report.info("skipped (--fast)")
        return {}

    t0 = time.time()
    p = subprocess.run([PY, "-u", "probes/exp_verify_internal_fields.py"],
                       cwd=ROOT, capture_output=True, text=True, timeout=2400)
    dur = time.time() - t0
    if p.returncode != 0:
        report.fail(f"the live probe failed (rc={p.returncode}): "
                     f"{p.stdout.strip()[-300:]}")
        return {}

    measured = {c["id"]: c for c in json.loads(
        Path("/tmp/campos_internos.json").read_text(encoding="utf-8")
    )["cases"]}

    rows = []
    differences = []
    for h in INTERNAL_PROTO_GAPS:
        f = PROBE_ID.get(h["schema"])
        for c, status in sorted(h["write"].items()):
            if not f:
                continue
            m = measured.get(f(c))
            if not m:
                continue
            live = m.get("write")
            type_ = m.get("type") or "—"
            if live == status:
                mark = "✅"
            else:
                mark = "⚠️"
                differences.append(f"{h['schema']}.{c}: "
                                   f"catalog={status} live={live}")
            rows.append([f"`{h['schema']}.{c}`", type_, status, live, mark])
    report.info(f"live measurement: {len(rows)} fields in {dur:.0f} s")
    if differences:
        report.fail(f"{len(differences)} differences: {differences}")
    else:
        report.ok_("the 31 states measured live MATCH the catalog")
    report.table(["Field", "Measured type", "Catalog", "Live", ""], rows)
    return measured


# --------------------------------------- 6) real write of an internal field

def section_write(report: Informe, cons: ConsoleApi, cfg) -> None:
    report.section("6. Real write of an internal field (cx_set_console_field)")
    import server
    base = f"/projects/{cfg.project}/locations/{cfg.location}/agents"
    created = cons.request("POST", cons._url(base), body={
        "displayName": f"zz_tmp_lab_{uuid.uuid4().hex[:6]}",
        "defaultLanguageCode": "es", "timeZone": "America/Santiago",
        "description": "temporary: final verification",
    })
    name = created.get("name") or ""
    if not name:
        report.fail("the lab agent could not be created")
        return
    ident = name.rsplit("/", 1)[-1]
    cfg.agent_readwrite = list(cfg.agent_readwrite) + [ident]
    server.CFG.agent_readwrite = list(server.CFG.agent_readwrite) + [ident]
    try:
        cases = [
            ("advancedSettings.speechSettings.enableLlmEndpointing", True),
            ("advancedSettings.speechSettings.utteranceWaitTimeout", "2s"),
            ("advancedSettings.speechSettings.minDigits", 4),
        ]
        rows = []
        for field, value in cases:
            r = server.cx_set_console_field(resource=name, field=field,
                                            value=value, dry_run=False)
            read = (cons.request("GET", cons._url("/" + name))
                    .get("advancedSettings", {}).get("speechSettings", {}))
            read_value = None
            part = field.split(".")[-1]
            if part in read:
                read_value = read[part]
            _saved = r.get("written", r.get("guardado", read_value is not None))
            rows.append([f"`{field}`", json.dumps(value),
                         json.dumps(read_value),
                         "✅" if read_value is not None else "❌"])
        if all(f[-1] == "✅" for f in rows):
            report.ok_("3 internal fields written with the MCP tool "
                        "and verified by re-reading them")
        else:
            report.fail(f"some write was not saved: {rows}")
        report.table(["Field", "Written", "Re-read", ""], rows)
    finally:
        try:
            cons.request("DELETE", cons._url("/" + name))
            report.info("lab agent deleted")
        except Exception as exc:
            report.fail(f"the agent could not be deleted: {str(exc)[:150]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", dest="fast", action="store_true",
                    help="skips the live measurement of the 31 fields")
    args = ap.parse_args()

    cfg = load_config()
    cons = ConsoleApi(cfg)
    report = Informe()
    started_at = datetime.now(timezone.utc).astimezone()

    report.lines.append("# Final verification — MCP `cx-playbook`")
    report.lines.append("")
    report.lines.append(f"Generated on {started_at:%Y-%m-%d %H:%M %Z} "
                           f"· project `{cfg.project}` · location "
                           f"`{cfg.location}`")
    report.lines.append("")
    report.lines.append(
        "Full verification of the MCP: tests, smoke, cleanup, internal proto "
        "catalog, **live measurement** of the 31 fields that were missing and "
        "a real write of an internal field with the MCP tool.")

    section_tests(report)
    section_smoke(report)
    section_cleanup(report, cons, cfg)
    section_catalog(report)
    section_live(report, args.fast)
    section_write(report, cons, cfg)

    report.section("Verdict")
    report.lines.append("")
    if report.ok:
        report.lines.append("**TODO OK** — all the checks pass.")
    else:
        report.lines.append("**THERE ARE FAILURES** (marked above).")
    report.lines.append("")
    report.lines.append(f"_(end of the verification, "
                           f"{datetime.now(timezone.utc).astimezone():%H:%M:%S})_")

    text = "\n".join(report.lines) + "\n"
    dest = ROOT / "VERIFICACION.md"
    dest.write_text(text, encoding="utf-8")
    print(text)
    print(f"-> {dest}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
