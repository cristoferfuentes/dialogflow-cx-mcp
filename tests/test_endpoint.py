"""Tests de la resolucion de rutas y versiones (`cxp/endpoint.py`).

No necesitan red: es logica pura.
"""
from __future__ import annotations

import pytest

from cxp.endpoint import (
    VERSIONES_SERVIDAS,
    PathError,
    es_absoluta,
    resolve_path,
    split_version,
    url_de,
)


# ------------------------------------------------------------------ version
def test_split_version():
    assert split_version("v3alpha1/users/me") == ("users/me", "v3alpha1")
    assert split_version("v3beta1/projects/p") == ("projects/p", "v3beta1")
    assert split_version("v2beta1/projects/p") == ("projects/p", "v2beta1")
    assert split_version("v3/projects/p") == ("projects/p", "v3")
    assert split_version("users/me") == ("users/me", "")
    assert split_version("") == ("", "")


def test_split_version_no_confunde_un_prefijo_similar():
    # Una palabra que empieza por 'v' pero NO lleva digitos no es version.
    assert split_version("vertices") == ("vertices", "")
    # Con digitos SI es una version (aunque no exista): se comprueba al
    # resolver, no al trocear.
    assert split_version("v3beta") == ("", "v3beta")
    assert split_version("v3") == ("", "v3")


def test_versiones_servidas():
    assert "v3beta1" in VERSIONES_SERVIDAS
    assert "v3alpha1" in VERSIONES_SERVIDAS
    assert "v3" in VERSIONES_SERVIDAS
    assert "v2beta1" in VERSIONES_SERVIDAS


# ------------------------------------------------------------------ rutas
def test_ruta_relativa_antepone_proyecto_y_location():
    ruta, version, q = resolve_path("agents/{agent}/flows", project="p",
                                     location="global", agent="a")
    assert ruta == "projects/p/locations/global/agents/a/flows"
    assert version == "v3beta1"
    assert q == {}


def test_ruta_absoluta_de_proyecto_no_se_toca():
    ruta, _, _ = resolve_path("projects/otro/agent/visibility", project="p",
                              location="global")
    assert ruta == "projects/otro/agent/visibility"


def test_ruta_absoluta_desde_la_raiz():
    ruta, _, _ = resolve_path("/users/me/userSettings/recentProjects",
                              project="p", location="global")
    assert ruta == "users/me/userSettings/recentProjects"


def test_la_version_de_la_ruta_gana_a_la_del_parametro():
    _, version, _ = resolve_path("v3alpha1/projects/p/x", version="v3beta1",
                                  project="p", location="global")
    assert version == "v3alpha1"


def test_el_parametro_version_gana_al_por_defecto():
    _, version, _ = resolve_path("projects/p/x", version="v2beta1",
                                  project="p", location="global")
    assert version == "v2beta1"


def test_nombre_completo_como_valor_de_name():
    nombre = "projects/p/locations/global/agents/a/flows/f"
    ruta, _, _ = resolve_path("{+name}", name=nombre, project="p", location="global")
    assert ruta == nombre


def test_placeholders_de_path():
    ruta, _, _ = resolve_path("projects/{project}/locations/{location}/agents/{agent}",
                              project="p", location="global", agent="a")
    assert ruta == "projects/p/locations/global/agents/a"


def test_faltan_parametros_da_error():
    with pytest.raises(PathError):
        resolve_path("projects/{project}/agents/{agent}", project="",
                     location="global", agent="")


def test_ruta_relativa_sin_proyecto_da_error():
    with pytest.raises(PathError):
        resolve_path("agents/{agent}/flows", project="", location="global",
                     agent="a")


def test_ruta_vacia_da_error():
    with pytest.raises(PathError):
        resolve_path("", project="p", location="global")


def test_es_absoluta():
    assert es_absoluta("/users/me")
    assert es_absoluta("projects/p/x")
    assert not es_absoluta("agents/{agent}/flows")


def test_params_no_usados_en_la_ruta_van_a_la_query():
    ruta, _, q = resolve_path("projects/p/locations/global/agents/a/intents",
                              project="p", location="global",
                              params={"languageCode": "es", "pageSize": "10"})
    assert ruta == "projects/p/locations/global/agents/a/intents"
    assert q == {"languageCode": "es", "pageSize": "10"}


def test_params_usados_en_la_ruta_no_van_a_la_query():
    _, _, q = resolve_path("agents/{agent}/flows/{flow}",
                            project="p", location="global",
                            params={"agent": "a", "flow": "f"})
    assert q == {}


# -------------------------------------------------------------------- url
def test_url_de():
    url = url_de("v3alpha1", "projects/p/x", "host.example", {"a": "1"}, key="K")
    assert url == "https://host.example/v3alpha1/projects/p/x?a=1&key=K"


def test_url_de_sin_query():
    assert url_de("v3", "x", "h") == "https://h/v3/x"
