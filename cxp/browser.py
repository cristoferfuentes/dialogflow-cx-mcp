"""Navegador: perfil persistente + primer arranque con ventana (login) + headless.

Mecanismo de arranque automatico:

* La PRIMERA vez que se invoca el MCP, si no hay sesion de Google valida, se abre
  un Chromium **con ventana** (headed) para que el usuario inicie sesion en su
  cuenta. El perfil (cookies) queda persistido en `profile_dir`.
* A partir de ahi, todas las invocaciones corren **headless** con ese perfil: no
  aparece ninguna ventana y la latencia es la de una llamada HTTP.

Ademas captura el trafico del navegador, para poder aprender (o replicar) la
peticion exacta que hace la consola.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from .auth import first_party_headers, pick_google_cookies, pick_sapisid
from .config import Config
from .mitm import MitmProxy

# Hosts que el proxy local tiene que saber responder.
MITM_HOSTS = [
    "conversational-agents.cloud.google.com",
    "accounts.google.com",
    "google.com",
    "*.google.com",
    "*.googleapis.com",
    "*.gstatic.com",
    "*.clients6.google.com",
]


# Hosts a los que se puede inyectar `Authorization: Bearer <token ADC>`
# cuando el navegador NO tiene sesion de Google (primera parte / UDS).
BEARER_HOSTS = {
    "dialogflow.clients6.google.com",
    "conversational-agents.cloud.google.com",
    "global-dialogflow.googleapis.com",
}


@dataclass
class SessionState:
    logged_in: bool
    user_email: str | None = None
    reason: str = ""
    cookies: list[dict] | None = None


class ConsoleBrowser:
    """Perfil persistente de Chromium para la consola Conversational Agents."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self._ctx = None
        self._page = None
        self._proxy: MitmProxy | None = None
        self.captured: list[dict] = []
        self._record = False
        self._token: str | None = None
        self._token_ts: float = 0.0
        # Flags extra de Chromium (p.ej. "--disable-quic"). Se anaden a los
        # argumentos de arranque; util para diagnostico de red.
        self.extra_args: list[str] = []
        # Si es False, el navegador NO usa el proxy MITM local (TLS nativo).
        self.use_proxy = True
        # Inyectar `Authorization: Bearer <token ADC>` en las peticiones del
        # navegador. OJO: por defecto DESACTIVADO: al añadir una cabecera
        # `Authorization` a un XHR cross-origin, el navegador dispara un
        # preflight CORS y el proxy firma también el OPTIONS -> el navegador
        # rechaza la respuesta y la consola se queda en blanco (medido).
        # Para hablar con el backend sin sesion de Google, usar las llamadas
        # desde Python (cxp/cxapi.py), que no tienen CORS.
        self.inject_bearer = False
        # Usar el `storage_state` (cookies) en vez del perfil persistente.
        # Evita el lock del perfil si el login sigue abierto en otro navegador.
        self.use_state_file = False
        # Usar el CHROME REAL (de escritorio) controlado por CDP en vez del
        # Chromium de Playwright. Google rechaza el login en navegadores
        # headless y el proxy MITM cambia el JA3 del TLS: con Chrome real
        # (TLS nativo, con ventana) tanto el login como la consola funcionan.
        self.use_real_chrome = True
        self._real = None
        self._browser = None
        # User-Agent. Google RECHAZA el login de cuentas en navegadores
        # headless ("No puedes acceder: es posible que no sean seguros este
        # navegador o la app" -> /v3/signin/rejected) porque su UA dice
        # "HeadlessChrome". Se presenta el UA de un Chrome de escritorio.
        # (La sesion es la del propio usuario, en su propia maquina.)
        self.user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/156.0.0.0 Safari/537.36"
        )

    # ------------------------------------------------------------------ ciclo
    def _bearer_token(self) -> str | None:
        """Token OAuth de ADC (gcloud), cacheado ~50 min (caduca en 1 h)."""
        if self._token and (time.time() - self._token_ts) < 3000:
            return self._token
        from .auth import gcloud_access_token

        tok = gcloud_access_token()
        if tok:
            self._token, self._token_ts = tok, time.time()
        return tok

    def _window_size(self) -> str:
        """Tamano de la ventana = el de la pantalla virtual (Xvfb :99 = 1280x800).

        Si la ventana es mas grande que la pantalla, el gestor de ventanas la
        deja fuera de vista y el usuario no ve nada por noVNC.
        """
        try:
            import subprocess

            out = subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "DISPLAY": self.cfg.display},
            )
            w, h = (int(x) for x in out.stdout.split()[:2])
            return f"{w},{max(600, h - 60)}"
        except Exception:
            return "1280,760"

    # ------------------------------------------------------------------ ciclo
    def _launch_real_chrome(self, headless: bool):
        """Usa el Chrome REAL (por CDP) en vez del Chromium de Playwright.

        Google rechaza el login desde navegadores headless
        (`/v3/signin/rejected`) y el proxy MITM cambia el JA3 del TLS. Con un
        Chrome de escritorio (el de Google, instalado en `/opt/google/chrome`)
        y TLS nativo, la sesion y la consola funcionan de verdad.
        """
        from .chrome_cdp import RealChrome

        if self._real is None:
            self._real = RealChrome(
                display=self.cfg.display,
                profile_dir=self.cfg.real_profile_dir,
                port=self.cfg.cdp_port,
                headless=headless,
            )
        try:
            page = self._real.connect()
        except Exception:
            # No habia un Chrome escuchando: se lanza (con el perfil del paquete).
            self._real.launch("about:blank")
            page = self._real.connect()
        self._ctx = page.context
        self._browser = self._real._browser
        self._ctx.set_default_timeout(45_000)
        self._ctx.on("request", self._on_request)
        self._ctx.on("response", self._on_response)
        self._page = page
        return page

    def _launch(self, headless: bool):
        from playwright.sync_api import sync_playwright

        # La pantalla virtual debe existir ANTES de arrancar el driver de
        # Playwright: el navegador es hijo del driver y hereda su entorno.
        if self.cfg.display:
            os.environ["DISPLAY"] = self.cfg.display

        # --- Chrome REAL (por CDP) ------------------------------------------
        if self.use_real_chrome:
            return self._launch_real_chrome(headless)

        if self._pw is None:
            self._pw = sync_playwright().start()
        # El navegador no puede hacer TLS saliente en esta maquina: se pone el
        # proxy MITM local (ver cxp/mitm.py) para que el TLS con Google lo haga
        # Python, que si funciona.
        if self.use_proxy and self._proxy is None:
            self._proxy = MitmProxy(
                self.cfg.profile_dir.parent / ".proxy-cert",
                MITM_HOSTS,
                quota_project=self.cfg.quota_project,
                bearer_token=self._bearer_token() if self.inject_bearer else None,
                bearer_hosts=BEARER_HOSTS,
            ).start()
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            f"--window-size={self._window_size()}",
            "--window-position=0,0",
            "--ignore-certificate-errors",
            *self.extra_args,
        ]
        if self.use_proxy and self._proxy is not None:
            args.append(f"--proxy-server={self._proxy.url}")
        kwargs = dict(
            user_data_dir=str(self.cfg.profile_dir),
            headless=headless,
            args=args,
            locale="es-419",
            timezone_id="America/Santiago",
            user_agent=self.user_agent,
            env=dict(os.environ),
        )
        if headless:
            # En headless no hay ventana: el viewport define el tamano.
            kwargs["viewport"] = {"width": 1280, "height": 900}
        else:
            # Con ventana real, el viewport fijo pelea con --window-size y deja
            # la ventana mas grande que la pantalla virtual (no se ve por noVNC).
            kwargs["no_viewport"] = True
        state = self.cfg.state_file
        use_state = bool(state.exists() and self.use_state_file)
        try:
            # `channel="chromium"` usa el build completo de Chromium en modo
            # headless nuevo: mucho mas estable con las sesiones de Google que
            # el `chromium_headless_shell`.
            if use_state:
                # Contexto TEMPORAL con las cookies del `storage_state`: no
                # toca el perfil persistente, asi que no choca con el lock si
                # el login sigue abierto en otro navegador.
                browser = self._pw.chromium.launch(channel="chromium", headless=headless, args=args, env=dict(os.environ))
                self._ctx = browser.new_context(
                    storage_state=str(state),
                    locale="es-419",
                    timezone_id="America/Santiago",
                    user_agent=self.user_agent,
                    **({"viewport": {"width": 1280, "height": 900}} if headless else {"no_viewport": True}),
                )
                self._browser = browser
            else:
                self._ctx = self._pw.chromium.launch_persistent_context(channel="chromium", **kwargs)
        except Exception:
            if use_state:
                browser = self._pw.chromium.launch(headless=headless, args=args, env=dict(os.environ))
                self._ctx = browser.new_context(
                    storage_state=str(state),
                    locale="es-419",
                    timezone_id="America/Santiago",
                    user_agent=self.user_agent,
                    **({"viewport": {"width": 1280, "height": 900}} if headless else {"no_viewport": True}),
                )
                self._browser = browser
            else:
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self._ctx.set_default_timeout(30_000)
        self._ctx.on("request", self._on_request)
        self._ctx.on("response", self._on_response)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self._page

    def close(self):
        """Cierra lo que el MCP ha abierto.

        Importante: en modo Chrome REAL el navegador es del USUARIO (es su
        sesion de Google). NO se cierra: solo se suelta la conexion CDP, para
        que el login siga vivo y el MCP pueda reconectarse.
        """
        try:
            if self._ctx and not self.use_real_chrome:
                self._ctx.close()
        except Exception:
            pass
        finally:
            self._ctx = None
            self._page = None
            if self._browser is not None and not self.use_real_chrome:
                try:
                    self._browser.close()
                except Exception:
                    pass
            self._browser = None
            if self._proxy:
                self._proxy.stop()
                self._proxy = None
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            if self.use_real_chrome:
                # El Chrome real sigue abierto: solo se suelta el driver.
                self._real = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- captura
    def _on_request(self, request):
        if not self._record:
            return
        url = request.url
        if "clients6.google.com" not in url and "dialogflow" not in url:
            return
        try:
            headers = request.headers
        except Exception:
            headers = {}
        self.captured.append(
            {
                "method": request.method,
                "url": url,
                "headers": {k: v for k, v in headers.items() if k.lower() not in ("cookie",)},
                "post_data": (request.post_data or "")[:20000],
                "ts": time.time(),
            }
        )

    def _on_response(self, response):
        if not self._record:
            return
        try:
            if "clients6.google.com" not in response.url:
                return
            body = response.text()[:4000]
        except Exception:
            return
        for entry in reversed(self.captured):
            if entry["url"] == response.url and entry["method"] == response.request.method:
                entry["status"] = response.status
                entry["response_preview"] = body
                break

    def start_recording(self):
        self.captured = []
        self._record = True

    def stop_recording(self) -> list[dict]:
        self._record = False
        return self.captured

    # ---------------------------------------------------------------- sesion
    def _looks_logged_in(self) -> tuple[bool, str, str | None]:
        """La señal real de sesion son las cookies de Google (no la URL)."""
        try:
            url = self._page.url
        except Exception:
            return False, "sin pagina", None
        cookies = self.cookies()
        names = {c.get("name") for c in cookies}
        sapisid = pick_sapisid(cookies)
        if not sapisid and "SID" not in names:
            return False, "sin cookies de sesion de Google (SID/__Secure-3PSID)", None
        if "accounts.google.com" in url:
            return False, "redirigido a accounts.google.com", None
        try:
            title = (self._page.title() or "").lower()
        except Exception:
            title = ""
        if "sign in" in title or "iniciar sesión" in title:
            return False, "titulo de login", None
        email = self._page_email()
        return True, f"url={url} | cookies={sorted(names & {'SID', 'SAPISID', '__Secure-3PSID', 'HSID', 'SSID'})}", email

    def _page_email(self) -> str | None:
        for sel in ("#gAIzaun", "[aria-label*='Cuenta de Google']", "[aria-label*='Google Account']"):
            try:
                el = self._page.query_selector(sel)
                if el:
                    val = (el.get_attribute("aria-label") or "").strip()
                    if "@" in val:
                        return val.split("(")[0].strip()
            except Exception:
                continue
        try:
            for cookie in self._ctx.cookies():
                if cookie.get("name") == "ACCOUNT_CHOOSER" and cookie.get("value"):
                    return None
        except Exception:
            pass
        return None

    def open_console(self, url: str, wait_ms: int = 3500):
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self._page.wait_for_timeout(wait_ms)

    def ensure_session(self, console_url: str, headed_timeout: int = 600) -> SessionState:
        """Devuelve una sesion valida. Si no la hay, abre ventana para el login."""
        self._launch(headless=True)
        self.open_console(console_url)
        ok, reason, email = self._looks_logged_in()
        if ok:
            return SessionState(True, email, reason)

        # No hay sesion -> se abre la ventana para que el usuario entre a Google.
        self.close()
        self._launch(headless=False)
        self.open_console(console_url)
        deadline = time.time() + headed_timeout
        while time.time() < deadline:
            ok, reason, email = self._looks_logged_in()
            if ok:
                # Guarda las cookies de forma explicita.
                try:
                    self._ctx.storage_state(path=str(self.cfg.state_file))
                except Exception:
                    pass
                return SessionState(True, email, f"login completado: {reason}")
            time.sleep(2)
        self.close()
        return SessionState(False, None, "timeout esperando el inicio de sesion")

    # ---------------------------------------------------------------- cookies
    def cookies(self) -> list[dict]:
        if not self._ctx:
            return []
        try:
            return self._ctx.cookies()
        except Exception:
            return []

    def auth_headers(self) -> dict[str, str]:
        """Cabeceras de primera parte (UDS) equivalentes a las de la consola."""
        cookies = self.cookies()
        sapisid = pick_sapisid(cookies)
        if not sapisid:
            raise RuntimeError("sin cookie SAPISID: la sesion de Google no esta activa")
        headers = first_party_headers(self.cfg.console_origin, sapisid)
        extra = pick_google_cookies(cookies)
        if extra:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in extra.items())
        return headers

    def fetch_in_page(self, method: str, url: str, payload: Any = None) -> tuple[int, str]:
        """Hace la peticion DESDE la pagina: el navegador pone sus cookies y CORS."""
        body = None
        if payload is not None:
            body = json.dumps(payload)
        result = self._page.evaluate(
            """async ({method, url, body}) => {
                const opts = {method, credentials: 'include', headers: {}};
                if (body !== null) {
                    opts.body = body;
                    opts.headers['Content-Type'] = 'application/json';
                }
                const r = await fetch(url, opts);
                return {status: r.status, text: await r.text()};
            }""",
            {"method": method, "url": url, "body": body},
        )
        return result["status"], result["text"]
