# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for NeptuneAnalyticsLexicalStore implementation.

Tests use a mocked boto3 client to verify query construction,
response parsing, batching logic, and error handling without
requiring a live Neptune Analytics endpoint.

Requirements: 1.3, 2.1, 2.2, 2.4, 2.5, 2.6, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13
"""

import io
import json
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.stores.na_lexical import (
    NeptuneAnalyticsLexicalStore,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassRecord,
    ClassSchemaResult,
    EntityRecord,
    FactRecord,
    LexicalGraphStore,
    RelationRecord,
    TopicRecord,
)


def _make_payload(results: list[dict]) -> dict:
    """Create a mock execute_query response with a readable payload."""
    payload_bytes = json.dumps({"results": results}).encode()
    return {"payload": io.BytesIO(payload_bytes)}


def _make_store(mock_client: MagicMock) -> NeptuneAnalyticsLexicalStore:
    """Create a store instance with a mocked boto3 client."""
    return NeptuneAnalyticsLexicalStore(
        graph_id="g-test123",
        region="us-east-1",
        boto_client=mock_client,
    )


class TestNeptuneAnalyticsLexicalStoreInit:
    """Test constructor validation."""

    def test_requires_graph_id(self):
        """Should raise ValueError when graph_id is empty."""
        with pytest.raises(ValueError, match="graph_id is required"):
            NeptuneAnalyticsLexicalStore(graph_id="")

    def test_accepts_valid_graph_id(self):
        """Should construct successfully with a valid graph_id."""
        mock_client = MagicMock()
        store = NeptuneAnalyticsLexicalStore(
            graph_id="g-abc123",
            boto_client=mock_client,
        )
        assert store._graph_id == "g-abc123"

    def test_satisfies_protocol(self):
        """NeptuneAnalyticsLexicalStore should satisfy LexicalGraphStore protocol."""
        mock_client = MagicMock()
        store = NeptuneAnalyticsLexicalStore(
            graph_id="g-test",
            boto_client=mock_client,
        )
        assert isinstance(store, LexicalGraphStore)


class TestGetClassNodes:
    """Test get_class_nodes() method."""

    def test_returns_class_records(self):
        """Should parse __SYS_Class__ query results into ClassRecord list."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"value": "Person", "count": 150},
                {"value": "Company", "count": 80},
                {"value": "Location", "count": 45},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_class_nodes()

        assert len(result) == 3
        assert result[0] == ClassRecord(value="Person", count=150)
        assert result[1] == ClassRecord(value="Company", count=80)
        assert result[2] == ClassRecord(value="Location", count=45)

    def test_returns_empty_list_when_no_classes(self):
        """Should return empty list when no __SYS_Class__ nodes exist."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload([])

        store = _make_store(mock_client)
        result = store.get_class_nodes()

        assert result == []

    def test_skips_rows_with_null_value(self):
        """Should skip rows where value is None."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"value": "Person", "count": 10},
                {"value": None, "count": 5},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_class_nodes()

        assert len(result) == 1
        assert result[0].value == "Person"

    def test_handles_null_count_as_zero(self):
        """Should treat null count as 0."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"value": "Person", "count": None},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_class_nodes()

        assert result[0].count == 0


class TestGetClassRelations:
    """Test get_class_relations() method."""

    def test_returns_relation_records(self):
        """Should parse __SYS_RELATION__ query results into RelationRecord list."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {
                    "source_class": "Person",
                    "predicate": "works_for",
                    "target_class": "Company",
                    "count": 42,
                },
                {
                    "source_class": "Company",
                    "predicate": "located_in",
                    "target_class": "Location",
                    "count": 15,
                },
            ]
        )

        store = _make_store(mock_client)
        result = store.get_class_relations()

        assert len(result) == 2
        assert result[0] == RelationRecord(
            source_class="Person",
            predicate="works_for",
            target_class="Company",
            count=42,
        )

    def test_skips_rows_with_null_fields(self):
        """Should skip rows where required fields are None."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"source_class": "Person", "predicate": None, "target_class": "Company", "count": 5},
                {"source_class": "Person", "predicate": "knows", "target_class": "Person", "count": 10},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_class_relations()

        assert len(result) == 1
        assert result[0].predicate == "knows"


class TestGetEntitiesByClass:
    """Test get_entities_by_class() method."""

    def test_returns_entity_records(self):
        """Should parse __Entity__ query results into EntityRecord list."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"entity_id": "e1", "value": "John Smith", "classification": "Person"},
                {"entity_id": "e2", "value": "Jane Doe", "classification": "Person"},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_entities_by_class("Person", limit=10)

        assert len(result) == 2
        assert result[0] == EntityRecord(entity_id="e1", value="John Smith", classification="Person")

    def test_passes_classification_and_limit_as_params(self):
        """Should pass classification and limit as query parameters."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload([])

        store = _make_store(mock_client)
        store.get_entities_by_class("Company", limit=50)

        call_kwargs = mock_client.execute_query.call_args[1]
        assert call_kwargs["parameters"]["classification"] == "Company"
        assert call_kwargs["parameters"]["limit"] == 50

    def test_returns_empty_list_for_unknown_class(self):
        """Should return empty list when no entities match (Req 2.8)."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload([])

        store = _make_store(mock_client)
        result = store.get_entities_by_class("NonExistent")

        assert result == []

    def test_skips_rows_with_null_entity_id(self):
        """Should skip rows where entity_id is None."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"entity_id": None, "value": "Orphan", "classification": "Person"},
                {"entity_id": "e1", "value": "Valid", "classification": "Person"},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_entities_by_class("Person")

        assert len(result) == 1
        assert result[0].entity_id == "e1"


class TestGetFactsForEntities:
    """Test get_facts_for_entities() method."""

    def test_returns_spo_facts(self):
        """Should return SPO facts (object entity populated) with the
        predicate parsed out of ``fact_value``.
        """
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {
                    "subject_value": "John",
                    "subject_class": "Person",
                    "fact_value": "John WORKS FOR Acme Corp",
                    "object_value": "Acme Corp",
                    "object_class": "Company",
                },
            ]
        )

        store = _make_store(mock_client)
        result = store.get_facts_for_entities(["e1"])

        assert len(result) == 1
        assert result[0] == FactRecord(
            subject_value="John",
            subject_class="Person",
            predicate="WORKS FOR",
            object_value="Acme Corp",
            object_class="Company",
            complement=None,
        )

    def test_returns_spc_facts_with_parsed_complement(self):
        """SPC facts (no object entity) get their predicate and
        complement parsed out of ``fact_value`` via the
        uppercase-words heuristic.
        """
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {
                    "subject_value": "John",
                    "subject_class": "Person",
                    "fact_value": "John HAS AGE 35",
                    "object_value": None,
                    "object_class": None,
                },
            ]
        )

        store = _make_store(mock_client)
        result = store.get_facts_for_entities(["e1"])

        assert len(result) == 1
        assert result[0].object_value is None
        assert result[0].predicate == "HAS AGE"
        assert result[0].complement == "35"

    def test_excludes_facts_with_empty_fact_value(self):
        """Should exclude facts with empty/null ``fact_value`` (no
        predicate to parse).
        """
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {
                    "subject_value": "John",
                    "subject_class": "Person",
                    "fact_value": None,
                    "object_value": None,
                    "object_class": None,
                },
                {
                    "subject_value": "John",
                    "subject_class": "Person",
                    "fact_value": "John WORKS FOR Acme",
                    "object_value": "Acme",
                    "object_class": "Company",
                },
            ]
        )

        store = _make_store(mock_client)
        result = store.get_facts_for_entities(["e1"])

        assert len(result) == 1
        assert result[0].predicate == "WORKS FOR"

    def test_returns_empty_list_for_empty_input(self):
        """Should return empty list when no entity IDs provided."""
        mock_client = MagicMock()
        store = _make_store(mock_client)
        result = store.get_facts_for_entities([])

        assert result == []
        mock_client.execute_query.assert_not_called()

    def test_batches_at_500_ids(self):
        """Should batch queries when entity_ids exceeds 500 (Req 2.11)."""
        mock_client = MagicMock()
        mock_client.execute_query.side_effect = lambda **kwargs: _make_payload([])

        store = _make_store(mock_client)
        # Create 750 entity IDs — should result in 2 batches (500 + 250)
        entity_ids = [f"e{i}" for i in range(750)]
        store.get_facts_for_entities(entity_ids)

        assert mock_client.execute_query.call_count == 2

    def test_batches_exactly_500_in_single_call(self):
        """Should use a single call for exactly 500 IDs."""
        mock_client = MagicMock()
        mock_client.execute_query.side_effect = lambda **kwargs: _make_payload([])

        store = _make_store(mock_client)
        entity_ids = [f"e{i}" for i in range(500)]
        store.get_facts_for_entities(entity_ids)

        assert mock_client.execute_query.call_count == 1

    def test_batches_501_in_two_calls(self):
        """Should use two calls for 501 IDs."""
        mock_client = MagicMock()
        mock_client.execute_query.side_effect = lambda **kwargs: _make_payload([])

        store = _make_store(mock_client)
        entity_ids = [f"e{i}" for i in range(501)]
        store.get_facts_for_entities(entity_ids)

        assert mock_client.execute_query.call_count == 2


class TestGetTopics:
    """Test get_topics() method."""

    def test_returns_topic_records(self):
        """Should parse __Topic__ query results into TopicRecord list."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"topic_id": "t1", "value": "Finance", "statement_count": 25},
                {"topic_id": "t2", "value": "Technology", "statement_count": 10},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_topics()

        assert len(result) == 2
        assert result[0] == TopicRecord(topic_id="t1", value="Finance", statement_count=25)

    def test_includes_topics_with_zero_statements(self):
        """Should include topics with zero connected statements (Req 2.13)."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"topic_id": "t1", "value": "Empty Topic", "statement_count": 0},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_topics()

        assert len(result) == 1
        assert result[0].statement_count == 0

    def test_handles_null_statement_count(self):
        """Should treat null statement_count as 0."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload(
            [
                {"topic_id": "t1", "value": "Topic", "statement_count": None},
            ]
        )

        store = _make_store(mock_client)
        result = store.get_topics()

        assert result[0].statement_count == 0


class TestGetGraphSchema:
    """Test get_graph_schema() method."""

    def test_returns_class_schema_result(self):
        """Should return ClassSchemaResult with classes and relations."""
        mock_client = MagicMock()
        # First call: pg_schema via openCypher CALL (returns schema with nodeLabels/edgeLabels)
        # Second call: get_class_nodes query
        # Third call: get_class_relations query
        mock_client.execute_query.side_effect = [
            _make_payload(
                [
                    {
                        "schema": {
                            "nodeLabels": ["__SYS_Class__", "__Entity__", "__Fact__", "__Topic__"],
                            "edgeLabels": ["__SYS_RELATION__", "__SUBJECT__", "__OBJECT__"],
                            "nodeLabelDetails": {},
                            "edgeLabelDetails": {},
                            "labelTriples": [],
                        }
                    }
                ]
            ),  # pg_schema response
            _make_payload([{"value": "Person", "count": 100}]),  # class nodes
            _make_payload(
                [
                    {
                        "source_class": "Person",
                        "predicate": "knows",
                        "target_class": "Person",
                        "count": 50,
                    }
                ]
            ),  # relations
        ]

        store = _make_store(mock_client)
        result = store.get_graph_schema()

        assert isinstance(result, ClassSchemaResult)
        assert len(result.classes) == 1
        assert result.classes[0].value == "Person"
        assert len(result.relations) == 1
        assert result.relations[0].predicate == "knows"

    def test_raises_value_error_when_no_sys_class_label(self):
        """Should raise ValueError when graph lacks __SYS_Class__ nodes."""
        mock_client = MagicMock()
        mock_client.execute_query.side_effect = [
            _make_payload(
                [
                    {
                        "schema": {
                            "nodeLabels": ["Person", "Company"],
                            "edgeLabels": ["KNOWS"],
                            "nodeLabelDetails": {},
                            "edgeLabelDetails": {},
                            "labelTriples": [],
                        }
                    }
                ]
            ),
        ]

        store = _make_store(mock_client)

        with pytest.raises(ValueError, match="does not contain __SYS_Class__"):
            store.get_graph_schema()

    def test_raises_value_error_on_zero_classes(self):
        """Should raise ValueError when schema has zero class entries (Req 2.5)."""
        mock_client = MagicMock()
        mock_client.execute_query.side_effect = [
            _make_payload(
                [
                    {
                        "schema": {
                            "nodeLabels": ["__SYS_Class__", "__Entity__"],
                            "edgeLabels": ["__SYS_RELATION__"],
                            "nodeLabelDetails": {},
                            "edgeLabelDetails": {},
                            "labelTriples": [],
                        }
                    }
                ]
            ),  # pg_schema validates structure OK
            _make_payload([]),  # empty class nodes
            _make_payload([]),  # empty relations
        ]

        store = _make_store(mock_client)

        with pytest.raises(ValueError, match="zero class entries"):
            store.get_graph_schema()

    def test_raises_value_error_on_empty_pg_schema(self):
        """Should raise ValueError when pg_schema returns empty results."""
        mock_client = MagicMock()
        mock_client.execute_query.side_effect = [
            _make_payload([]),  # empty pg_schema response
        ]

        store = _make_store(mock_client)

        with pytest.raises(ValueError, match="empty results"):
            store.get_graph_schema()


class TestHealthCheck:
    """Test health_check() method."""

    def test_returns_healthy_on_success(self):
        """Should return healthy status when query succeeds."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload([{"ok": 1}])

        store = _make_store(mock_client)
        result = store.health_check()

        assert result == {"status": "healthy", "backend": "neptune_analytics"}

    def test_returns_unhealthy_on_exception(self):
        """Should return unhealthy status when query fails."""
        mock_client = MagicMock()
        mock_client.execute_query.side_effect = Exception("Connection refused")

        store = _make_store(mock_client)
        result = store.health_check()

        assert result == {"status": "unhealthy", "backend": "neptune_analytics"}

    def test_returns_unhealthy_on_empty_results(self):
        """Should return unhealthy when query returns no results."""
        mock_client = MagicMock()
        mock_client.execute_query.return_value = _make_payload([])

        store = _make_store(mock_client)
        result = store.health_check()

        assert result == {"status": "unhealthy", "backend": "neptune_analytics"}


class TestErrorHandling:
    """Test error handling and descriptive error messages."""

    def test_raises_runtime_error_on_client_error(self):
        """Should raise RuntimeError with descriptive message on ClientError (Req 2.4)."""
        mock_client = MagicMock()
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}}
        mock_client.execute_query.side_effect = ClientError(error_response, "ExecuteQuery")

        store = _make_store(mock_client)

        with pytest.raises(RuntimeError, match="backend=neptune_analytics") as exc_info:
            store.get_class_nodes()

        assert "Access denied" in str(exc_info.value)
        assert "g-test123" in str(exc_info.value)

    def test_retries_on_unprocessable_exception(self):
        """Should retry on UnprocessableException before failing."""
        mock_client = MagicMock()
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "UnprocessableException", "Message": "Retry me"}}
        # Fail twice, then succeed
        mock_client.execute_query.side_effect = [
            ClientError(error_response, "ExecuteQuery"),
            ClientError(error_response, "ExecuteQuery"),
            _make_payload([{"ok": 1}]),
        ]

        store = _make_store(mock_client)
        result = store._oc_query("RETURN 1 AS ok")

        assert result == [{"ok": 1}]
        assert mock_client.execute_query.call_count == 3

    def test_raises_after_max_retries(self):
        """Should raise RuntimeError after exhausting retries."""
        mock_client = MagicMock()
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "UnprocessableException", "Message": "Always fails"}}
        mock_client.execute_query.side_effect = ClientError(error_response, "ExecuteQuery")

        store = _make_store(mock_client)

        with pytest.raises(RuntimeError, match="backend=neptune_analytics"):
            store._oc_query("RETURN 1 AS ok")


# ── Fact value parsing helpers ─────────────────────────────────────────


from coa_ontology.inducer.unstructured.stores.na_lexical import (  # noqa: E402
    _extract_spc_predicate_complement,
    _extract_spo_predicate,
    _is_caseless_predicate_word,
    _is_predicate_word,
    _strip_object_suffix,
    _strip_subject_prefix,
)


class TestStripSubjectPrefix:
    """``_strip_subject_prefix`` does case-insensitive prefix removal."""

    def test_exact_case_match(self):
        assert _strip_subject_prefix("company AUDITS Apple", "company") == "AUDITS Apple"

    def test_case_insensitive_match(self):
        # The lexical graph mixes "company" / "Company".
        assert _strip_subject_prefix("Company AUDITS Apple", "company") == "AUDITS Apple"
        assert _strip_subject_prefix("company AUDITS Apple", "Company") == "AUDITS Apple"

    def test_multiword_subject(self):
        assert (
            _strip_subject_prefix(
                "Internal Revenue Service AUDITS Company",
                "Internal Revenue Service",
            )
            == "AUDITS Company"
        )

    def test_no_match_returns_unchanged(self):
        # Defensive: extraction drift means subject isn't always at
        # the start.
        assert _strip_subject_prefix("PAID Company", "Awardee") == "PAID Company"

    def test_strips_following_whitespace(self):
        assert _strip_subject_prefix("company    AUDITS", "company") == "AUDITS"


class TestStripObjectSuffix:
    """``_strip_object_suffix`` does case-insensitive suffix removal."""

    def test_exact_case_match(self):
        assert _strip_object_suffix("AUDITS Apple", "Apple") == "AUDITS"

    def test_case_insensitive_match(self):
        assert _strip_object_suffix("EMPLOYED BY Company", "company") == "EMPLOYED BY"

    def test_no_match_returns_unchanged(self):
        assert _strip_object_suffix("AUDITS Apple", "Microsoft") == "AUDITS Apple"

    def test_strips_preceding_whitespace(self):
        assert _strip_object_suffix("AUDITS    Apple", "Apple") == "AUDITS"


class TestIsPredicateWord:
    """``_is_predicate_word`` distinguishes predicate phrase tokens
    (UPPERCASE content words and curated connectors) from complement
    tokens (lowercase, mixed case, pure numbers)."""

    @pytest.mark.parametrize(
        "tok",
        [
            "AUDITS",
            "PAYS",
            "EMPLOYED",
            "PARTICIPATES_IN",
            "INDICATOR",
            "RESPONSIBLE",
            "RESTRICTED",
            "TIME",
            "PERIOD",
            "SIGNIFICANCE",
        ],
    )
    def test_uppercase_content_words_qualify(self, tok):
        assert _is_predicate_word(tok)

    @pytest.mark.parametrize(
        "tok",
        ["OF", "IN", "ON", "TO", "BY", "FOR", "WITH", "AS", "AT"],
    )
    def test_curated_connectors_qualify(self, tok):
        assert _is_predicate_word(tok)

    @pytest.mark.parametrize(
        "tok",
        [
            "company",  # lowercase
            "Apple",  # mixed case (proper noun)
            "first",  # lowercase
            "fiscal",  # lowercase
            "2024",  # pure numeric
            "April",  # mixed case
            "the",  # lowercase
        ],
    )
    def test_complement_tokens_do_not_qualify(self, tok):
        assert not _is_predicate_word(tok)

    def test_empty_string(self):
        assert not _is_predicate_word("")

    def test_punctuation_only(self):
        assert not _is_predicate_word("...")

    def test_punctuation_stripped_around(self):
        # Trailing punctuation doesn't disqualify the token.
        assert _is_predicate_word("AUDITS,")
        assert _is_predicate_word("(EMPLOYED)")

    def test_pure_numeric_does_not_qualify(self):
        assert not _is_predicate_word("123")


class TestExtractSpoPredicate:
    """``_extract_spo_predicate`` recovers the predicate phrase from
    SPO fact values."""

    def test_basic_single_word_predicate(self):
        assert (
            _extract_spo_predicate(
                "Awardee EMPLOYED BY Company",
                "Awardee",
                "company",
            )
            == "EMPLOYED BY"
        )

    def test_single_word_predicate(self):
        assert (
            _extract_spo_predicate(
                "Internal Revenue Service AUDITS Company",
                "Internal Revenue Service",
                "company",
            )
            == "AUDITS"
        )

    def test_multiword_predicate(self):
        assert (
            _extract_spo_predicate(
                "Gaming GPUs POTENTIAL IMPACT ON DEMAND FOR company",
                "Gaming GPUs",
                "company",
            )
            == "POTENTIAL IMPACT ON DEMAND FOR"
        )

    def test_subject_case_drift(self):
        """Subject as ``company`` in the fact, ``Company`` in the
        entity record (or vice versa) — case-insensitive match handles
        this."""
        assert _extract_spo_predicate("Company AUDITS Apple", "company", "Apple") == "AUDITS"

    def test_object_case_drift(self):
        assert (
            _extract_spo_predicate(
                "Awardee EMPLOYED BY Company",
                "Awardee",
                "Company",  # exact case
            )
            == "EMPLOYED BY"
        )

    def test_falls_back_when_subject_doesnt_match(self):
        """If the subject prefix doesn't match (extraction drift),
        return the input unchanged minus the object suffix.
        """
        # No clean fall back to do — predicate ends up including
        # whatever was at the front of fact_value.
        assert _extract_spo_predicate("PAID Company", "Awardee", "Company") == "PAID"


class TestExtractSpcPredicateComplement:
    """``_extract_spc_predicate_complement`` splits SPC fact values
    into a predicate phrase and a complement using the
    "leading uppercase tokens" heuristic."""

    def test_single_word_predicate_with_complement(self):
        assert _extract_spc_predicate_complement(
            "downstream users RESTRICTED ON resale",
            "downstream users",
        ) == ("RESTRICTED ON", "resale")

    def test_multiword_predicate_with_complement(self):
        assert _extract_spc_predicate_complement(
            "Short-term lease expenses TIME PERIOD first half of fiscal year 2024",
            "Short-term lease expenses",
        ) == (
            "TIME PERIOD",
            "first half of fiscal year 2024",
        )

    def test_predicate_with_connectors(self):
        assert _extract_spc_predicate_complement(
            "company INDICATOR OF IMPAIRMENT decline in stock price",
            "company",
        ) == (
            "INDICATOR OF IMPAIRMENT",
            "decline in stock price",
        )

    def test_predicate_with_descriptive_modifier(self):
        # "DESCRIPTION" alone — single-token predicate, complement
        # follows.
        assert _extract_spc_predicate_complement(
            "company DESCRIPTION leading global software and device maker",
            "company",
        ) == (
            "DESCRIPTION",
            "leading global software and device maker",
        )

    def test_subject_case_drift(self):
        # Subject "Company" in fact, "company" in record.
        assert _extract_spc_predicate_complement(
            "Company HAS ALLOWANCE income tax contingencies",
            "company",
        ) == (
            "HAS ALLOWANCE",
            "income tax contingencies",
        )

    def test_predicate_only_no_complement_returns_none(self):
        """If every token is a predicate word, the complement is
        None (Rule 4 will skip the fact)."""
        assert _extract_spc_predicate_complement(
            "Awardee ACKNOWLEDGES",
            "Awardee",
        ) == ("ACKNOWLEDGES", None)

    def test_empty_fact_value_after_subject_strip(self):
        assert _extract_spc_predicate_complement("Awardee", "Awardee") == ("", None)

    def test_complement_starting_with_number(self):
        assert _extract_spc_predicate_complement(
            "Apple FOUNDED 1976",
            "Apple",
        ) == ("FOUNDED", "1976")

    def test_complement_with_mixed_case_in_middle(self):
        assert _extract_spc_predicate_complement(
            "company RISK orders to stop noncompliant activity",
            "company",
        ) == (
            "RISK",
            "orders to stop noncompliant activity",
        )

    def test_real_dev_graph_examples(self):
        """End-to-end examples copied verbatim from the dev graph
        probe — tests that the heuristic handles the actual data
        we have."""
        cases = [
            (
                "Short-term lease expenses TIME PERIOD first half of fiscal year 2024",
                "Short-term lease expenses",
                ("TIME PERIOD", "first half of fiscal year 2024"),
            ),
            (
                "Short-term lease expenses SIGNIFICANCE not significant",
                "Short-term lease expenses",
                ("SIGNIFICANCE", "not significant"),
            ),
            (
                "downstream users RESTRICTED ON resale",
                "downstream users",
                ("RESTRICTED ON", "resale"),
            ),
            (
                "industry security expectations DESCRIBED BY evolving",
                "industry security expectations",
                ("DESCRIBED BY", "evolving"),
            ),
            (
                "company RESPONSIBLE FOR hardware defects",
                "company",
                ("RESPONSIBLE FOR", "hardware defects"),
            ),
            (
                "company RISK significant expenses from safety alerts",
                "company",
                ("RISK", "significant expenses from safety alerts"),
            ),
            (
                "Company HAS ALLOWANCE income tax contingencies",
                "company",
                ("HAS ALLOWANCE", "income tax contingencies"),
            ),
            (
                "Company ALLOWANCE STATUS adequate",
                "company",
                ("ALLOWANCE STATUS", "adequate"),
            ),
            (
                "company INDICATOR OF IMPAIRMENT decline in stock price and market capitalization",
                "company",
                (
                    "INDICATOR OF IMPAIRMENT",
                    "decline in stock price and market capitalization",
                ),
            ),
        ]
        for fact_value, subject, expected in cases:
            result = _extract_spc_predicate_complement(fact_value, subject)
            assert result == expected, (
                f"Mismatch for {fact_value!r} (subject={subject!r}):\n  expected: {expected}\n  actual:   {result}"
            )


class TestCaselessPredicates:
    """Predicates in a script without case (Japanese, Chinese, Korean).

    The A1 extraction prompt tells the model to write predicate names in the
    namespace language, uppercase only where letters have case and an
    underscore where the name would contain a space. Before this fix every
    such SPC fact parsed to an empty predicate and Rule 4 dropped it, so a
    Japanese document produced no datatype properties.
    """

    @pytest.mark.parametrize(
        ("fact_value", "subject", "expected"),
        [
            ("プレス機 KX-7 点検周期 30日", "プレス機 KX-7", ("点検周期", "30日")),
            ("設備 担当者 田中", "設備", ("担当者", "田中")),
            ("プレス機 KX-7 設置場所 第二工場", "プレス機 KX-7", ("設置場所", "第二工場")),
            ("プレス機 KX-7 点検_周期 30 日", "プレス機 KX-7", ("点検_周期", "30 日")),
            ("压力机 安装位置 第二工厂", "压力机", ("安装位置", "第二工厂")),
            ("프레스기 설치_장소 제2공장", "프레스기", ("설치_장소", "제2공장")),
            # Full-width punctuation around the predicate is not part of it.
            ("設備 「担当者」 田中", "設備", ("担当者", "田中")),
        ],
    )
    def test_caseless_predicate_is_the_first_token(self, fact_value, subject, expected):
        assert _extract_spc_predicate_complement(fact_value, subject) == expected

    def test_caseless_predicate_without_complement(self):
        # Same contract as an English predicate-only fact: Rule 4 skips it.
        assert _extract_spc_predicate_complement("設備 稼働中", "設備") == ("稼働中", None)

    def test_uppercase_latin_predicate_after_japanese_subject_is_unchanged(self):
        assert _extract_spc_predicate_complement("プレス機 KX-7 INSPECTION_CYCLE 30日", "プレス機 KX-7") == (
            "INSPECTION_CYCLE",
            "30日",
        )

    def test_quantity_is_not_a_predicate(self):
        # Starts with a digit, so it is the complement, not a predicate.
        assert _extract_spc_predicate_complement("設備 30日", "設備") == ("", "30日")

    def test_english_complement_after_uppercase_predicate_is_unchanged(self):
        # A caseless token only starts a predicate; after an uppercase
        # predicate it is complement as before.
        assert _extract_spc_predicate_complement("company LOCATION 東京", "company") == ("LOCATION", "東京")

    @pytest.mark.parametrize(
        ("fact_value", "expected"),
        [
            ("設備 担当者A 田中", ("担当者A", "田中")),
            # A caseless token after it is complement, as after any uppercase predicate.
            ("設備 担当者A 点検 田中", ("担当者A", "点検 田中")),
            # A further uppercase token joins the predicate, as in English.
            ("設備 担当者A B 田中", ("担当者A B", "田中")),
        ],
    )
    def test_caseless_with_uppercase_latin_suffix_is_uppercase_predicate(self, fact_value, expected):
        # ``"担当者A".isupper()`` is True because ``A`` is its only cased
        # letter, so the token keeps the existing uppercase rule. This MR does
        # not change how such a token parses.
        assert _is_caseless_predicate_word("担当者A") is False
        assert _extract_spc_predicate_complement(fact_value, "設備") == expected

    @pytest.mark.parametrize(
        ("tok", "expected"),
        [
            ("点検周期", True),
            ("担当者", True),
            ("点検_周期", True),
            ("설치_장소", True),
            ("安装位置", True),
            ("「担当者」", True),
            ("30日", False),
            ("KX-7", False),
            ("Company", False),
            ("AUDITS", False),
            ("", False),
            ("、。", False),
        ],
    )
    def test_is_caseless_predicate_word(self, tok, expected):
        assert _is_caseless_predicate_word(tok) is expected

    def test_japanese_spc_fact_yields_a_datatype_property(self):
        """End to end through Rule 4: the parsed fact becomes a Japanese property."""
        from coa_ontology.inducer.unstructured.services.induction_rules import induce_datatype_properties
        from coa_ontology.inducer.unstructured.services.iri_resolver import InMemoryIRIResolver
        from coa_ontology.inducer.unstructured.stores.protocol import FactRecord
        from rdflib import OWL, RDF, RDFS, Literal

        predicate, complement = _extract_spc_predicate_complement("プレス機 KX-7 点検周期 30日", "プレス機 KX-7")
        fact = FactRecord(
            subject_value="プレス機 KX-7",
            subject_class="設備",
            predicate=predicate,
            object_value=None,
            object_class=None,
            complement=complement,
        )
        resolver = InMemoryIRIResolver(namespace="n1", ontology_name="unstructured")
        graph = induce_datatype_properties(
            facts=[fact],
            class_iris={"設備": resolver.resolve("設備", "class")},
            iri_resolver=resolver,
            namespace_language="ja",
        )
        properties = list(graph.subjects(RDF.type, OWL.DatatypeProperty))
        assert len(properties) == 1, graph.serialize(format="turtle")
        labels = list(graph.objects(properties[0], RDFS.label))
        assert Literal("点検周期", lang="ja") in labels, labels
