#!/usr/bin/env python3
"""Final test: PURE HTTP call (python, no curl and no browser) to the
console's internal backend with first party authentication.

Key measured fact: you have to send ONLY the cookies of the `.google.com` domain
(the ones the browser sends). Sending the ones of `accounts.google.com` or other
domains breaks the credential -> HTTP 401.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import agent, probe_targets

from cxp.config import load_config
from cxp.console_api import CONSOLE_HOST
from cxp.httpclient import Session

ORIG = agent("default")
_TARGET = next(iter(probe_targets().values()), {"flow": "", "page": ""})
OF = _TARGET["flow"]
OP = _TARGET["page"]


def cookies_for_google(state_file: Path) -> tuple[str, str]:
    """(sapisid, cookie header) with ONLY the `.google.com` cookies."""
    state = json.loads(state_file.read_text(encoding="utf-8"))
    keep = []
    for c in state.get("cookies", []):
        dom = (c.get("domain") or "").lstrip(".")
        if dom == "google.com" or dom.endswith(".google.com"):
            keep.append((c.get("name"), c.get("value")))
    by_name = {n: v for n, v in keep if v}
    sapisid = by_name.get("SAPISID") or by_name.get("__Secure-3PSID")
    header = "; ".join(f"{n}={v}" for n, v in keep if v)
    return sapisid or "", header


def auth_header(origin: str, sapisid: str, ts: int) -> str:
    h = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h} SAPISID1PHASH {ts}_{h} SAPISID3PHASH {ts}_{h}"


def main() -> int:
    cfg = load_config()
    sapisid, cookie = cookies_for_google(cfg.state_file)
    print(f"== .google.com cookies: {len(cookie.split('; '))}  sapisid={'yes' if sapisid else 'NO'}")

    headers = {
        "accept": "*/*",
        "authorization": auth_header(cfg.console_origin, sapisid, int(time.time())),
        "cookie": cookie,
        "referer": f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1",
        "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/157.0.0.0 Safari/537.36"),
        "x-goog-authuser": "0",
        "x-origin": cfg.console_origin,
        "x-referer": cfg.console_origin,
    }
    url = (f"https://{CONSOLE_HOST}/v3alpha1/projects/{cfg.project}/locations/global/agents/{ORIG}"
           f"/flows/{OF}/pages/{OP}?" + urlencode({"key": cfg.api_key, "languageCode": "es"}))

    http = Session(timeout=60.0, maxseg=cfg.tcp_maxseg, retries=1)
    t = time.time()
    status, body = http.request("GET", url, headers)
    print(f"== HTTP {status} in {time.time()-t:.1f}s")
    if status == 200:
        data = json.loads(body)
        print("   etag:", data.get("etag"))
        print("   keys:", sorted(data.keys()))
        for r in data.get("transitionRoutes") or []:
            print(f"   - {r.get('name','').split('/')[-1]}  targetPlaybook="
                  f"{str(r.get('targetPlaybook')).split('/')[-1]}")
    else:
        print("   ", " ".join(body.split())[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
