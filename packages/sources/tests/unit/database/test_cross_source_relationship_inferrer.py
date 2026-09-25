# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cross-source FK inference (#1088)."""

from __future__ import annotations

from unittest.mock import MagicMock

from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    EnrichmentSource,
    ForeignKey,
    ReviewStatus,
    Table,
)
from coa_sources.database.enrichment.cross_source_relationship_inferrer import (
    apply_cross_source_relationships,
    build_cross_source_prompt,
    infer_cross_source_relationships,
)


def _table(name: str, ds: str, columns: list[str], *, desc: str = "", col_desc: dict[str, str] | None = None) -> Table:
    col_desc = col_desc or {}
    return Table(
        name=name,
        database="db",
        data_source_id=ds,
        business_metadata=BusinessMetadata(description=desc),
        columns=[
            Column(name=c, data_type="INTEGER", business_metadata=BusinessMetadata(description=col_desc.get(c, "")))
            for c in columns
        ],
    )


def _two_source_union() -> list[Table]:
    return [
        _table("account_xref", "DS#1", ["customer_id", "account_id"], desc="maps a customer to an account"),
        _table("customers", "DS#2", ["id", "name"], col_desc={"id": "customer primary key"}),
    ]


class TestBuildCrossSourcePrompt:
    def test_assigns_source_aliases_and_carries_descriptions(self):
        prompt = build_cross_source_prompt(_two_source_union())
        # Distinct datasource aliases assigned deterministically.
        assert "ds1 = source DS#1" in prompt
        assert "ds2 = source DS#2" in prompt
        # Table + column descriptions are included as hints.
        assert "maps a customer to an account" in prompt
        assert "customer primary key" in prompt
        # Tables are labelled with their source alias.
        assert "ds1.account_xref" in prompt
        assert "ds2.customers" in prompt

    def test_malformed_table_is_skipped_not_fatal(self):
        # A corrupt record (columns=None) must not abort the whole prompt; the
        # healthy tables are still rendered.
        tables = _two_source_union()
        broken = _table("broken", "DS#1", ["id"])
        broken.columns = None  # type: ignore[assignment]
        prompt = build_cross_source_prompt([*tables, broken])
        assert "ds1.account_xref" in prompt and "ds2.customers" in prompt
        assert "ds1.broken" not in prompt


class TestApplyCrossSource:
    def _cand(self, **over):
        base = {
            "source_table": "ds1.account_xref",
            "column": "customer_id",
            "target_table": "ds2.customers",
            "target_column": "id",
            "confidence": 0.9,
            "rationale": "account_xref.customer_id maps to customers.id",
        }
        base.update(over)
        return base

    def test_cross_source_candidate_written_pending_with_provenance(self):
        tables = _two_source_union()
        applied = apply_cross_source_relationships(tables, [self._cand()])
        assert applied == 1
        fk = tables[0].foreign_keys[0]
        assert fk.column == "customer_id"
        assert fk.target_table == "customers"
        assert fk.target_datasource_id == "DS#2"
        assert fk.source == EnrichmentSource.AI_INFERRED
        assert fk.review_status == ReviewStatus.PENDING_REVIEW
        assert "customers.id" in fk.provenance

    def test_same_source_candidate_is_skipped(self):
        # Both tables in one source -> not this pass's job (Pass 2 handles it).
        tables = [
            _table("orders", "DS#1", ["customer_id"]),
            _table("customers", "DS#1", ["id"]),
        ]
        applied = apply_cross_source_relationships(
            tables, [self._cand(source_table="ds1.orders", target_table="ds1.customers")]
        )
        assert applied == 0
        assert tables[0].foreign_keys == []

    def test_missing_source_column_skipped(self):
        tables = _two_source_union()
        assert apply_cross_source_relationships(tables, [self._cand(column="nonexistent")]) == 0

    def test_protected_fk_blocks_candidate(self):
        tables = _two_source_union()
        tables[0].foreign_keys = [
            ForeignKey(column="customer_id", target_table="x", source=EnrichmentSource.DETERMINISTIC)
        ]
        assert apply_cross_source_relationships(tables, [self._cand()]) == 0

    def test_idempotent_rerun_does_not_duplicate(self):
        tables = _two_source_union()
        assert apply_cross_source_relationships(tables, [self._cand()]) == 1
        # Second run with the same candidate adds nothing.
        assert apply_cross_source_relationships(tables, [self._cand()]) == 0
        assert len(tables[0].foreign_keys) == 1

    def test_synthesized_provenance_when_no_rationale(self):
        tables = _two_source_union()
        apply_cross_source_relationships(tables, [self._cand(rationale="")])
        assert tables[0].foreign_keys[0].provenance.startswith("cross-source inference:")


class TestInferShortCircuit:
    def test_single_source_returns_empty_without_calling_bedrock(self):
        client = MagicMock()
        emitter = MagicMock()
        tables = [_table("orders", "DS#1", ["id"]), _table("customers", "DS#1", ["id"])]
        assert infer_cross_source_relationships(tables, client, emitter) == []
        client.invoke.assert_not_called()


class TestPairwiseInference:
    """MR !1215 review: bounded per-pair prompts, focus-source work bound, dedup."""

    @staticmethod
    def _four_sources():
        return [
            _table("a", "DS#1", ["id"]),
            _table("b", "DS#2", ["id"]),
            _table("c", "DS#3", ["id"]),
            _table("d", "DS#4", ["id"]),
        ]

    @staticmethod
    def _ok(items):
        r = MagicMock()
        r.result = items
        r.latency_ms = 1
        r.input_tokens = 1
        r.output_tokens = 1
        return r

    def test_full_sweep_makes_one_call_per_source_pair(self):
        client = MagicMock()
        client.invoke.return_value = self._ok([])
        infer_cross_source_relationships(self._four_sources(), client, MagicMock())
        # 4 sources -> C(4,2) = 6 pairs, each a separate bounded prompt.
        assert client.invoke.call_count == 6

    def test_focus_source_bounds_work_to_new_times_existing(self):
        client = MagicMock()
        client.invoke.return_value = self._ok([])
        infer_cross_source_relationships(self._four_sources(), client, MagicMock(), focus_datasource_id="DS#4")
        # Only the 3 pairs involving the newly-enriched source, not all 6.
        assert client.invoke.call_count == 3
        # Every prompt includes the focus source and exactly one other.
        for call in client.invoke.call_args_list:
            prompt = call.args[1]
            assert "ds4 = source DS#4" in prompt
            assert sum(f"ds{i} = source DS#{i}" in prompt for i in (1, 2, 3)) == 1

    def test_pair_prompt_never_contains_the_whole_union(self):
        client = MagicMock()
        client.invoke.return_value = self._ok([])
        infer_cross_source_relationships(self._four_sources(), client, MagicMock())
        for call in client.invoke.call_args_list:
            prompt = call.args[1]
            # Exactly two sources listed per prompt — never all four.
            assert prompt.count(" = source DS#") == 2

    def test_aliases_are_union_wide_and_stable_across_pairs(self):
        # ds3 must mean DS#3 in EVERY pair prompt (aliases assigned once over the
        # union), otherwise apply() could not resolve "ds3.c" back to the table.
        client = MagicMock()
        client.invoke.return_value = self._ok([])
        infer_cross_source_relationships(self._four_sources(), client, MagicMock())
        for call in client.invoke.call_args_list:
            prompt = call.args[1]
            if "DS#3" in prompt:
                assert "ds3 = source DS#3" in prompt

    def test_duplicates_across_pairs_are_merged_highest_confidence(self):
        cand = {
            "source_table": "ds1.a",
            "column": "id",
            "target_table": "ds2.b",
            "target_column": "id",
        }
        client = MagicMock()
        # Same relationship reported by two pair prompts with different confidence.
        client.invoke.side_effect = [
            self._ok([{**cand, "confidence": 0.6}]),
            self._ok([{**cand, "confidence": 0.9}]),
            self._ok([]),
        ]
        tables = [_table("a", "DS#1", ["id"]), _table("b", "DS#2", ["id"]), _table("c", "DS#3", ["id"])]
        out = infer_cross_source_relationships(tables, client, MagicMock())
        assert len(out) == 1
        assert out[0]["confidence"] == 0.9

    def test_focus_source_with_no_tables_is_noop(self):
        client = MagicMock()
        infer_cross_source_relationships(self._four_sources(), client, MagicMock(), focus_datasource_id="DS#99")
        client.invoke.assert_not_called()
