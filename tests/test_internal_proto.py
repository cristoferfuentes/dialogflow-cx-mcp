"""Tests of the INTERNAL proto catalogue (`cxp/proto_interno.py`).

The catalogue is MEASURED data (extracted from the console bundle and verified
by writing), so these tests check that it does not break and that the fields
that the public discovery doc declares do NOT show up as gaps.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cxp.gaps import SchemaIndex
from cxp.proto_interno import (
    INTERNAL_PROTO_GAPS,
    WRITE_STATES,
    internal_proto_catalogue,
    summary,
)

ROOT = Path(__file__).resolve().parent.parent
DISCOVERY = ROOT / ".discovery"


def test_the_catalogue_is_not_empty():
    assert len(INTERNAL_PROTO_GAPS) >= 10


def test_every_entry_has_the_minimum():
    for h in INTERNAL_PROTO_GAPS:
        assert h["schema"]
        assert h["fields"]
        assert all(isinstance(f, str) for f in h["fields"])


def test_the_states_are_known():
    valid = {"writable", "ignored", "dropped", "derived",
             "input_only", "not_accepted", "not_tested"}
    for h in INTERNAL_PROTO_GAPS:
        for field, state in (h.get("write") or {}).items():
            assert state in valid, (h["schema"], field, state)
            assert field in h["fields"], (h["schema"], field)


def test_the_catalogue_returns_copies():
    a = internal_proto_catalogue()
    a[0]["fields"] = []
    b = internal_proto_catalogue()
    assert b[0]["fields"]


def test_the_summary_adds_up():
    r = summary()
    total = sum(len(h["fields"]) for h in INTERNAL_PROTO_GAPS)
    assert r["fields"] == total
    assert r["schemas"] == len(INTERNAL_PROTO_GAPS)


def test_no_field_is_left_unmeasured():
    """The 38 fields of the internal proto have a MEASURED state."""
    r = summary()
    assert r["unmeasured"] == [], r["unmeasured"]
    assert r["measured"] == r["fields"]


def test_the_new_states_are_valid():
    assert {"writable", "ignored", "dropped", "derived",
            "input_only", "not_accepted"} <= WRITE_STATES


@pytest.mark.skipif(not (DISCOVERY / "v3beta1.json").exists(),
                    reason="no cached discovery doc")
def test_no_field_of_the_catalogue_is_declared_in_the_public_doc():
    """If a field shows up here, the public descriptor does NOT declare it."""
    idx = SchemaIndex.load(DISCOVERY / "v3beta1.json", version="v3beta1")
    for h in INTERNAL_PROTO_GAPS:
        short = h["schema"]
        declared = idx.declared(short)
        if not declared:
            # The name may not exist as a loose schema: it is looked up by
            # suffix among all the ones of the doc.
            for name, props in idx.props.items():
                if name.endswith(short):
                    declared = set(props)
                    break
        assert declared, f"the schema {short} is not found"
        for field in h["fields"]:
            assert field not in declared, (short, field)
