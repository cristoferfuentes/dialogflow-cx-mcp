"""Tests del barrido de LISTADOS (`cxp/gaps.py`).

MEDIDO (2026-11-15): un listado devuelve un ENVOLTORIO
(`{"testCaseResults": [...]}`) cuyo esquema es distinto del del ITEM
(`TestCaseResult`). Barrer el envoltorio con el esquema del item (o al reves)
produce **falsos positivos**. Estos tests fijan ese comportamiento.
"""
from __future__ import annotations

import pytest

from cxp.gaps import SchemaIndex, scan_coleccion

DOC = {
    "schemas": {
        "GoogleCloudDialogflowCxV3beta1TestCase": {
            "properties": {"name": {"type": "string"},
                           "displayName": {"type": "string"}}
        },
        "GoogleCloudDialogflowCxV3beta1ListTestCasesResponse": {
            "properties": {
                "nextPageToken": {"type": "string"},
                "testCases": {"type": "array",
                              "items": {"$ref": "GoogleCloudDialogflowCxV3beta1TestCase"}},
            }
        },
        "GoogleCloudDialogflowCxV3beta1TestCaseResult": {
            "properties": {"name": {"type": "string"},
                           "testResult": {"type": "string"}}
        },
        "GoogleCloudDialogflowCxV3beta1ListTestCaseResultsResponse": {
            "properties": {
                "testCaseResults": {
                    "type": "array",
                    "items": {"$ref": "GoogleCloudDialogflowCxV3beta1TestCaseResult"}},
            }
        },
    },
    "resources": {
        "projects": {"resources": {"locations": {"resources": {"agents": {
            "resources": {
                "testCases": {
                    "methods": {
                        "get": {"response": {"$ref": "GoogleCloudDialogflowCxV3beta1TestCase"}},
                        "list": {"response": {"$ref": "GoogleCloudDialogflowCxV3beta1ListTestCasesResponse"}},
                    },
                    "resources": {
                        "results": {
                            "methods": {
                                "get": {"response": {"$ref": "GoogleCloudDialogflowCxV3beta1TestCaseResult"}},
                                "list": {"response": {"$ref": "GoogleCloudDialogflowCxV3beta1ListTestCaseResultsResponse"}},
                            },
                        }
                    },
                },
            },
        }}}}},
    },
}


@pytest.fixture()
def idx() -> SchemaIndex:
    return SchemaIndex.from_doc(DOC, version="v3beta1")


def test_esquema_lista_de(idx):
    assert idx.esquema_lista_de("testCases") == \
        "GoogleCloudDialogflowCxV3beta1ListTestCasesResponse"
    assert idx.esquema_lista_de("testCases.results") == \
        "GoogleCloudDialogflowCxV3beta1ListTestCaseResultsResponse"


def test_esquema_de_es_el_del_item(idx):
    assert idx.esquema_de("testCases") == "GoogleCloudDialogflowCxV3beta1TestCase"
    assert idx.esquema_de("testCases.results") == \
        "GoogleCloudDialogflowCxV3beta1TestCaseResult"


def test_item_de_lista(idx):
    assert idx.item_de_lista(
        "GoogleCloudDialogflowCxV3beta1ListTestCaseResultsResponse") == \
        "GoogleCloudDialogflowCxV3beta1TestCaseResult"


def test_scan_coleccion_de_un_listado_baja_a_los_items(idx):
    """El envoltorio se barre con su esquema: los items, con el suyo."""
    datos = {"testCaseResults": [
        {"name": "r1", "testResult": "PASSED"},
        {"name": "r2", "testResult": "PASSED", "campoSecreto": 1},
    ]}
    b = scan_coleccion(datos, idx, "testCases.results")
    assert b["n_items"] == 2
    assert b["envoltorio"] == []
    assert [h["campo"] for h in b["items"]] == ["campoSecreto"]


def test_scan_coleccion_no_confunde_el_envoltorio_con_el_item(idx):
    """REGRESA: barrer el envoltorio con el esquema del item da falsos positivos."""
    datos = {"testCaseResults": [{"name": "r1"}]}
    b = scan_coleccion(datos, idx, "testCases.results")
    # `testCaseResults` NO es un hueco: es un campo del envoltorio.
    assert all(h["campo"] != "testCaseResults" for h in b["envoltorio"])
    assert all(h["campo"] != "testCaseResults" for h in b["items"])


def test_scan_coleccion_de_un_recurso_suelto(idx):
    """Un `GET {name}` (no un listado) se barre con el esquema del item."""
    datos = {"name": "c1", "displayName": "x"}
    b = scan_coleccion(datos, idx, "testCases")
    assert b["envoltorio"] == []
    assert b["items"] == []
    assert b["n_items"] == 1


def test_scan_coleccion_detecta_un_campo_del_envoltorio(idx):
    datos = {"testCases": [{"name": "c1"}], "campoRaro": 1}
    b = scan_coleccion(datos, idx, "testCases")
    assert [h["campo"] for h in b["envoltorio"]] == ["campoRaro"]
