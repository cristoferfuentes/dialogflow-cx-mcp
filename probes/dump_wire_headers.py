#!/usr/bin/env python3
"""Extracts the browser fingerprint (wire headers + cookies) and leaves it ready
for curl / httpx. It also tries the transport variants.

Usage: .venv/bin/python dump_wire_headers.py [--agent clon]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import agentes_de_sonda

from cxp.config import load_config
from cxp.console_api import CONSOLE_HOST, sapisid_authorization

AGENTS = agentes_de_sonda()

BROWSER_HEADERS = {
    "accept": "*/*",
    "accept-language": "es-419,es;q=0.9,es;q=0.9;q=0.8",
    "sec-ch-ua": '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "Linux",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/157.0.0.0 Safari/537.36"),
    "x-browser-channel": "stable",
    "x-browser-copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
    "x-browser-year": "2026",
    "x-goog-authuser": "0",
    "x-goog-encode-response-if-executable": "base64",
    "x-javascript-user-agent": "google-api-javascript-client/1.1.0",
    "x-origin": None,     # filled in
    "x-referer": None,
    "x-requested-with": "XMLHttpRequest",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="clon", choices=list(AGENTS))
    ap.add_argument("--out", default="/tmp/curl_headers.txt")
    args = ap.parse_args()

    cfg = load_config()
    from cxp.chrome_cdp import RealChrome

    ch = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir, port=cfg.cdp_port)
    page = ch.connect()
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    extra: dict[str, str] = {}

    def on_extra(ev):
        for k in ("x-browser-validation",):
            if k in (ev.get("headers") or {}):
                extra[k] = ev["headers"][k]

    cdp.on("Network.requestWillBeSentExtraInfo", on_extra)
    page.goto(f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1",
              wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)

    live = {c["name"]: c["value"] for c in page.context.cookies() if c.get("value")}
    sapisid = live.get("SAPISID") or live.get("__Secure-3PSID")
    ts = int(time.time())

    headers = dict(BROWSER_HEADERS)
    headers.update(extra)
    headers["x-origin"] = cfg.console_origin
    headers["x-referer"] = cfg.console_origin
    headers["authorization"] = sapisid_authorization(cfg.console_origin, sapisid, ts)
    headers["cookie"] = "; ".join(f"{k}={v}" for k, v in live.items())
    headers["referer"] = f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1"

    with open(args.out, "w", encoding="utf-8") as fh:
        for k, v in headers.items():
            fh.write(f"{k}: {v}\n")
    print(f"== headers in {args.out}")

    a, f, pg = AGENTS[args.agent]
    url = (f"https://{CONSOLE_HOST}/v3alpha1/projects/{cfg.project}/locations/global/agents/{a}"
           f"/flows/{f}/pages/{pg}?" + urlencode({"key": cfg.api_key, "languageCode": "es"}))

    for label, extra_args in (("curl --http2", ["--http2"]), ("curl http1.1", ["--http1.1"])):
        cmd = ["curl", "-sS", "-o", "/tmp/curl_out.json", "-w", "%{http_code} %{http_version}",
               *extra_args, "-H", f"@{args.out}", url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        print(f"== {label}: {r.stdout.strip()}  {r.stderr.strip()[:200]}")
        if r.stdout.startswith("200"):
            body = Path("/tmp/curl_out.json").read_text(encoding="utf-8")
            data = json.loads(body)
            print("   etag:", data.get("etag"))
            for r2 in data.get("transitionRoutes") or []:
                print(f"   - {r2.get('name','').split('/')[-1]} targetPlaybook="
                      f"{str(r2.get('targetPlaybook')).split('/')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
