# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""ontology_uri_prefix is charset-checked on every induction request model.

The prefix is minted into every induced class/property IRI and stored as the
ontology ID, so markup, quotes, whitespace, and query strings must be rejected
at the API boundary rather than persisted.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_ontology.induce_catalog import WorkbenchInductionRequest
from coa_ontology.inducer.schemas import InductionRequest
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_UNSAFE = [
    "http://<script>alert(1)</script>",
    "https://example.com/onto<script>",
    'https://example.com/a"onmouseover=1',
    "https://example.com/a b#",
    "https://user@example.com/o#",
    "javascript:alert(1)",
]


@pytest.mark.parametrize("model", [WorkbenchInductionRequest, InductionRequest])
@pytest.mark.parametrize("prefix", _UNSAFE)
def test_unsafe_prefix_rejected(model: type, prefix: str) -> None:
    with pytest.raises(ValidationError, match="ontology_uri_prefix"):
        model(ontology_uri_prefix=prefix)


def test_workbench_still_normalizes_bare_prefix() -> None:
    body = WorkbenchInductionRequest(ontology_uri_prefix="  https://example.com/ontology/retail  ")
    assert body.ontology_uri_prefix == "https://example.com/ontology/retail#"


@pytest.mark.parametrize(
    "prefix", ["http://x/o#", "https://example.com/ontology/", "http://test.org/ontology/induced#"]
)
def test_workbench_accepts_existing_prefixes(prefix: str) -> None:
    assert WorkbenchInductionRequest(ontology_uri_prefix=prefix).ontology_uri_prefix == prefix


# ── HTTP boundary: the mounted routes reject before any job is created ──────


@pytest.fixture
def client():
    from coa_ontology import induce_catalog
    from coa_ontology.inducer.routers import induce_unstructured
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(induce_catalog.router, prefix="/induce")
    app.include_router(induce_unstructured.router, prefix="/induce/unstructured")
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/induce/", {"datasource_ids": ["ds-1"]}),
        (
            "/induce/unstructured/",
            {"name": "news", "graph_arn": "arn:aws:neptune-graph:us-east-1:123456789012:graph/g-abc1234567"},
        ),
    ],
)
def test_route_rejects_unsafe_prefix_without_starting_a_job(client, path: str, body: dict) -> None:
    with (
        patch("coa_ontology.induce_catalog.Thread") as thread_cls,
        patch("coa_ontology.induce_catalog.dynamo_store") as store,
    ):
        resp = client.post(
            path,
            params={"namespace": "ns-1"},
            json={**body, "ontology_uri_prefix": "http://<script>alert(1)</script>"},
        )
    assert resp.status_code == 422
    assert any("ontology_uri_prefix" in err["loc"] for err in resp.json()["detail"])
    thread_cls.assert_not_called()
    assert store.method_calls == []
