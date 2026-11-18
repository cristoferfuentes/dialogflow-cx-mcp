#!/usr/bin/env python3
"""ABLATION: which headers are really needed for the internal backend
to accept the first party call (pure HTTP, no browser).

It replays a request that DID work (200 in the browser) removing groups of
headers and seeing in which ones it falls to 401.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import agent, probe_targets
from _redact import redact, write_private

from cxp.config import load_config
from cxp.console_api import CONSOLE_HOST, sapisid_authorization

ORIG = agent("default")
_TARGET = next(iter(probe_targets().values()), {"flow": "", "page": ""})
OF = _TARGET["flow"]
OP = _TARGET["page"]

GROUPS = {
    "x-browser-validation": ["x-browser-validation"],
    "x-browser-*": ["x-browser-channel", "x-browser-copyright", "x-browser-year",
                     "x-browser-validation"],
    "sec-fetch-*": ["sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"],
    "sec-ch-ua*": ["sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"],
    "x-javascript-user-agent": ["x-javascript-user-agent"],
    "x-requested-with": ["x-requested-with"],
    "accept-language": ["accept-language"],
    "x-goog-encode-*": ["x-goog-encode-response-if-executable"],
}


def curl(headers: dict[str, str], url: str) -> tuple[int, str]:
    write_private("/tmp/abl.txt", "\n".join(f"{k}: {v}" for k, v in redact(headers).items()),
                                    encoding="utf-8")
    r = subprocess.run(
        ["curl", "-sS", "--compressed", "--http2", "-o", "/tmp/abl_out.json",
         "-w", "%{http_code}", "-H", "@/tmp/abl.txt", url],
        capture_output=True, text=True, timeout=90)
    code = r.stdout.strip()
    body = ""
    try:
        body = Path("/tmp/abl_out.json").read_text(encoding="utf-8")
    except Exception:
        pass
    return (int(code) if code.isdigit() else 0), body


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
        if "authorization" in h:
            wire.append(dict(h))

    cdp.on("Network.requestWillBeSentExtraInfo", on_extra)
    page.goto(f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1",
              wait_until="domcontentloaded", timeout=60_000)
    time.sleep(3)

    live = {c["name"]: c["value"] for c in page.context.cookies() if c.get("value")}
    ts = int(time.time())
    url = (f"https://{CONSOLE_HOST}/v3alpha1/projects/{cfg.project}/locations/global/agents/{ORIG}"
           f"/flows/{OF}/pages/{OP}?key={cfg.api_key}&languageCode=es")
    page.evaluate(
        """async ({url, headers}) => { await fetch(url,{credentials:'include',headers}); }""",
        {"url": url, "headers": {
            "Authorization": sapisid_authorization(cfg.console_origin, live.get("SAPISID"), ts),
            "X-Origin": cfg.console_origin, "X-Referer": cfg.console_origin,
            "X-Goog-AuthUser": "0", "X-Goog-Encode-Response-If-Executable": "base64",
            "X-JavaScript-User-Agent": "google-api-javascript-client/1.1.0",
            "X-Requested-With": "XMLHttpRequest"}},
    )
    time.sleep(3)
    if not wire:
        print("== no headers were captured")
        return 1
    base = {k: v for k, v in wire[-1].items() if not k.startswith(":")}
    print(f"== base headers: {len(base)}\n")

    # fresh ts on every attempt (the hash depends on the ts)
    def fresh(h: dict[str, str]) -> dict[str, str]:
        h = dict(h)
        h["authorization"] = sapisid_authorization(cfg.console_origin, live.get("SAPISID"), int(time.time()))
        return h

    code, body = curl(fresh(base), url)
    print(f"A) COMPLETE                       -> HTTP {code}")

    for name, keys in GROUPS.items():
        h = {k: v for k, v in base.items() if k not in keys}
        code, body = curl(fresh(h), url)
        print(f"B) without {name:28s} -> HTTP {code}")

    # minimum: only the essentials
    minimum = {k: base[k] for k in (
        "accept", "authorization", "cookie", "referer", "user-agent",
        "x-goog-authuser", "x-origin", "x-referer", "x-browser-validation",
    ) if k in base}
    code, body = curl(fresh(minimum), url)
    print(f"C) MINIMUM (9 headers)             -> HTTP {code}")

    # minimum + http1.1
    write_private("/tmp/abl.txt", "\n".join(f"{k}: {v}" for k, v in redact(fresh(minimum)).items()),
                                    encoding="utf-8")
    r = subprocess.run(["curl", "-sS", "--compressed", "--http1.1", "-o", "/tmp/abl_out.json",
                        "-w", "%{http_code}", "-H", "@/tmp/abl.txt", url],
                       capture_output=True, text=True, timeout=90)
    print(f"D) MINIMUM with HTTP/1.1             -> HTTP {r.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
