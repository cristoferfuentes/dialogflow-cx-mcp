#!/usr/bin/env python3
"""Conclusive experiment: repeat with curl EXACTLY the wire headers
that Chrome put on a request that already worked (200).

Usage: .venv/bin/python probe_wire_replay.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import agente, targets_de_sonda

from cxp.config import load_config
from cxp.console_api import CONSOLE_HOST, sapisid_authorization

ORIG = agente("por_defecto")
_TARGET = next(iter(targets_de_sonda().values()), {"flow": "", "page": ""})
OF = _TARGET["flow"]
OP = _TARGET["page"]


def main() -> int:
    cfg = load_config()
    from cxp.chrome_cdp import RealChrome

    ch = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir, port=cfg.cdp_port)
    page = ch.connect()
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")

    wire: list[dict] = []

    def on_extra(ev):
        h = ev.get("headers") or {}
        if "/v3alpha1/" in (h.get(":path", "") or "") or "authorization" in h:
            wire.append(dict(h))

    cdp.on("Network.requestWillBeSentExtraInfo", on_extra)

    page.goto(f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1",
              wait_until="domcontentloaded", timeout=60_000)
    time.sleep(3)

    live = {c["name"]: c["value"] for c in page.context.cookies() if c.get("value")}
    ts = int(time.time())
    auth = sapisid_authorization(cfg.console_origin, live.get("SAPISID") or live.get("__Secure-3PSID"), ts)
    url = (f"https://{CONSOLE_HOST}/v3alpha1/projects/{cfg.project}/locations/global/agents/{ORIG}"
           f"/flows/{OF}/pages/{OP}?key={cfg.api_key}&languageCode=es")
    status = page.evaluate(
        """async ({url, headers}) => { const r = await fetch(url,{credentials:'include',headers});
            return r.status; }""",
        {"url": url, "headers": {
            "Authorization": auth, "X-Origin": cfg.console_origin, "X-Referer": cfg.console_origin,
            "X-Goog-AuthUser": "0", "X-Goog-Encode-Response-If-Executable": "base64",
            "X-JavaScript-User-Agent": "google-api-javascript-client/1.1.0",
            "X-Requested-With": "XMLHttpRequest"}},
    )
    time.sleep(3)
    print(f"== browser fetch (via proxy.html): HTTP {status}")

    if not wire:
        print("== no wire headers were captured")
        return 1
    w = wire[-1]
    print("\n== wire headers of that request:")
    for k, v in sorted(w.items()):
        print(f"   {k}: {str(v)[:130]}")

    # They are replayed as they are (only the Authorization is recomputed with a fresh ts).
    hdrs = {k: v for k, v in w.items() if not k.startswith(":") and k != "cookie"}
    hdrs["cookie"] = w.get("cookie", "")
    Path("/tmp/wire_exact.txt").write_text(
        "\n".join(f"{k}: {v}" for k, v in hdrs.items()), encoding="utf-8")

    for label, extra in (("http2", ["--http2"]), ("http1.1", ["--http1.1"])):
        r = subprocess.run(
            ["curl", "-sS", "-o", "/tmp/wire_out.json", "-w", "%{http_code}",
             *extra, "-H", "@/tmp/wire_exact.txt", url],
            capture_output=True, text=True, timeout=90)
        print(f"\n== EXACT replay with curl {label}: HTTP {r.stdout.strip()} {r.stderr.strip()[:120]}")
        if r.stdout.strip() == "200":
            d = json.loads(Path("/tmp/wire_out.json").read_text(encoding="utf-8"))
            print("   etag:", d.get("etag"))
            for rr in d.get("transitionRoutes") or []:
                print(f"   - {rr.get('name','').split('/')[-1]} -> "
                      f"{str(rr.get('targetPlaybook')).split('/')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
