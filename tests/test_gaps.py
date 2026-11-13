"""Tests de la capa de huecos (`cxp/gaps.py`) y de las rutas.

No necesitan red: todo es logica pura sobre el discovery doc.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cxp.gaps import (
    SchemaIndex,
    agrupar_gaps,
    apply_path,
    find_gaps,
    norm_schema,
    parse_path,
    read_path,
    strip_derived,
    top_level_keys,
)

# Un discovery doc MINIMO, con el patron real del oficial: los mismos
# conceptos con prefijos de version distintos (`V3beta1` y `V3`), y una
# coleccion cuyo `get` declara el `$ref` de su tipo.
DOC_FALSO = {
    "schemas": {
        "GoogleCloudDialogflowCxV3beta1Agent": {
            "properties": {
                "name": {"type": "string"},
                "displayName": {"type": "string"},
                "advancedSettings": {"$ref": "GoogleCloudDialogflowCxV3beta1AdvancedSettings"},
            }
        },
        "GoogleCloudDialogflowCxV3AdvancedSettings": {
            "properties": {"loggingSettings": {"type": "object"}}
        },
        "GoogleCloudDialogflowCxV3beta1AdvancedSettings": {
            "properties": {"loggingSettings": {"type": "object"}}
        },
        "GoogleCloudDialogflowCxV3beta1Page": {
            "properties": {
                "name": {"type": "string"},
                "transitionRoutes": {
                    "type": "array",
                    "items": {"$ref": "GoogleCloudDialogflowCxV3beta1TransitionRoute"},
                },
            }
        },
        "GoogleCloudDialogflowCxV3beta1TransitionRoute": {
            "properties": {
                "condition": {"type": "string"},
                "targetPage": {"type": "string"},
            }
        },
    },
    "resources": {
        "projects": {
            "resources": {
                "locations": {
                    "resources": {
                        "agents": {
                            "methods": {"get": {"response": {"$ref": "GoogleCloudDialogflowCxV3beta1Agent"}}},
                            "resources": {
                                "flows": {
                                    "resources": {
                                        "pages": {
                                            "methods": {"get": {"response": {"$ref": "GoogleCloudDialogflowCxV3beta1Page"}}},
                                        }
                                    }
                                }
                            },
                        }
                    }
                }
            }
        }
    },
}


@pytest.fixture()
def idx() -> SchemaIndex:
    return SchemaIndex.from_doc(DOC_FALSO, version="v3beta1")


# --------------------------------------------------------------------- rutas
def test_parse_path():
    assert parse_path("a") == [("a", None)]
    assert parse_path("a.b") == [("a", None), ("b", None)]
    assert parse_path("a[0]") == [("a", None), ("", 0)]
    assert parse_path("a[2].b") == [("a", None), ("", 2), ("b", None)]
    assert parse_path("a[0][1]") == [("a", None), ("", 0), ("", 1)]


def test_read_path():
    obj = {"a": {"b": [{"c": 1}]}}
    assert read_path(obj, "a.b[0].c") == 1
    assert read_path(obj, "a.b[1].c") is None
    assert read_path(obj, "a.x") is None
    assert read_path(obj, "") is None


def test_apply_path_crea_los_contenedores():
    obj: dict = {}
    apply_path(obj, "a.b[0].c", 7)
    assert obj == {"a": {"b": [{"c": 7}]}}


def test_apply_path_sobrescribe():
    obj = {"a": {"b": [{"c": 1}]}}
    apply_path(obj, "a.b[0].c", 2)
    assert obj["a"]["b"][0]["c"] == 2


def test_apply_path_en_un_elemento_de_lista():
    obj = {"transitionRoutes": [{"condition": "true"}]}
    apply_path(obj, "transitionRoutes[0].targetPlaybook", "projects/x/playbooks/y")
    assert obj["transitionRoutes"][0]["targetPlaybook"] == "projects/x/playbooks/y"
    # NO debe pisar el resto de la ruta
    assert obj["transitionRoutes"][0]["condition"] == "true"


def test_apply_path_ruta_vacia():
    with pytest.raises(ValueError):
        apply_path({}, "", 1)


def test_top_level_keys():
    assert top_level_keys("transitionRoutes[0].targetPlaybook") == ["transitionRoutes"]
    assert top_level_keys("webClientSettings.boxBorderRadius") == ["webClientSettings"]
    assert top_level_keys("") == []


def test_strip_derived():
    obj = {"name": "x", "etag": "e", "createTime": "t", "referencedFlows": [],
           "advancedSettings": {}}
    limpio = strip_derived(obj)
    assert "etag" not in limpio
    assert "createTime" not in limpio
    assert "referencedFlows" not in limpio
    assert limpio["name"] == "x"
    # no toca el original
    assert "etag" in obj


# ------------------------------------------------------------------ esquemas
def test_norm_schema():
    assert norm_schema("GoogleCloudDialogflowCxV3beta1TransitionRoute") == "TransitionRoute"
    assert norm_schema("GoogleCloudDialogflowCxV3TransitionRoute") == "TransitionRoute"


def test_schema_index_elige_la_version_correcta(idx):
    # El doc tiene dos `AdvancedSettings`: la de v3beta1 y la de v3.
    # Con api_version=v3beta1 tiene que elegir la V3beta1.
    assert idx.canonical["AdvancedSettings"] == "GoogleCloudDialogflowCxV3beta1AdvancedSettings"
    assert idx.declared("AdvancedSettings") == {"loggingSettings"}


def test_schema_index_resuelve_la_coleccion(idx):
    assert idx.esquema_de("agents") == "GoogleCloudDialogflowCxV3beta1Agent"
    assert idx.esquema_de("flows.pages") == "GoogleCloudDialogflowCxV3beta1Page"


def test_sub_schema_de_un_array(idx):
    sub = idx.sub_schema("GoogleCloudDialogflowCxV3beta1Page", "transitionRoutes")
    assert sub == "GoogleCloudDialogflowCxV3beta1TransitionRoute"


# --------------------------------------------------------------------- huecos
def test_find_gaps_detecta_el_campo_que_no_declara(idx):
    pagina = {"name": "x", "transitionRoutes": [{"condition": "true",
                                                 "targetPlaybook": "p"}]}
    huecos = find_gaps(pagina, idx.esquema_de("flows.pages"), idx)
    assert [h.campo for h in huecos] == ["targetPlaybook"]
    assert huecos[0].ruta == "transitionRoutes[0].targetPlaybook"
    assert huecos[0].valor == "p"


def test_find_gaps_no_se_inventa_nada(idx):
    pagina = {"name": "x", "transitionRoutes": [{"condition": "true"}]}
    assert find_gaps(pagina, idx.esquema_de("flows.pages"), idx) == []


def test_find_gaps_baja_a_los_sub_objetos(idx):
    agente = {"name": "a", "advancedSettings": {"loggingSettings": {},
                                                "enabled": True}}
    huecos = find_gaps(agente, idx.esquema_de("agents"), idx)
    assert [h.campo for h in huecos] == ["enabled"]
    assert huecos[0].ruta == "advancedSettings.enabled"


def test_find_gaps_marca_los_derivados(idx):
    agente = {"name": "a", "etag": "e"}
    huecos = find_gaps(agente, idx.esquema_de("agents"), idx)
    assert huecos[0].solo_lectura is True


def test_agrupar_gaps(idx):
    pagina = {"transitionRoutes": [{"targetPlaybook": "a"}, {"targetPlaybook": "b"}]}
    huecos = find_gaps(pagina, idx.esquema_de("flows.pages"), idx)
    grupo = agrupar_gaps(huecos)
    assert len(grupo) == 1
    assert grupo[0]["campo"] == "targetPlaybook"
    assert grupo[0]["apariciones"] == 2


# ----------------------------------------------------- contra el doc real
@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / ".discovery" / "v3beta1.json").exists(),
    reason="no hay discovery doc cacheado (no hace falta red en los tests)",
)
def test_contra_el_doc_real():
    raiz = Path(__file__).resolve().parent.parent
    idx = SchemaIndex.load(raiz / ".discovery" / "v3beta1.json", version="v3beta1")
    # `targetPlaybook` no esta en el descriptor publico (ni en v3 ni en v3beta1)
    assert "targetPlaybook" not in idx.declared("GoogleCloudDialogflowCxV3beta1TransitionRoute")
    # y el `get` de una pagina declara el schema de v3beta1
    assert idx.esquema_de("flows.pages") == "GoogleCloudDialogflowCxV3beta1Page"
    # json valido (el doc real se cargo bien)
    json.loads((raiz / ".discovery" / "v3beta1.json").read_text())
