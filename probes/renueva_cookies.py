#!/usr/bin/env python3
"""Renews the `.env` cookies from the real Chrome (which already has a session).

The Google session cookies **expire**: when that happens, the MCP answers 401
("invalid authentication credentials") even though the 5 cookies are in the `.env`.
The console keeps working because the browser has the live session.

This script:
  1. opens the real Chrome with the profile that already has the session,
  2. reads the `.google.com` cookies via CDP,
  3. updates `CXP_COOKIE` (and `CXP_SAPISID`) of the `.env`.

It does NOT touch anything of the agent: only the credentials.

    .venv/bin/python probes/renueva_cookies.py [--env .env] [--check]
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _redact import write_private

from cxp.chrome_cdp import RealChrome
from cxp.config import PKG_DIR, load_config

NECESARIAS = ("SAPISID", "SID", "HSID", "SSID", "APISID")


def google_cookies(page) -> dict[str, str]:
    """The `.google.com` cookies of the browser."""
    out: dict[str, str] = {}
    for c in page.context.cookies():
        dom = (c.get("domain") or "").lstrip(".")
        if c.get("value") and (dom == "google.com" or dom.endswith(".google.com")):
            out[c["name"]] = c["value"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="")
    ap.add_argument("--check", action="store_true",
                    help="only checks whether the browser session works")
    args = ap.parse_args()

    cfg = load_config()
    env_path = Path(args.env) if args.env else (cfg.env_file or PKG_DIR / ".env")

    chrome = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir,
                         port=cfg.cdp_port)
    chrome.launch("about:blank")
    chrome.connect()
    time.sleep(2)
    try:
        cookies = google_cookies(chrome.page)
    finally:
        try:
            chrome.close()
        except Exception:
            pass

    missing = [n for n in NECESARIAS if not cookies.get(n)]
    print(f"google.com cookies: {len(cookies)} | missing the required ones: {missing or 'none'}")
    if args.check:
        return 1 if missing else 0
    if missing:
        print("the browser has no session: open the console with your account and repeat")
        return 1

    # The WHOLE jar is written, not just the 5: with 5 cookies the backend
    # answers 401 as soon as the session is renewed (measured 2026-11-13).
    cookie = "; ".join(f"{n}={cookies[n]}" for n in sorted(cookies))
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    new_lines = []
    for line in text.splitlines():
        if line.startswith("CXP_COOKIE="):
            new_lines.append(f'CXP_COOKIE="{cookie}"')
        elif line.startswith("CXP_SAPISID="):
            new_lines.append(f"CXP_SAPISID={cookies['SAPISID']}")
        else:
            new_lines.append(line)
    if not any(line.startswith("CXP_COOKIE=") for line in new_lines):
        new_lines.append(f'CXP_COOKIE="{cookie}"')
    # The .env carries the session cookies: only its owner can read it.
    write_private(env_path, "\n".join(new_lines) + "\n")
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    print(f"cookies updated in {env_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
