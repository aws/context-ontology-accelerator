# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the tool modules' shared response/pagination helpers."""

from __future__ import annotations

import os
from typing import Any

import pytest
from coa_workflow_agents.clients.coa_api_client import CoaApiError
from coa_workflow_agents.tools import _shared
from coa_workflow_agents.tools._shared import (
    PRESIGNED_URL_OMITTED,
    as_list,
    clamp,
    collect_pages,
    datasource_ids,
    envelope_items,
    read_local_file,
    redact_presigned_urls,
    safe_relative_path,
    tolerate_initial_not_found,
)
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit


def test_envelope_items_first_list_key_wins_even_when_empty():
    """``items`` is checked first; an empty ``items`` list is the answer (COA
    sends only one envelope key), not a reason to fall through."""
    assert envelope_items({"items": [], "sources": [{"a": 1}]}, "sources") == []
    assert envelope_items({"sources": [{"a": 1}]}, "sources") == [{"a": 1}]


def test_collect_pages_stops_on_a_non_string_next_token():
    calls: list[Any] = []

    def fetch(token):
        calls.append(token)
        if len(calls) > 3:
            raise AssertionError("pagination did not terminate")
        return {"items": [{"x": 1}], "nextToken": 123}

    assert collect_pages(fetch) == [{"x": 1}]
    assert calls == [None]


@pytest.mark.parametrize(("value", "expected"), [(0, 1), (-3, 1), (7, 7), (99, 50), (2.5, 2), (True, 1)])
def test_clamp_returns_an_int_in_range(value, expected):
    out = clamp(value, 1, 50)
    assert out == expected and type(out) is int


def test_safe_relative_path_rejects_a_sibling_directory_sharing_the_prefix(tmp_path, monkeypatch):
    """`/work/cwdx` must not pass as inside `/work/cwd` by string prefix."""

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (tmp_path / "cwdx").mkdir()
    monkeypatch.chdir(cwd)
    with pytest.raises(ValueError, match="escapes"):
        safe_relative_path("../cwdx/file.ttl", label="path")
    assert safe_relative_path("sub/file.ttl", label="path").startswith(str(cwd.resolve()))


def test_redact_presigned_urls_replaces_only_signed_urls_and_walks_nesting():
    signed = "https://b.s3.amazonaws.com/k?X-Amz-Credential=AKIA%2Fx&X-Amz-Signature=abc"
    credential_only = "https://b.s3.amazonaws.com/k?X-Amz-Credential=AKIA%2Fx"
    token_only = "https://b.s3.amazonaws.com/k?X-Amz-Security-Token=IQoJ"
    value = {
        "url": signed,
        "nested": {"list": [signed, "plain", 3, None], "partial": credential_only, "token": token_only},
        "plain_url": "https://schema.org/version/latest/schemaorg-current-https.ttl",
        "key": "proposals/ns/p/latest/ontology.ttl",
    }

    out = redact_presigned_urls(value)

    assert out == {
        "url": PRESIGNED_URL_OMITTED,
        "nested": {
            "list": [PRESIGNED_URL_OMITTED, "plain", 3, None],
            "partial": PRESIGNED_URL_OMITTED,
            "token": PRESIGNED_URL_OMITTED,
        },
        "plain_url": "https://schema.org/version/latest/schemaorg-current-https.ttl",
        "key": "proposals/ns/p/latest/ontology.ttl",
    }
    assert value["url"] == signed  # the input is not mutated


def _not_found() -> dict[str, Any]:
    raise CoaApiError("GET", "/namespaces/ns1/induce/jobs/job-9", 404, "{}")


def test_tolerate_initial_not_found_reads_a_404_within_the_grace_as_the_placeholder(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(_shared.time, "monotonic", lambda: clock["t"])
    fetch = tolerate_initial_not_found(_not_found, {"status": "pending"}, 10.0)

    with capture_logs() as logs:
        assert fetch() == {"status": "pending"}
    # Logged by route shape only, never the raw path's ids.
    assert [(e["event"], e["route"]) for e in logs] == [
        ("poll_initial_not_found_tolerated", "/namespaces/{id}/induce/jobs/{id}")
    ]
    clock["t"] = 10.0
    with pytest.raises(CoaApiError):
        fetch()


def test_tolerate_initial_not_found_passes_through_a_read_and_other_errors():
    assert tolerate_initial_not_found(lambda: {"status": "done"}, {}, 10.0)() == {"status": "done"}

    def boom() -> dict[str, Any]:
        raise CoaApiError("GET", "/x", 500, "boom")

    with pytest.raises(CoaApiError, match="boom"):
        tolerate_initial_not_found(boom, {}, 10.0)()


@pytest.mark.parametrize(
    ("obj", "expected"),
    [({"sourceId": "s-1"}, "s-1"), ({"source_id": 7}, "7"), ({"sourceId": None}, None), ({}, None)],
)
def test_first_str_returns_a_string_or_none(obj, expected):
    assert _shared.first_str(obj, "sourceId", "source_id") == expected


def test_as_list_normalizes_non_lists_to_empty():
    assert as_list([1]) == [1]
    assert as_list(None) == []
    assert as_list({"a": 1}) == []


def test_envelope_items_accepts_a_bare_list_and_drops_non_objects():
    assert envelope_items([{"a": 1}, "x", 2]) == [{"a": 1}]
    assert envelope_items({"jobs": [{"j": 1}, None]}, "sources", "jobs") == [{"j": 1}]
    assert envelope_items("not a page") == []


def test_collect_pages_follows_tokens_through_an_empty_page_and_stops_on_a_repeat():
    pages = {None: {"items": [{"n": 1}], "nextToken": "a"}, "a": {"items": [], "nextToken": "b"}}
    pages["b"] = {"items": [{"n": 2}], "nextToken": "a"}
    calls: list[Any] = []

    def fetch(token):
        calls.append(token)
        return pages[token]

    assert collect_pages(fetch) == [{"n": 1}, {"n": 2}]
    assert calls == [None, "a", "b"]


def test_safe_relative_path_rejects_an_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        safe_relative_path(str(tmp_path / "f.ttl"), label="path")


def test_read_local_file_reads_within_bounds_and_refuses_empty_or_oversized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ok.ttl").write_bytes(b"abc")
    (tmp_path / "empty.ttl").write_bytes(b"")
    (tmp_path / "big.ttl").write_bytes(b"abcd")
    resolved, data = read_local_file("ok.ttl", label="path", max_bytes=3)
    assert data == b"abc" and resolved == str((tmp_path / "ok.ttl").resolve())
    for name in ("empty.ttl", "big.ttl"):
        with pytest.raises(ValueError, match="1 byte to 3 bytes"):
            read_local_file(name, label="path", max_bytes=3)


def test_datasource_ids_reads_top_level_and_metadata_in_either_case():
    assert datasource_ids({"datasourceId": "d1", "metadata": {"datasource_ids": ["d2", None, 3]}}) == ["d1", "d2", "3"]
    assert datasource_ids({"metadata": "not a dict"}) == []


def test_safe_relative_path_rejects_a_symlink_that_points_outside_the_working_directory(tmp_path, monkeypatch):
    cwd, outside = tmp_path / "cwd", tmp_path / "outside"
    cwd.mkdir()
    outside.mkdir()
    (outside / "file.ttl").write_bytes(b"secret")
    os.symlink(outside, cwd / "link")
    monkeypatch.chdir(cwd)
    with pytest.raises(ValueError, match="escapes"):
        safe_relative_path("link/file.ttl", label="path")
    with pytest.raises(ValueError, match="escapes"):
        read_local_file("link/file.ttl", label="path")
