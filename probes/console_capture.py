"""Espera la sesion de Google y captura la consola en la pagina de la ruta.

Salida (en `console_capture/`):
  * `dom.html`      -> DOM de la pagina de la ruta (para ajustar selectores)
  * `text.txt`      -> texto visible
  * `requests.json` -> las peticiones XHR que hace la consola (con cabeceras)
  * `summary.json`  -> resumen legible

Uso:
    .venv/bin/python console_capture.py [--wait-minutes 30] [--agent clon]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.auth import pick_sapisid
from cxp.browser import ConsoleBrowser
from cxp.config import load_config

from _entorno import targets_de_sonda

OUT = Path(__file__).resolve().parent / "console_capture"

# Paginas donde hay rutas que sabemos que existen.
TARGETS = targets_de_sonda()


def wait_for_session(cfg, minutes: int) -> bool:
    """Espera a que haya sesion de Google.

    NO abre el navegador: el loginHelper tiene el perfil persistente abierto
    (lock) y abrirlo aqui lo romperia. Se mira el `storage_state` que escribe
    el helper al detectar la sesion y, como respaldo, el SQLite de cookies del
    perfil (copiado, no abierto).
    """
    import shutil
    import sqlite3

    deadline = time.time() + minutes * 60
    state = cfg.state_file
    while time.time() < deadline:
        if state.exists():
            try:
                data = json.loads(state.read_text(encoding="utf-8"))
                names = {c.get("name") for c in data.get("cookies", [])}
                if pick_sapisid(data.get("cookies", [])):
                    print(f"== SESION DETECTADA en {state}. cookies={sorted(names & {'SID','SAPISID','__Secure-3PSID','__Secure-1PSID','HSID','SSID'})}", flush=True)
                    return True
            except Exception:
                pass
        # Respaldo: el SQLite de cookies del perfil (sin abrir el navegador).
        try:
            src = cfg.real_profile_dir / "Default" / "Cookies"
            tmp = Path("/tmp/_ck_probe.db")
            shutil.copy(src, tmp)
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            rows = list(con.execute(
                "select name, encrypted_value from cookies where host_key like '%google%'"
            ))
            con.close()
            if any(n in ("__Secure-3PSID", "SAPISID", "__Secure-1PSID") for n, _ in rows):
                print(f"== SESION DETECTADA en el perfil real: {[n for n, _ in rows]}", flush=True)
                return True
        except Exception:
            pass
        time.sleep(10)
    print("== se acabo la espera sin sesion", flush=True)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-minutes", type=int, default=30)
    ap.add_argument("--agent", choices=list(TARGETS), default="clon")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--settle", type=int, default=9000, help="ms de espera tras cargar")
    args = ap.parse_args()

    cfg = load_config()
    OUT.mkdir(exist_ok=True)
    if not args.no_wait:
        if not wait_for_session(cfg, args.wait_minutes):
            return 1

    tgt = TARGETS[args.agent]
    url = cfg.console_url(tgt["agent"], tgt["flow"], tgt["page"])
    print(f"== URL: {url}", flush=True)

    # Se REUTILIZA el Chrome real que esta abierto (con la sesion del usuario)
    # conectandose por CDP: no se relanza y no se pierde la sesion.
    from cxp.chrome_cdp import RealChrome

    ch = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir, port=cfg.cdp_port)
    br = ConsoleBrowser(cfg)
    br.use_proxy = False
    try:
        page = ch.connect()
        print("== conectado por CDP al Chrome real", flush=True)
    except Exception as exc:
        print(f"== no se pudo conectar al Chrome real: {exc}", flush=True)
        print("== se lanza uno nuevo con el perfil (con sesion si esta guardada)", flush=True)
        ch.launch(url)
        page = ch.connect()
    br._page = page
    br._ctx = page.context
    br._browser = ch._browser
    br.start_recording()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    except Exception as exc:
        print(f"== la navegacion a la consola fallo: {type(exc).__name__}: {str(exc)[:90]}", flush=True)
    time.sleep(args.settle / 1000)
    # Dejar que la consola resuelva sus pestañas
    for label in ("Routes", "Transition routes", "Rutas", "Routes"):
        try:
            loc = br._page.get_by_text(label, exact=False).first
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                print(f"== pestana '{label}' abierta", flush=True)
                time.sleep(3)
                break
        except Exception:
            continue

    dom = br._page.content()
    try:
        text = br._page.inner_text("body")
    except Exception:
        text = "<sin texto>"
    reqs = br.stop_recording()

    (OUT / f"dom_{args.agent}.html").write_text(dom, encoding="utf-8")
    (OUT / f"text_{args.agent}.txt").write_text(text, encoding="utf-8")
    (OUT / f"requests_{args.agent}.json").write_text(json.dumps(reqs, indent=1), encoding="utf-8")

    summary = {
        "url": br._page.url,
        "titulo": br._page.title(),
        "dom_bytes": len(dom),
        "texto": text[:4000],
        "peticiones": [
            {"method": r["method"], "url": r["url"], "status": r.get("status"),
             "post_len": len(r.get("post_data") or "")}
            for r in reqs
        ],
    }
    (OUT / f"summary_{args.agent}.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print("== captura guardada en", OUT, flush=True)
    print("== titulo:", summary["titulo"], flush=True)
    for p in summary["peticiones"]:
        print(f"   {p['status']} {p['method']} {p['url'][:130]}", flush=True)
    br.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
