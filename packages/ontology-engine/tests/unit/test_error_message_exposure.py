# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client-facing error messages carry no backend exception text and no echoed input (CWE-209).

The graph routes used to append the store's exception (``{e}``) to their 501s
and to put the caller's URI back into 400/404 details; the ontology fetch path
named the private address a hostname resolved to — an internal-DNS oracle. Each
now answers a fixed message and logs the detail.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.catalog.routers.ontologies import _resolve_and_validate_url
from coa_ontology.main import app
from fastapi import HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

client = TestClient(app)

_PROBE_URI = "http://probe.example.org/onto#Secret"


@pytest.fixture()
def graph_store():
    store = MagicMock()
    store.__class__.__name__ = "NeptuneDBGraphStore"
    with patch("coa_ontology.catalog.routers.graph.build_stores", return_value=(store, MagicMock())):
        yield store


@pytest.mark.parametrize("path", ["/graph/class/", "/graph/vertex"])
def test_lookup_backend_unsupported_hides_the_exception(graph_store, path):
    graph_store.get_vertex.side_effect = NotImplementedError("cluster-xyz.neptune.amazonaws.com")
    resp = client.get(path, params={"uri": _PROBE_URI, "namespace": "ns"})
    assert resp.status_code == 501
    assert "neptune.amazonaws.com" not in resp.text


@pytest.mark.parametrize("path", ["/graph/class/", "/graph/vertex"])
def test_invalid_uri_does_not_echo_the_uri(graph_store, path):
    graph_store.get_vertex.side_effect = ValueError(f"URI contains forbidden characters: {_PROBE_URI!r}")
    resp = client.get(path, params={"uri": _PROBE_URI, "namespace": "ns"})
    assert resp.status_code == 400
    assert "probe.example.org" not in resp.text


@pytest.mark.parametrize("path", ["/graph/class/", "/graph/vertex"])
def test_not_found_does_not_echo_the_uri_or_namespace(graph_store, path):
    graph_store.get_vertex.return_value = None
    resp = client.get(path, params={"uri": _PROBE_URI, "namespace": "tenant-secret"})
    assert resp.status_code == 404
    assert "probe.example.org" not in resp.text
    assert "tenant-secret" not in resp.text


def test_overview_errors_hide_the_exception(graph_store):
    graph_store.get_ontology_overview.side_effect = NotImplementedError("NeptuneAnalyticsStore internals")
    resp = client.get("/graph/ontology-overview", params={"ontology_id": "urn:o", "namespace": "ns"})
    assert resp.status_code == 501
    assert "internals" not in resp.text

    graph_store.get_ontology_overview.side_effect = ValueError("bad iri 'urn:o'")
    resp = client.get("/graph/ontology-overview", params={"ontology_id": "urn:o", "namespace": "ns"})
    assert resp.status_code == 400
    assert "bad iri" not in resp.text


def test_fetch_does_not_name_the_private_address():
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]
    with patch("socket.getaddrinfo", return_value=infos), pytest.raises(HTTPException) as exc:
        _resolve_and_validate_url("https://internal.example.org/o.ttl")
    assert exc.value.status_code == 400
    assert "10.1.2.3" not in str(exc.value.detail)


def test_fetch_does_not_echo_the_resolver_error():
    with (
        patch("socket.getaddrinfo", side_effect=socket.gaierror(8, "nodename nor servname provided")),
        pytest.raises(HTTPException) as exc,
    ):
        _resolve_and_validate_url("https://nowhere.example.org/o.ttl")
    assert exc.value.status_code == 400
    assert "nodename" not in str(exc.value.detail)
