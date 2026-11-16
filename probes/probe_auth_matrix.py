#!/usr/bin/env python3
"""Authentication matrix against the console's INTERNAL backend
(dialogflow.clients6.google.com) until one variant returns 200.

Only READ requests (listing the clone flows).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import agente

from cxp.config import load_config
from cxp.httpclient import Session

# The console API key is read from the environment (`CXP_API_KEY`); it is never hardwired.
API_KEY = os.environ.get("CXP_API_KEY", "")
ORIGIN = "https://conversational-agents.cloud.google.com"
CLON = agente("por_defecto")
def main() -> int:
    cfg = load_config()
    state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    ck = {c["name"]: c["value"] for c in state["cookies"]}
    sapisid = ck.get("SAPISID", "")
    ts = int(time.time())

    def h(val: str) -> str:
        return hashlib.sha1(f"{ts} {val} {ORIGIN}".encode()).hexdigest()

    one = f"{ts}_{h(sapisid)}"
    triple = (
        f"SAPISIDHASH {one} SAPISID1PHASH {one} SAPISID3PHASH {one}"
    )
    all_ck = "; ".join(f"{k}={v}" for k, v in ck.items() if v)
    core = "; ".join(
        f"{k}={ck[k]}"
        for k in ("__Secure-3PSID", "SAPISID", "__Secure-1PSID", "APISID", "SID", "HSID", "SSID", "NID")
        if ck.get(k)
    )

    base_headers = {
        "Accept": "application/json",
        "Referer": f"{ORIGIN}/",
        "Origin": ORIGIN,
        "x-goog-user-project": cfg.quota_project,
    }
    url = (
        f"https://dialogflow.clients6.google.com/v3alpha1/projects/{cfg.project}"
        f"/locations/global/agents/{CLON}/flows?key={API_KEY}"
    )

    variantes = {
        "1 cookies-todas + SAPISIDHASH simple": {
            **base_headers,
            "Authorization": f"SAPISIDHASH {one}",
            "Cookie": all_ck,
        },
        "2 cookies-todas + triple hash": {
            **base_headers,
            "Authorization": triple,
            "Cookie": all_ck,
        },
        "3 cookies-nucleo + triple hash": {
            **base_headers,
            "Authorization": triple,
            "Cookie": core,
        },
        "4 solo cookies (sin Authorization)": {
            **base_headers,
            "Cookie": all_ck,
        },
        "5 solo cookies + X-Goog-AuthUser": {
            **base_headers,
            "Cookie": all_ck,
            "X-Goog-AuthUser": "0",
        },
        "6 triple hash + cookies + X-Goog-AuthUser + api-client": {
            **base_headers,
            "Authorization": triple,
            "Cookie": all_ck,
            "X-Goog-AuthUser": "0",
            "x-goog-api-client": "gl-node/20 js-node/20",
        },
    }

    http = Session(timeout=60.0, maxseg=cfg.tcp_maxseg, retries=1)
    for nombre, hdr in variantes.items():
        try:
            status, text = http.request("GET", url, hdr)
            snippet = ""
            if status == 200:
                try:
                    d = json.loads(text)
                    snippet = f"flows={len(d.get('flows', []))}"
                except Exception:
                    snippet = text[:80]
            else:
                snippet = " ".join(text.split())[:160]
            print(f"== {nombre}: HTTP {status}  {snippet}", flush=True)
        except Exception as exc:
            print(f"== {nombre}: {type(exc).__name__}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
