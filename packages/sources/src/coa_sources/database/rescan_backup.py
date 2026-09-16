# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Re-scan backup blob: the pre-rescan state that approve/reject need.

A re-scan overwrites live DataZone assets in place, and the in-memory diff that
knows what changed (:func:`rescan.diff_tables`) is discarded once the merged
set is written. So before the merge overwrites anything, discovery writes this
blob to S3. It is the single durable record of what a re-scan changed, carrying
exactly what the two review outcomes need:

  * ``modified_backup`` — the pre-rescan form of every *modified* table, so a
    REJECT can restore each overwritten asset to its approved state;
  * ``added_tables`` — table ids the re-scan created fresh, so a REJECT can
    delete them (they did not exist before the re-scan);
  * ``removed_tables`` — table ids gone from the source, so an APPROVE can
    delete their assets (a re-scan leaves them untouched and still approved
    until the steward confirms the removal);
  * ``removed_columns`` — per modified table, the columns gone from the source,
    so an APPROVE can drop them from the asset.

Unchanged tables are never touched by a re-scan, so they are not recorded here.
This module is pure — it never touches S3, DataZone, or DynamoDB. Writing the
blob and reading it back at approve/reject time is I/O and lives in the write
path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from coa_common.datazone_forms import serialize_form
from coa_common.domain_models import Table
from coa_control_plane_server.models.rescan_column_change_status import RescanColumnChangeStatus

from coa_sources.database.rescan import RescanDiff

BACKUP_SCHEMA_VERSION = 1

# S3 error codes that mean "the backup blob is not there", as opposed to a real
# fault. An absent blob is a NORMAL state: a re-scan that finds no drift writes
# none, and a source with no open re-scan has none either.
#
# Lives here with ``backup_s3_key`` and for the same reason. Every reader of the
# blob has to tell absent from broken, because the two demand opposite handling:
# absent means "nothing to report", broken must surface. Each reader still
# chooses its own policy for the broken case — discovery degrades to the live
# assets, the tables API and the approve/reject worker both fail loudly — but
# they must agree on which codes mean absent, or the same missing object reads
# as absent in one caller and as a fault in another.
#
# Note that S3 only reports NoSuchKey to a caller holding s3:ListBucket on the
# bucket; without it a missing key comes back as AccessDenied, which is NOT in
# this set and must not be added to it. AccessDenied is a real permission fault,
# and treating it as absent would silently hide pending deletions from a review
# page while approve still deleted them. The grant is the fix, not this set.
S3_ABSENT_CODES = ("404", "NoSuchKey", "NotFound")


def backup_s3_key(source_id: str) -> str:
    """S3 key for a source's current re-scan backup blob.

    Keyed by source id alone: a source is in re-scan review for at most one
    re-scan at a time, so one "current" blob per source is sufficient, and a
    fresh re-scan of an already-in-review source correctly supersedes it. The
    writer (discovery) and the readers (approve/reject worker) MUST derive the
    key the same way, so it lives here as the single source of truth.
    """
    return f"{source_id}/rescan-backup/current.json"


def build_rescan_backup(
    diff: RescanDiff,
    accepted: list[Table],
    *,
    source_id: str,
    scan_job_sk: str,
    prior_summary: dict[str, Any] | None = None,
    orphaned_added_tables: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the re-scan backup blob from a computed diff and the accepted tables.

    Args:
        diff: the partition produced by :func:`rescan.diff_tables`.
        accepted: the tables reconstructed from the source's live (approved)
            assets — the pre-rescan state, and the source of the modified-table
            backup.
        source_id: owning source id, recorded for traceability.
        scan_job_sk: the re-scan's scan-job sort key, recorded for traceability.
        prior_summary: the source row's pre-rescan summary counts
            (tablesDiscovered / discoveredSchemas / lastScan* / tablesApproved),
            stored so a reject can restore them after the re-scan rewrote them.
        orphaned_added_tables: table_ids a PRIOR (still-open) re-scan added that
            are gone from this fresh scan. reconstruct_approved_baseline drops
            them from the baseline and they are absent from the fresh scan, so
            diff_tables files them under neither added nor removed and their live
            assets would be stranded across this review. Carried into BOTH
            removed_tables (approve deletes) and added_tables (reject deletes) so
            either outcome reaps them. They are deliberately left out of
            merged_write_set, so the assets stay live until a review outcome.

    Returns:
        A JSON-serializable dict (see the module docstring for the fields). Pure
        — performs no I/O.
    """
    accepted_by_id = {t.table_id: t for t in accepted}

    removed_columns: dict[str, list[str]] = {}
    modified_backup: dict[str, dict[str, Any]] = {}
    for change in diff.modified:
        accepted_table = accepted_by_id.get(change.table_id)
        if accepted_table is None:
            # diff_tables only marks a table modified when it is in BOTH scans,
            # so the accepted table is always present; skip defensively rather
            # than fabricate a backup we cannot restore.
            continue
        modified_backup[change.table_id] = serialize_form(accepted_table)
        removed = [col.name for col in change.columns if col.status == RescanColumnChangeStatus.REMOVED]
        if removed:
            removed_columns[change.table_id] = removed

    # Fold the prior re-scan's now-vanished added tables into BOTH lists so
    # either review outcome reaps them (approve deletes removed_tables, reject
    # deletes added_tables). diff.removed/diff.added never contain them (dropped
    # from the baseline, absent from the fresh scan), so the filter is defensive.
    orphaned = list(dict.fromkeys(orphaned_added_tables))
    removed_tables = list(diff.removed) + [t for t in orphaned if t not in diff.removed]
    added_tables = list(diff.added) + [t for t in orphaned if t not in diff.added]

    return {
        "version": BACKUP_SCHEMA_VERSION,
        "source_id": source_id,
        "scan_job_sk": scan_job_sk,
        "removed_tables": removed_tables,
        "added_tables": added_tables,
        "removed_columns": removed_columns,
        "modified_backup": modified_backup,
        # Pre-rescan source summary counts. A re-scan rewrites the live counts to
        # the fresh scan; a reject restores these so the source's numbers match
        # the restored (pre-rescan) assets.
        "source_summary": dict(prior_summary or {}),
    }
