"""Tests of the gaps layer (`cxp/gaps.py`) and of the routes.

They need no network: it is all pure logic over the discovery doc.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cxp.gaps import (
    SchemaIndex,
    apply_path,
    find_gaps,
    group_gaps,
    norm_schema,
    parse_path,
    read_path,
    strip_derived,
    top_level_keys,
)

# A MINIMUM discovery doc, with the real pattern of the official one: the same
# concepts with different version prefixes (`V3beta1` and `V3`), and a
# collection whose `get` declares the `$ref` of its type.
FAKE_DOC = {
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
    return SchemaIndex.from_doc(FAKE_DOC, version="v3beta1")


# -------------------------------------------------------------------- routes
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


def test_apply_path_creates_the_containers():
    obj: dict = {}
    apply_path(obj, "a.b[0].c", 7)
    assert obj == {"a": {"b": [{"c": 7}]}}


def test_apply_path_overwrites():
    obj = {"a": {"b": [{"c": 1}]}}
    apply_path(obj, "a.b[0].c", 2)
    assert obj["a"]["b"][0]["c"] == 2


def test_apply_path_in_a_list_item():
    obj = {"transitionRoutes": [{"condition": "true"}]}
    apply_path(obj, "transitionRoutes[0].targetPlaybook", "projects/x/playbooks/y")
    assert obj["transitionRoutes"][0]["targetPlaybook"] == "projects/x/playbooks/y"
    # It must NOT overwrite the rest of the route
    assert obj["transitionRoutes"][0]["condition"] == "true"


def test_apply_path_with_an_empty_route():
    with pytest.raises(ValueError):
        apply_path({}, "", 1)


def test_top_level_keys():
    assert top_level_keys("transitionRoutes[0].targetPlaybook") == ["transitionRoutes"]
    assert top_level_keys("webClientSettings.boxBorderRadius") == ["webClientSettings"]
    assert top_level_keys("") == []


def test_strip_derived():
    obj = {"name": "x", "etag": "e", "createTime": "t", "referencedFlows": [],
           "advancedSettings": {}}
    clean = strip_derived(obj)
    assert "etag" not in clean
    assert "createTime" not in clean
    assert "referencedFlows" not in clean
    assert clean["name"] == "x"
    # it does not touch the original
    assert "etag" in obj


# -------------------------------------------------------------------- schemas
def test_norm_schema():
    assert norm_schema("GoogleCloudDialogflowCxV3beta1TransitionRoute") == "TransitionRoute"
    assert norm_schema("GoogleCloudDialogflowCxV3TransitionRoute") == "TransitionRoute"


def test_schema_index_picks_the_correct_version(idx):
    # The doc has two `AdvancedSettings`: the one of v3beta1 and the one of v3.
    # With api_version=v3beta1 it has to pick the V3beta1 one.
    assert idx.canonical["AdvancedSettings"] == "GoogleCloudDialogflowCxV3beta1AdvancedSettings"
    assert idx.declared("AdvancedSettings") == {"loggingSettings"}


def test_schema_index_resolves_the_collection(idx):
    assert idx.schema_of("agents") == "GoogleCloudDialogflowCxV3beta1Agent"
    assert idx.schema_of("flows.pages") == "GoogleCloudDialogflowCxV3beta1Page"


def test_the_sub_schema_of_an_array(idx):
    sub = idx.sub_schema("GoogleCloudDialogflowCxV3beta1Page", "transitionRoutes")
    assert sub == "GoogleCloudDialogflowCxV3beta1TransitionRoute"


# ---------------------------------------------------------------------- gaps
def test_find_gaps_detects_the_field_that_is_not_declared(idx):
    page = {"name": "x", "transitionRoutes": [{"condition": "true",
                                               "targetPlaybook": "p"}]}
    gaps = find_gaps(page, idx.schema_of("flows.pages"), idx)
    assert [g.field for g in gaps] == ["targetPlaybook"]
    assert gaps[0].route == "transitionRoutes[0].targetPlaybook"
    assert gaps[0].value == "p"


def test_find_gaps_invents_nothing(idx):
    page = {"name": "x", "transitionRoutes": [{"condition": "true"}]}
    assert find_gaps(page, idx.schema_of("flows.pages"), idx) == []


def test_find_gaps_descends_to_the_sub_objects(idx):
    agent = {"name": "a", "advancedSettings": {"loggingSettings": {},
                                              "enabled": True}}
    gaps = find_gaps(agent, idx.schema_of("agents"), idx)
    assert [g.field for g in gaps] == ["enabled"]
    assert gaps[0].route == "advancedSettings.enabled"


def test_find_gaps_marks_the_derived_ones(idx):
    agent = {"name": "a", "etag": "e"}
    gaps = find_gaps(agent, idx.schema_of("agents"), idx)
    assert gaps[0].read_only is True


def test_group_gaps(idx):
    page = {"transitionRoutes": [{"targetPlaybook": "a"}, {"targetPlaybook": "b"}]}
    gaps = find_gaps(page, idx.schema_of("flows.pages"), idx)
    group = group_gaps(gaps)
    assert len(group) == 1
    assert group[0]["field"] == "targetPlaybook"
    assert group[0]["occurrences"] == 2


# ------------------------------------------------------- against the real doc
@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / ".discovery" / "v3beta1.json").exists(),
    reason="no cached discovery doc (the tests need no network)",
)
def test_against_the_real_doc():
    root = Path(__file__).resolve().parent.parent
    idx = SchemaIndex.load(root / ".discovery" / "v3beta1.json", version="v3beta1")
    # `targetPlaybook` is not in the public descriptor (neither in v3 nor in v3beta1)
    assert "targetPlaybook" not in idx.declared("GoogleCloudDialogflowCxV3beta1TransitionRoute")
    # and the `get` of a page declares the v3beta1 schema
    assert idx.schema_of("flows.pages") == "GoogleCloudDialogflowCxV3beta1Page"
    # valid json (the real doc loaded fine)
    json.loads((root / ".discovery" / "v3beta1.json").read_text())
