"""Tests del HOST REGIONAL (medido el 2026-11-15).

La consola llama a un host por location:
    global      -> dialogflow.clients6.google.com
    us-central1 -> us-central1-dialogflow.clients6.google.com

y el `referer` tiene que llevar el host que se llama (si no: 400
"Origin doesn't match Host for XD3").
"""
from __future__ import annotations

from cxp.console_api import (
    CONSOLE_HOST,
    LOCACIONES_CONOCIDAS,
    host_de_path,
    host_para_location,
    location_de_path,
)


def test_global_usa_el_host_global():
    assert host_para_location("global") == CONSOLE_HOST
    assert host_para_location("") == CONSOLE_HOST
    assert host_para_location(None) == CONSOLE_HOST


def test_otra_location_usa_el_host_regional():
    assert host_para_location("us-central1") == \
        "us-central1-dialogflow.clients6.google.com"
    assert host_para_location("eu") == "eu-dialogflow.clients6.google.com"


def test_location_de_path():
    assert location_de_path(
        "/projects/p/locations/us-central1/agents/a") == "us-central1"
    assert location_de_path("/projects/p/locations/global/agents/a") == "global"
    assert location_de_path("/projects/p/agent/visibility") == ""


def test_host_de_path():
    assert host_de_path("/projects/p/locations/us-central1/agents/a") == \
        "us-central1-dialogflow.clients6.google.com"
    # Sin location en el path -> host global (rutas de proyecto, etc.).
    assert host_de_path("/projects/p/agent/visibility") == CONSOLE_HOST


def test_las_locations_conocidas_no_repiten_y_no_incluyen_inventadas():
    assert len(LOCACIONES_CONOCIDAS) == len(set(LOCACIONES_CONOCIDAS))
    assert "global" in LOCACIONES_CONOCIDAS
    assert "us-central1" in LOCACIONES_CONOCIDAS
    # Las que solo existen en otra region (no sirven para este proyecto).
    assert "southamerica-east1" not in LOCACIONES_CONOCIDAS
