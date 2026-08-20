# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for execution tools (query, translate_sparql, rag_retrieval, graph_traversal).

Execution tools invoke the Context Manager via ContextManagerClient.invoke().
These tests mock the CM client to verify correct payload construction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_mcp.tools.execution import (
    execute_graph_traversal,
    execute_query,
    execute_rag_retrieval,
    execute_translate_sparql,
)


@pytest.fixture
def mock_cm():
    """Mock ContextManagerClient with async invoke."""
    cm = AsyncMock()
    cm.invoke = AsyncMock(
        return_value={
            "result": {
                "tier": 2,
                "confidence": {"score": 0.85, "rationale": "ontology match"},
                "resultRows": [{"region": "us-east-1", "revenue": "1234567"}],
                "trace": [
                    {"step": "vector_search", "status": "success", "durationMs": 45},
                    {"step": "nl_to_sparql", "status": "success", "durationMs": 1200},
                ],
            }
        }
    )
    return cm


@pytest.fixture
def profile():
    return {"namespace": "sales", "userId": "user1", "globalRoles": ["viewer"]}


@pytest.mark.unit
class TestExecuteQuery:
    """Tests for the query execution tool."""

    @pytest.mark.asyncio
    async def test_invokes_cm_with_correct_payload(self, mock_cm, profile):
        await execute_query(mock_cm, "what is revenue by region?", "sales", profile, "my-token")
        mock_cm.invoke.assert_called_once()
        payload, token = mock_cm.invoke.call_args[0]
        assert payload["query"] == "what is revenue by region?"
        assert payload["namespace"] == "sales"
        assert token == "my-token"

    @pytest.mark.asyncio
    async def test_returns_query_result_envelope(self, mock_cm, profile):
        """Response now matches the Smithy ``Query`` output shape:
        ``{result, requestId, sessionId}`` — same envelope data-layer returns."""
        mock_cm.invoke.return_value = {
            "result": {
                "tier": 2,
                "confidence": {"score": 0.85, "rationale": "ontology match"},
                "resultRows": [{"region": "us-east-1", "revenue": "1234567"}],
                "trace": [
                    {"step": "vector_search", "status": "success", "durationMs": 45},
                    {"step": "nl_to_sparql", "status": "success", "durationMs": 1200},
                ],
            },
            "requestId": "req-abc",
            "sessionId": "sess-xyz",
        }
        result = await execute_query(mock_cm, "revenue", "sales", profile, "tok")
        assert result["result"]["tier"] == 2
        assert result["result"]["confidence"]["score"] == 0.85
        assert len(result["result"]["trace"]) == 2
        assert result["requestId"] == "req-abc"
        assert result["sessionId"] == "sess-xyz"

    @pytest.mark.asyncio
    async def test_flat_cm_response_wrapped_into_envelope(self, mock_cm, profile):
        """If CM returns a flat dict (no ``result`` key), the tool still wraps
        it in the Smithy envelope so callers see a consistent shape. Missing
        identifiers surface as ``None``."""
        mock_cm.invoke.return_value = {
            "tier": 1,
            "confidence": {"score": 1.0, "rationale": "metric"},
            "resultRows": [],
        }
        result = await execute_query(mock_cm, "test", "ns", profile, "tok")
        assert result["result"]["tier"] == 1
        assert result["requestId"] is None
        assert result["sessionId"] is None

    @pytest.mark.asyncio
    async def test_tier_override_passed(self, mock_cm, profile):
        await execute_query(mock_cm, "test", "ns", profile, "tok", tier_override=1)
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["tierOverride"] == 1

    @pytest.mark.asyncio
    async def test_max_results_capped_at_10000(self, mock_cm, profile):
        await execute_query(mock_cm, "test", "ns", profile, "tok", max_results=99999)
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["maxResults"] == 10000

    @pytest.mark.asyncio
    async def test_include_supporting_false(self, mock_cm, profile):
        await execute_query(mock_cm, "test", "ns", profile, "tok", include_supporting=False)
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["includeSupporting"] is False

    @pytest.mark.asyncio
    async def test_execute_false_translates_only(self, mock_cm, profile):
        """Smithy input parity: ``execute=False`` short-circuits to translate-only,
        matching data-layer's Query operation behavior."""
        await execute_query(mock_cm, "test", "ns", profile, "tok", execute=False)
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["execute"] is False

    @pytest.mark.asyncio
    async def test_dimensions_and_mode_forwarded(self, mock_cm, profile):
        """The Smithy inputs the old tool signature dropped (``dimensions``,
        ``mode``) now reach the CM payload. ``dimensions`` is normalized from
        the Smithy list-of-DimensionFilter wire shape into the ``{name: value}``
        mapping CM's Tier-1 resolver expects — reverting this normalization
        500s every dimensioned Tier-1 query with ``AttributeError`` on
        ``.items()``.

        ``timeoutMs`` is intentionally NOT in the tool signature: the Smithy
        contract declares it but the Context Manager has no reader for
        ``options.timeoutMs`` today, so accepting it here would silently
        no-op. Pin its absence so a future revert doesn't reintroduce the
        silent-ignore bug MR !939 comment ``b6dea1de`` flagged.
        """
        await execute_query(
            mock_cm,
            "test",
            "ns",
            profile,
            "tok",
            mode="agentic",
            dimensions=[{"name": "region", "value": "us-east"}],
        )
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["mode"] == "agentic"
        assert payload["options"]["dimensions"] == {"region": "us-east"}
        assert "timeoutMs" not in payload["options"]

    @pytest.mark.asyncio
    async def test_dimensions_operator_field_is_dropped(self, mock_cm, profile):
        """CM's Tier-1 ``substitute_dimensions`` supports equality only. The
        Smithy DimensionFilter may carry an ``operator`` field; it must be
        dropped rather than forwarded, or the resolver silently applies the
        wrong filter."""
        await execute_query(
            mock_cm,
            "test",
            "ns",
            profile,
            "tok",
            dimensions=[{"name": "region", "value": "us-east", "operator": "!="}],
        )
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["dimensions"] == {"region": "us-east"}

    @pytest.mark.asyncio
    async def test_empty_dimensions_list_is_not_forwarded(self, mock_cm, profile):
        """An empty list normalises to an empty dict; forwarding that would
        add a useless ``options.dimensions`` key. Skip it instead."""
        await execute_query(
            mock_cm,
            "test",
            "ns",
            profile,
            "tok",
            dimensions=[],
        )
        payload = mock_cm.invoke.call_args[0][0]
        assert "dimensions" not in payload["options"]


@pytest.mark.unit
class TestExecuteTranslateSparql:
    """Tests for the translate_sparql tool."""

    @pytest.mark.asyncio
    async def test_invokes_translate_action(self, mock_cm, profile):
        mock_cm.invoke.return_value = {
            "result": {
                "sparql_generated": "SELECT ?x WHERE { ?x a :Thing }",
                "confidence": {"score": 0.9, "rationale": "ontology"},
                "trace": [],
            }
        }
        await execute_translate_sparql(mock_cm, "revenue by region", "sales", profile, "tok")
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["action"] == "translate"
        assert payload["query"] == "revenue by region"
        assert payload["namespace"] == "sales"

    @pytest.mark.asyncio
    async def test_returns_sparql_query(self, mock_cm, profile):
        mock_cm.invoke.return_value = {
            "result": {
                "sparql_generated": "SELECT ?x WHERE { ?x a :Thing }",
                "confidence": {"score": 0.9, "rationale": "ontology"},
                "trace": [],
            }
        }
        result = await execute_translate_sparql(mock_cm, "test", "ns", profile, "tok")
        assert result["sparqlQuery"] == "SELECT ?x WHERE { ?x a :Thing }"
        assert result["confidence"]["score"] == 0.9


@pytest.mark.unit
class TestExecuteRagRetrieval:
    """Tests for the rag_retrieval tool."""

    @pytest.mark.asyncio
    async def test_invokes_kb_search_action(self, mock_cm, profile):
        mock_cm.invoke.return_value = {"result": {"supportingContent": [{"text": "chunk1"}], "trace": []}}
        await execute_rag_retrieval(mock_cm, "revenue growth", "sales", profile, "tok")
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["action"] == "kbSearch"
        assert payload["query"] == "revenue growth"
        assert payload["namespace"] == "sales"

    @pytest.mark.asyncio
    async def test_top_k_capped_at_100(self, mock_cm, profile):
        mock_cm.invoke.return_value = {"result": {"supportingContent": [], "trace": []}}
        await execute_rag_retrieval(mock_cm, "test", "ns", profile, "tok", top_k=999)
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["topK"] == 100

    @pytest.mark.asyncio
    async def test_min_score_passed(self, mock_cm, profile):
        mock_cm.invoke.return_value = {"result": {"supportingContent": [], "trace": []}}
        await execute_rag_retrieval(mock_cm, "test", "ns", profile, "tok", min_score=0.7)
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["minScore"] == 0.7

    @pytest.mark.asyncio
    async def test_returns_chunks(self, mock_cm, profile):
        mock_cm.invoke.return_value = {
            "result": {"supportingContent": [{"text": "Revenue grew 15%", "relevanceScore": 0.92}], "trace": []}
        }
        result = await execute_rag_retrieval(mock_cm, "revenue", "sales", profile, "tok")
        assert len(result["chunks"]) == 1
        assert result["chunks"][0]["text"] == "Revenue grew 15%"


@pytest.mark.unit
class TestExecuteGraphTraversal:
    """Tests for the graph_traversal tool."""

    @pytest.mark.asyncio
    async def test_invokes_graph_traverse_action(self, mock_cm, profile):
        mock_cm.invoke.return_value = {"result": {"graphContext": {"entities": [], "relationships": []}, "trace": []}}
        await execute_graph_traversal(mock_cm, "urn:customer:1", "sales", profile, "tok")
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["action"] == "graphTraverse"
        # ``startUri`` now lives inside ``options`` to match the data-layer
        # handler shape (see ``_handle_graph_traverse``). This is the CM
        # contract; the previous top-level placement diverged from data-layer.
        assert payload["options"]["startUri"] == "urn:customer:1"
        assert payload["namespace"] == "sales"

    @pytest.mark.asyncio
    async def test_relationship_filter_forwarded(self, mock_cm, profile):
        """Missing input the previous signature dropped — now reaches CM."""
        mock_cm.invoke.return_value = {"result": {"graphContext": {"entities": [], "relationships": []}, "trace": []}}
        await execute_graph_traversal(
            mock_cm,
            "urn:customer:1",
            "sales",
            profile,
            "tok",
            relationship_filter=["skos:related", "rdfs:subClassOf"],
        )
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["relationshipFilter"] == ["skos:related", "rdfs:subClassOf"]

    @pytest.mark.asyncio
    async def test_max_depth_capped_at_5(self, mock_cm, profile):
        mock_cm.invoke.return_value = {"result": {"graphContext": {"entities": [], "relationships": []}, "trace": []}}
        await execute_graph_traversal(mock_cm, "urn:x", "ns", profile, "tok", max_depth=99)
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["maxDepth"] == 5

    @pytest.mark.asyncio
    async def test_max_results_capped_at_1000(self, mock_cm, profile):
        mock_cm.invoke.return_value = {"result": {"graphContext": {"entities": [], "relationships": []}, "trace": []}}
        await execute_graph_traversal(mock_cm, "urn:x", "ns", profile, "tok", max_results=9999)
        payload = mock_cm.invoke.call_args[0][0]
        assert payload["options"]["maxResults"] == 1000

    @pytest.mark.asyncio
    async def test_invalid_direction_raises(self, mock_cm, profile):
        with pytest.raises(ValueError, match="Invalid direction"):
            await execute_graph_traversal(mock_cm, "urn:x", "ns", profile, "tok", direction="sideways")

    @pytest.mark.asyncio
    async def test_returns_entities_and_relationships(self, mock_cm, profile):
        mock_cm.invoke.return_value = {
            "result": {
                "graphContext": {
                    "entities": [{"uri": "urn:customer:1", "label": "Acme Corp"}],
                    "relationships": [{"sourceUri": "urn:customer:1", "targetUri": "urn:order:42"}],
                },
                "trace": [],
            }
        }
        result = await execute_graph_traversal(mock_cm, "urn:customer:1", "sales", profile, "tok")
        assert len(result["entities"]) == 1
        assert result["entities"][0]["label"] == "Acme Corp"
        assert len(result["relationships"]) == 1
