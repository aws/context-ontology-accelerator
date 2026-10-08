# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""APPROVED-source enforcement for metric creation/update.

The UI's Create Metric form only offers sources with status APPROVED or
COMPLETED; the API and OSI import must enforce the same rule so they cannot
be used to bypass it. This module reads the source record directly from the
unified sources table (``PK=NS#{namespaceId}``, ``SK=SRC#{sourceId}`` —
written by ``packages/sources``) and checks its ``status``.

Fail-closed by default: if the sources table is not configured or the read
fails, enforcement raises ``SourceValidationUnavailableError`` (callers map
this to a 503) instead of silently allowing the reference. Set
``ALLOW_PERMISSIVE_SOURCE_LOOKUP=true`` (dev/testing only) to skip
enforcement — a WARNING is logged whenever permissive mode is active.
"""

from __future__ import annotations

import os
import re

import structlog
from botocore.exceptions import BotoCoreError, ClientError
from coa_common.dao import DynamoDBDAO
from coa_control_plane_server.models.source_status import SourceStatus

logger = structlog.get_logger(__name__)

PERMISSIVE_ENV = "ALLOW_PERMISSIVE_SOURCE_LOOKUP"

# Statuses a metric may reference — keep in sync with the UI filter in
# packages/web-app/src/pages/MetricForm.tsx (APPROVED || COMPLETED).
_ALLOWED_STATUSES = frozenset({SourceStatus.APPROVED.value, SourceStatus.COMPLETED.value})

# Mirrors ``DataSourceId`` in models/src/main/smithy/metric-service.smithy.
# Applied here too because OSI import builds metrics without the generated model.
_DATA_SOURCE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
DATA_SOURCE_ID_FORMAT_MESSAGE = "dataSourceId must be 1-128 characters of letters, digits, '.', '_', or '-'"


class SourceValidationUnavailableError(RuntimeError):
    """Source status could not be verified (missing config or read failure)."""


def permissive_lookup_enabled() -> bool:
    """True when the dev-only permissive mode is explicitly enabled via env."""
    return os.environ.get(PERMISSIVE_ENV, "").lower() == "true"


def check_source_approved(namespace: str, data_source_id: str) -> str | None:
    """Verify the referenced data source is APPROVED/COMPLETED.

    Args:
        namespace: The namespace the metric belongs to.
        data_source_id: The source referenced by the metric's ``dataSourceId``.

    Returns:
        None when the source is approved (or permissive mode is enabled),
        otherwise a human-readable rejection message for a 400 response.

    Raises:
        SourceValidationUnavailableError: When enforcement is impossible
            (table not configured, or the DynamoDB read failed) and
            permissive mode is not enabled — fail closed.
    """
    if not data_source_id:
        return "dataSourceId is required"

    if not _DATA_SOURCE_ID_RE.fullmatch(data_source_id):
        return DATA_SOURCE_ID_FORMAT_MESSAGE

    if permissive_lookup_enabled():
        logger.warning(
            "permissive_source_lookup_active",
            namespace=namespace,
            data_source_id=data_source_id,
            detail="source approval NOT enforced — dev/testing mode",
        )
        return None

    table = os.environ.get("DATA_SOURCES_TABLE", "")
    if not table:
        raise SourceValidationUnavailableError("DATA_SOURCES_TABLE not configured")

    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        item = DynamoDBDAO(table, region=region).get({"PK": f"NS#{namespace}", "SK": f"SRC#{data_source_id}"})
    except ClientError as exc:
        # Surface the specific AWS error code (ResourceNotFoundException,
        # AccessDeniedException, ProvisionedThroughputExceededException, …)
        # so operational issues are diagnosable from the 503 + logs.
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "sources_table_read_failed",
            table=table,
            namespace=namespace,
            data_source_id=data_source_id,
            error_code=error_code,
            exc_info=True,
        )
        raise SourceValidationUnavailableError(f"sources table read failed ({error_code}): {exc}") from exc
    except BotoCoreError as exc:
        # Non-API boto3 failures (connection, endpoint, credential resolution).
        logger.error(
            "sources_table_read_failed",
            table=table,
            namespace=namespace,
            data_source_id=data_source_id,
            error_code=type(exc).__name__,
            exc_info=True,
        )
        raise SourceValidationUnavailableError(f"sources table read failed ({type(exc).__name__}): {exc}") from exc
    except Exception as exc:
        # Last-resort fallback so an unexpected error still fails closed as a
        # 503 (SourceValidationUnavailableError) instead of a raw 500.
        logger.error(
            "sources_table_read_failed_unexpected",
            table=table,
            namespace=namespace,
            data_source_id=data_source_id,
            error_code=type(exc).__name__,
            exc_info=True,
        )
        raise SourceValidationUnavailableError(f"sources table read failed: {exc}") from exc

    if not item:
        return f"Data source '{data_source_id}' does not exist in namespace '{namespace}'"

    status = str(item.get("status", ""))
    if status not in _ALLOWED_STATUSES:
        return (
            f"Data source '{data_source_id}' has status '{status or 'UNKNOWN'}' — "
            f"metrics can only reference APPROVED or COMPLETED sources"
        )
    return None


def check_source_table_exists(namespace: str, data_source_id: str, source_table: str) -> str | None:
    """Verify the declared ``sourceTable`` exists in the source's catalog (#161).

    Rejects only **provable** absence. An unconfigured catalog lookup falls back
    to the existing soft warning, while a configured lookup that cannot read the
    catalog fails closed with a 503. A source can legitimately be COMPLETED while
    none of its assets are indexed yet — so a table is treated as absent only
    when the catalog was read successfully, knows at least one table for the
    source, and no unambiguous approved asset resolves for the declared name.
    This rejects unapproved assets and ambiguous bare names as well as missing
    tables.

    "Cannot configure" and "read failed" are deliberately NOT the same thing:

    - ``build_data_source_lookup`` returns ``None`` for benign, expected
      reasons — env vars unset, the namespace row absent, no
      ``dataZoneProjectId`` yet. That is an *unprovisioned* namespace, not a
      failure, and 503-ing it would make every ``sourceTable`` metric
      un-creatable there. It degrades to the soft warning (pre-#161 behavior).
    - A lookup that *built* but whose catalog read failed
      (``catalog_available()`` false) is a genuine outage: enforcement is
      impossible on a source we should be able to read, so that fails closed
      with a 503.

    Args:
        namespace: The namespace the metric belongs to.
        data_source_id: The source referenced by the metric's ``dataSourceId``.
        source_table: The metric's declared ``sourceTable``.

    Returns:
        None when the table exists, its absence cannot be proven, the lookup
        cannot be configured, or permissive mode is enabled; otherwise a
        rejection message for a 400.

    Raises:
        SourceValidationUnavailableError: When the lookup was built but its
            catalog could not be read — fail closed (503), matching
            ``check_source_approved``.
    """
    if not source_table:
        return None

    if permissive_lookup_enabled():
        logger.warning(
            "permissive_source_lookup_active",
            namespace=namespace,
            data_source_id=data_source_id,
            detail="sourceTable existence NOT enforced — dev/testing mode",
        )
        return None

    from coa_metrics import data_source_lookup_factory

    try:
        lookup = data_source_lookup_factory.build_data_source_lookup(namespace)
    except Exception as exc:
        logger.error("data_source_lookup_init_failed", namespace=namespace, error=str(exc), exc_info=True)
        raise SourceValidationUnavailableError(f"data source lookup unavailable: {exc}") from exc

    if lookup is None:
        # Not configured / namespace not provisioned — not an outage. Degrade to
        # the soft table_reference warning validate_metric already emits.
        logger.info(
            "source_table_check_skipped_no_lookup",
            namespace=namespace,
            data_source_id=data_source_id,
            source_table=source_table,
        )
        return None

    if not lookup.catalog_available(data_source_id):
        raise SourceValidationUnavailableError(
            f"table catalog unavailable for data source '{data_source_id}' in namespace '{namespace}'"
        )

    known = lookup.known_tables(data_source_id)
    if not known:
        # Source known to the sources table but with no indexed assets yet —
        # absence is not provable, so fall back to the soft warning. Debug, not
        # info: this fires on every create against a COMPLETED source.
        logger.debug(
            "source_table_check_skipped_empty_catalog",
            namespace=namespace,
            data_source_id=data_source_id,
            source_table=source_table,
        )
        return None

    declared = source_table.lower()
    exists = lookup.table_exists(data_source_id, source_table)

    # A legacy catalog can expose a genuinely bare asset name while the metric
    # uses the documented ``schema.table`` form. Only fall back when that bare
    # name is itself present in the raw catalog. Never synthesize it from another
    # qualified asset, which would let ``sales.orders`` resolve to
    # ``archive.orders``.
    bare = declared.rsplit(".", 1)[-1]
    if not exists and "." in declared and bare in known:
        exists = lookup.table_exists(data_source_id, bare)

    # The per-asset approval read can fail after the name index succeeded.
    # Re-check availability so a transient failure remains a 503 rather than
    # being cached or reported as a definitive rejection.
    if not lookup.catalog_available(data_source_id):
        raise SourceValidationUnavailableError(
            f"table catalog unavailable for data source '{data_source_id}' in namespace '{namespace}'"
        )

    if exists:
        return None

    return (
        f"Source table '{source_table}' not found in data source '{data_source_id}' — "
        f"known tables: {', '.join(sorted(known))}"
    )
