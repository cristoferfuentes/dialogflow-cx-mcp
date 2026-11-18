#!/usr/bin/env python3
"""Replay of the endpoints the console calls that are NOT in the
v3beta1 discovery doc (measured with the traffic captured live).

It uses the FRESH cookies from the real Chrome (the ones in the `.env` expire):
that is why the browser is used and not the credentials file.

    .venv/bin/python probes/probe_console_endpoints.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import probe_targets

from cxp.chrome_cdp import RealChrome
from cxp.config import load_config
from cxp.console_api import CONSOLE_HOST
from cxp.httpclient import Session


def browser_cookies(chrome) -> dict[str, str]:
    ctx = chrome.page.context
    cookies = ctx.cookies()
    return {c["name"]: c["value"] for c in cookies
            if c.get("value") and c.get("domain", "").lstrip(".").endswith("google.com")}


def main() -> int:
    cfg = load_config()
    targets = probe_targets()
    target = next(iter(targets.values()), {"agent": cfg.only_agent()})
    agent = target["agent"]
    project = cfg.project

    chrome = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir,
                         port=cfg.cdp_port)
    chrome.launch("about:blank")
    chrome.connect()
    time.sleep(2)
    cookies = browser_cookies(chrome)
    print(f"google.com cookies in the browser: {len(cookies)}")
    sapisid = cookies.get("SAPISID") or cfg.sapisid
    cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
    print("SAPISID found:", bool(sapisid))

    ses = Session(timeout=cfg.socket_timeout, maxseg=cfg.tcp_maxseg,
                  retries=cfg.http_retries, connect_timeout=cfg.connect_timeout)

    import hashlib

    def headers_for(origin: str) -> dict[str, str]:
        ts = int(time.time())
        h = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
        return {
            "accept": "*/*",
            "authorization": (f"SAPISIDHASH {ts}_{h} SAPISID1PHASH {ts}_{h} "
                              f"SAPISID3PHASH {ts}_{h}"),
            "cookie": cookie,
            "referer": f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1",
            "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"),
            "x-goog-authuser": "0",
            "x-origin": cfg.console_origin,
            "x-referer": cfg.console_origin,
        }

    cases = [
        ("v3beta1", f"v3beta1/projects/{project}/locations/global/agents/{agent}",
         "CONTROL: the MCP does it"),
        ("v3alpha1", f"v3alpha1/projects/{project}/locations/global/agents/{agent}",
         "the console uses v3alpha1"),
        ("v3alpha1", f"v3alpha1/projects/{project}/locations/global/systemEntityTypes:list",
         "systemEntityTypes:list"),
        ("v3alpha1", f"v3alpha1/projects/{project}/locations/global/agents/{agent}/currentUsers",
         "currentUsers"),
        ("v3alpha1", f"v3alpha1/projects/{project}/locations/global/locationSettings",
         "locationSettings"),
        ("v3alpha1", "v3alpha1/users/me/userSettings/recentProjects",
         "recentProjects (user level)"),
        ("v3alpha1", f"v3alpha1/projects/{project}/locations/global/agents/{agent}:listAvailableModels",
         "listAvailableModels"),
        ("v3alpha1", f"v3alpha1/projects/{project}/locations/global/agents/{agent}:getSupportedLanguages",
         "getSupportedLanguages"),
        ("v2beta1", f"v2beta1/projects/{project}/agent/visibility",
         "agent/visibility (v2beta1)"),
        ("v2beta1", f"v2beta1/projects/{project}/locations/global/conversationProfiles",
         "conversationProfiles (v2beta1)"),
        ("v3", f"v3/projects/{project}/locations/global/locationSettings",
         "locationSettings (v3)"),
        ("v3", f"v3/projects/{project}/locations/global/agents/{agent}",
         "control in v3"),
    ]

    print()
    for version, path, note in cases:
        url = f"https://{CONSOLE_HOST}/{path}?key={cfg.api_key}"
        for label, origin in (("signature=console", cfg.console_origin),
                             ("signature=api", f"https://{CONSOLE_HOST}")):
            try:
                data = ses.json("GET", url, headers_for(origin))
                txt = json.dumps(data, ensure_ascii=False)
                print(f"OK   [{version:<8}] {note:<34} ({label})")
                print(f"     -> {txt[:260]}")
                break
            except Exception as exc:
                msg = str(exc)[:110].replace("\n", " ")
                if label == "signature=api":
                    print(f"ERR  [{version:<8}] {note:<34} -> {msg}")
    try:
        chrome.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
