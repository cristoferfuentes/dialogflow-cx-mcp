#!/usr/bin/env python3
"""Discovers the REAL name of a field that the public proto does NOT declare.

TRICK (measured on 2026-11-15): the server **silently ignores** the fields it
does not know, but **fails with the FIELD NAME** when the field exists and the
value has the wrong type:

    {"versionConfigs": "BASURA"}
      -> 400 "Invalid value at 'environment.version_configs' (TYPE_...)"

That type error reveals the EXACT path of the field (`environment.<field>` or
`environment.version_configs[0].<field>`). A deliberately impossible value is sent
("BASURA") and the path returned by the error is read.

With this, the fields that the public discovery doc does not declare can be
probed, without guessing the type and without creating anything.

    .venv/bin/python probes/exp_discovers_field.py [--field playbookConfigs]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi

CAMPOS = [
    "playbookConfigs", "playbooks", "playbookNames", "includedPlaybooks",
    "referencedPlaybooks", "playbookVersions", "playbooksToInclude",
    "playbookConfig", "selectedPlaybooks", "playbookIds", "additionalPlaybooks",
    "extraPlaybooks", "playbook", "playbookVersionConfigs", "includedPlaybook",
]


def _error_path(exc: Exception) -> str:
    """Extracts the field path from the proto error message."""
    txt = str(exc)
    for marker in ("Invalid value at '", "unknown field \""):
        if marker in txt:
            ini = txt.index(marker) + len(marker)
            fin = txt.find("'", ini)
            if fin > ini:
                return txt[ini:fin]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", dest="agent", default="")
    ap.add_argument("--project", dest="project", default="")
    ap.add_argument("--field", dest="field", default="")
    ap.add_argument("--where", dest="where", default="root",
                    choices=["root", "raiz", "versionConfigs"],
                    help="where the field is placed: at the root or inside versionConfigs")
    args = ap.parse_args()

    cfg = load_config()
    agent = args.agent or cfg.only_agent()
    project = args.project or cfg.project
    cons = ConsoleApi(cfg)
    base = f"/projects/{project}/locations/{cfg.location}/agents/{agent}"

    fields = [args.field] if args.field else CAMPOS
    for field in fields:
        body: dict = {"displayName": "zz_tmp_disco_campo"}
        if args.where in ("root", "raiz"):
            body[field] = "BASURA"
        else:
            body["versionConfigs"] = [{"version": "BASURA", field: "BASURA"}]
        try:
            cons.request("POST", cons._url(f"{base}/environments"), body=body)
            print(f"  {args.where}.{field:<26} -> no type error "
                  f"(field NOT declared or validated earlier)")
        except Exception as exc:
            route = _error_path(exc)
            if field in route or field.lower().replace("_", "") in route.lower().replace("_", ""):
                print(f"  {args.where}.{field:<26} -> *** DECLARED: {route} ***")
                print("     ", str(exc)[:260].replace("\n", " "))
            else:
                print(f"  {args.where}.{field:<26} -> path={route or '(no path)'} "
                      f"| {str(exc)[:90]}".replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
