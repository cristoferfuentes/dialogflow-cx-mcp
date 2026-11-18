"""Tests of the METHOD gap catalogue (`cxp/console_endpoints.py`).

The catalogue is MEASURED data: these tests check that it does not break and
that the numbers it states still add up with the real descriptor.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cxp.console_endpoints import (
    METHOD_GAPS,
    METHODS_BY_VERSION,
    _ids_of,
    method_catalogue,
    method_gaps,
)
from cxp.gaps import SchemaIndex

ROOT = Path(__file__).resolve().parent.parent
DISCOVERY = ROOT / ".discovery"


# ------------------------------------------------------------- the catalogue
def test_the_catalogue_is_not_empty():
    assert len(METHOD_GAPS) >= 8


def test_every_endpoint_has_the_minimum():
    for h in METHOD_GAPS:
        assert h["id"]
        assert h["version"] in METHODS_BY_VERSION or h["version"] == "v2beta1"
        assert h["method"] in ("GET", "POST", "PATCH", "DELETE")
        # The routes hang from a project, from the root (`users/`) or they are
        # the collection of projects of the ROOT of the backend (`projects`).
        assert (h["path"].startswith("projects/") or h["path"].startswith("users/")
                or h["path"] == "projects")
        assert h["status"] in ("verified", "no_data", "error")
        assert h["purpose"]


def test_projects_list_is_an_endpoint_of_the_root():
    """MEASURED (2026-11-15): the listing of projects lives in the root of the
    backend (`/v3alpha1/projects`), not under a project: that is why the MCP
    resolves it as an ABSOLUTE route."""
    p = next(h for h in METHOD_GAPS if h["id"] == "projects.list")
    assert p["path"] == "projects"
    assert p["version"] == "v3alpha1"


def test_the_ids_are_unique():
    ids = [h["id"] for h in METHOD_GAPS]
    assert len(ids) == len(set(ids))


def test_the_method_catalogue_returns_copies():
    a = method_catalogue()
    a[0]["id"] = "touched"
    b = method_catalogue()
    assert b[0]["id"] != "touched"


def test_the_absolute_routes_do_not_hang_from_a_project():
    # `users/me/...` is a route from the root of the backend: that is why the
    # MCP has to resolve absolute routes.
    recent = next(h for h in METHOD_GAPS if h["id"] == "users.recentProjects")
    assert recent["path"].startswith("users/")


# ------------------------------------------------------ against the descriptor
@pytest.mark.skipif(not (DISCOVERY / "v3beta1.json").exists(),
                    reason="no cached discovery doc")
def test_the_method_counts_add_up():
    idx = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    assert len(_ids_of(idx)) == METHODS_BY_VERSION["v3beta1"] == 162


@pytest.mark.skipif(not (DISCOVERY / "v3alpha1.json").exists(),
                    reason="no cached discovery doc")
def test_v3beta1_is_the_superset():
    betas = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    alpha = SchemaIndex.load(DISCOVERY / "v3alpha1.json", version="v3alpha1")
    ids_betas = _ids_of(betas)
    ids_alpha = _ids_of(alpha)
    assert len(ids_alpha) == METHODS_BY_VERSION["v3alpha1"] == 8
    # No method of v3alpha1 is missing in v3beta1.
    assert not (ids_alpha - ids_betas)


@pytest.mark.skipif(not (DISCOVERY / "v3.json").exists(),
                    reason="no cached discovery doc")
def test_v3_has_four_methods_less():
    betas = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    v3 = SchemaIndex.load(DISCOVERY / "v3.json", version="v3")
    assert len(_ids_of(v3)) == METHODS_BY_VERSION["v3"] == 158
    assert len(_ids_of(betas) - _ids_of(v3)) == 4


@pytest.mark.skipif(not (DISCOVERY / "v3beta1.json").exists(),
                    reason="no cached discovery doc")
def test_method_gaps_between_versions():
    betas = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    v3 = SchemaIndex.load(DISCOVERY / "v3.json", version="v3")
    # The ids are taken from the SAME descriptor (the one of the version asked).
    res = method_gaps(betas, "v3beta1", "v3beta1")
    assert res["only_in_version"] == []
    assert res["only_in_against"] == []
    # The doc of v3 has 4 methods less: it shows when comparing docs.
    assert len(_ids_of(betas) - _ids_of(v3)) == 4
