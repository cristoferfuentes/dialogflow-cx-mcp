"""Chrome REAL (no "Chrome for Testing" de Playwright, ni headless) controlado por CDP.

Por que hace falta:
* Playwright lanza `HeadlessChrome/156` -> Google RECHAZA el login de cuentas
  ("No puedes acceder: es posible que no sean seguros este navegador o la app"
  -> /v3/signin/rejected).
* El proxy MITM termina el TLS con Python: el JA3/JA4 del cliente es el de
  OpenSSL, no el de Chrome -> Google tambien lo puede marcar como raro.
* Un Chrome de escritorio, con ventana y TLS propio, es lo que mas se parece a
  un navegador real.

El control es por **CDP** (`--remote-debugging-port`), que es lo que permite
al MCP pilotarlo igual que lo pilota Playwright, pero sobre el navegador real.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

# Binarios de Chrome, en orden de preferencia.
CHROME_CANDIDATES = [
    # Google Chrome oficial primero (Chrome for Testing NO: Google avisa de
    # que es para testing y puede tratarlo distinto en el login).
    "/opt/google/chrome/chrome",
    "/opt/chrome-cft/chrome-linux64/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    found = shutil.which("google-chrome") or shutil.which("chromium")
    return found


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class RealChrome:
    """Chrome de escritorio con puerto CDP, en una pantalla virtual."""

    def __init__(
        self,
        display: str = ":99",
        profile_dir: str | Path = "/tmp/cx-playbook-chrome-profile",
        port: int = 9222,
        window_size: str = "1280,740",
        extra_args: list[str] | None = None,
        headless: bool = False,
    ):
        self.display = display
        self.profile_dir = Path(profile_dir)
        self.port = port
        self.window_size = window_size
        self.extra_args = extra_args or []
        self.headless = headless
        self.proc: subprocess.Popen | None = None
        self.binary: str | None = None
        self._pw = None
        self._browser = None
        self._ctx = None
        self.page = None

    # ------------------------------------------------------------- ciclo
    def launch(self, start_url: str = "about:blank") -> str:
        binary = find_chrome()
        if not binary:
            raise RuntimeError(
                "no hay Chrome real instalado (busque en "
                + ", ".join(CHROME_CANDIDATES) + ")"
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "DISPLAY": self.display}
        base = [
            binary,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--lang=es-419",
            "--accept-lang=es-419,es;q=0.9",
            f"--window-size={self.window_size}",
            "--window-position=0,0",
            # Sin --headless: el UA debe ser el de un Chrome de escritorio.
            "--disable-features=Translate,TranslateUI,OptimizationHints",
            # --test-type: Chrome no muestra el aviso de "unsupported
            # command-line flag" (queda la pantalla limpia para el login).
            "--test-type",
            *self.extra_args,
            start_url,
        ]
        if self.headless:
            base.insert(1, "--headless=new")
        # Primero se intenta CON sandbox (Chrome se queja del flag --no-sandbox).
        for extra in ([], ["--no-sandbox", "--disable-dev-shm-usage"]):
            args = base[:1] + extra + base[1:]
            self.proc = subprocess.Popen(
                args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.time() + 40
            while time.time() < deadline:
                if port_open(self.port):
                    self.binary = binary
                    return binary
                if self.proc.poll() is not None:
                    break
                time.sleep(0.4)
            # No abrio el puerto: se reintenta con --no-sandbox.
            try:
                self.proc.terminate()
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("Chrome no abrio el puerto CDP")

    def connect(self) -> Any:
        """Se conecta por CDP y devuelve el contexto con la primera pestana."""
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.port}", timeout=60_000
        )
        ctxs = self._browser.contexts
        if not ctxs:
            raise RuntimeError("Chrome no tiene contextos")
        self._ctx = ctxs[0]
        pages = self._ctx.pages
        self.page = pages[0] if pages else self._ctx.new_page()
        self.page.set_default_timeout(45_000)
        return self.page

    def ensure(self, start_url: str = "about:blank") -> Any:
        if self.page is None:
            self.launch(start_url)
            self.connect()
        return self.page

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._browser = self._pw = None
        self.page = None
        # El proceso se queda (es el navegador del usuario); se cierra aparte.
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------- info
    def info(self) -> dict:
        """UA, version y estado del CDP (util para diagnostico)."""
        out: dict = {"binario": find_chrome(), "puerto": self.port, "pid": self.proc.pid if self.proc else None}
        try:
            req = subprocess.run(
                ["curl", "-s", f"http://127.0.0.1:{self.port}/json/version"],
                capture_output=True, text=True, timeout=10,
            )
            out["cdp"] = json.loads(req.stdout)
        except Exception as exc:
            out["cdp_error"] = str(exc)
        if self.page is not None:
            try:
                out["ua"] = self.page.evaluate("navigator.userAgent")
                out["secure"] = self.page.evaluate("window.isSecureContext")
            except Exception as exc:
                out["page_error"] = str(exc)
        return out
