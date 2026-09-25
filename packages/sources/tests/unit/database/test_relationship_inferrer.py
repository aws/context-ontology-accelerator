# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Pass 2 cross-table FK inference (relationship_inferrer)."""

from __future__ import annotations

from unittest.mock import MagicMock

from coa_common.bedrock import BedrockTruncationError
from coa_common.domain_models import (
    Column,
    EnrichmentSource,
    ForeignKey,
    Table,
)
from coa_sources.database.enrichment.relationship_inferrer import (
    TABLES_PER_BATCH,
    _deduplicate,
    apply_inferred_relationships,
    infer_relationships,
)


def _make_table(name: str, columns: list[str] | None = None) -> Table:
    cols = columns or ["id", "name"]
    return Table(
        name=name,
        database="db",
        columns=[Column(name=c, data_type="INTEGER") for c in cols],
    )


class TestDeterministicFKTakesPrecedence:
    """Deterministic FKs from #25 block AI-inferred candidates for the same column."""

    def test_deterministic_fk_blocks_ai_candidate(self) -> None:
        table = _make_table("orders", ["id", "customer_id"])
        table.foreign_keys = [
            ForeignKey(
                column="customer_id",
                target_table="customers",
                target_column="id",
                source=EnrichmentSource.DETERMINISTIC,
                confidence=1.0,
            )
        ]

        candidates = [
            {
                "source_table": "orders",
                "column": "customer_id",
                "target_table": "clients",
                "target_column": "id",
                "confidence": 0.9,
            }
        ]

        count = apply_inferred_relationships([table], candidates)

        assert count == 0
        assert len(table.foreign_keys) == 1
        assert table.foreign_keys[0].target_table == "customers"

    def test_steward_specified_fk_blocks_ai_candidate(self) -> None:
        table = _make_table("orders", ["id", "product_id"])
        table.foreign_keys = [
            ForeignKey(
                column="product_id",
                target_table="products",
                target_column="id",
                source=EnrichmentSource.STEWARD_SPECIFIED,
                confidence=1.0,
            )
        ]

        candidates = [
            {
                "source_table": "orders",
                "column": "product_id",
                "target_table": "items",
                "target_column": "item_id",
                "confidence": 0.85,
            }
        ]

        count = apply_inferred_relationships([table], candidates)

        assert count == 0
        assert len(table.foreign_keys) == 1

    def test_different_column_not_blocked(self) -> None:
        table = _make_table("orders", ["id", "customer_id", "product_id"])
        table.foreign_keys = [
            ForeignKey(
                column="customer_id",
                target_table="customers",
                target_column="id",
                source=EnrichmentSource.DETERMINISTIC,
                confidence=1.0,
            )
        ]
        products_table = _make_table("products", ["id", "name"])

        candidates = [
            {
                "source_table": "orders",
                "column": "product_id",
                "target_table": "products",
                "target_column": "id",
                "confidence": 0.92,
            }
        ]

        count = apply_inferred_relationships([table, products_table], candidates)

        assert count == 1
        assert len(table.foreign_keys) == 2
        assert table.foreign_keys[1].column == "product_id"
        assert table.foreign_keys[1].source == EnrichmentSource.AI_INFERRED

    def test_ai_inferred_existing_does_not_block(self) -> None:
        table = _make_table("orders", ["id", "cat_id"])
        table.foreign_keys = [
            ForeignKey(
                column="cat_id",
                target_table="old_categories",
                target_column="id",
                source=EnrichmentSource.AI_INFERRED,
                confidence=0.5,
            )
        ]
        categories_table = _make_table("categories", ["id", "name"])

        candidates = [
            {
                "source_table": "orders",
                "column": "cat_id",
                "target_table": "categories",
                "target_column": "id",
                "confidence": 0.95,
            }
        ]

        count = apply_inferred_relationships([table, categories_table], candidates)

        assert count == 1


class TestCrossBatchMergeDedupes:
    """Cross-batch merge deduplicates FK candidates."""

    def test_duplicate_candidates_keep_highest_confidence(self) -> None:
        dupes = [
            {
                "source_table": "orders",
                "column": "product_id",
                "target_table": "products",
                "target_column": "id",
                "confidence": 0.7,
            },
            {
                "source_table": "orders",
                "column": "product_id",
                "target_table": "products",
                "target_column": "id",
                "confidence": 0.95,
            },
        ]

        result = _deduplicate(dupes)

        assert len(result) == 1
        assert result[0]["confidence"] == 0.95

    def test_different_fks_not_deduped(self) -> None:
        candidates = [
            {
                "source_table": "orders",
                "column": "customer_id",
                "target_table": "customers",
                "target_column": "id",
                "confidence": 0.9,
            },
            {
                "source_table": "orders",
                "column": "product_id",
                "target_table": "products",
                "target_column": "id",
                "confidence": 0.85,
            },
        ]

        result = _deduplicate(candidates)

        assert len(result) == 2

    def test_large_source_deduplicates_across_batches(self) -> None:
        mock_client = MagicMock()
        from coa_common.bedrock import BedrockInvocationResult

        mock_client.invoke.return_value = BedrockInvocationResult(
            result=[
                {
                    "source_table": "t0",
                    "column": "fk_col",
                    "target_table": "t1",
                    "target_column": "id",
                    "confidence": 0.8,
                }
            ],
            input_tokens=50,
            output_tokens=30,
            latency_ms=100.0,
        )

        tables = [_make_table(f"t{i}", ["id", "fk_col"]) for i in range(600)]
        result = infer_relationships(tables, mock_client, emitter=MagicMock())

        # Same FK returned from all 3 batches → deduplicated to 1
        assert len(result) == 1
        assert result[0]["confidence"] == 0.8


class TestEmptySchemaShortCircuits:
    """Empty schema short-circuits without a Bedrock call."""

    def test_empty_tables_no_bedrock_call(self) -> None:
        mock_client = MagicMock()

        result = infer_relationships([], mock_client, emitter=MagicMock())

        assert result == []
        mock_client.invoke.assert_not_called()

    def test_apply_empty_candidates_returns_zero(self) -> None:
        tables = [_make_table("t1")]

        count = apply_inferred_relationships(tables, [])

        assert count == 0


class TestLargeSchemaChunking:
    """Large-schema chunking splits into batches of TABLES_PER_BATCH."""

    def test_tables_per_batch_is_250(self) -> None:
        assert TABLES_PER_BATCH == 250

    def test_small_source_single_call(self) -> None:
        from coa_common.bedrock import BedrockInvocationResult

        mock_client = MagicMock()
        mock_client.invoke.return_value = BedrockInvocationResult(
            result=[], input_tokens=50, output_tokens=10, latency_ms=100.0
        )

        tables = [_make_table(f"t{i}") for i in range(100)]
        infer_relationships(tables, mock_client, emitter=MagicMock())

        assert mock_client.invoke.call_count == 1

    def test_large_source_multiple_batches(self) -> None:
        from coa_common.bedrock import BedrockInvocationResult

        mock_client = MagicMock()
        mock_client.invoke.return_value = BedrockInvocationResult(
            result=[], input_tokens=50, output_tokens=10, latency_ms=100.0
        )

        tables = [_make_table(f"t{i}") for i in range(600)]
        infer_relationships(tables, mock_client, emitter=MagicMock())

        # 600 / 250 = 2.4 → 3 batches
        assert mock_client.invoke.call_count == 3

    def test_exact_boundary_single_call(self) -> None:
        from coa_common.bedrock import BedrockInvocationResult

        mock_client = MagicMock()
        mock_client.invoke.return_value = BedrockInvocationResult(
            result=[], input_tokens=50, output_tokens=10, latency_ms=100.0
        )

        tables = [_make_table(f"t{i}") for i in range(250)]
        infer_relationships(tables, mock_client, emitter=MagicMock())

        assert mock_client.invoke.call_count == 1

    def test_one_over_boundary_two_calls(self) -> None:
        from coa_common.bedrock import BedrockInvocationResult

        mock_client = MagicMock()
        mock_client.invoke.return_value = BedrockInvocationResult(
            result=[], input_tokens=50, output_tokens=10, latency_ms=100.0
        )

        tables = [_make_table(f"t{i}") for i in range(251)]
        infer_relationships(tables, mock_client, emitter=MagicMock())

        assert mock_client.invoke.call_count == 2


class TestInferBatchErrorPaths:
    """Verify _infer_batch handles errors and non-list responses."""

    def test_truncation_returns_empty_and_preserves_typed_error(self, caplog) -> None:
        truncation = BedrockTruncationError(model_id="test-model", output_tokens=8192, max_tokens=8192)
        mock_client = MagicMock()
        mock_client.invoke.side_effect = truncation
        mock_emitter = MagicMock()

        result = infer_relationships([_make_table("t1")], mock_client, emitter=mock_emitter)

        assert result == []
        mock_emitter.emit_bedrock_invocation_error.assert_called_once_with(stage="Pass2", exc=truncation)
        assert "Pass 2 batch truncated" in caplog.text
        assert "requested_max_tokens=8192" in caplog.text

    def test_bedrock_exception_returns_empty_and_emits_error(self) -> None:
        mock_client = MagicMock()
        mock_client.invoke.side_effect = Exception("Bedrock timeout")
        mock_emitter = MagicMock()

        tables = [_make_table("t1")]
        result = infer_relationships(tables, mock_client, emitter=mock_emitter)

        assert result == []
        mock_emitter.emit_bedrock_invocation_error.assert_called_once()
        call_kwargs = mock_emitter.emit_bedrock_invocation_error.call_args[1]
        assert call_kwargs["stage"] == "Pass2"

    def test_non_list_response_returns_empty(self) -> None:
        from coa_common.bedrock import BedrockInvocationResult

        mock_client = MagicMock()
        mock_client.invoke.return_value = BedrockInvocationResult(
            result={"unexpected": "dict response"},
            input_tokens=50,
            output_tokens=10,
            latency_ms=100.0,
        )

        tables = [_make_table("t1")]
        result = infer_relationships(tables, mock_client, emitter=MagicMock())

        assert result == []


class TestApplyInferredRelationshipsSkipPaths:
    """Verify candidates are skipped when column/target invalid."""

    def test_candidate_skipped_when_column_not_in_table(self) -> None:
        table = _make_table("orders", ["id", "name"])
        other = _make_table("products", ["id", "name"])

        candidates = [
            {
                "source_table": "orders",
                "column": "nonexistent_col",
                "target_table": "products",
                "target_column": "id",
                "confidence": 0.9,
            }
        ]

        count = apply_inferred_relationships([table, other], candidates)

        assert count == 0
        assert len(table.foreign_keys) == 0

    def test_candidate_skipped_when_target_table_not_in_map(self) -> None:
        table = _make_table("orders", ["id", "customer_id"])

        candidates = [
            {
                "source_table": "orders",
                "column": "customer_id",
                "target_table": "nonexistent_table",
                "target_column": "id",
                "confidence": 0.9,
            }
        ]

        count = apply_inferred_relationships([table], candidates)

        assert count == 0
        assert len(table.foreign_keys) == 0

    def test_candidate_skipped_when_source_table_not_in_map(self) -> None:
        table = _make_table("orders", ["id", "name"])

        candidates = [
            {
                "source_table": "unknown_table",
                "column": "id",
                "target_table": "orders",
                "target_column": "id",
                "confidence": 0.8,
            }
        ]

        count = apply_inferred_relationships([table], candidates)

        assert count == 0

    def test_candidate_skipped_when_protected_fk_exists_on_column(self) -> None:
        """Candidate dropped when target table exists but column has a protected FK."""
        orders = _make_table("orders", ["id", "customer_id"])
        customers = _make_table("customers", ["id", "name"])
        orders.foreign_keys = [
            ForeignKey(
                column="customer_id",
                target_table="customers",
                target_column="id",
                source=EnrichmentSource.DETERMINISTIC,
                confidence=1.0,
            )
        ]

        candidates = [
            {
                "source_table": "orders",
                "column": "customer_id",
                "target_table": "customers",
                "target_column": "cust_id",
                "confidence": 0.85,
            }
        ]

        count = apply_inferred_relationships([orders, customers], candidates)

        assert count == 0
        assert len(orders.foreign_keys) == 1
        assert orders.foreign_keys[0].target_column == "id"


class TestCrossSchemaCollision:
    """Two schemas sharing a table name must NOT collapse to one Table."""

    def test_same_name_different_schema_resolved_by_qualified_id(self) -> None:
        # A single source scanning schemaFilter 'public|sales' discovers two
        # tables both named 'orders'. Candidates must be schema-qualified and
        # each FK must attach to the correct Table, not last-wins.
        public_orders = Table(
            name="orders",
            database="public",
            columns=[Column(name="id", data_type="INTEGER"), Column(name="customer_id", data_type="INTEGER")],
        )
        sales_orders = Table(
            name="orders",
            database="sales",
            columns=[Column(name="id", data_type="INTEGER"), Column(name="rep_id", data_type="INTEGER")],
        )
        customers = _make_table("customers", ["id", "name"])
        reps = _make_table("reps", ["id", "name"])
        tables = [public_orders, sales_orders, customers, reps]

        # LLM sees qualified names (public.orders / sales.orders) in the prompt.
        candidates = [
            {
                "source_table": "public.orders",
                "column": "customer_id",
                "target_table": "customers",
                "target_column": "id",
                "confidence": 0.9,
            },
            {
                "source_table": "sales.orders",
                "column": "rep_id",
                "target_table": "reps",
                "target_column": "id",
                "confidence": 0.9,
            },
        ]

        count = apply_inferred_relationships(tables, candidates)

        assert count == 2
        # Each FK landed on the correct schema's Table — no collision/loss.
        assert [fk.column for fk in public_orders.foreign_keys] == ["customer_id"]
        assert public_orders.foreign_keys[0].target_table == "customers"
        assert [fk.column for fk in sales_orders.foreign_keys] == ["rep_id"]
        assert sales_orders.foreign_keys[0].target_table == "reps"

    def test_unique_name_still_uses_bare_name(self) -> None:
        # When names are unique in the batch, the bare name is used (unchanged
        # behavior) so a candidate keyed by bare name still resolves.
        orders = _make_table("orders", ["id", "customer_id"])
        customers = _make_table("customers", ["id", "name"])
        candidates = [
            {
                "source_table": "orders",
                "column": "customer_id",
                "target_table": "customers",
                "target_column": "id",
                "confidence": 0.9,
            }
        ]
        assert apply_inferred_relationships([orders, customers], candidates) == 1
        assert orders.foreign_keys[0].target_table == "customers"


class TestReScanIdempotency:
    """A re-scan must not append a duplicate AI_INFERRED FK."""

    def test_identical_inferred_fk_not_reappended(self) -> None:
        # Run 1 inferred orders.customer_id -> customers.id and it was persisted;
        # a re-scan reads it back and re-infers the SAME FK. It must be skipped,
        # not appended again.
        orders = _make_table("orders", ["id", "customer_id"])
        customers = _make_table("customers", ["id", "name"])
        orders.foreign_keys = [
            ForeignKey(
                column="customer_id",
                target_table="customers",
                target_column="id",
                source=EnrichmentSource.AI_INFERRED,
                confidence=0.9,
            )
        ]
        candidates = [
            {
                "source_table": "orders",
                "column": "customer_id",
                "target_table": "customers",
                "target_column": "id",
                "confidence": 0.9,
            }
        ]

        count = apply_inferred_relationships([orders, customers], candidates)

        assert count == 0
        assert len(orders.foreign_keys) == 1  # not duplicated

    def test_inferred_fk_to_different_target_still_added(self) -> None:
        # Same column but a genuinely different target is NOT a duplicate.
        orders = _make_table("orders", ["id", "cat_id"])
        categories = _make_table("categories", ["id", "name"])
        old = _make_table("old_categories", ["id", "name"])
        orders.foreign_keys = [
            ForeignKey(
                column="cat_id",
                target_table="old_categories",
                target_column="id",
                source=EnrichmentSource.AI_INFERRED,
                confidence=0.5,
            )
        ]
        candidates = [
            {
                "source_table": "orders",
                "column": "cat_id",
                "target_table": "categories",
                "target_column": "id",
                "confidence": 0.95,
            }
        ]

        count = apply_inferred_relationships([orders, categories, old], candidates)

        assert count == 1
        assert len(orders.foreign_keys) == 2
