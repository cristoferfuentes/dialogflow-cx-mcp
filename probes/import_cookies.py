"""Imports Google session cookies into the persistent browser profile.

Why it exists: on this machine the browser CANNOT complete the login flow of
`accounts.google.com` (measured: the handshake hangs), but the session cookies
are portable. The user can export the cookies from THEIR desktop Chrome
(where they do log into Google, with their VPN) and the MCP loads them here to
operate with first party (SAPISIDHASH).

Usage:
    .venv/bin/python import_cookies.py <cookies.json> [--url <check url>]

Accepted formats for <cookies.json>:
  1. List of Playwright-style objects:
     [{"name":"__Secure-3PSID","value":"...","domain":".google.com","path":"/"}, ...]
  2. The Playwright `storage_state` dump:
     {"cookies":[{"name":...,"value":...,"domain":...}], "origins":[...]}
  3. A simple object {"__Secure-3PSID": "...", "SAPISID": "..."}
     (domain is assumed to be ".google.com", path "/")
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.auth import pick_sapisid
from cxp.browser import ConsoleBrowser
from cxp.config import load_config

# Cookies needed for the Google first party (UDS).
NEEDED = ("__Secure-3PSID", "SAPISID", "__Secure-1PSID", "SID", "HSID", "SSID", "APISID")


def normalize(raw: object) -> list[dict]:
    """Converts any of the 3 supported formats to the Playwright list."""
    cookies: list[dict] = []
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]
    if isinstance(raw, dict):
        # {name: value}
        for name, value in raw.items():
            cookies.append({"name": name, "value": value, "domain": ".google.com", "path": "/"})
        return cookies
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".google.com"
        item = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", c.get("http_only", True))),
        }
        if c.get("expires") or c.get("expirationDate"):
            item["expires"] = float(c.get("expires") or c.get("expirationDate"))
        if c.get("sameSite"):
            ss = str(c["sameSite"]).lower()
            item["sameSite"] = {"strict": "Strict", "lax": "Lax", "none": "None"}.get(ss, "Lax")
        cookies.append(item)
    return cookies


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cookies", help="JSON file with the cookies")
    ap.add_argument("--url", default="https://conversational-agents.cloud.google.com/")
    ap.add_argument("--keep-open", action="store_true", help="leaves the browser open")
    args = ap.parse_args()

    raw = json.loads(Path(args.cookies).read_text(encoding="utf-8"))
    cookies = normalize(raw)
    found = {c["name"] for c in cookies} & set(NEEDED)
    print(f"== cookies read: {len(cookies)}; session ones: {sorted(found)}", flush=True)
    if not found:
        print("== WARNING: there is no Google session cookie in the file", flush=True)

    cfg = load_config()
    br = ConsoleBrowser(cfg)
    br._launch(headless=True)
    br._ctx.add_cookies(cookies)
    print(f"== {len(cookies)} cookies injected into the profile", flush=True)

    try:
        br.open_console(args.url, wait_ms=3000)
    except Exception as exc:
        print(f"== the navigation failed: {type(exc).__name__}: {str(exc)[:90]}", flush=True)

    ok, reason, email = br._looks_logged_in()
    print(f"== session: logged_in={ok}  ({reason})  email={email}", flush=True)

    sap = pick_sapisid(br.cookies())
    if sap:
        print("== SAPISID available: the first party (UDS) will work", flush=True)
        try:
            br._ctx.storage_state(path=str(cfg.state_file))
            print(f"== state saved in {cfg.state_file}", flush=True)
        except Exception as exc:
            print(f"== the state could not be saved: {exc}", flush=True)

    if args.keep_open:
        print("== browser open; press Ctrl+C to exit", flush=True)
        try:
            while True:
                br._page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            pass
    br.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
