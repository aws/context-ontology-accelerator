# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SSRF prevention in unstructured induction.

Validates that the router at
``src/coa_ontology/inducer/routers/induce_unstructured.py``
properly sanitizes graph_arn input to prevent Server-Side Request Forgery
attacks where an attacker could steal ECS task IAM credentials by supplying
a malicious graph_arn value.

Key security properties tested:
- Pydantic schema rejects malformed graph_arn values at the HTTP layer
- _build_lexical_store NEVER uses user input as an endpoint URL for Neptune DB
- Neptune DB endpoint is ALWAYS resolved from server-side configuration
- Attacker-controlled hostnames containing "neptune.amazonaws.com" are rejected
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_common.constants import GRAPH_BASE_URI
from pydantic import ValidationError

pytestmark = pytest.mark.unit

from coa_ontology.inducer.routers.induce_unstructured import (
    _build_lexical_store,
)
from coa_ontology.inducer.unstructured.schemas import (
    UnstructuredInductionRequest,
)


class TestGraphArnValidation:
    """Pydantic schema validation rejects malicious graph_arn values."""

    def test_valid_neptune_analytics_arn_accepted(self):
        """A well-formed Neptune Analytics ARN passes validation."""
        request = UnstructuredInductionRequest(
            name="test",
            graph_arn="arn:aws:neptune-graph:us-east-1:123456789012:graph/g-abc123xyz",
            ontology_uri_prefix="http://example.com/ontology/test",
        )
        assert request.graph_arn == "arn:aws:neptune-graph:us-east-1:123456789012:graph/g-abc123xyz"

    def test_neptune_db_sentinel_accepted(self):
        """The literal string 'neptune-db' is accepted as a sentinel value."""
        request = UnstructuredInductionRequest(
            name="test",
            graph_arn="neptune-db",
            ontology_uri_prefix="http://example.com/ontology/test",
        )
        assert request.graph_arn == "neptune-db"

    def test_attacker_subdomain_rejected(self):
        """A hostname like 'neptune.amazonaws.com.attacker.com' is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            UnstructuredInductionRequest(
                name="test",
                graph_arn="neptune.amazonaws.com.attacker.com",
                ontology_uri_prefix="http://example.com/ontology/test",
            )
        # Pydantic raises ValidationError with details about the pattern mismatch
        assert "pattern" in str(exc_info.value).lower() or "graph_arn" in str(exc_info.value)

    def test_attacker_subdomain_with_https_rejected(self):
        """An attacker URL with https:// prefix is rejected."""
        with pytest.raises(ValidationError):
            UnstructuredInductionRequest(
                name="test",
                graph_arn="https://neptune.amazonaws.com.attacker.com",
                ontology_uri_prefix="http://example.com/ontology/test",
            )

    def test_arbitrary_url_rejected(self):
        """An arbitrary attacker-controlled URL is rejected."""
        with pytest.raises(ValidationError):
            UnstructuredInductionRequest(
                name="test",
                graph_arn="https://evil.com/steal-creds",
                ontology_uri_prefix="http://example.com/ontology/test",
            )

    def test_malformed_arn_missing_graph_segment_rejected(self):
        """An ARN missing the graph/ segment is rejected."""
        with pytest.raises(ValidationError):
            UnstructuredInductionRequest(
                name="test",
                graph_arn="arn:aws:neptune-graph:us-east-1:123456789012:g-abc",
                ontology_uri_prefix="http://example.com/ontology/test",
            )

    def test_arn_with_wrong_service_rejected(self):
        """An ARN for a different service (e.g., neptune-db) is rejected."""
        with pytest.raises(ValidationError):
            UnstructuredInductionRequest(
                name="test",
                graph_arn="arn:aws:neptune:us-east-1:123456789012:cluster:my-cluster",
                ontology_uri_prefix="http://example.com/ontology/test",
            )

    def test_path_traversal_attempt_rejected(self):
        """Path traversal attempts in graph_arn are rejected."""
        with pytest.raises(ValidationError):
            UnstructuredInductionRequest(
                name="test",
                graph_arn="../../../../etc/passwd",
                ontology_uri_prefix="http://example.com/ontology/test",
            )


class TestBuildLexicalStoreSSRFPrevention:
    """_build_lexical_store never uses user input as an endpoint URL."""

    def test_neptune_analytics_arn_creates_na_store(self):
        """A valid Neptune Analytics ARN creates a NeptuneAnalyticsLexicalStore."""
        graph_arn = "arn:aws:neptune-graph:us-west-2:123456789012:graph/g-test"
        app_config = {"neptune_endpoint": "cluster.us-west-2.neptune.amazonaws.com"}

        with (
            patch("coa_ontology.inducer.routers.induce_unstructured.NeptuneAnalyticsLexicalStore") as mock_na,
            patch("coa_ontology.inducer.routers.induce_unstructured.NeptuneDatabaseLexicalStore") as mock_ndb,
        ):
            _build_lexical_store(graph_arn, "us-west-2", app_config)

            mock_na.assert_called_once_with(graph_id="g-test", region="us-west-2")
            mock_ndb.assert_not_called()

    def test_neptune_db_sentinel_uses_server_config_endpoint(self):
        """The 'neptune-db' sentinel creates NDB store from server config ONLY."""
        graph_arn = "neptune-db"
        app_config = {"neptune_endpoint": "https://real-cluster.us-east-1.neptune.amazonaws.com:8182"}

        with (
            patch("coa_ontology.inducer.routers.induce_unstructured.NeptuneAnalyticsLexicalStore") as mock_na,
            patch("coa_ontology.inducer.routers.induce_unstructured.NeptuneDatabaseLexicalStore") as mock_ndb,
        ):
            _build_lexical_store(graph_arn, "us-east-1", app_config, namespace="550e8400-e29b-41d4-a716-446655440002")

            mock_ndb.assert_called_once()
            call_kwargs = mock_ndb.call_args.kwargs
            assert call_kwargs["endpoint"] == "real-cluster.us-east-1.neptune.amazonaws.com"
            assert call_kwargs["region"] == "us-east-1"
            assert call_kwargs["tenant_id"] != ""

            mock_na.assert_not_called()

    def test_neptune_db_missing_config_raises_valueerror(self):
        """When neptune_endpoint is not configured, ValueError is raised."""
        graph_arn = "neptune-db"
        app_config = {}  # No neptune_endpoint configured

        with pytest.raises(ValueError) as exc_info:
            _build_lexical_store(graph_arn, "us-east-1", app_config)

        assert "not configured" in str(exc_info.value).lower()

    def test_user_input_never_becomes_endpoint_url(self):
        """Even if graph_arn passed Pydantic validation, it's not used as an endpoint.

        This is a defense-in-depth test: if the Pydantic pattern were somehow
        bypassed, _build_lexical_store should still not use the value as an
        endpoint URL for Neptune DB.
        """
        malicious_arn = "attacker.com"
        app_config = {"neptune_endpoint": "https://safe.neptune.amazonaws.com:8182"}

        with (
            patch("coa_ontology.inducer.routers.induce_unstructured.NeptuneAnalyticsLexicalStore"),
            patch("coa_ontology.inducer.routers.induce_unstructured.NeptuneDatabaseLexicalStore") as mock_ndb,
        ):
            # The function treats any non-ARN input as a Neptune DB sentinel
            _build_lexical_store(malicious_arn, "us-east-1", app_config)

            mock_ndb.assert_called_once()
            call_kwargs = mock_ndb.call_args.kwargs
            assert call_kwargs["endpoint"] == "safe.neptune.amazonaws.com"


class TestDispatchBuildsValidGraphArn:
    """Regression: the structured→unstructured dispatch in
    ``induce_catalog.start_induction`` must build an ``UnstructuredInductionRequest``
    whose ``graph_arn`` satisfies the SSRF pattern.
    """

    def _dispatch(self, body_graph_arn, config):
        """Invoke the dispatch branch; return the UnstructuredInductionRequest it built."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from coa_ontology.induce_catalog import (
            WorkbenchInductionRequest,
            start_induction,
        )

        body = WorkbenchInductionRequest(
            datasource_ids=["ds-1"],
            ontology_uri_prefix=f"{GRAPH_BASE_URI}/namespace/550e8400-e29b-41d4-a716-446655440003/ontology/induced#",
            strategy="unstructured_lexical_graph",
            graph_arn=body_graph_arn,
        )
        req = MagicMock()
        req.app.state.config = config
        with patch("coa_ontology.inducer.routers.induce_unstructured.start_unstructured_job") as mock_unstr:
            mock_unstr.return_value = SimpleNamespace(job_id="j", status="pending")
            start_induction(body=body, request=req, namespace="550e8400-e29b-41d4-a716-446655440003")
            mock_unstr.assert_called_once()
            return mock_unstr.call_args.kwargs["body"]

    def test_no_explicit_arn_uses_neptune_db_sentinel_not_raw_url(self):
        """With no body graph_arn and an NDB cluster URL in config, the dispatch
        must send the ``neptune-db`` sentinel — NOT the raw URL (which 500s)."""
        built = self._dispatch(
            body_graph_arn=None,
            config={"neptune_endpoint": "-dev-neptune.cluster-abc.us-east-1.neptune.amazonaws.com:8182"},
        )
        # The sentinel, which passes the SSRF regex and routes to the config
        # endpoint inside _build_lexical_store.
        assert built.graph_arn == "neptune-db"

    def test_explicit_na_arn_is_rejected_at_customer_dispatch(self):
        """The customer-facing dispatch rejects an explicit NA source."""
        from fastapi import HTTPException

        arn = "arn:aws:neptune-graph:us-east-1:123456789012:graph/g-abc123"
        with pytest.raises(HTTPException) as raised:
            self._dispatch(body_graph_arn=arn, config={"neptune_endpoint": "ignored"})

        assert raised.value.status_code == 422
        assert raised.value.detail == {
            "code": "UNSUPPORTED_SOURCE",
            "message": "The requested lexical graph source is not supported.",
        }

    def test_missing_neptune_endpoint_returns_structured_configuration_error(self):
        """A missing server source uses the same structured error contract."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as raised:
            self._dispatch(body_graph_arn=None, config={})

        assert raised.value.status_code == 422
        assert raised.value.detail == {
            "code": "CONFIGURATION_ERROR",
            "message": "The lexical graph source is not configured.",
        }
