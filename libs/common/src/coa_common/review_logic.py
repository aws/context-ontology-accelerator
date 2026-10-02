# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared review-decision logic for table-level review/approve/reject flows.

Both the synchronous per-table review handler and the asynchronous bulk worker
need to apply the same cascade rules when an APPROVE or REJECT decision is
made on a table. Centralizing the rules here prevents the two paths from
drifting and creating subtle correctness bugs.

Invariant — children before parent
-----------------------------------
For every decision the column cascade is applied **first**, and the table's
own ``review_status`` is only flipped **after** the columns have been
updated. The caller then persists the table and all of its columns in a
single atomic DataZone asset revision (``build_forms_input(table)``), so a
parent table never reaches a terminal status while one of its child columns
is still ``PENDING_REVIEW``.

Cascade rules
-------------
  APPROVE:
    ``PENDING_REVIEW`` columns flip to ``APPROVED`` in both modes. ``REJECTED``
    columns are preserved by a *bulk* approve (an approved table may
    legitimately contain deliberately rejected columns) but re-approved by a
    *per-asset* approve of the table: a table-level REJECT cascades REJECTED
    onto every child, so treating those as deliberate on the next approve
    stranded them — approve -> reject -> approve left an APPROVED table over
    all-REJECTED columns and relationships. The table flips to ``APPROVED``
    only once every column is terminal (always true after the cascade with
    the current 3-value enum).

  REJECT (aggressive on columns):
    Every non-``REJECTED`` column flips to ``REJECTED`` — this clobbers prior
    ``APPROVED`` columns as well as ``PENDING_REVIEW`` ones. The table then
    flips to ``REJECTED``.

Mode differences (``bulk`` only changes *table selection*, not column rules)
----------------------------------------------------------------------------
  Per-asset (``bulk=False``) — ``PUT /tables/{tid}/review``:
    An explicit command on this one table. The table is always acted on
    (its status flips even if it was previously the opposite decision).

  Bulk (``bulk=True``) — ``ApproveSource`` / ``RejectSource`` worker:
    APPROVE is a *default*: tables with an explicit prior REJECTED decision
    are skipped so "Approve All" never silently overrides a steward's earlier
    reject. Tables already APPROVED still cascade to any remaining PENDING
    columns (fixes the case where a table was APPROVED before its columns).
    REJECT is a *command* (aggressive, matching per-asset): every
    non-``REJECTED`` table and column is flipped to ``REJECTED``.

ASYMMETRIES (intentional — read before changing)
-------------------------------------------------
1. APPROVE is conservative on columns; REJECT is aggressive.
   APPROVE flips only PENDING_REVIEW columns and preserves REJECTED ones (an
   approved table may contain rejected columns). REJECT flips every
   non-REJECTED column — clobbering prior APPROVED decisions. Rationale:
   approving a table must not silently un-reject a column a steward
   deliberately rejected, but rejecting a table means "none of this is
   usable", so earlier approvals are moot.

2. Bulk APPROVE skips tables with a prior decision; bulk REJECT does not.
   "Approve All" preserves explicitly APPROVED/REJECTED tables (a default).
   "Reject All" is a command and flips every non-REJECTED table. Per-asset
   and bulk REJECT are therefore identical; per-asset and bulk APPROVE differ
   only in that bulk skips already-decided tables.

3. Source terminal states are asymmetric (see the bulk worker _lifecycle and
   the API _bulk_lifecycle / _REVIEWABLE_STATES):
     - APPROVED is "soft": per-asset re-review of individual tables/columns is
       still allowed; bulk approve/reject are not (APPROVED is not an allowed
       entry state for either).
     - REJECTED is a "hard" terminal lock: no per-asset review, no bulk
       approve/reject — the steward must re-onboard a new source.
     - APPROVAL_FAILED → re-approve only; REJECTION_FAILED → re-reject only.

These asymmetries are enforced in two layers: the column/table cascade here,
and the source-status guards in database_routes (_REVIEWABLE_STATES,
_bulk_lifecycle) + the worker (_lifecycle).
"""

from __future__ import annotations

from coa_common.domain_models import Table


def _cascade_to_relationships(table: Table, decision: str, *, bulk: bool) -> bool:
    """Apply ``decision`` to the table's reviewable (inferred) foreign keys.

    Inferred relationships (#1088) carry their own ``review_status`` and are
    withheld from the ontology while ``PENDING_REVIEW``. Before this, approving a
    source/table cascaded to columns only, so the normal scan -> approve -> induce
    path silently produced an ontology with none of its cross-source
    relationships in it. Same rule as columns, in both modes:

      * bulk APPROVE is a default — flips only ``PENDING_REVIEW`` -> ``APPROVED``
        and preserves an explicit ``REJECTED`` (a steward can reject a
        relationship, then bulk-approve the source, and keep that rejection);
      * per-asset APPROVE of the table re-approves ``REJECTED`` ones too — see
        :func:`apply_decision_to_table` for why;
      * REJECT is aggressive — every non-``REJECTED`` reviewable FK -> ``REJECTED``.

    FKs with an empty ``review_status`` (deterministic, steward-specified,
    pre-#1088) are authoritative and untouched.
    """
    from coa_control_plane_server.models.review_decision import ReviewDecision
    from coa_control_plane_server.models.review_status import ReviewStatus

    changed = False
    for fk in table.foreign_keys or []:
        if not fk.review_status:
            continue
        if decision == ReviewDecision.APPROVED:
            flip = fk.review_status == ReviewStatus.PENDING_REVIEW or (
                not bulk and fk.review_status == ReviewStatus.REJECTED
            )
            if flip:
                fk.review_status = ReviewStatus.APPROVED
                changed = True
        elif fk.review_status != ReviewStatus.REJECTED:
            fk.review_status = ReviewStatus.REJECTED
            changed = True
    return changed


def apply_decision_to_table(table: Table, decision: str, *, bulk: bool = False) -> bool:
    """Apply a review decision to a Table object in-place.

    Args:
        table: The table whose business metadata + columns will be mutated.
        decision: A ``ReviewDecision`` value — ``"APPROVED"`` or ``"REJECTED"``.
        bulk: If ``True``, preserve all explicit prior decisions (used by
            the bulk approve/reject worker). If ``False``, the per-asset
            mode flips the table status unconditionally and cascades to
            PENDING *and* REJECTED children (an explicit approve of the table
            re-approves what a table-level reject cascaded). See the module
            docstring for full semantics.

    Returns:
        ``True`` if the table or any of its columns changed status,
        ``False`` if the call was a no-op.

    Raises:
        ValueError: If ``decision`` is not a recognized value.
    """
    # Local imports to keep this module independent of the generated server
    # models for environments that don't pre-install Smithy artifacts.
    from coa_control_plane_server.models.review_decision import ReviewDecision
    from coa_control_plane_server.models.review_status import ReviewStatus

    if decision not in (ReviewDecision.APPROVED, ReviewDecision.REJECTED):
        raise ValueError(f"Unsupported decision: {decision}")

    changed = False
    table_status = table.business_metadata.review_status
    terminal = {ReviewStatus.APPROVED, ReviewStatus.REJECTED}

    if decision == ReviewDecision.APPROVED:
        # Bulk APPROVE is a *default*: never override an explicit prior table
        # decision. If the table is already REJECTED, leave it (and
        # its columns) untouched. Per-asset APPROVE always proceeds.
        # However, APPROVED tables with PENDING columns still need their
        # columns cascaded — the table was approved but columns weren't yet.
        if bulk and table_status == ReviewStatus.REJECTED:
            return False
        if bulk and table_status == ReviewStatus.APPROVED:
            # Still cascade to PENDING columns on already-APPROVED tables.
            # Early return is safe here: the table is already APPROVED so the
            # "parent after children" check (all columns terminal) doesn't
            # apply — we only need to flip remaining PENDING columns. The table
            # status itself is not touched.
            for col in table.columns:
                if col.business_metadata.review_status == ReviewStatus.PENDING_REVIEW:
                    col.business_metadata.review_status = ReviewStatus.APPROVED
                    changed = True
            return _cascade_to_relationships(table, decision, bulk=True) or changed

        # 1) Children first. PENDING_REVIEW columns always flip to APPROVED.
        #    REJECTED columns are preserved by a BULK approve (an approved table
        #    may contain deliberately rejected columns) but re-approved by an
        #    explicit PER-ASSET approve of the table. Without that, the sequence
        #    approve -> reject -> approve stranded every child: the table-level
        #    REJECT had cascaded REJECTED onto each column (and FK), and the
        #    re-approve then treated that fallout as a deliberate per-column
        #    decision and kept it — an APPROVED table over all-REJECTED children,
        #    with none of them reaching the ontology. Approving the table by hand
        #    is read as "approve this table", children included; a steward who
        #    wants one column out re-rejects it afterwards, exactly as they would
        #    have to after the (already aggressive) table-level reject.
        for col in table.columns:
            st = col.business_metadata.review_status
            if st == ReviewStatus.PENDING_REVIEW or (not bulk and st == ReviewStatus.REJECTED):
                col.business_metadata.review_status = ReviewStatus.APPROVED
                changed = True

        # 2) Parent after children: only flip the table once every column is
        #    terminal. With the current 3-value enum the cascade above already
        #    guarantees this; the guard is defensive against a future enum
        #    gaining another non-terminal state.
        if table_status != ReviewStatus.APPROVED and all(
            c.business_metadata.review_status in terminal for c in table.columns
        ):
            table.business_metadata.review_status = ReviewStatus.APPROVED
            changed = True

        # 3) Inferred relationships follow the same rule as columns.
        changed = _cascade_to_relationships(table, decision, bulk=bulk) or changed
    else:
        # REJECT is aggressive in BOTH modes (per-asset and bulk): an explicit
        # reject of the table rejects everything under it.
        # 1) Children first: every non-REJECTED column flips to REJECTED,
        #    clobbering prior APPROVED columns as well as PENDING ones.
        for col in table.columns:
            if col.business_metadata.review_status != ReviewStatus.REJECTED:
                col.business_metadata.review_status = ReviewStatus.REJECTED
                changed = True

        # 2) Parent after children.
        if table_status != ReviewStatus.REJECTED:
            table.business_metadata.review_status = ReviewStatus.REJECTED
            changed = True

        # 3) A rejected table's inferred relationships are rejected with it.
        changed = _cascade_to_relationships(table, decision, bulk=bulk) or changed

    return changed
