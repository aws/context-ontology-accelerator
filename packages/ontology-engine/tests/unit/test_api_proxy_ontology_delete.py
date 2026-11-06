# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The public ontology DELETE must name one ontology (``ontology_id`` is required by the API model).

The proxy refuses a ``DELETE /namespaces/{ns}/ontologies`` with no ``ontology_id``, an empty or blank one, or the
camelCase spelling with 400 before forwarding. The engine's namespace-wide delete is used only by the namespace
deletion pipeline, which calls the engine directly (control-plane namespace/cleanup.py, ``delete_all_ontologies``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

NAMESPACE = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology-engine.ns:8001")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")  # as test_api_proxy_handler: read at import


def _delete(query: dict[str, str] | None) -> tuple[dict[str, Any], MagicMock]:
    from coa_ontology import api_proxy_handler

    response = MagicMock(status=202)
    response.read.return_value = b"{}"
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    event = {
        "httpMethod": "DELETE",
        "resource": "/namespaces/{namespaceId}/ontologies",
        "pathParameters": {"namespaceId": NAMESPACE},
        "queryStringParameters": query,
    }
    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        return api_proxy_handler.handler(event, None), urlopen


@pytest.mark.parametrize(
    "query",
    [None, {}, {"ontology_id": ""}, {"ontology_id": "   "}, {"ontologyId": "urn:x#y"}, {"ontologyId": ""}],
    ids=["missing", "no-params", "empty", "blank", "camelCase-alias", "empty-alias"],
)
def test_a_delete_that_names_no_ontology_is_refused_before_it_is_forwarded(query: dict[str, str] | None) -> None:
    result, urlopen = _delete(query)
    assert result["statusCode"] == 400, result
    assert "ontology_id" in result["body"]
    urlopen.assert_not_called()


def test_the_camel_case_alias_is_refused_even_beside_ontology_id() -> None:
    result, urlopen = _delete({"ontology_id": "urn:x#y", "ontologyId": "urn:x#z"})
    assert result["statusCode"] == 400
    urlopen.assert_not_called()


def test_a_single_ontology_delete_is_forwarded_unchanged() -> None:
    result, urlopen = _delete({"ontology_id": "urn:x#y"})
    assert result["statusCode"] == 202
    (request,), _ = urlopen.call_args
    assert request.get_method() == "DELETE"
    # The engine host is read at import, and other test modules import with their own; the path and query are ours.
    assert request.full_url.endswith(f"/ontologies/?ontology_id=urn%3Ax%23y&namespace={NAMESPACE}")
