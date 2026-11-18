"""Tests of the LISTINGS sweep (`cxp/gaps.py`).

MEASURED (2026-11-15): a listing returns a WRAPPER
(`{"testCaseResults": [...]}`) whose schema differs from that of the ITEM
(`TestCaseResult`). Sweeping the wrapper with the item schema (or the other
way round) produces **false positives**. These tests pin down that behaviour.
"""
from __future__ import annotations

import pytest

from cxp.gaps import SchemaIndex, scan_collection

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


def test_the_list_schema(idx):
    assert idx.list_schema_of("testCases") == \
        "GoogleCloudDialogflowCxV3beta1ListTestCasesResponse"
    assert idx.list_schema_of("testCases.results") == \
        "GoogleCloudDialogflowCxV3beta1ListTestCaseResultsResponse"


def test_the_schema_of_is_the_item_one(idx):
    assert idx.schema_of("testCases") == "GoogleCloudDialogflowCxV3beta1TestCase"
    assert idx.schema_of("testCases.results") == \
        "GoogleCloudDialogflowCxV3beta1TestCaseResult"


def test_the_item_of_the_list(idx):
    assert idx.list_item(
        "GoogleCloudDialogflowCxV3beta1ListTestCaseResultsResponse") == \
        "GoogleCloudDialogflowCxV3beta1TestCaseResult"


def test_scan_collection_of_a_listing_descends_to_the_items(idx):
    """The wrapper is swept with its schema: the items, with theirs."""
    data = {"testCaseResults": [
        {"name": "r1", "testResult": "PASSED"},
        {"name": "r2", "testResult": "PASSED", "campoSecreto": 1},
    ]}
    b = scan_collection(data, idx, "testCases.results")
    assert b["n_items"] == 2
    assert b["wrapper"] == []
    assert [h["field"] for h in b["items"]] == ["campoSecreto"]


def test_scan_collection_does_not_confuse_the_wrapper_with_the_item(idx):
    """WARNING: sweeping the wrapper with the item schema gives false positives."""
    data = {"testCaseResults": [{"name": "r1"}]}
    b = scan_collection(data, idx, "testCases.results")
    # `testCaseResults` is NOT a gap: it is a field of the wrapper.
    assert all(h["field"] != "testCaseResults" for h in b["wrapper"])
    assert all(h["field"] != "testCaseResults" for h in b["items"])


def test_scan_collection_of_a_loose_resource(idx):
    """A `GET {name}` (not a listing) is swept with the item schema."""
    data = {"name": "c1", "displayName": "x"}
    b = scan_collection(data, idx, "testCases")
    assert b["wrapper"] == []
    assert b["items"] == []
    assert b["n_items"] == 1


def test_scan_collection_detects_a_field_of_the_wrapper(idx):
    data = {"testCases": [{"name": "c1"}], "campoRaro": 1}
    b = scan_collection(data, idx, "testCases")
    assert [h["field"] for h in b["wrapper"]] == ["campoRaro"]
