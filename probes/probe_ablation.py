#!/usr/bin/env python3
"""ABLASION: que cabeceras son realmente necesarias para que el backend
interno acepte la llamada de primera parte (HTTP puro, sin navegador).

Reproduce una peticion que SI funciono (200 en el navegador) quitando grupos
de cabeceras y viendo en cuales cae a 401.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.config import load_config

from _entorno import agente, targets_de_sonda
from cxp.console_api import CONSOLE_HOST, sapisid_authorization

ORIG = agente("por_defecto")
_TARGET = next(iter(targets_de_sonda().values()), {"flow": "", "page": ""})
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
    Path("/tmp/abl.txt").write_text("\n".join(f"{k}: {v}" for k, v in headers.items()),
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
        print("== no se capturaron cabeceras")
        return 1
    base = {k: v for k, v in wire[-1].items() if not k.startswith(":")}
    print(f"== cabeceras base: {len(base)}\n")

    # ts fresco en cada intento (el hash depende del ts)
    def fresh(h: dict[str, str]) -> dict[str, str]:
        h = dict(h)
        h["authorization"] = sapisid_authorization(cfg.console_origin, live.get("SAPISID"), int(time.time()))
        return h

    code, body = curl(fresh(base), url)
    print(f"A) COMPLETAS                       -> HTTP {code}")

    for nombre, claves in GROUPS.items():
        h = {k: v for k, v in base.items() if k not in claves}
        code, body = curl(fresh(h), url)
        print(f"B) sin {nombre:28s} -> HTTP {code}")

    # minimo: solo lo imprescindible
    minimo = {k: base[k] for k in (
        "accept", "authorization", "cookie", "referer", "user-agent",
        "x-goog-authuser", "x-origin", "x-referer", "x-browser-validation",
    ) if k in base}
    code, body = curl(fresh(minimo), url)
    print(f"C) MINIMO (9 cabeceras)             -> HTTP {code}")

    # minimo + http1.1
    Path("/tmp/abl.txt").write_text("\n".join(f"{k}: {v}" for k, v in fresh(minimo).items()),
                                    encoding="utf-8")
    r = subprocess.run(["curl", "-sS", "--compressed", "--http1.1", "-o", "/tmp/abl_out.json",
                        "-w", "%{http_code}", "-H", "@/tmp/abl.txt", url],
                       capture_output=True, text=True, timeout=90)
    print(f"D) MINIMO con HTTP/1.1             -> HTTP {r.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
