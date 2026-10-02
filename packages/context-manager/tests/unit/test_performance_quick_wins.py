# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for performance quick wins — items #2, #4, #6, #7, #9.

Tests cover:
- #2: entity_type filter in OpenSearch search and SQL generator
- #4: tier2_sparql + failure_reason in fallback log
- #6: SELECT variable specificity in NL→SPARQL prompt
- #7: rdfs:range validation in SPARQL validator
- #9: ObjectProperty (FK join paths) in T-Box context
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.clients.base import ConverseResult, QueryResult, VectorHit
from coa_serve.clients.vkg import VKGResult
from coa_serve.models import InvokeRequest
from coa_serve.orchestrator import Orchestrator
from coa_serve.tier1.metric_resolver import MetricMatch
from coa_serve.tier2.nl_to_sql.sql_generator import SQLGenerator
from coa_serve.tier2.ontop.nl_to_sparql import TranslationResult
from coa_serve.tier2.ontop.sparql_validator import SPARQLValidator
from coa_serve.tier2.ontop.tbox_context import TBoxContext, TBoxContextBuilder
from coa_serve.tier2.ontop.vkg_translator import Tier2Result
from coa_serve.tier2.strategy import StrategyOption, StrategyResult, StructuredQueryTier
from coa_serve.tier3.knowledge_retriever import Tier3Result

# ═══════════════════════════════════════════════════════════════════════
# Item #2: entity_type filter in retrieval
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEntityTypeFilter:
    """Verify that NL-to-SQL sql_generator passes entity_type='class' to vector search."""

    @pytest.mark.asyncio
    async def test_sql_generator_passes_entity_type_class(self):
        """sql_generator.generate() should pass entity_type='class' to vector search.

        generate() first gates the mapped filter via count_documents, then calls
        search with entity_type='class'; a non-zero mapped count also sets
        require_mapped=True.
        """
        mock_llm = AsyncMock()
        mock_llm.embed.return_value = [0.1] * 1024
        mock_llm.converse.return_value = ConverseResult(
            text="```sql\nSELECT count(*) FROM orders\n```\nConfidence: 0.85"
        )

        mock_vector = AsyncMock()
        mock_vector.count_documents.return_value = 1  # namespace has mapped classes
        mock_vector.search.return_value = [
            VectorHit(
                id="1",
                text="Table: orders | Description: Orders | Columns: id (int)",
                score=0.9,
                metadata={"entity_type": "class"},
            ),
        ]

        gen = SQLGenerator(llm_client=mock_llm, vector_client=mock_vector)
        await gen.generate("How many orders?", namespace="demo", index="idx")

        mock_vector.search.assert_called_once()
        call_kwargs = mock_vector.search.call_args.kwargs
        assert call_kwargs["entity_type"] == "class"
        assert call_kwargs["require_mapped"] is True  # mapped-gate applied

    @pytest.mark.asyncio
    async def test_sql_generator_passes_entity_type_with_precomputed_embedding(self):
        """When embedding is pre-computed, entity_type filter is still applied."""
        mock_llm = AsyncMock()
        mock_llm.converse.return_value = ConverseResult(text="```sql\nSELECT 1\n```\nConfidence: 0.9")

        mock_vector = AsyncMock()
        mock_vector.count_documents.return_value = 1
        mock_vector.search.return_value = [
            VectorHit(id="1", text="Table: t | Columns: x (int)", score=0.9, metadata={}),
        ]

        gen = SQLGenerator(llm_client=mock_llm, vector_client=mock_vector)
        await gen.generate("q", namespace="ns", embedding=[0.5] * 1024)

        call_kwargs = mock_vector.search.call_args.kwargs
        assert call_kwargs["entity_type"] == "class"

    def test_opensearch_search_accepts_entity_type(self):
        """The search method exposes the entity_type parameter (server-side filter)."""
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        client = OpenSearchVectorClient(endpoint="test.aoss.amazonaws.com")

        import inspect

        sig = inspect.signature(client.search)
        assert "entity_type" in sig.parameters

    @pytest.mark.asyncio
    async def test_opensearch_search_without_entity_type_no_filter(self):
        """Without entity_type, the shared client is called with no filters."""
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        client = OpenSearchVectorClient(endpoint="test.aoss.amazonaws.com")
        aoss = MagicMock()
        aoss.knn_search.return_value = []
        client._aoss = aoss
        await client.search([0.1] * 10, namespace="ns", top_k=5)

        assert aoss.knn_search.call_args.kwargs["filters"] is None

    @pytest.mark.asyncio
    async def test_opensearch_search_with_entity_type_filters_server_side(self):
        """With entity_type='class', the shared client gets a server-side term filter."""
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        client = OpenSearchVectorClient(endpoint="test.aoss.amazonaws.com")
        aoss = MagicMock()
        aoss.knn_search.return_value = [{"_id": "a", "_score": 0.9, "text": "Claim", "entity_type": "class"}]
        client._aoss = aoss
        hits = await client.search([0.1] * 10, namespace="ns", top_k=5, entity_type="class")

        assert aoss.knn_search.call_args.kwargs["filters"] == [{"term": {"entity_type": "class"}}]
        assert [h.id for h in hits] == ["a"]


# ═══════════════════════════════════════════════════════════════════════
# Item #4: tier2_sparql + failure_reason in fallback log
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestFallbackLogging:
    """Verify that tier2_fallback_decision log includes tier2_sparql and failure_reason."""

    def _make_orchestrator(self, tier2_result=None, sparql_valid=True, sparql_text="SELECT ?x WHERE { ?x a :C }"):
        metric_resolver = AsyncMock()
        metric_resolver.match.return_value = MetricMatch(found=False)

        # Build a mock strategy that either succeeds with tier2_result or fails
        mock_strategy = MagicMock()
        mock_strategy.name = StrategyOption.ONTOP

        if tier2_result is not None and tier2_result.query_result is not None:
            # Convert old Tier2Result to new StrategyResult for success case
            mock_strategy.resolve = AsyncMock(
                return_value=StrategyResult(
                    sql=tier2_result.vkg_result.sql if tier2_result.vkg_result else "",
                    rows=tier2_result.query_result.rows,
                    columns=tier2_result.query_result.columns,
                    confidence=0.85 if sparql_valid else 0.0,
                    strategy_name=StrategyOption.ONTOP,
                    trace_steps=[],
                    ontop_assembly=True,
                    sparql=sparql_text if sparql_valid else "",
                    row_count=tier2_result.query_result.row_count,
                    truncated=tier2_result.query_result.truncated,
                )
            )
        else:
            # Strategy returns None (failure) so orchestrator falls to Tier 3
            mock_strategy.resolve = AsyncMock(return_value=None)

        structured_query_tier = StructuredQueryTier(strategies=[mock_strategy])

        knowledge_retriever = AsyncMock()
        knowledge_retriever.lexical_enabled = False
        knowledge_retriever.resolve.return_value = Tier3Result(
            synthesized_answer="fallback answer",
            supporting_content=(),
            graph_context=(),
            confidence=0.5,
        )

        bedrock = AsyncMock()
        bedrock.embed.return_value = [0.1] * 10
        bedrock.model_id = "test-model"

        orch = Orchestrator(
            metric_resolver=metric_resolver,
            knowledge_retriever=knowledge_retriever,
            structured_query_tier=structured_query_tier,
            bedrock_client=bedrock,
        )
        # Preserve references for test assertions
        orch._nl_to_sparql = AsyncMock()
        orch._nl_to_sparql.translate = AsyncMock(
            return_value=TranslationResult(
                sparql=sparql_text if sparql_valid else None,
                confidence=0.85 if sparql_valid else 0.0,
                valid=sparql_valid,
                trace_steps=[],
            )
        )
        return orch

    @pytest.mark.asyncio
    async def test_logs_failure_reason_all_strategies_failed(self):
        """When all strategies return None, failure_reason='all_strategies_failed'."""
        orch = self._make_orchestrator()
        request = InvokeRequest(query="test", namespace="demo")

        with patch("coa_serve.orchestrator.logger") as mock_logger:
            await orch.resolve(request)
            # Find the tier2_fallback_decision log call
            fallback_calls = [c for c in mock_logger.info.call_args_list if c.args[0] == "tier2_fallback_decision"]
            assert len(fallback_calls) == 1
            kwargs = fallback_calls[0].kwargs
            assert kwargs["failure_reason"] == "all_strategies_failed"

    @pytest.mark.asyncio
    async def test_logs_failure_reason_when_sparql_invalid(self):
        """When strategy fails (sparql invalid path), fallback log is emitted."""
        orch = self._make_orchestrator(sparql_valid=False)
        request = InvokeRequest(query="test", namespace="demo")

        with patch("coa_serve.orchestrator.logger") as mock_logger:
            await orch.resolve(request)
            fallback_calls = [c for c in mock_logger.info.call_args_list if c.args[0] == "tier2_fallback_decision"]
            assert len(fallback_calls) == 1
            kwargs = fallback_calls[0].kwargs
            assert kwargs["failure_reason"] == "all_strategies_failed"

    @pytest.mark.asyncio
    async def test_tier2_low_confidence_accepted_when_db_succeeded(self):
        """When Tier 2 succeeds with DB rows, result is accepted regardless of confidence."""
        tier2_result = Tier2Result(
            query_result=QueryResult(rows=[{"x": 1}], columns=["x"], row_count=1, truncated=False),
            vkg_result=VKGResult(sql="SELECT x", dialect="athena", ontology_version="v1"),
            trace_steps=[],
        )
        orch = self._make_orchestrator(tier2_result=tier2_result, sparql_text="SELECT ?x WHERE { ?x a :C }")

        request = InvokeRequest(query="test", namespace="demo")

        with patch("coa_serve.orchestrator.logger") as mock_logger:
            response = await orch.resolve(request)
            # Result is returned as Tier 2 (not fallen through)
            assert response.result.tier == 2
            assert response.result.confidence.score == 0.85
            # Verify tier2_accepted log emitted
            accepted_calls = [c for c in mock_logger.info.call_args_list if c.args[0] == "tier2_accepted"]
            assert len(accepted_calls) == 1
            assert accepted_calls[0].kwargs["tier2_confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_fallback_log_includes_namespace(self):
        """tier2_fallback_decision log includes namespace for correlation."""
        orch = self._make_orchestrator()
        request = InvokeRequest(query="test", namespace="demo")

        with patch("coa_serve.orchestrator.logger") as mock_logger:
            await orch.resolve(request)
            fallback_calls = [c for c in mock_logger.info.call_args_list if c.args[0] == "tier2_fallback_decision"]
            assert len(fallback_calls) == 1
            kwargs = fallback_calls[0].kwargs
            assert kwargs["namespace"] == "demo"


# ═══════════════════════════════════════════════════════════════════════
# Item #6: SELECT variable specificity in system prompt
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestSelectVariablePrompt:
    """Verify that the NL→SPARQL system prompt includes SELECT specificity rules."""

    def test_system_prompt_includes_select_variable_rules(self):
        from coa_serve.tier2.ontop.nl_to_sparql import _SYSTEM_PROMPT

        assert "SELECT only the specific variables" in _SYSTEM_PROMPT
        assert "NOT the subject variable ?s" in _SYSTEM_PROMPT
        assert "named variables" in _SYSTEM_PROMPT

    def test_system_prompt_still_has_aggregation_rules(self):
        from coa_serve.tier2.ontop.nl_to_sparql import _SYSTEM_PROMPT

        assert "SUM(IF(" in _SYSTEM_PROMPT
        assert "NEVER use nested subqueries" in _SYSTEM_PROMPT

    def test_select_rules_come_after_examples(self):
        from coa_serve.tier2.ontop.nl_to_sparql import _SYSTEM_PROMPT

        select_pos = _SYSTEM_PROMPT.index("SELECT VARIABLE RULES")
        example_pos = _SYSTEM_PROMPT.index("Example 11")
        assert select_pos > example_pos


# ═══════════════════════════════════════════════════════════════════════
# Item #7: rdfs:range validation in SPARQL validator
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestRangeValidation:
    """Verify that sparql_validator checks rdfs:range for typed literals."""

    @pytest.mark.asyncio
    async def test_range_mismatch_returns_invalid(self):
        """A typed literal incompatible with declared range should fail validation.

        Uses xsd:string (not a subtype of anything in the hierarchy) so the
        XSD subtype loop finds no match.
        """
        client = AsyncMock()
        client.query.return_value = [{"found": "1"}]
        # Domain: pass (no domain declared)
        # Range: exact match fails, has_range=True, no XSD subtype matches
        client.ask.side_effect = [
            False,  # _is_domain_compatible: compat check
            False,  # _is_domain_compatible: has_domain check -> None (pass)
            False,  # _is_range_compatible: exact match check
            True,  # _is_range_compatible: has_range check -> range IS declared
            # XSD subtype loop: xsd:string is not in any _XSD_SUBTYPES values,
            # so no additional ASK calls are made → returns False (mismatch)
        ]
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX ind: <http://example.org/ontology#>
        SELECT ?name WHERE {
            ?p a ind:Product .
            ?p ind:product_price "expensive"^^<http://www.w3.org/2001/XMLSchema#string> .
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is False
        assert "range" in result.error.lower()

    @pytest.mark.asyncio
    async def test_range_compatible_passes(self):
        """A typed literal matching the declared range should pass."""
        client = AsyncMock()
        client.query.return_value = [{"found": "1"}]
        # Domain: pass (no domain)
        # Range: compatible (first ask=True)
        client.ask.side_effect = [
            False,  # domain compat
            False,  # has domain -> None (pass)
            True,  # range compat -> True (pass)
        ]
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX ind: <http://example.org/ontology#>
        SELECT ?name WHERE {
            ?p a ind:Product .
            ?p ind:product_price "100"^^<http://www.w3.org/2001/XMLSchema#decimal> .
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_no_range_declared_passes(self):
        """When no rdfs:range is declared, the check passes (fail-open)."""
        client = AsyncMock()
        client.query.return_value = [{"found": "1"}]
        # Domain: pass (no domain)
        # Range: exact match fails, has_range=False -> None (pass)
        client.ask.side_effect = [
            False,  # domain compat
            False,  # has domain -> None
            False,  # range exact match
            False,  # has range -> None (pass, no range declared)
        ]
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX ind: <http://example.org/ontology#>
        SELECT ?name WHERE {
            ?p a ind:Product .
            ?p ind:product_price "100"^^<http://www.w3.org/2001/XMLSchema#decimal> .
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_range_check_error_is_failopen(self):
        """Neptune errors during range check should pass (fail-open)."""
        client = AsyncMock()
        client.query.return_value = [{"found": "1"}]
        # Domain: pass
        # Range: Neptune error
        client.ask.side_effect = [
            False,  # domain compat
            False,  # has domain
            RuntimeError("Neptune timeout"),  # range check throws
        ]
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX ind: <http://example.org/ontology#>
        SELECT ?name WHERE {
            ?p a ind:Product .
            ?p ind:product_price "100"^^<http://www.w3.org/2001/XMLSchema#decimal> .
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_no_typed_literals_skips_range_check(self):
        """Queries without typed literals should skip range validation entirely."""
        client = AsyncMock()
        client.query.return_value = [{"found": "1"}]
        client.ask.return_value = False  # No domain declared -> pass domain check
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX ind: <http://example.org/ontology#>
        SELECT ?name WHERE {
            ?p a ind:Product .
            ?p ind:product_name ?name .
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is True

    def test_extract_property_literal_types_basic(self):
        """Extracts (property_uri, type_uri) from typed literals in triple patterns."""
        client = AsyncMock()
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX ind: <http://example.org/ontology#>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?p WHERE {
            ?p a ind:Product .
            ?p ind:price "100"^^xsd:decimal .
        }
        """
        pairs = validator._extract_property_literal_types(sparql)
        assert len(pairs) == 1
        assert pairs[0] == (
            "http://example.org/ontology#price",
            "http://www.w3.org/2001/XMLSchema#decimal",
        )

    def test_extract_property_literal_types_full_uri(self):
        """Handles full URI syntax (angle brackets) for both property and type."""
        client = AsyncMock()
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        SELECT ?p WHERE {
            ?p <http://example.org/ontology#score> "5"^^<http://www.w3.org/2001/XMLSchema#integer> .
        }
        """
        pairs = validator._extract_property_literal_types(sparql)
        assert len(pairs) == 1
        assert pairs[0][0] == "http://example.org/ontology#score"
        assert pairs[0][1] == "http://www.w3.org/2001/XMLSchema#integer"

    def test_extract_property_literal_types_no_typed_literals(self):
        """Returns empty list when no typed literals are present."""
        client = AsyncMock()
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX ind: <http://example.org/ontology#>
        SELECT ?name WHERE { ?p a ind:Product . ?p ind:name ?name . }
        """
        pairs = validator._extract_property_literal_types(sparql)
        assert pairs == []

    def test_extract_property_literal_types_skips_standard_prefixes(self):
        """Standard W3C vocabulary properties are excluded from range checking."""
        client = AsyncMock()
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?x WHERE { ?x rdfs:label "test"^^xsd:string . }
        """
        pairs = validator._extract_property_literal_types(sparql)
        assert pairs == []

    def test_extract_property_literal_types_handles_escaped_quotes(self):
        """Regex handles escaped quotes in literal values without breaking."""
        client = AsyncMock()
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = r"""
        PREFIX ind: <http://example.org/ontology#>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?p WHERE {
            ?p a ind:Product .
            ?p ind:description "say \"hello\""^^xsd:string .
        }
        """
        pairs = validator._extract_property_literal_types(sparql)
        assert len(pairs) == 1
        assert pairs[0][0] == "http://example.org/ontology#description"
        assert pairs[0][1] == "http://www.w3.org/2001/XMLSchema#string"

    @pytest.mark.asyncio
    async def test_xsd_integer_compatible_with_range_decimal(self):
        """xsd:integer literal should be compatible with rdfs:range xsd:decimal (subtype)."""
        client = AsyncMock()
        client.query.return_value = [{"found": "1"}]
        # Domain: pass (no domain)
        # Range: exact match for xsd:integer fails, has_range=True,
        # then XSD subtype loop checks xsd:decimal (supertype) → True
        client.ask.side_effect = [
            False,  # domain compat
            False,  # has domain -> None
            False,  # range exact match (xsd:integer != declared range)
            True,  # has range -> True (range IS declared)
            True,  # XSD subtype check: prop has range xsd:decimal? Yes → compatible
        ]
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        sparql = """
        PREFIX ind: <http://example.org/ontology#>
        SELECT ?p WHERE {
            ?p a ind:Product .
            ?p ind:product_price "100"^^<http://www.w3.org/2001/XMLSchema#integer> .
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is True

    def test_resolve_uri_token_shared_method(self):
        """_resolve_uri_token works for both full URIs and prefixed names."""
        client = AsyncMock()
        validator = SPARQLValidator(client, graph_uri_template="https://test.local/{namespace}")

        prefixes = {"ind": "http://example.org/ontology#", "xsd": "http://www.w3.org/2001/XMLSchema#"}
        assert validator._resolve_uri_token("http://example.org/x", prefixes) == "http://example.org/x"
        assert validator._resolve_uri_token("ind:Foo", prefixes) == "http://example.org/ontology#Foo"
        assert validator._resolve_uri_token("xsd:integer", prefixes) == "http://www.w3.org/2001/XMLSchema#integer"
        assert validator._resolve_uri_token("unknown:Bar", prefixes) is None
        assert validator._resolve_uri_token("nocolon", prefixes) is None


# ═══════════════════════════════════════════════════════════════════════
# Item #9: ObjectProperty (FK join paths) in T-Box context
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestObjectPropertyContext:
    """Verify that TBoxContextBuilder fetches and formats ObjectProperties."""

    @pytest.mark.asyncio
    async def test_fetch_object_properties_returns_results(self):
        """_fetch_object_properties should query Neptune for owl:ObjectProperty when >1 class."""
        client = AsyncMock()
        client.query.side_effect = [
            [],  # named-graph resolution: none found -> prefix-filter fallback
            # Full-context count query
            [{"cnt": "50"}],
            # Full-context CLASSES query (separate from properties now; 2 classes to trigger OP fetch)
            [
                {"class": "http://ex.org/ont#Order", "label": "Order", "parentClass": None},
                {"class": "http://ex.org/ont#Customer", "label": "Customer", "parentClass": None},
            ],
            # Full-context datatype-PROPERTIES query (separate; none here)
            [],
            # ObjectProperty fetch
            [
                {
                    "op": "http://ex.org/ont#order_customer",
                    "opLabel": "order_customer",
                    "domain": "http://ex.org/ont#Order",
                    "domainLabel": "Order",
                    "range": "http://ex.org/ont#Customer",
                    "rangeLabel": "Customer",
                }
            ],
            # aiContext glossary
            [],
        ]
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")
        context = await builder.build([], "demo", query="orders by customer")

        assert len(context.object_properties) == 1
        op = context.object_properties[0]
        assert op["uri"] == "http://ex.org/ont#order_customer"
        assert op["domain"] == "http://ex.org/ont#Order"
        assert op["range_class"] == "http://ex.org/ont#Customer"
        assert op["domain_label"] == "Order"
        assert op["range_label"] == "Customer"

    @pytest.mark.asyncio
    async def test_fetch_object_properties_graceful_on_error(self):
        """_fetch_object_properties should return [] on Neptune errors (fail-open)."""
        client = AsyncMock()
        call_count = [0]

        async def query_side(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return [{"cnt": "500"}]  # count (skip full context)
            if call_count[0] == 3:
                raise RuntimeError("Neptune timeout")  # object property fetch fails
            return []

        client.query.side_effect = query_side
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")
        context = await builder.build([], "demo", query="test")

        assert context.object_properties == []

    @pytest.mark.asyncio
    async def test_fetch_object_properties_skips_incomplete(self):
        """ObjectProperties missing domain or range are excluded."""
        client = AsyncMock()
        client.query.side_effect = [
            [],  # named-graph resolution: none found -> prefix-filter fallback
            [{"cnt": "50"}],
            # CLASSES query — 2 classes to trigger OP fetch
            [
                {"class": "http://ex.org/ont#X", "label": "X", "parentClass": None},
                {"class": "http://ex.org/ont#Y", "label": "Y", "parentClass": None},
            ],
            # datatype-PROPERTIES query (separate; none here)
            [],
            # ObjectProperty results: one complete, one missing range
            [
                {
                    "op": "http://ex.org/ont#complete",
                    "opLabel": "complete",
                    "domain": "http://ex.org/ont#A",
                    "domainLabel": "A",
                    "range": "http://ex.org/ont#B",
                    "rangeLabel": "B",
                },
                {
                    "op": "http://ex.org/ont#incomplete",
                    "opLabel": "incomplete",
                    "domain": "http://ex.org/ont#A",
                    "domainLabel": "A",
                    "range": None,
                    "rangeLabel": None,
                },
            ],
            [],
        ]
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")
        context = await builder.build([], "demo", query="test")

        assert len(context.object_properties) == 1
        assert context.object_properties[0]["uri"] == "http://ex.org/ont#complete"

    @pytest.mark.asyncio
    async def test_format_for_prompt_includes_join_paths(self):
        """format_for_prompt should include a 'Join paths' section for ObjectProperties."""
        client = AsyncMock()
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        context = TBoxContext(
            classes=[
                {"uri": "http://example.org/ontology#Order", "label": "Order", "parent": None},
                {"uri": "http://example.org/ontology#Customer", "label": "Customer", "parent": None},
            ],
            properties=[],
            object_properties=[
                {
                    "uri": "http://example.org/ontology#order_customerId",
                    "label": "order_customerId",
                    "domain": "http://example.org/ontology#Order",
                    "domain_label": "Order",
                    "range_class": "http://example.org/ontology#Customer",
                    "range_label": "Customer",
                },
            ],
        )

        text = builder.format_for_prompt(context, "demo")

        assert "Join paths" in text
        assert "ind:Order" in text
        assert "ind:Customer" in text
        assert "ind:order_customerId" in text
        assert "Order → Customer" in text

    @pytest.mark.asyncio
    async def test_format_for_prompt_no_join_paths_when_empty(self):
        """No 'Join paths' section when object_properties is empty."""
        client = AsyncMock()
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        context = TBoxContext(
            classes=[{"uri": "http://example.org/ontology#X", "label": "X", "parent": None}],
            properties=[],
            object_properties=[],
        )

        text = builder.format_for_prompt(context, "demo")
        assert "Join paths" not in text

    @pytest.mark.asyncio
    async def test_object_properties_in_build_output(self):
        """The build() method should populate object_properties when >1 class."""
        client = AsyncMock()
        client.query.side_effect = [
            [],  # named-graph resolution: none found -> prefix-filter fallback
            [{"cnt": "500"}],  # count (skip full context)
            # _fetch_by_entities class query — 2 classes to trigger OP fetch
            [
                {
                    "class": "http://ex.org/ont#X",
                    "label": "X",
                    "parentClass": None,
                },
                {
                    "class": "http://ex.org/ont#Y",
                    "label": "Y",
                    "parentClass": None,
                },
            ],
            # _fetch_by_entities datatype-property query (split out so property-row
            # volume cannot evict a matched class)
            [],
            # Object properties
            [
                {
                    "op": "http://ex.org/ont#xToY",
                    "opLabel": "xToY",
                    "domain": "http://ex.org/ont#X",
                    "domainLabel": "X",
                    "range": "http://ex.org/ont#Y",
                    "rangeLabel": "Y",
                }
            ],
            # aiContext
            [],
        ]
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")
        context = await builder.build([], "demo", query="how many X relate to Y")

        assert len(context.object_properties) == 1
        assert context.object_properties[0]["label"] == "xToY"

    @pytest.mark.asyncio
    async def test_object_properties_skipped_for_single_class(self):
        """ObjectProperty fetch is skipped when only 1 class to avoid unnecessary roundtrip."""
        from coa_serve.tier2.ontop.types import VectorHit as RVectorHit

        client = AsyncMock()
        client.query.side_effect = [
            [],  # named-graph resolution: none found -> prefix-filter fallback
            [{"cnt": "500"}],  # count (skip full context)
            # T-Box query from vector hit — only 1 class
            [
                {
                    "class": "http://ex.org/ont#Order",
                    "label": "Order",
                    "parentClass": None,
                    "property": None,
                    "propLabel": None,
                    "range": None,
                }
            ],
            # aiContext (vector hit has URI)
            [],
        ]
        hit = RVectorHit(type="ontology_class", score=0.9, entity_id="e1", uri="http://ex.org/ont#Order")
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")
        context = await builder.build([hit], "demo")

        assert context.object_properties == []
        # 4 queries: named-graph resolution + count + tbox + aiContext
        # (OP skipped: only 1 class)
        assert client.query.call_count == 4

    def test_token_estimate_includes_object_properties(self):
        """_estimate_tokens should count object_properties in the budget."""
        client = AsyncMock()
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        estimate = builder._estimate_tokens(
            [{"uri": "x", "label": "X"}] * 5,
            [{"uri": "y", "label": "Y", "domain": "x", "range": "z"}] * 10,
            [],
            [{"uri": "op", "label": "op", "domain": "x", "range_class": "y"}] * 4,
        )
        # 5*20 + 10*30 + 4*25 + 0*40 = 100 + 300 + 100 + 0 = 500
        assert estimate == 500
