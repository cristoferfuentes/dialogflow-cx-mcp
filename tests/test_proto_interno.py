"""Tests del catalogo del proto INTERNO (`cxp/proto_interno.py`).

El catalogo es un dato MEDIDO (extraido del bundle de la consola y verificado
escribiendo), asi que estos tests comprueban que no se rompa y que los campos
que el discovery doc publico declara NO aparezcan como huecos.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cxp.gaps import SchemaIndex
from cxp.proto_interno import (
    HUECOS_PROTO_INTERNO,
    catalogo_proto_interno,
    resumen,
)

RAIZ = Path(__file__).resolve().parent.parent
DISCOVERY = RAIZ / ".discovery"


def test_el_catalogo_no_esta_vacio():
    assert len(HUECOS_PROTO_INTERNO) >= 10


def test_cada_entrada_tiene_lo_minimo():
    for h in HUECOS_PROTO_INTERNO:
        assert h["esquema"]
        assert h["campos"]
        assert all(isinstance(c, str) for c in h["campos"])


def test_los_estados_son_conocidos():
    validos = {"escribible", "ignorado", "descartado", "derivado",
               "solo_entrada", "no_aceptado", "no_probado"}
    for h in HUECOS_PROTO_INTERNO:
        for campo, estado in (h.get("escritura") or {}).items():
            assert estado in validos, (h["esquema"], campo, estado)
            assert campo in h["campos"], (h["esquema"], campo)


def test_catalogo_devuelve_copias():
    a = catalogo_proto_interno()
    a[0]["campos"] = []
    b = catalogo_proto_interno()
    assert b[0]["campos"]


def test_resumen_cuadra():
    r = resumen()
    total = sum(len(h["campos"]) for h in HUECOS_PROTO_INTERNO)
    assert r["campos"] == total
    assert r["esquemas"] == len(HUECOS_PROTO_INTERNO)


def test_no_queda_ningun_campo_sin_medir():
    """Los 38 campos del proto interno tienen un estado MEDIDO."""
    r = resumen()
    assert r["sin_medir"] == [], r["sin_medir"]
    assert r["medidos"] == r["campos"]


def test_los_estados_nuevos_son_validos():
    from cxp.proto_interno import ESTADOS_ESCRITURA
    assert {"escribible", "ignorado", "descartado", "derivado",
            "solo_entrada", "no_aceptado"} <= ESTADOS_ESCRITURA


@pytest.mark.skipif(not (DISCOVERY / "v3beta1.json").exists(),
                    reason="no hay discovery doc cacheado")
def test_ningun_campo_del_catalogo_esta_declarado_en_el_doc_publico():
    """Si un campo aparece aqui, el descriptor publico NO lo declara."""
    idx = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    for h in HUECOS_PROTO_INTERNO:
        corto = h["esquema"]
        declarados = idx.declared(corto)
        if not declarados:
            # El nombre puede no existir como esquema suelto: se busca por
            # sufijo en todos los del doc.
            for nombre, props in idx.props.items():
                if nombre.endswith(corto):
                    declarados = set(props)
                    break
        assert declarados, f"no se encuentra el esquema {corto}"
        for campo in h["campos"]:
            assert campo not in declarados, (corto, campo)
