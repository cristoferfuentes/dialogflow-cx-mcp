"""Control del Chrome REAL por MCP (acciones de bajo nivel sobre la consola).

El MCP no usa una API publica para esto: la consola "Conversational Agents"
llama a un backend interno (`dialogflow.clients6.google.com`) con la
autenticacion de primera parte (cookies de Google) y con un proto que SI
expone `targetPlaybook` en `TransitionRoute`. Por eso hay que pilotar el
navegador real.

Todas las acciones devuelven un dict con lo que ha pasado (no lanzan
excepciones al llamante) para que el agente pueda decidir el siguiente paso.
"""
from __future__ import annotations

import time
from typing import Any

from .browser import ConsoleBrowser


class BrowserControlError(RuntimeError):
    pass


def _page(br: ConsoleBrowser):
    if br._page is None:
        br._launch(headless=False)
    return br._page


# --------------------------------------------------------------------- acciones
def ctl_status(br: ConsoleBrowser) -> dict:
    page = _page(br)
    out: dict[str, Any] = {}
    try:
        out["url"] = page.url
        out["titulo"] = page.title()
        out["ua"] = page.evaluate("navigator.userAgent")
        out["secure_context"] = page.evaluate("window.isSecureContext")
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    cookies = br.cookies()
    names = sorted({c.get("name") for c in cookies})
    out["cookies_google"] = [n for n in names if "PSID" in n or n in ("SID", "SAPISID", "HSID", "SSID")]
    out["total_cookies"] = len(cookies)
    out["sesion_google"] = bool(set(out["cookies_google"]) & {"__Secure-3PSID", "SAPISID", "SID"})
    try:
        out["pestanas"] = [p.url for p in page.context.pages]
    except Exception:
        pass
    return out


def ctl_open(br: ConsoleBrowser, url: str, wait_ms: int = 3000) -> dict:
    page = _page(br)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    except Exception as exc:
        return {"ok": False, "error": f"goto: {type(exc).__name__}: {exc}", "url": page.url}
    if wait_ms:
        page.wait_for_timeout(wait_ms)
    return {"ok": True, "url": page.url, "titulo": page.title()}


def ctl_text(br: ConsoleBrowser, selector: str = "body", limit: int = 6000) -> dict:
    page = _page(br)
    try:
        if selector == "body":
            txt = page.inner_text("body")
        else:
            loc = page.locator(selector)
            txt = "\n".join(t for t in loc.all_inner_texts())
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "selector": selector, "texto": txt[:limit], "largo": len(txt)}


def ctl_click(br: ConsoleBrowser, selector: str = "", texto: str = "", nth: int = 0,
              timeout: float = 15.0) -> dict:
    """Pulsa un elemento por selector CSS o por texto visible."""
    page = _page(br)
    loc = None
    if texto:
        loc = page.get_by_text(texto, exact=False)
    elif selector:
        loc = page.locator(selector)
    else:
        return {"ok": False, "error": "indica selector= o texto="}
    try:
        n = loc.count()
    except Exception as exc:
        return {"ok": False, "error": f"count: {type(exc).__name__}: {exc}"}
    if n == 0:
        return {"ok": False, "error": "no se encontro el elemento", "coincidencias": 0}
    idx = min(max(nth, 0), n - 1)
    try:
        target = loc.nth(idx)
        target.scroll_into_view_if_needed(timeout=timeout * 1000)
        target.click(timeout=timeout * 1000)
    except Exception as exc:
        return {"ok": False, "error": f"click: {type(exc).__name__}: {exc}", "coincidencias": n}
    page.wait_for_timeout(800)
    return {"ok": True, "coincidencias": n, "pulsado_indice": idx, "url": page.url}


def ctl_fill(br: ConsoleBrowser, selector: str, valor: str, timeout: float = 15.0) -> dict:
    page = _page(br)
    try:
        loc = page.locator(selector).first
        loc.fill(valor, timeout=timeout * 1000)
    except Exception as exc:
        return {"ok": False, "error": f"fill: {type(exc).__name__}: {exc}"}
    return {"ok": True, "selector": selector}


def ctl_press(br: ConsoleBrowser, tecla: str = "Enter") -> dict:
    page = _page(br)
    try:
        page.keyboard.press(tecla)
    except Exception as exc:
        return {"ok": False, "error": f"press: {type(exc).__name__}: {exc}"}
    page.wait_for_timeout(500)
    return {"ok": True, "tecla": tecla}


def ctl_screenshot(br: ConsoleBrowser, ruta: str = "/tmp/cx-playbook-consola.png",
                   full_page: bool = True) -> dict:
    page = _page(br)
    try:
        page.screenshot(path=ruta, full_page=full_page)
    except Exception as exc:
        return {"ok": False, "error": f"screenshot: {type(exc).__name__}: {exc}"}
    return {"ok": True, "ruta": ruta, "url": page.url}


def ctl_eval(br: ConsoleBrowser, js: str) -> dict:
    page = _page(br)
    try:
        val = page.evaluate(js)
    except Exception as exc:
        return {"ok": False, "error": f"eval: {type(exc).__name__}: {exc}"}
    return {"ok": True, "resultado": val}


def ctl_requests(br: ConsoleBrowser) -> dict:
    """Devuelve las peticiones XHR capturadas (hay que llamar antes a start_recording)."""
    reqs = br.stop_recording()
    return {
        "ok": True,
        "n": len(reqs),
        "peticiones": [
            {
                "method": r.get("method"),
                "url": r.get("url"),
                "status": r.get("status"),
                "post_len": len(r.get("post_data") or ""),
                "post_data": (r.get("post_data") or "")[:4000],
                "headers": {k: v for k, v in (r.get("headers") or {}).items()
                            if k.lower() in ("content-type", "x-goog-user-project", "authorization",
                                             "x-goog-request-params", "referer", "origin")},
            }
            for r in reqs
        ],
    }


def ctl_wait(br: ConsoleBrowser, ms: int = 2000) -> dict:
    page = _page(br)
    page.wait_for_timeout(int(ms))
    return {"ok": True, "esperado_ms": ms, "url": page.url}


# --------------------------------------------------------------------- despacho
ACCIONES = {
    "status": ctl_status,
    "open": ctl_open,
    "text": ctl_text,
    "click": ctl_click,
    "fill": ctl_fill,
    "press": ctl_press,
    "screenshot": ctl_screenshot,
    "eval": ctl_eval,
    "requests": ctl_requests,
    "wait": ctl_wait,
}


def dispatch(br: ConsoleBrowser, accion: str, **kw) -> dict:
    fn = ACCIONES.get(accion)
    if fn is None:
        return {
            "ok": False,
            "error": f"accion desconocida: {accion}",
            "acciones": sorted(ACCIONES),
        }
    try:
        return fn(br, **kw)
    except TypeError as exc:
        return {"ok": False, "error": f"argumentos invalidos para '{accion}': {exc}",
                "acciones": sorted(ACCIONES)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------- recipes UI
def list_selectors(br: ConsoleBrowser, limite: int = 120) -> dict:
    """Lista los elementos interactivos de la pagina (para afinar selectores)."""
    page = _page(br)
    js = """
    () => {
      const out = [];
      const sel = 'button,a[role=button],[role=tab],[role=menuitem],[role=option],'
                + 'mat-option,[role=listbox] [role=option],input,textarea,'
                + '[ng-reflect-message],[class*=route],[class*=transition]';
      document.querySelectorAll(sel).forEach((el) => {
        const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
        if (!t || t.length > 120) return;
        out.push({tag: el.tagName.toLowerCase(), cls: (el.className || '').toString().slice(0, 80),
                  role: el.getAttribute('role') || '', texto: t.slice(0, 90),
                  visible: !!(el.offsetParent || el.getClientRects().length)});
      });
      return out.slice(0, __LIMITE__);
    }
    """.replace("__LIMITE__", str(limite))
    try:
        items = page.evaluate(js)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "n": len(items), "elementos": items}


def wait_for_text(br: ConsoleBrowser, texto: str, timeout_s: float = 30.0) -> dict:
    """Espera a que aparezca un texto en la pagina."""
    page = _page(br)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            loc = page.get_by_text(texto, exact=False)
            if loc.count() > 0:
                try:
                    vis = any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
                except Exception:
                    vis = True
                if vis:
                    return {"ok": True, "encontrado": texto, "url": page.url}
        except Exception:
            pass
        page.wait_for_timeout(700)
    return {"ok": False, "error": f"timeout esperando '{texto}'", "url": page.url}
