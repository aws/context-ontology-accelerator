# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``coa_common.review_logic``.

The cascade rules in this module are the single source of truth shared by
the synchronous per-table review handler and the asynchronous bulk review
worker. Drift between the two callers is the bug class these tests guard
against.
"""

from __future__ import annotations

import pytest
from coa_common.domain_models import BusinessMetadata, Column, EnrichmentSource, ForeignKey, Table
from coa_common.review_logic import apply_decision_to_table


def _table(*, status: str = "PENDING_REVIEW", columns: list[tuple[str, str]] | None = None) -> Table:
    if columns is None:
        columns = [("a", "PENDING_REVIEW")]
    return Table(
        name="t",
        database="db",
        business_metadata=BusinessMetadata(review_status=status),
        columns=[
            Column(name=n, data_type="string", business_metadata=BusinessMetadata(review_status=cs))
            for n, cs in columns
        ],
    )


@pytest.mark.unit
class TestApplyDecisionToTable:
    def test_pending_to_approved_flips_table_and_all_columns(self):
        # Per-asset approve of the table approves its children, including a
        # REJECTED one: "approve this table" means the table and what is in it.
        t = _table(status="PENDING_REVIEW", columns=[("c1", "APPROVED"), ("c2", "REJECTED")])
        changed = apply_decision_to_table(t, "APPROVED")
        assert changed is True
        assert t.business_metadata.review_status == "APPROVED"
        assert t.columns[0].business_metadata.review_status == "APPROVED"
        assert t.columns[1].business_metadata.review_status == "APPROVED"

    def test_per_asset_approve_cascades_pending_and_rejected_columns(self):
        t = _table(status="PENDING_REVIEW", columns=[("c1", "PENDING_REVIEW"), ("c2", "REJECTED")])
        changed = apply_decision_to_table(t, "APPROVED")
        assert changed is True
        assert t.business_metadata.review_status == "APPROVED"
        assert [c.business_metadata.review_status for c in t.columns] == ["APPROVED", "APPROVED"]

    def test_bulk_approve_preserves_rejected_columns(self):
        # BULK approve is a default and must not undo a deliberate per-column
        # rejection made before the steward bulk-approved the source.
        t = _table(
            status="PENDING_REVIEW",
            columns=[("c1", "REJECTED"), ("c2", "APPROVED"), ("c3", "APPROVED")],
        )
        apply_decision_to_table(t, "APPROVED", bulk=True)
        assert {c.name: c.business_metadata.review_status for c in t.columns} == {
            "c1": "REJECTED",
            "c2": "APPROVED",
            "c3": "APPROVED",
        }

    def test_approve_reject_approve_leaves_table_and_children_in_sync(self):
        # The reported sequence (per-asset PUT .../review x3). Before: the reject
        # cascaded REJECTED onto every column, and the re-approve preserved that
        # "decision" — an APPROVED table over all-REJECTED columns.
        t = _table(status="PENDING_REVIEW", columns=[("c1", "PENDING_REVIEW"), ("c2", "PENDING_REVIEW")])
        for decision in ("APPROVED", "REJECTED", "APPROVED"):
            apply_decision_to_table(t, decision)
        assert t.business_metadata.review_status == "APPROVED"
        assert [c.business_metadata.review_status for c in t.columns] == ["APPROVED", "APPROVED"]

    def test_per_asset_reject_overwrites_non_rejected_columns(self):
        """Per-asset (non-bulk) REJECT keeps legacy 'clobber non-REJECTED' behavior.

        The user is explicitly rejecting the table; column-level approvals
        from earlier are not preserved. Bulk REJECT differs (see bulk tests).
        """
        t = _table(
            status="APPROVED",
            columns=[("c1", "APPROVED"), ("c2", "REJECTED"), ("c3", "PENDING_REVIEW")],
        )
        apply_decision_to_table(t, "REJECTED")
        assert {c.name: c.business_metadata.review_status for c in t.columns} == {
            "c1": "REJECTED",
            "c2": "REJECTED",
            "c3": "REJECTED",
        }

    def test_idempotent_when_already_in_target_state(self):
        t = _table(status="APPROVED", columns=[("c1", "APPROVED")])
        assert apply_decision_to_table(t, "APPROVED") is False

    def test_returns_false_when_already_approved_all_terminal(self):
        # Table already APPROVED and all columns terminal — no-op
        t = _table(status="APPROVED", columns=[("c1", "APPROVED")])
        assert apply_decision_to_table(t, "APPROVED") is False

    def test_reapprove_cascades_pending_column(self):
        # Re-approving a table with a newly-PENDING column cascades it to
        # APPROVED (the table stays APPROVED); change is reported so the
        # caller persists the column flip.
        t = _table(status="APPROVED", columns=[("c1", "PENDING_REVIEW")])
        assert apply_decision_to_table(t, "APPROVED") is True
        assert t.business_metadata.review_status == "APPROVED"
        assert t.columns[0].business_metadata.review_status == "APPROVED"

    def test_table_with_zero_columns(self):
        t = _table(status="PENDING_REVIEW", columns=[])
        assert apply_decision_to_table(t, "APPROVED") is True
        assert t.business_metadata.review_status == "APPROVED"

    def test_invalid_decision_raises_value_error(self):
        t = _table()
        with pytest.raises(ValueError, match="Unsupported decision"):
            apply_decision_to_table(t, "MAYBE")

    @pytest.mark.parametrize("decision", ["", None, "approve", "approved"])
    def test_unsupported_decision_values_raise(self, decision):
        t = _table()
        with pytest.raises(ValueError):
            apply_decision_to_table(t, decision)


@pytest.mark.unit
class TestApplyDecisionToTableBulkMode:
    """Bulk-mode semantics.

    Bulk APPROVE is a *default*: it must NOT silently overwrite a steward's
    earlier per-asset decisions — tables/columns with an explicit
    APPROVED/REJECTED status are skipped/preserved.

    Bulk REJECT is a *command* (aggressive, identical to per-asset reject):
    every non-REJECTED table and column is flipped to REJECTED, clobbering
    prior APPROVED decisions. "Reject All" means reject everything.
    """

    def test_bulk_approve_skips_rejected_table(self):
        t = _table(status="REJECTED", columns=[("c1", "PENDING_REVIEW")])
        assert apply_decision_to_table(t, "APPROVED", bulk=True) is False
        # Table preserved; column NOT cascaded (worker skips the table entirely).
        assert t.business_metadata.review_status == "REJECTED"
        assert t.columns[0].business_metadata.review_status == "PENDING_REVIEW"

    def test_bulk_reject_flips_approved_table_aggressively(self):
        # Bulk REJECT now matches per-asset reject: it flips a previously
        # APPROVED table (and its columns) to REJECTED.
        t = _table(status="APPROVED", columns=[("c1", "PENDING_REVIEW")])
        assert apply_decision_to_table(t, "REJECTED", bulk=True) is True
        assert t.business_metadata.review_status == "REJECTED"
        assert t.columns[0].business_metadata.review_status == "REJECTED"

    def test_bulk_approve_skips_already_approved_table(self):
        # Idempotent — already in target state with all columns terminal.
        t = _table(status="APPROVED", columns=[("c1", "APPROVED")])
        assert apply_decision_to_table(t, "APPROVED", bulk=True) is False

    def test_bulk_approve_cascades_pending_columns_on_approved_table(self):
        """Bulk APPROVE still cascades PENDING columns on already-APPROVED tables."""
        t = _table(status="APPROVED", columns=[("c1", "PENDING_REVIEW"), ("c2", "APPROVED")])
        assert apply_decision_to_table(t, "APPROVED", bulk=True) is True
        assert t.business_metadata.review_status == "APPROVED"
        assert t.columns[0].business_metadata.review_status == "APPROVED"
        assert t.columns[1].business_metadata.review_status == "APPROVED"

    def test_bulk_approve_flips_pending_table_and_pending_columns_only(self):
        t = _table(
            status="PENDING_REVIEW",
            columns=[("c1", "PENDING_REVIEW"), ("c2", "REJECTED"), ("c3", "APPROVED")],
        )
        changed = apply_decision_to_table(t, "APPROVED", bulk=True)
        assert changed is True
        # All columns now terminal → table gets approved
        assert t.business_metadata.review_status == "APPROVED"
        assert {c.name: c.business_metadata.review_status for c in t.columns} == {
            "c1": "APPROVED",  # was PENDING → flipped
            "c2": "REJECTED",  # preserved
            "c3": "APPROVED",  # preserved
        }

    def test_bulk_approve_terminalizes_all_columns_then_approves_table(self):
        """Bulk APPROVE flips every PENDING_REVIEW column to APPROVED, so all
        columns end terminal and the table is approved.

        Note: the ``if not all_terminal: return`` branch in the implementation
        is defensive. With the current three-value ``ReviewStatus`` enum it is
        unreachable here, because the cascade flips the only non-terminal value
        (PENDING_REVIEW) to APPROVED before the check. It guards against a
        future enum gaining an additional non-terminal state.
        """
        t = _table(
            status="PENDING_REVIEW",
            columns=[("c1", "PENDING_REVIEW"), ("c2", "PENDING_REVIEW")],
        )
        changed = apply_decision_to_table(t, "APPROVED", bulk=True)
        assert changed is True
        assert t.business_metadata.review_status == "APPROVED"
        assert all(c.business_metadata.review_status == "APPROVED" for c in t.columns)

    def test_bulk_reject_clobbers_approved_columns(self):
        """Bulk REJECT is aggressive (same as per-asset): every non-REJECTED
        column — including APPROVED — flips to REJECTED."""
        t = _table(
            status="PENDING_REVIEW",
            columns=[("c1", "APPROVED"), ("c2", "PENDING_REVIEW"), ("c3", "REJECTED")],
        )
        changed = apply_decision_to_table(t, "REJECTED", bulk=True)
        assert changed is True
        assert t.business_metadata.review_status == "REJECTED"
        assert {c.name: c.business_metadata.review_status for c in t.columns} == {
            "c1": "REJECTED",  # APPROVED → REJECTED (clobbered)
            "c2": "REJECTED",
            "c3": "REJECTED",
        }

    def test_bulk_approve_pending_table_with_no_pending_columns_returns_changed(self):
        # Even if the only change is the table itself, bulk reports changed.
        t = _table(status="PENDING_REVIEW", columns=[("c1", "REJECTED")])
        assert apply_decision_to_table(t, "APPROVED", bulk=True) is True
        assert t.business_metadata.review_status == "APPROVED"
        assert t.columns[0].business_metadata.review_status == "REJECTED"

    def test_bulk_reject_idempotent_on_fully_rejected_table(self):
        """A table already REJECTED with all columns REJECTED is a no-op."""
        t = _table(status="REJECTED", columns=[("c1", "REJECTED"), ("c2", "REJECTED")])
        assert apply_decision_to_table(t, "REJECTED", bulk=True) is False
        assert all(c.business_metadata.review_status == "REJECTED" for c in t.columns)


def _fk(target: str, status: str, *, source: str = EnrichmentSource.AI_INFERRED, cross: bool = True) -> ForeignKey:
    return ForeignKey(
        column="a",
        target_table=target,
        target_column="id",
        source=source,
        review_status=status,
        target_datasource_id="DS#other" if cross else "",
    )


@pytest.mark.unit
class TestRelationshipCascade:
    """#1088 follow-up: a review decision on a table must also decide its inferred
    relationships. Both callers (per-asset handler, bulk worker) go through the
    same function, so ``bulk`` is parametrized rather than tested per caller."""

    @pytest.mark.parametrize("bulk", [False, True])
    def test_approve_flips_pending_relationships(self, bulk):
        t = _table()
        t.foreign_keys = [_fk("crm.customers", "PENDING_REVIEW"), _fk("erp.customers", "PENDING_REVIEW")]
        assert apply_decision_to_table(t, "APPROVED", bulk=bulk) is True
        assert [fk.review_status for fk in t.foreign_keys] == ["APPROVED", "APPROVED"]

    def test_bulk_approve_preserves_explicitly_rejected_relationship(self):
        # Bulk approve is a default: a steward who rejected a relationship before
        # bulk-approving the source keeps that decision.
        t = _table()
        t.foreign_keys = [_fk("crm.customers", "REJECTED"), _fk("erp.customers", "PENDING_REVIEW")]
        apply_decision_to_table(t, "APPROVED", bulk=True)
        assert [fk.review_status for fk in t.foreign_keys] == ["REJECTED", "APPROVED"]

    def test_per_asset_approve_re_approves_rejected_relationship(self):
        # An explicit approve of the table takes its relationships with it,
        # symmetric with the table-level reject that put them there.
        t = _table()
        t.foreign_keys = [_fk("crm.customers", "REJECTED"), _fk("erp.customers", "PENDING_REVIEW")]
        apply_decision_to_table(t, "APPROVED", bulk=False)
        assert [fk.review_status for fk in t.foreign_keys] == ["APPROVED", "APPROVED"]

    def test_approve_reject_approve_keeps_relationships_in_sync_with_table(self):
        # The reported sequence, with cross-source relationships on the table.
        t = _table()
        t.foreign_keys = [_fk("crm.customers", "PENDING_REVIEW"), _fk("loyalty.members", "PENDING_REVIEW")]
        for decision in ("APPROVED", "REJECTED", "APPROVED"):
            apply_decision_to_table(t, decision)
        assert t.business_metadata.review_status == "APPROVED"
        assert [fk.review_status for fk in t.foreign_keys] == ["APPROVED", "APPROVED"]

    def test_bulk_approve_on_already_approved_table_still_decides_pending_relationships(self):
        # The exact reported shape: relationships inferred AFTER the table was
        # approved (cross-source pass runs at the enrichment tail), then the
        # steward bulk-approves the source again. Table + columns are already
        # terminal; the early-return path must still cascade to relationships.
        t = _table(status="APPROVED", columns=[("a", "APPROVED")])
        t.foreign_keys = [_fk("ref.account_xref", "PENDING_REVIEW")]
        assert apply_decision_to_table(t, "APPROVED", bulk=True) is True
        assert t.foreign_keys[0].review_status == "APPROVED"

    @pytest.mark.parametrize("bulk", [False, True])
    def test_reject_rejects_all_reviewable_relationships(self, bulk):
        t = _table()
        t.foreign_keys = [_fk("crm.customers", "APPROVED"), _fk("erp.customers", "PENDING_REVIEW")]
        apply_decision_to_table(t, "REJECTED", bulk=bulk)
        assert [fk.review_status for fk in t.foreign_keys] == ["REJECTED", "REJECTED"]

    @pytest.mark.parametrize("decision", ["APPROVED", "REJECTED"])
    def test_authoritative_relationships_are_never_touched(self, decision):
        # Deterministic / steward FKs (and pre-#1088 rows) carry no review status;
        # they are not reviewable and the cascade must leave them alone.
        t = _table()
        t.foreign_keys = [
            _fk("billing_account", "", source=EnrichmentSource.DETERMINISTIC, cross=False),
            _fk("accounts", "", source=EnrichmentSource.STEWARD_SPECIFIED, cross=False),
        ]
        apply_decision_to_table(t, decision)
        assert [fk.review_status for fk in t.foreign_keys] == ["", ""]

    def test_bulk_approve_skips_rejected_table_relationships_too(self):
        # A REJECTED table is left entirely alone by bulk approve — including its FKs.
        t = _table(status="REJECTED", columns=[("a", "REJECTED")])
        t.foreign_keys = [_fk("crm.customers", "PENDING_REVIEW")]
        assert apply_decision_to_table(t, "APPROVED", bulk=True) is False
        assert t.foreign_keys[0].review_status == "PENDING_REVIEW"

    def test_idempotent_when_relationships_already_decided(self):
        t = _table(status="APPROVED", columns=[("a", "APPROVED")])
        t.foreign_keys = [_fk("crm.customers", "APPROVED")]
        assert apply_decision_to_table(t, "APPROVED", bulk=True) is False
