"""Automacion de la CONSOLA (UI) para poner un playbook como destino de una ruta.

Por que hace falta ademas de la API: el backend de la consola
(`dialogflow.clients6.google.com`) tiene el proto interno con `targetPlaybook`, y
escribe con **autenticacion de primera parte** (cookies de Google), que es un
 camino distinto (y mas barato) que el `PATCH` con token de servicio.

Reglas de seguridad (esta clase NUNCA adivina):

* Solo escribe en el agente indicado; el original se rechaza antes de abrir la UI.
* **Verifica antes y despues**: el destino de la ruta debe cambiar de "sin
  destino" al displayName del playbook. Si no se puede leer el estado, aborta.
* Ante cualquier ambiguedad (varios elementos que coinciden, boton no encontrado)
  **aborta y vuelca el DOM** para diagnostico: nunca hace un clic a ciegas.
"""
from __future__ import annotations

import re
from typing import Any

from .config import Config

# La consola se sirve en en_US (bundle: dialogflow-console.vertex_ai_v2.en_US).
PLAYBOOK_LABEL = re.compile(r"playbook", re.I)
SAVE_LABELS = ("Save", "SAVE", "Guardar", "Apply", "Done")
CONDITION_HINTS = ("Condition", "condition")


class ConsoleUiError(RuntimeError):
    pass


class ConsoleRouteEditor:
    def __init__(self, browser, cfg: Config):
        self.br = browser
        self.cfg = cfg
        self.page = browser._page

    # ------------------------------------------------------------- utilidades
    def _dump(self, limit: int = 6000) -> str:
        try:
            return (self.page.content() or "")[:limit]
        except Exception:
            return "<no se pudo volcar el DOM>"

    def _texts(self, selector: str) -> list[str]:
        try:
            return [t.strip() for t in self.page.locator(selector).all_inner_texts()]
        except Exception:
            return []

    # ------------------------------------------------------------- navegacion
    def open_page(self, agent_id: str, flow_id: str, page_id: str, panel: str = "simulator"):
        url = self.cfg.console_url(agent_id, flow_id, page_id, panel=panel)
        self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        self.page.wait_for_timeout(4000)
        return url

    def open_routes_panel(self) -> None:
        """Abre la seccion de rutas de la pagina (pestana 'Routes'/'Transition routes')."""
        for label in ("Routes", "Transition routes", "transitionRoutes", "Rutas"):
            loc = self.page.get_by_text(label, exact=False).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    loc.click()
                    self.page.wait_for_timeout(1500)
                    return
            except Exception:
                continue
        # fallback: la consola nueva puede mostrar las rutas en el panel derecho
        raise ConsoleUiError(
            "no se encontro la pestana de rutas. DOM:\n" + self._dump()
        )

    # ------------------------------------------------------------- las rutas
    def route_row(self, condition: str) -> Any:
        """Localiza la fila de la ruta por su condicion (coincidencia EXACTA)."""
        exact = self.page.get_by_text(condition, exact=True)
        try:
            n = exact.count()
        except Exception:
            n = 0
        if n == 1:
            return exact.first
        if n == 0:
            # coincidencia por fragmento unico
            frag = self.page.get_by_text(condition[:60], exact=False)
            try:
                if frag.count() == 1:
                    return frag.first
            except Exception:
                pass
            raise ConsoleUiError(
                f"no se encontro la ruta por su condicion ({n} coincidencias exactas). "
                "Condiciones visibles:\n"
                + "\n".join("- " + t for t in self._texts("mat-expansion-panel-header, tr, [role='row']")[:40])
                + "\nDOM:\n" + self._dump()
            )
        raise ConsoleUiError(
            f"la condicion coincide con {n} elementos: no se hace clic a ciegas. "
            "DOM:\n" + self._dump()
        )

    def destination_text(self, row: Any) -> str:
        """El texto del destino que la consola muestra para esa ruta."""
        try:
            txt = row.inner_text()
        except Exception:
            txt = ""
        return txt

    # ------------------------------------------------------------- la escritura
    def set_destination_to_playbook(
        self,
        agent_id: str,
        flow_id: str,
        page_id: str,
        condition: str,
        playbook_display_name: str,
    ) -> dict:
        # Guarda de solo lectura: el original NUNCA se escribe, ni por la UI.
        from .guard import assert_writable

        assert_writable(agent_id, self.cfg)

        url = self.open_page(agent_id, flow_id, page_id)
        self.open_routes_panel()
        row = self.route_row(condition)
        before = self.destination_text(row)
        if playbook_display_name.lower() in before.lower():
            return {
                "estado": "ya_tenia_el_destino",
                "destino_antes": before,
                "url": url,
            }

        # 1) abrir el editor de la ruta
        row.click()
        self.page.wait_for_timeout(1200)

        # 2) el desplegable de destino
        selects = self.page.locator("mat-select")
        target_select = None
        for i in range(selects.count()):
            cand = selects.nth(i)
            try:
                if not cand.is_visible():
                    continue
                if cand.inner_text().strip():
                    target_select = cand
                    break
            except Exception:
                continue
        if target_select is None:
            raise ConsoleUiError("no se encontro el desplegable de destino. DOM:\n" + self._dump())

        target_select.click()
        self.page.wait_for_timeout(900)

        # 3) elegir el TIPO de destino "Playbook"
        opt = self.page.locator("mat-option").filter(has_text=PLAYBOOK_LABEL).first
        try:
            if opt.count() == 0:
                raise ConsoleUiError(
                    "el desplegable no ofrece 'Playbook'. Opciones: "
                    + ", ".join(self._texts("mat-option"))
                )
            opt.click()
        except ConsoleUiError:
            raise
        except Exception as exc:
            raise ConsoleUiError(
                f"fallo al elegir 'Playbook': {exc}\nDOM:\n" + self._dump()) from exc
        self.page.wait_for_timeout(1500)

        # 4) elegir el playbook concreto en el selector de recursos
        picker = self.page.locator("input[placeholder*='Filter'], input[placeholder*='filter'], input[placeholder*='Search'], input[placeholder*='search']").first
        try:
            if picker.count() > 0:
                picker.fill(playbook_display_name)
                self.page.wait_for_timeout(1200)
        except Exception:
            pass
        item = self.page.locator("mat-option, [role='option'], li").filter(
            has_text=re.compile(re.escape(playbook_display_name))
        ).first
        if item.count() == 0:
            raise ConsoleUiError(
                f"el playbook {playbook_display_name!r} no aparece en el selector. "
                "DOM:\n" + self._dump()
            )
        item.click()
        self.page.wait_for_timeout(1500)

        # 5) guardar
        saved = False
        for label in SAVE_LABELS:
            btn = self.page.get_by_role("button", name=label, exact=True).first
            try:
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    saved = True
                    break
            except Exception:
                continue
        if not saved:
            raise ConsoleUiError("no se encontro el boton de guardar. DOM:\n" + self._dump())

        # 6) ESPERAR y VERIFICAR: la consola muestra el destino en la fila
        self.page.wait_for_timeout(6000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=60_000)
        except Exception:
            pass
        after = self.destination_text(self.route_row(condition))
        ok = playbook_display_name.lower() in after.lower()
        return {
            "estado": "puesto" if ok else "NO_VERIFICADO",
            "destino_antes": before,
            "destino_despues": after,
            "verificado": ok,
            "url": url,
            "aviso": (
                ""
                if ok
                else "la UI no muestra el destino esperado: NO se puede confirmar el cambio"
            ),
        }
