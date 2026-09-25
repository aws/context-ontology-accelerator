# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace-scoped orchestration for cross-source relationship detection (#1088).

The detection algorithm (``cross_source_relationship_inferrer``) is pure. This
module is what actually RUNS it in a namespace: it enumerates the namespace's
enriched sources, reads all their tables, infers cross-source relationships over
the union, and writes back only the tables that gained a relationship.

It is invoked from the per-source enrichment task (see ``enrichment_handler``)
once a source finishes enriching: at that point the namespace may hold two or
more enriched sources whose tables can finally be compared. The pass is
best-effort and idempotent — re-running it as each new source lands re-scans the
whole namespace, and ``apply_cross_source_relationships`` drops duplicates, so a
relationship is written once even across repeated runs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from botocore.exceptions import ClientError
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.domain_models import Table
from coa_control_plane_server.models.source_status import SourceStatus

from coa_sources.database.enrichment.bedrock_client import BedrockClient
from coa_sources.database.enrichment.cross_source_relationship_inferrer import (
    apply_cross_source_relationships,
    infer_cross_source_relationships,
)
from coa_sources.database.pipeline.enrichment_metrics import EnrichmentMetricEmitter

logger = logging.getLogger(__name__)

# Source statuses whose tables are enriched enough to compare. A source still
# scanning/discovering/failed has nothing (or nothing trustworthy) to offer.
_ENRICHED_STATUSES: frozenset[str] = frozenset(
    {SourceStatus.PENDING_REVIEW, SourceStatus.APPROVED, SourceStatus.RESCAN_REVIEW}
)

# Per-namespace lock so two sources finishing enrichment together cannot run the
# cross-source pass concurrently (a lost-update race on the child tables they
# both write). Held on the sources table under SK=XSRC_LOCK. Stale-TTL'd: a
# crashed holder's lock is reclaimable after _LOCK_TTL_S so detection can never
# wedge permanently.
_LOCK_SK = "XSRC_LOCK"
_LOCK_TTL_S = 900


def _acquire_namespace_lock(dao: DynamoDBDAO, namespace_id: str) -> bool:
    """Conditionally claim the namespace's cross-source lock; True when acquired.

    Succeeds when no lock exists or the existing one is older than _LOCK_TTL_S
    (stale holder). Concurrent callers race on the same conditional write; exactly
    one wins. Only a failed CONDITION (another pass holds a fresh lock) is treated
    as "not acquired"; any other error (table missing, DynamoDB unreachable, …) is
    an infrastructure fault and propagates, so it is never mistaken for contention.
    """
    now = int(time.time())
    try:
        dao.update(
            key={"PK": f"NS#{namespace_id}", "SK": _LOCK_SK},
            update_fields={"lockedAt": now},
            condition="attribute_not_exists(lockedAt) OR lockedAt < :stale",
            condition_values={":stale": now - _LOCK_TTL_S},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _release_namespace_lock(dao: DynamoDBDAO, namespace_id: str) -> None:
    """Release the lock so the next pass can acquire immediately.

    Best-effort: a failure here must not mask the pass's own outcome, but it is
    logged because an unreleased lock delays the next pass by up to _LOCK_TTL_S.
    """
    try:
        dao.update(
            key={"PK": f"NS#{namespace_id}", "SK": _LOCK_SK},
            update_fields={"lockedAt": 0},
        )
    except Exception:
        logger.warning(
            "cross_source_lock_release_failed: namespace=%s — next pass may wait up to %ss for the stale TTL",
            namespace_id,
            _LOCK_TTL_S,
            exc_info=True,
        )


def run_cross_source_inference(
    *,
    datasource_ids: list[str],
    read_tables: Callable[[str], list[Table]],
    write_tables: Callable[[list[Table]], None],
    client: BedrockClient,
    emitter: EnrichmentMetricEmitter,
    focus_datasource_id: str | None = None,
) -> dict:
    """Infer cross-source relationships over a set of datasources and persist them.

    Reads every datasource's tables (via ``read_tables``), infers relationships
    across the union, applies the cross-source ones as PENDING_REVIEW, and calls
    ``write_tables`` with ONLY the tables that actually changed. Dependency-
    injected so the orchestration is unit-testable without AWS.

    A namespace with fewer than two datasources is a no-op — there is nothing to
    cross.
    """
    if len(datasource_ids) < 2:
        logger.info("cross-source inference skipped: %d source(s) in namespace", len(datasource_ids))
        return {"sources": len(datasource_ids), "relationships_written": 0, "tables_updated": 0}

    tables: list[Table] = []
    read_ok = 0
    for ds_id in datasource_ids:
        # One unreadable source must not abort detection for the rest of the
        # namespace: skip it (its relationships are simply not considered this
        # run — the pass is idempotent and re-runs on the next enrichment).
        try:
            tables.extend(read_tables(ds_id))
            read_ok += 1
        except Exception:
            logger.exception("cross_source_read_failed: datasource=%s — skipping this source", ds_id)
            emitter.emit_metric("CrossSourceReadFailed", 1, "Count")
    if read_ok < 2:
        logger.info("cross-source inference skipped: only %d readable source(s)", read_ok)
        return {"sources": read_ok, "relationships_written": 0, "tables_updated": 0}

    fk_counts_before = {id(t): len(t.foreign_keys) for t in tables}
    candidates = infer_cross_source_relationships(tables, client, emitter, focus_datasource_id=focus_datasource_id)
    applied = apply_cross_source_relationships(tables, candidates)

    changed = [t for t in tables if len(t.foreign_keys) != fk_counts_before[id(t)]]
    if changed:
        try:
            write_tables(changed)
        except Exception:
            # Nothing is permanently lost: the relationships are re-derived and
            # re-written by the next (idempotent) pass. Surface it so a persistently
            # failing write is visible rather than silently swallowed.
            logger.exception("cross_source_write_failed: %d table(s) not persisted", len(changed))
            emitter.emit_metric("CrossSourceWriteFailed", 1, "Count")
            return {"sources": read_ok, "relationships_written": 0, "tables_updated": 0, "write_failed": True}

    logger.info(
        "cross-source inference complete: sources=%d relationships_written=%d tables_updated=%d",
        read_ok,
        applied,
        len(changed),
    )
    return {"sources": read_ok, "relationships_written": applied, "tables_updated": len(changed)}


def enriched_datasource_ids(
    namespace_id: str, *, region: str, sources_table: str, dao: DynamoDBDAO | None = None
) -> list[str]:
    """Return the ``DS#{id}`` of every enriched source in the namespace.

    Queries the sources table (PK=``NS#{namespace_id}``, SK begins_with ``SRC#``)
    and keeps sources whose status is enriched. ``dao`` is injectable for tests.
    """
    dao = dao or DynamoDBDAO(sources_table, region=region)
    ids: list[str] = []
    start: dict | None = None
    while True:
        page = dao.query(
            QueryParams(
                key_condition="PK = :pk AND begins_with(SK, :sk)",
                expression_values={":pk": f"NS#{namespace_id}", ":sk": "SRC#"},
                exclusive_start_key=start,
            )
        )
        for item in page.items:
            if item.get("status") in _ENRICHED_STATUSES:
                sk = item.get("SK", "")
                if sk.startswith("SRC#"):
                    ids.append("DS#" + sk[len("SRC#") :])
        start = page.last_evaluated_key
        if not start:
            break
    return ids


def run_for_namespace(
    namespace_id: str,
    project_id: str,
    domain_id: str,
    *,
    region: str,
    sources_table: str,
    emitter: EnrichmentMetricEmitter,
    client: BedrockClient | None = None,
    dao: DynamoDBDAO | None = None,
    focus_datasource_id: str | None = None,
) -> dict:
    """Default wiring: enumerate the namespace's sources, then run detection.

    ``focus_datasource_id`` is the source that just finished enriching; when given,
    only pairs involving it are inferred (bounding a pass to O(new x existing)
    instead of re-inferring every pair on every enrichment).

    Reads via the DataZone metadata store and writes back via the enrichment
    write path (same asset-revision mechanism Pass 1 uses).

    Serialised per namespace by a conditional-write lock so two sources finishing
    enrichment at the same time cannot run the pass concurrently and write the
    same child tables (a lost-update race on the shared asset). A pass that does
    not win the lock skips — the holder already covers the current settled state.
    The pass only ever reads/writes sources in ``_ENRICHED_STATUSES``, so it never
    touches a source that is still mid-enrichment; the only contention it must
    guard is pass-vs-pass, which this lock removes. The lock is stale-TTL'd so a
    crashed holder cannot wedge detection permanently.
    """
    lock_dao = dao or DynamoDBDAO(sources_table, region=region)
    if not _acquire_namespace_lock(lock_dao, namespace_id):
        logger.info("cross-source inference skipped: another pass holds the namespace lock (ns=%s)", namespace_id)
        return {"sources": 0, "relationships_written": 0, "tables_updated": 0, "skipped": "locked"}
    try:
        from coa_common.metadata_store.reader import read_assets_for_datasource

        from coa_sources.database.enrichment.table_enricher import (
            TABLE_TIMEOUT_SEC,
            _resolve_guardrail_id,
            _write_enriched_assets,
        )

        if client is None:
            try:
                client = BedrockClient(read_timeout=TABLE_TIMEOUT_SEC, guardrail_id=_resolve_guardrail_id())
            except Exception:
                # IAM / network / guardrail misconfiguration. Log with context and
                # surface a metric; re-raise so the caller's non-fatal boundary
                # records CrossSourceDetectionFailed. The lock is released by the
                # enclosing finally.
                logger.exception("cross_source_bedrock_client_init_failed: namespace=%s", namespace_id)
                emitter.emit_metric("CrossSourceBedrockInitFailed", 1, "Count")
                raise

        ds_ids = enriched_datasource_ids(namespace_id, region=region, sources_table=sources_table, dao=lock_dao)
        return run_cross_source_inference(
            datasource_ids=ds_ids,
            read_tables=lambda ds_id: read_assets_for_datasource(domain_id, project_id, ds_id),
            write_tables=lambda changed: _write_enriched_assets(changed, domain_id, project_id),
            client=client,
            emitter=emitter,
            focus_datasource_id=focus_datasource_id,
        )
    finally:
        _release_namespace_lock(lock_dao, namespace_id)
