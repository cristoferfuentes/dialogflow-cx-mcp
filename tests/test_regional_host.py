"""Tests of the REGIONAL HOST (measured on 2026-11-15).

The console calls a host per location:
    global      -> dialogflow.clients6.google.com
    us-central1 -> us-central1-dialogflow.clients6.google.com

and the `referer` has to carry the host that is called (otherwise: 400
"Origin doesn't match Host for XD3").
"""
from __future__ import annotations

from cxp.console_api import (
    CONSOLE_HOST,
    KNOWN_LOCATIONS,
    host_for_location,
    host_from_path,
    location_from_path,
)


def test_global_uses_the_global_host():
    assert host_for_location("global") == CONSOLE_HOST
    assert host_for_location("") == CONSOLE_HOST
    assert host_for_location(None) == CONSOLE_HOST


def test_another_location_uses_the_regional_host():
    assert host_for_location("us-central1") == \
        "us-central1-dialogflow.clients6.google.com"
    assert host_for_location("eu") == "eu-dialogflow.clients6.google.com"


def test_location_from_path():
    assert location_from_path(
        "/projects/p/locations/us-central1/agents/a") == "us-central1"
    assert location_from_path("/projects/p/locations/global/agents/a") == "global"
    assert location_from_path("/projects/p/agent/visibility") == ""


def test_host_from_path():
    assert host_from_path("/projects/p/locations/us-central1/agents/a") == \
        "us-central1-dialogflow.clients6.google.com"
    # No location in the path -> global host (project routes, etc.).
    assert host_from_path("/projects/p/agent/visibility") == CONSOLE_HOST


def test_the_known_locations_do_not_repeat_and_do_not_include_invented_ones():
    assert len(KNOWN_LOCATIONS) == len(set(KNOWN_LOCATIONS))
    assert "global" in KNOWN_LOCATIONS
    assert "us-central1" in KNOWN_LOCATIONS
    # The ones that only exist in another region (they do not work for this project).
    assert "southamerica-east1" not in KNOWN_LOCATIONS
