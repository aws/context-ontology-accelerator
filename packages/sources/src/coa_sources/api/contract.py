# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime enforcement of the ``unified-sources.smithy`` input bounds.

API Gateway does not validate request bodies, query strings or path labels
against the model, so the Sources API Lambda is the only place the declared
``@length`` / ``@range`` bounds can hold. Two mechanisms:

* **Request bodies** are checked against the operation's generated
  ``*RequestContent`` model (:data:`BODY_MODELS`), which carries every bound in
  the model. Only *upper*-bound violations are reported here: type, required
  and enum checks stay with the handlers, which already answer them with their
  own messages, and lower bounds on optional strings (an empty ``columnName``
  meaning "absent") are existing, tolerated behaviour. Bodies the full model
  already validates (CreateSource, RescanSource) are not listed.
* **Query parameters and path labels** are not part of those models, so their
  bounds are constants here. ``tests/unit/api/test_contract_bounds.py`` asserts
  each constant equals the Smithy trait it mirrors.

Error messages name the member and the bound, never the rejected value.
"""

from __future__ import annotations

from typing import Any

from coa_control_plane_server.models.get_source_upload_urls_request_content import GetSourceUploadUrlsRequestContent
from coa_control_plane_server.models.keep_rescan_removal_request_content import KeepRescanRemovalRequestContent
from coa_control_plane_server.models.put_source_event_rescan_request_content import PutSourceEventRescanRequestContent
from coa_control_plane_server.models.put_source_rescan_schedule_request_content import (
    PutSourceRescanScheduleRequestContent,
)
from coa_control_plane_server.models.review_source_column_request_content import ReviewSourceColumnRequestContent
from coa_control_plane_server.models.review_source_table_request_content import ReviewSourceTableRequestContent
from coa_control_plane_server.models.update_source_column_metadata_request_content import (
    UpdateSourceColumnMetadataRequestContent,
)
from coa_control_plane_server.models.update_source_metadata_request_content import UpdateSourceMetadataRequestContent
from coa_control_plane_server.models.update_source_table_keys_request_content import UpdateSourceTableKeysRequestContent
from coa_control_plane_server.models.update_source_table_metadata_request_content import (
    UpdateSourceTableMetadataRequestContent,
)
from pydantic import BaseModel, ValidationError

# Opaque cursor bound (common.smithy PaginationToken).
PAGINATION_TOKEN_MAX_LENGTH = 8192

# ListSources maxResults: @range(min: 1, max: 100).
LIST_SOURCES_MAX_RESULTS = 100

# ListSourceTables maxResults: @range(min: 1, max: 50) — the catalog search
# page limit. Values from 51 to LIST_SOURCE_TABLES_LEGACY_MAX_RESULTS were
# accepted under the earlier 1000 bound and are still reduced to 50 rather than
# rejected, so an older client keeps working.
LIST_SOURCE_TABLES_MAX_RESULTS = 50
LIST_SOURCE_TABLES_LEGACY_MAX_RESULTS = 1000

# ListSourceScanJobs maxResults: @range(min: 1, max: 1000); also the default.
LIST_SOURCE_SCAN_JOBS_MAX_RESULTS = 1000

# Path-label length bounds: TableId, ColumnName and GetSourceScanJob.jobId.
# GetSourceTable declares its own, wider tableId (non-ASCII names percent-decode
# to more characters than TableId's ASCII pattern anticipates).
LABEL_MAX_LENGTHS: dict[str, int] = {"tableId": 512, "columnName": 256, "jobId": 128}
GET_SOURCE_TABLE_ID_MAX_LENGTH = 1024

_BASE = "/namespaces/{namespaceId}/sources"
_GET_SOURCE_TABLE = ("GET", f"{_BASE}/{{sourceId}}/tables/{{tableId}}")

# (METHOD, resource) -> generated request-content model whose upper bounds the
# body must respect.
BODY_MODELS: dict[tuple[str, str], type[BaseModel]] = {
    ("POST", f"{_BASE}/upload-urls"): GetSourceUploadUrlsRequestContent,
    ("PUT", f"{_BASE}/{{sourceId}}/metadata"): UpdateSourceMetadataRequestContent,
    ("PUT", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/review"): ReviewSourceTableRequestContent,
    ("PUT", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/keep"): KeepRescanRemovalRequestContent,
    ("PATCH", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/metadata"): UpdateSourceTableMetadataRequestContent,
    ("PATCH", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/keys"): UpdateSourceTableKeysRequestContent,
    ("PUT", f"{_BASE}/{{sourceId}}/tables/{{tableId}}/columns/{{columnName}}/review"): ReviewSourceColumnRequestContent,
    (
        "PATCH",
        f"{_BASE}/{{sourceId}}/tables/{{tableId}}/columns/{{columnName}}/metadata",
    ): UpdateSourceColumnMetadataRequestContent,
    ("PUT", f"{_BASE}/{{sourceId}}/rescan-schedule"): PutSourceRescanScheduleRequestContent,
    ("PUT", f"{_BASE}/{{sourceId}}/event-rescan"): PutSourceEventRescanRequestContent,
}

# Pydantic error types that mean "above the declared maximum".
_UPPER_BOUND_ERRORS = frozenset({"string_too_long", "too_long", "less_than", "less_than_equal"})


def _field_path(loc: tuple[int | str, ...], model: type[BaseModel]) -> str:
    """Render a pydantic error location with wire (camelCase) names, e.g. ``foreignKeys[3].provenance``."""
    parts: list[str] = []
    current: Any = model
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
            continue
        wire = item
        fields = getattr(current, "model_fields", None) or {}
        field = fields.get(item)
        if field is not None:
            wire = field.alias or item
            current = _inner_model(field.annotation)
        parts.append(f".{wire}" if parts else wire)
    return "".join(parts)


def _inner_model(annotation: Any) -> Any:
    """The BaseModel nested in ``annotation`` (through Optional / List / Annotated), if any."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in getattr(annotation, "__args__", ()) or ():
        found = _inner_model(arg)
        if found is not None:
            return found
    return None


def body_bound_error(method: str, resource: str, body: Any) -> str | None:
    """First upper-bound violation in ``body`` for the route, or None.

    Returns None for routes without a listed model and for a body that is not
    a JSON object (the handler reports those).
    """
    model = BODY_MODELS.get((method, resource))
    if model is None or not isinstance(body, dict):
        return None
    try:
        model.model_validate(body)
    except ValidationError as exc:
        for err in exc.errors():
            if err.get("type") in _UPPER_BOUND_ERRORS:
                bound = (err.get("ctx") or {}).get("max_length") or (err.get("ctx") or {}).get("le")
                where = _field_path(tuple(err.get("loc", ())), model)
                suffix = f" (maximum {bound})" if bound is not None else ""
                return f"{where} exceeds its maximum allowed size{suffix}"
    return None


def label_bound_error(method: str, resource: str, path_params: dict[str, str]) -> str | None:
    """First path label longer than its declared maximum, or None."""
    for label, limit in LABEL_MAX_LENGTHS.items():
        if label == "tableId" and (method, resource) == _GET_SOURCE_TABLE:
            limit = GET_SOURCE_TABLE_ID_MAX_LENGTH
        value = path_params.get(label)
        if value is not None and len(value) > limit:
            return f"{label} exceeds the maximum length of {limit}"
    return None


def pagination_token_error(token: str | None) -> str | None:
    """Error for a ``nextToken`` longer than :data:`PAGINATION_TOKEN_MAX_LENGTH`, or None."""
    if token is not None and len(token) > PAGINATION_TOKEN_MAX_LENGTH:
        return f"nextToken exceeds the maximum length of {PAGINATION_TOKEN_MAX_LENGTH}"
    return None
