#!/usr/bin/env python3
"""Details the temporary resources left behind in the agent and cleans them if possible.

    .venv/bin/python probes/limpia_temporales.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    cons = ConsoleApi(cfg)
    base = (f"/projects/{cfg.project}/locations/{cfg.location}"
            f"/agents/{cfg.default_agent}")

    print("== PLAYBOOK versions ==")
    pbs = (cons.request("GET", cons._url(f"{base}/playbooks", pageSize="100"))
           .get("playbooks") or [])
    for p in pbs:
        vs = (cons.request("GET", cons._url(f"/{p['name']}/versions", pageSize="10"))
              .get("playbookVersions") or [])
        for v in vs:
            desc = str(v.get("description") or "")
            mio = desc.startswith("temporal")
            print(f"  {p.get('displayName'):<40} {v['name'].rsplit('/', 1)[-1]:<4}"
                  f" desc={desc[:40]!r} {'<-- MINE' if mio else ''}")
            if mio and not args.dry_run:
                try:
                    cons.request("DELETE", cons._url("/" + v["name"]))
                    print("      deleted")
                except Exception as exc:
                    print("      Could NOT delete:", str(exc)[:120])

    print("== FLOW versions (they cannot be deleted: it is checked) ==")
    flows = (cons.request("GET", cons._url(f"{base}/flows", pageSize="100"))
             .get("flows") or [])
    for f in flows:
        vs = (cons.request("GET", cons._url(
            f"{base}/flows/{f['name'].rsplit('/', 1)[-1]}/versions", pageSize="50"))
            .get("versions") or [])
        mio = [v for v in vs if str(v.get("description") or "").startswith("temporal")
               or str(v.get("displayName") or "").startswith("zz_tmp")]
        if mio:
            print(f"  {f.get('displayName'):<32} {len(vs)} versions, "
                  f"{len(mio)} temporary: "
                  f"{[v.get('displayName') for v in mio]}")
            if not args.dry_run:
                for v in mio:
                    try:
                        cons.request("DELETE", cons._url("/" + v["name"]))
                        print("      deleted")
                    except Exception as exc:
                        print(f"      {v.get('displayName')}: it cannot be deleted "
                              f"({str(exc)[:110]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
