"""Tests of the generic write (`cxp/console_fields.py`) and of its helpers.

They need no network: `set_field` is tested with a `ConsoleApi` double.
"""
from __future__ import annotations

import json
from typing import Any

from cxp.console_fields import diff_json, set_field
from cxp.gaps import read_path


class FakeApi:
    """Double of `ConsoleApi`: keeps the resource in memory and simulates the PATCH.

    It reproduces the real behaviour measured from the backend:
      * a PATCH with `updateMask` only touches the first-level key;
      * a PATCH with an obsolete `etag` fails with 409;
      * if the written message has subfields the server does not know,
        it empties them.
    """

    def __init__(self, resource: dict[str, Any],
                 fields_ignored_by_the_server: set[str] | None = None):
        self.resource = resource
        self.writes: list[tuple[str, dict, str | None]] = []
        self.ignored = fields_ignored_by_the_server or set()
        self.fail_with_etag = True

    # --- what `set_field` uses ---
    def _url(self, path: str, **params):
        return path

    def request(self, method: str, url: str, body: dict | None = None, **kw):
        if method == "GET":
            return json.loads(json.dumps(self.resource))
        raise AssertionError(f"unexpected method: {method}")

    def patch_resource(self, name, body, update_mask=None, **kw):
        if self.fail_with_etag and "etag" in body:
            raise RuntimeError("HTTP 409: FAILED_PRECONDITION (obsolete etag)")
        self.writes.append((name, body, update_mask))
        mask = (update_mask or "").split(",")
        for key in mask:
            if key in body:
                self.resource[key] = body[key]
            else:
                self.resource.pop(key, None)
        # the server empties the subfields it does not know
        self.resource = self._empty_unknown(self.resource)
        return json.loads(json.dumps(self.resource))

    def _empty_unknown(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {k: ({} if k in self.ignored else self._empty_unknown(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [self._empty_unknown(v) for v in node]
        return node


# ----------------------------------------------------------------- diff_json
def test_diff_json_detects_changes():
    changes = diff_json({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert changes == [{"route": "b", "before": 2, "after": 3}]


def test_diff_json_detects_additions_and_removals():
    changes = diff_json({"a": 1}, {"b": 2})
    routes = {c["route"] for c in changes}
    assert routes == {"a", "b"}


def test_diff_json_walks_lists():
    changes = diff_json({"r": [{"x": 1}]}, {"r": [{"x": 2}]})
    assert changes[0]["route"] == "r[0].x"


def test_diff_json_without_changes():
    assert diff_json({"a": 1}, {"a": 1}) == []


# ------------------------------------------------------------------ set_field
def test_set_field_dry_run_does_not_write():
    api = FakeApi({"name": "r", "transitionRoutes": [{"condition": "true"}]})
    res = set_field(api, "r", "transitionRoutes[0].targetPlaybook", "pb", dry_run=True)
    assert res.saved is False
    assert api.writes == []
    assert res.update_mask == ["transitionRoutes"]
    assert res.after == "pb"


def test_set_field_writes_and_verifies():
    api = FakeApi({"name": "r", "transitionRoutes": [{"condition": "true"}]})
    res = set_field(api, "r", "transitionRoutes[0].targetPlaybook", "pb", dry_run=False)
    assert res.saved is True
    assert res.verified is True
    assert read_path(api.resource, "transitionRoutes[0].targetPlaybook") == "pb"
    # the mask is the first-level key
    assert api.writes[0][2] == "transitionRoutes"


def test_set_field_detects_what_the_server_drops():
    api = FakeApi({"name": "r", "advancedSettings": {"enabled": True}},
                  fields_ignored_by_the_server={"enabled"})
    res = set_field(api, "r", "advancedSettings", {"enabled": False}, dry_run=False)
    assert res.saved is True
    assert res.verified is False
    assert res.dropped  # it says that it was lost


def test_set_field_does_not_send_the_etag():
    api = FakeApi({"name": "r", "etag": "old", "displayName": "d"})
    res = set_field(api, "r", "displayName", "new", dry_run=False)
    assert "etag" not in api.writes[0][1]
    assert res.verified is True


def test_set_field_with_an_obsolete_etag_fails():
    api = FakeApi({"name": "r", "etag": "old"})
    # the double fails if the etag arrives: as `set_field` removes it, it does
    # NOT fail
    res = set_field(api, "r", "name", "other", dry_run=False)
    assert res.error == ""
    assert res.verified is True


def test_set_field_read_error():
    class Broken(FakeApi):
        def request(self, method, url, body=None, **kw):
            raise RuntimeError("HTTP 404")

    res = set_field(Broken({}), "r", "x", 1, dry_run=False)
    assert res.error.startswith("could not read")
    assert res.saved is False
