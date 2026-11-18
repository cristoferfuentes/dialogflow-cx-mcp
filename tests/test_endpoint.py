"""Tests of the route and version resolution (`cxp/endpoint.py`).

They need no network: it is pure logic.
"""
from __future__ import annotations

import pytest

from cxp.endpoint import (
    SERVED_VERSIONS,
    PathError,
    is_absolute,
    resolve_path,
    split_version,
    url_for,
)


# ------------------------------------------------------------------ version
def test_split_version():
    assert split_version("v3alpha1/users/me") == ("users/me", "v3alpha1")
    assert split_version("v3beta1/projects/p") == ("projects/p", "v3beta1")
    assert split_version("v2beta1/projects/p") == ("projects/p", "v2beta1")
    assert split_version("v3/projects/p") == ("projects/p", "v3")
    assert split_version("users/me") == ("users/me", "")
    assert split_version("") == ("", "")


def test_split_version_does_not_confuse_a_similar_prefix():
    # A word that starts with 'v' but carries NO digits is not a version.
    assert split_version("vertices") == ("vertices", "")
    # With digits it IS a version (even if it does not exist): that is checked
    # when resolving, not when splitting.
    assert split_version("v3beta") == ("", "v3beta")
    assert split_version("v3") == ("", "v3")


def test_the_served_versions():
    assert "v3beta1" in SERVED_VERSIONS
    assert "v3alpha1" in SERVED_VERSIONS
    assert "v3" in SERVED_VERSIONS
    assert "v2beta1" in SERVED_VERSIONS


# ------------------------------------------------------------------ routes
def test_a_relative_route_prepends_project_and_location():
    route, version, q = resolve_path("agents/{agent}/flows", project="p",
                                     location="global", agent="a")
    assert route == "projects/p/locations/global/agents/a/flows"
    assert version == "v3beta1"
    assert q == {}


def test_an_absolute_project_route_is_not_touched():
    route, _, _ = resolve_path("projects/other/agent/visibility", project="p",
                              location="global")
    assert route == "projects/other/agent/visibility"


def test_an_absolute_route_from_the_root():
    route, _, _ = resolve_path("/users/me/userSettings/recentProjects",
                              project="p", location="global")
    assert route == "users/me/userSettings/recentProjects"


def test_the_version_of_the_route_wins_over_the_parameter_one():
    _, version, _ = resolve_path("v3alpha1/projects/p/x", version="v3beta1",
                                  project="p", location="global")
    assert version == "v3alpha1"


def test_the_version_parameter_wins_over_the_default_one():
    _, version, _ = resolve_path("projects/p/x", version="v2beta1",
                                  project="p", location="global")
    assert version == "v2beta1"


def test_the_full_name_as_the_name_value():
    name = "projects/p/locations/global/agents/a/flows/f"
    route, _, _ = resolve_path("{+name}", name=name, project="p", location="global")
    assert route == name


def test_the_path_placeholders():
    route, _, _ = resolve_path("projects/{project}/locations/{location}/agents/{agent}",
                              project="p", location="global", agent="a")
    assert route == "projects/p/locations/global/agents/a"


def test_missing_parameters_raise_an_error():
    with pytest.raises(PathError):
        resolve_path("projects/{project}/agents/{agent}", project="",
                     location="global", agent="")


def test_a_relative_route_without_a_project_raises_an_error():
    with pytest.raises(PathError):
        resolve_path("agents/{agent}/flows", project="", location="global",
                     agent="a")


def test_an_empty_route_raises_an_error():
    with pytest.raises(PathError):
        resolve_path("", project="p", location="global")


def test_is_absolute():
    assert is_absolute("/users/me")
    assert is_absolute("projects/p/x")
    assert not is_absolute("agents/{agent}/flows")


def test_the_params_not_used_in_the_route_go_to_the_query():
    route, _, q = resolve_path("projects/p/locations/global/agents/a/intents",
                              project="p", location="global",
                              params={"languageCode": "es", "pageSize": "10"})
    assert route == "projects/p/locations/global/agents/a/intents"
    assert q == {"languageCode": "es", "pageSize": "10"}


def test_the_params_used_in_the_route_do_not_go_to_the_query():
    _, _, q = resolve_path("agents/{agent}/flows/{flow}",
                            project="p", location="global",
                            params={"agent": "a", "flow": "f"})
    assert q == {}


# -------------------------------------------------------------------- url
def test_url_for():
    url = url_for("v3alpha1", "projects/p/x", "host.example", {"a": "1"}, key="K")
    assert url == "https://host.example/v3alpha1/projects/p/x?a=1&key=K"


def test_url_for_without_a_query():
    assert url_for("v3", "x", "h") == "https://h/v3/x"
