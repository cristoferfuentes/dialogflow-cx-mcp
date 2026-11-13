"""Tests de la escritura generica (`cxp/console_fields.py`) y de los helpers.

No necesitan red: `set_field` se prueba con un doble de `ConsoleApi`.
"""
from __future__ import annotations

import json
from typing import Any

from cxp.console_fields import diff_json, set_field
from cxp.gaps import read_path


class ApiFalso:
    """Doble de `ConsoleApi`: guarda el recurso en memoria y simula el PATCH.

    Reproduce el comportamiento real medido del backend:
      * el PATCH con `updateMask` solo toca la clave de primer nivel;
      * un PATCH con un `etag` obsoleto falla con 409;
      * si el mensaje escrito tiene subcampos que el servidor no conoce,
        los vacia.
    """

    def __init__(self, recurso: dict[str, Any], campos_que_el_servidor_ignora: set[str] | None = None):
        self.recurso = recurso
        self.escrituras: list[tuple[str, dict, str | None]] = []
        self.ignorados = campos_que_el_servidor_ignora or set()
        self.fallar_con_etag = True

    # --- lo que usa `set_field` ---
    def _url(self, path: str, **params):
        return path

    def request(self, method: str, url: str, body: dict | None = None, **kw):
        if method == "GET":
            return json.loads(json.dumps(self.recurso))
        raise AssertionError(f"metodo inesperado: {method}")

    def patch_resource(self, name, body, update_mask=None, **kw):
        if self.fallar_con_etag and "etag" in body:
            raise RuntimeError("HTTP 409: FAILED_PRECONDITION (etag obsoleto)")
        self.escrituras.append((name, body, update_mask))
        mascara = (update_mask or "").split(",")
        for clave in mascara:
            if clave in body:
                self.recurso[clave] = body[clave]
            else:
                self.recurso.pop(clave, None)
        # el servidor vacia los subcampos que no conoce
        self.recurso = self._vaciar_desconocidos(self.recurso)
        return json.loads(json.dumps(self.recurso))

    def _vaciar_desconocidos(self, nodo: Any) -> Any:
        if isinstance(nodo, dict):
            return {k: ({} if k in self.ignorados else self._vaciar_desconocidos(v))
                    for k, v in nodo.items()}
        if isinstance(nodo, list):
            return [self._vaciar_desconocidos(v) for v in nodo]
        return nodo


# ----------------------------------------------------------------- diff_json
def test_diff_json_detecta_cambios():
    cambios = diff_json({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert cambios == [{"ruta": "b", "antes": 2, "despues": 3}]


def test_diff_json_detecta_altas_y_bajas():
    cambios = diff_json({"a": 1}, {"b": 2})
    rutas = {c["ruta"] for c in cambios}
    assert rutas == {"a", "b"}


def test_diff_json_recorre_listas():
    cambios = diff_json({"r": [{"x": 1}]}, {"r": [{"x": 2}]})
    assert cambios[0]["ruta"] == "r[0].x"


def test_diff_json_sin_cambios():
    assert diff_json({"a": 1}, {"a": 1}) == []


# ------------------------------------------------------------------ set_field
def test_set_field_dry_run_no_escribe():
    api = ApiFalso({"name": "r", "transitionRoutes": [{"condition": "true"}]})
    res = set_field(api, "r", "transitionRoutes[0].targetPlaybook", "pb", dry_run=True)
    assert res.escrito is False
    assert api.escrituras == []
    assert res.mascara == ["transitionRoutes"]
    assert res.despues == "pb"


def test_set_field_escribe_y_verifica():
    api = ApiFalso({"name": "r", "transitionRoutes": [{"condition": "true"}]})
    res = set_field(api, "r", "transitionRoutes[0].targetPlaybook", "pb", dry_run=False)
    assert res.escrito is True
    assert res.verificado is True
    assert read_path(api.recurso, "transitionRoutes[0].targetPlaybook") == "pb"
    # la mascara es la clave de primer nivel
    assert api.escrituras[0][2] == "transitionRoutes"


def test_set_field_detecta_lo_que_el_servidor_descarta():
    api = ApiFalso({"name": "r", "advancedSettings": {"enabled": True}},
                   campos_que_el_servidor_ignora={"enabled"})
    res = set_field(api, "r", "advancedSettings", {"enabled": False}, dry_run=False)
    assert res.escrito is True
    assert res.verificado is False
    assert res.perdidas  # dice que se perdio


def test_set_field_no_manda_el_etag():
    api = ApiFalso({"name": "r", "etag": "viejo", "displayName": "d"})
    res = set_field(api, "r", "displayName", "nuevo", dry_run=False)
    assert "etag" not in api.escrituras[0][1]
    assert res.verificado is True


def test_set_field_con_etag_obsoleto_falla():
    api = ApiFalso({"name": "r", "etag": "viejo"})
    # el doble falla si llega el etag: como `set_field` lo quita, NO falla
    res = set_field(api, "r", "name", "otro", dry_run=False)
    assert res.error == ""
    assert res.verificado is True


def test_set_field_error_de_lectura():
    class Roto(ApiFalso):
        def request(self, method, url, body=None, **kw):
            raise RuntimeError("HTTP 404")

    res = set_field(Roto({}), "r", "x", 1, dry_run=False)
    assert res.error.startswith("no se pudo leer")
    assert res.escrito is False
