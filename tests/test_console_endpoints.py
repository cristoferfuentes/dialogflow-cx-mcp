"""Tests del catalogo de huecos de METODO (`cxp/console_endpoints.py`).

El catalogo es un dato MEDIDO: estos tests comprueban que no se rompa y que
los numeros que afirma sigan cuadrando con el descriptor real.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cxp.console_endpoints import (
    HUECOS_METODO,
    METODOS_POR_VERSION,
    _ids_de,
    catalogo_metodos,
    huecos_de_metodo,
)
from cxp.gaps import SchemaIndex

RAIZ = Path(__file__).resolve().parent.parent
DISCOVERY = RAIZ / ".discovery"


# --------------------------------------------------------------- el catalogo
def test_el_catalogo_no_esta_vacio():
    assert len(HUECOS_METODO) >= 8


def test_cada_endpoint_tiene_lo_minimo():
    for h in HUECOS_METODO:
        assert h["id"]
        assert h["version"] in METODOS_POR_VERSION or h["version"] == "v2beta1"
        assert h["metodo"] in ("GET", "POST", "PATCH", "DELETE")
        # Las rutas cuelgan de un proyecto, de la raiz (`users/`) o son la
        # coleccion de proyectos de la RAIZ del backend (`projects`).
        assert (h["path"].startswith("projects/") or h["path"].startswith("users/")
                or h["path"] == "projects")
        assert h["estado"] in ("verificado", "sin_datos", "error")
        assert h["para_que"]


def test_projects_list_es_un_endpoint_de_la_raiz():
    """MEDIDO (2026-11-15): el listado de proyectos vive en la raiz del
    backend (`/v3alpha1/projects`), no bajo un proyecto: por eso el MCP lo
    resuelve como ruta ABSOLUTA."""
    p = next(h for h in HUECOS_METODO if h["id"] == "projects.list")
    assert p["path"] == "projects"
    assert p["version"] == "v3alpha1"


def test_los_ids_son_unicos():
    ids = [h["id"] for h in HUECOS_METODO]
    assert len(ids) == len(set(ids))


def test_catalogo_metodos_devuelve_copias():
    a = catalogo_metodos()
    a[0]["id"] = "tocada"
    b = catalogo_metodos()
    assert b[0]["id"] != "tocada"


def test_las_rutas_absolutas_no_cuelgan_de_un_proyecto():
    # `users/me/...` es una ruta desde la raiz del backend: por eso el MCP
    # necesita resolver rutas absolutas.
    recientes = next(h for h in HUECOS_METODO if h["id"] == "users.recentProjects")
    assert recientes["path"].startswith("users/")


# ------------------------------------------------------- contra el descriptor
@pytest.mark.skipif(not (DISCOVERY / "v3beta1.json").exists(),
                    reason="no hay discovery doc cacheado")
def test_los_numeros_de_metodos_cuadran():
    idx = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    assert len(_ids_de(idx)) == METODOS_POR_VERSION["v3beta1"] == 162


@pytest.mark.skipif(not (DISCOVERY / "v3alpha1.json").exists(),
                    reason="no hay discovery doc cacheado")
def test_v3beta1_es_el_superconjunto():
    betas = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    alpha = SchemaIndex.load(DISCOVERY / "v3alpha1.json", version="v3alpha1")
    ids_betas = _ids_de(betas)
    ids_alpha = _ids_de(alpha)
    assert len(ids_alpha) == METODOS_POR_VERSION["v3alpha1"] == 8
    # Ningun metodo de v3alpha1 falta en v3beta1.
    assert not (ids_alpha - ids_betas)


@pytest.mark.skipif(not (DISCOVERY / "v3.json").exists(),
                    reason="no hay discovery doc cacheado")
def test_v3_tiene_cuatro_metodos_menos():
    betas = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    v3 = SchemaIndex.load(DISCOVERY / "v3.json", version="v3")
    assert len(_ids_de(v3)) == METODOS_POR_VERSION["v3"] == 158
    assert len(_ids_de(betas) - _ids_de(v3)) == 4


@pytest.mark.skipif(not (DISCOVERY / "v3beta1.json").exists(),
                    reason="no hay discovery doc cacheado")
def test_huecos_de_metodo_entre_versions():
    betas = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    v3 = SchemaIndex.load(DISCOVERY / "v3.json", version="v3")
    # Los ids se sacan del MISMO descriptor (el de la version pedida).
    res = huecos_de_metodo(betas, "v3beta1", "v3beta1")
    assert res["solo_en_version"] == []
    assert res["solo_en_contra"] == []
    # El doc de v3 tiene 4 metodos menos: se ve al comparar docs.
    assert len(_ids_de(betas) - _ids_de(v3)) == 4
