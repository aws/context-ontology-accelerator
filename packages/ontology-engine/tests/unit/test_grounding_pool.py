# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Guard for the shared bedrock-runtime client's connection-pool cap.

The rerank ThreadPool (pipeline.py, _grounding_workers default 24) has ALL its
threads share ONE lazily-built bedrock-runtime client. botocore's default
``max_pool_connections=10`` throttles a multi-threaded pool. This locks that the
client is built with a pool cap that is >= the worker default so no rerank
thread starves on a connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.services import grounding
from coa_ontology.inducer.services.grounding import (
    _BEDROCK_MAX_POOL_CONNECTIONS,
    GroundingService,
)
from coa_ontology.inducer.services.pipeline import _grounding_workers


def test_bedrock_client_caps_connection_pool(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_client(service_name: str, **kwargs: object) -> MagicMock:
        captured["service_name"] = service_name
        captured["config"] = kwargs.get("config")
        captured["region_name"] = kwargs.get("region_name")
        return MagicMock()

    monkeypatch.setattr(grounding.boto3, "client", _fake_client)

    svc = GroundingService(
        ontology_catalog=MagicMock(),
        embedding_generator=MagicMock(),
        llm_region="us-west-2",
    )
    _ = svc.bedrock  # trigger lazy construction

    assert captured["service_name"] == "bedrock-runtime"
    assert captured["region_name"] == "us-west-2"  # region preserved
    assert captured["config"].max_pool_connections == _BEDROCK_MAX_POOL_CONNECTIONS


def test_pool_cap_covers_worker_default(monkeypatch) -> None:
    # The whole point: the shared client's pool must not be the bottleneck.
    monkeypatch.delenv("INDUCER_GROUNDING_WORKERS", raising=False)
    assert _grounding_workers() <= _BEDROCK_MAX_POOL_CONNECTIONS
