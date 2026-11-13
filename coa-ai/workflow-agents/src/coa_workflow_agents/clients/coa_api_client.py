# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Thin REST client for COA's Control Plane API.

Methods map to Smithy operations, plus ``put_presigned``/``download_presigned``/
``download_presigned_prefix`` for presigned S3 transfers. No polling or business
logic (that is the tool layer's job): build the call, attach the bearer token,
raise ``CoaApiError`` on an HTTP error. Transport errors propagate as ``requests``
exceptions.

The only retry is for POSTs that carry a ``clientToken``: a timeout, connection
error or HTTP 503 is retried with the same token, which COA de-duplicates, and a
409 saying the token's first request is still running is waited out. Every other
call is sent once.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import requests
import structlog
from coa_control_plane_server.models.export_format import ExportFormat

logger = structlog.get_logger(__name__)

_DEFAULT_HTTP_TIMEOUT_S = 30.0

# Path segments of the three GetGraph* routes (graph/{kind}).
_GRAPH_VERTEX_ROUTES = frozenset({"class", "object-property", "datatype-property"})

# The literal words of this client's request paths (plus the graph vertex kinds).
# A logged route keeps only these. A unit test derives the set from this module's
# path strings, so a new endpoint's words must be added here.
_ROUTE_WORDS = (
    frozenset(
        {
            "accept",
            "approve",
            "bulk-delete-metrics",
            "cancel",
            "columns",
            "compile-constraints",
            "datasources",
            "download",
            "event-rescan",
            "export-osi",
            "fetch",
            "foundational",
            "grants",
            "graph",
            "health",
            "import-jobs",
            "import-osi",
            "induce",
            "induced",
            "infer-constraints",
            "ingest-from-s3",
            "ingest-status",
            "jobs",
            "keep",
            "keys",
            "load",
            "me",
            "metadata",
            "metrics",
            "namespaces",
            "ontologies",
            "ontology",
            "ontology-overview",
            "principals",
            "proposals",
            "reject",
            "repair-datatypes",
            "rescan",
            "rescan-schedule",
            "review",
            "roles",
            "scan",
            "schema",
            "search",
            "sources",
            "status",
            "system-health",
            "tables",
            "update",
            "upload-url",
            "upload-urls",
            "validate",
        }
    )
    | _GRAPH_VERTEX_ROUTES
)


def sanitized_route(path: str) -> str:
    """``path`` without its query, every segment not in ``_ROUTE_WORDS`` replaced by ``{id}``.

    ``/namespaces/<uuid>/metrics/churn-rate`` -> ``/namespaces/{id}/metrics/{id}``, so a log
    never carries an id or name. Split without URL-decoding: an encoded ``/`` stays in one ``{id}``.
    """
    route = path.split("?", 1)[0]
    return "/".join(seg if not seg or seg in _ROUTE_WORDS else "{id}" for seg in route.split("/"))


# clientToken retry policy. Only 503 is retried: a 504 means the backend may have
# started the job, and COA releases the token's claim on any non-2xx, so a retry
# could start a duplicate. SSLError subclasses ConnectionError but is permanent.
_IDEMPOTENT_RETRY_ATTEMPTS = 3
_IDEMPOTENT_RETRY_BACKOFF_S = 1.0
_RETRYABLE_HTTP_STATUS = 503
_CLIENT_TOKEN_FIELD = "clientToken"
# A retry that reaches COA while the token's first request still runs gets a 409
# with this text (coa_common.idempotency). Wait for it: the first response is
# then replayed, rather than failing a job that did start.
_IN_PROGRESS_CONFLICT_TEXT = "clientToken is still in progress"
_IN_PROGRESS_RETRY_WAIT_S = 5.0
_IN_PROGRESS_MAX_RETRIES = 6

# Largest maxResults each paged list endpoint accepts (Smithy @range); the tools
# clamp page sizes to these. Values below 1 are rejected everywhere.
LIST_SOURCES_MAX_RESULTS = 100
LIST_SOURCE_TABLES_MAX_RESULTS = 50
LIST_SOURCE_SCAN_JOBS_MAX_RESULTS = 1000
LIST_INDUCTION_JOBS_MAX_RESULTS = 200
LIST_PROPOSALS_MAX_RESULTS = 200
LIST_INDUCED_DATASOURCES_MAX_RESULTS = 1000
LIST_ONTOLOGIES_MAX_RESULTS = 1000
LIST_METRICS_MAX_RESULTS = 200
LIST_NAMESPACES_MAX_RESULTS = 100
LIST_PRINCIPAL_GRANTS_MAX_RESULTS = 1000
LIST_GRANTS_MAX_RESULTS = 100

# Presigned S3 transfers. The URL is a bearer credential: never logged or put in an error.
_PRESIGNED_TIMEOUT_S = 120.0
_PRESIGNED_CHUNK_BYTES = 1 << 20


class TokenProvider(Protocol):
    """Source of the bearer token: ``auth.caller.CallerTokenProvider``, or a fake in tests."""

    def get_id_token(self) -> str:
        """Return a valid COA bearer (ID) token."""
        ...


def _seg(value: str) -> str:
    """Percent-encode one path segment, ``/`` included, so a value cannot add segments (CWE-116).

    ``.``, ``..`` and ``""`` are refused: ``requests`` collapses dot segments, so
    ``.../sources/..`` would reach a different operation than the one intended.
    """
    if value in ("", ".", ".."):
        raise ValueError(f"invalid COA path segment {value!r}")
    return quote(value, safe="")


def _query_string(params: dict[str, Any]) -> str:
    """URL-encoded ``?a=b&c=d``, dropping ``None`` and ``""`` values (``0`` is kept)."""
    filtered = {k: v for k, v in params.items() if v is not None and v != ""}
    if not filtered:
        return ""
    return "?" + urlencode(filtered)


def is_client_token_conflict(exc: Exception) -> bool:
    """True for COA's clientToken 409s, which are about the token, not the operation's state."""
    return isinstance(exc, CoaApiError) and exc.status_code == 409 and "clientToken" in exc.body


class CoaApiError(RuntimeError):
    """An HTTP error (>= 400) or non-JSON success body from COA, or a failed presigned transfer.

    A presigned transfer fails on a status >= 300, or with status 0 when there was no response.
    """

    def __init__(self, method: str, path: str, status_code: int, body: str) -> None:
        """Store the failed request's method, path, status and body."""
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status_code}: {body[:1000]}")


# Frozen: a client is bound to one caller's forwarded token for its lifetime.
@dataclass(frozen=True)
class CoaApiClient:
    """Calls COA's REST API with the caller's forwarded token (``auth/caller.py``).

    Keeps one ``requests.Session`` so polling reuses a keep-alive connection.
    Usable as a context manager; ``close()`` releases the pool.
    """

    base_url: str
    token_provider: TokenProvider
    timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S
    _session: requests.Session = field(default_factory=requests.Session, init=False, repr=False)

    def close(self) -> None:
        """Release the underlying connection pool. Safe to call more than once."""
        self._session.close()

    def __enter__(self) -> CoaApiClient:
        """Return self, for use as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the session on exit."""
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers_out: dict[str, str] | None = None,
    ) -> Any:
        url = self.base_url.rstrip("/") + path
        token = self.token_provider.get_id_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            resp = self._session.request(method, url, json=json_body, headers=headers, timeout=self.timeout_s)
        except requests.RequestException as exc:
            logger.warning(
                "coa_api_error",
                method=method,
                route=sanitized_route(path),
                status_code=0,
                error_type=type(exc).__name__,
            )
            raise
        if headers_out is not None:
            # The last response's headers, lower-cased keys (callers read e.g.
            # COA's ``X-Idempotent-Replay``); replaced on every attempt.
            headers_out.clear()
            headers_out.update({k.lower(): v for k, v in resp.headers.items()})
        if resp.status_code >= 400:
            logger.warning(
                "coa_api_error",
                method=method,
                route=sanitized_route(path),
                status_code=resp.status_code,
                error_type="",
            )
            raise CoaApiError(method, path, resp.status_code, resp.text)
        if not resp.content:
            return {}
        try:
            # ``Any``: some list endpoints return a bare JSON array.
            return resp.json()
        except ValueError as e:
            # A 2xx with a non-JSON body (a gateway error page) is still a typed failure.
            raise CoaApiError(method, path, resp.status_code, resp.text) from e

    def get(self, path: str) -> dict[str, Any]:
        """GET an object-returning endpoint (array endpoints call ``_request``)."""
        return self._as_object(self._request("GET", path))

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST a JSON body once, no retry."""
        return self._as_object(self._request("POST", path, json_body=body or {}))

    def post_idempotent(
        self, path: str, body: dict[str, Any], client_token: str, *, headers_out: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """POST with ``clientToken``, retrying transient failures with the same token.

        Up to ``_IDEMPOTENT_RETRY_ATTEMPTS`` timeouts, connection errors or 503s (linear
        backoff), and up to ``_IN_PROGRESS_MAX_RETRIES`` "still in progress" 409s every
        ``_IN_PROGRESS_RETRY_WAIT_S``. Anything else raises at once. ``headers_out``
        receives the final response's headers, keys lower-cased.
        """
        payload = {**body, _CLIENT_TOKEN_FIELD: client_token}
        transient_failures = 0
        in_progress_conflicts = 0
        while True:
            try:
                return self._as_object(self._request("POST", path, json_body=payload, headers_out=headers_out))
            except requests.exceptions.SSLError:
                raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                transient_failures += 1
                if transient_failures >= _IDEMPOTENT_RETRY_ATTEMPTS:
                    raise
                reason, wait_s = type(exc).__name__, _IDEMPOTENT_RETRY_BACKOFF_S * transient_failures
            except CoaApiError as exc:
                if exc.status_code == _RETRYABLE_HTTP_STATUS:
                    transient_failures += 1
                    if transient_failures >= _IDEMPOTENT_RETRY_ATTEMPTS:
                        raise
                    reason, wait_s = f"HTTP {exc.status_code}", _IDEMPOTENT_RETRY_BACKOFF_S * transient_failures
                elif exc.status_code == 409 and _IN_PROGRESS_CONFLICT_TEXT in exc.body:
                    in_progress_conflicts += 1
                    if in_progress_conflicts > _IN_PROGRESS_MAX_RETRIES:
                        raise
                    reason, wait_s = "HTTP 409 in progress", _IN_PROGRESS_RETRY_WAIT_S
                else:
                    raise
            logger.warning(
                "coa_api_client.idempotent_post_retry",
                route=sanitized_route(path),
                transient_failures=transient_failures,
                in_progress_conflicts=in_progress_conflicts,
                reason=reason,
            )
            time.sleep(wait_s)

    def _post_maybe_idempotent(
        self,
        path: str,
        body: dict[str, Any],
        client_token: str | None,
        *,
        headers_out: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """``post_idempotent`` when a token is given, else a plain single ``post``."""
        if client_token is None:
            return self._as_object(self._request("POST", path, json_body=body, headers_out=headers_out))
        return self.post_idempotent(path, body, client_token, headers_out=headers_out)

    def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PUT a JSON body."""
        return self._as_object(self._request("PUT", path, json_body=body))

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH a JSON body."""
        return self._as_object(self._request("PATCH", path, json_body=body))

    def delete(self, path: str) -> dict[str, Any]:
        """DELETE."""
        return self._as_object(self._request("DELETE", path))

    @staticmethod
    def _as_object(parsed: Any) -> dict[str, Any]:
        """``parsed`` if it is a dict, else ``{}``."""
        return parsed if isinstance(parsed, dict) else {}

    # ------------------------------------------------------------------
    # Sources (unified-sources.smithy)
    # ------------------------------------------------------------------

    def create_source(
        self,
        namespace_id: str,
        body: dict[str, Any],
        *,
        client_token: str | None = None,
        headers_out: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """``CreateSource`` — POST /namespaces/{namespaceId}/sources (retry-safe with ``client_token``)."""
        return self._post_maybe_idempotent(
            f"/namespaces/{_seg(namespace_id)}/sources", body, client_token, headers_out=headers_out
        )

    def get_source(self, namespace_id: str, source_id: str) -> dict[str, Any]:
        """``GetSource`` — GET /namespaces/{namespaceId}/sources/{sourceId}."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}")

    def list_sources(
        self,
        namespace_id: str,
        *,
        next_token: str | None = None,
        max_results: int = 50,
        source_type: str | None = None,
    ) -> Any:
        """``ListSources`` — GET /namespaces/{namespaceId}/sources. Object with items, or a bare array."""
        q = _query_string({"maxResults": max_results, "nextToken": next_token, "sourceType": source_type})
        return self._request("GET", f"/namespaces/{_seg(namespace_id)}/sources{q}")

    def list_source_tables(
        self,
        namespace_id: str,
        source_id: str,
        *,
        next_token: str | None = None,
        max_results: int = 50,
        review_status: str | None = None,
    ) -> dict[str, Any]:
        """``ListSourceTables`` — GET .../sources/{sourceId}/tables → ``{items, nextToken?, skippedAssets?}``."""
        q = _query_string({"maxResults": max_results, "nextToken": next_token, "reviewStatus": review_status})
        return self.get(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/tables{q}")

    def update_source_metadata(self, namespace_id: str, source_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``UpdateSourceMetadata`` — PUT .../sources/{sourceId}/metadata."""
        return self.put(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/metadata", body)

    def list_source_scan_jobs(
        self,
        namespace_id: str,
        source_id: str,
        *,
        next_token: str | None = None,
        max_results: int = LIST_SOURCE_SCAN_JOBS_MAX_RESULTS,
    ) -> dict[str, Any]:
        """``ListSourceScanJobs`` — GET .../sources/{sourceId}/scan → ``{items, nextToken?}``, newest first."""
        q = _query_string({"maxResults": max_results, "nextToken": next_token})
        return self.get(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/scan{q}")

    def keep_rescan_removal(
        self, namespace_id: str, source_id: str, table_id: str, *, column_name: str | None = None
    ) -> dict[str, Any]:
        """``KeepRescanRemoval`` — PUT .../tables/{tableId}/keep. ``column_name=None`` keeps the whole table.

        A blank name is refused: COA treats ``""`` as absent and would keep the whole table, irreversibly.
        """
        if column_name is not None and not column_name.strip():
            raise ValueError("column_name must not be blank; pass None to keep the whole table")
        body = {"columnName": column_name} if column_name is not None else {}
        return self.put(
            f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/tables/{_seg(table_id)}/keep", body
        )

    def put_source_rescan_schedule(self, namespace_id: str, source_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``PutSourceRescanSchedule`` — PUT .../rescan-schedule. A missing ``enabled`` deletes the schedule."""
        return self.put(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/rescan-schedule", body)

    def put_source_event_rescan(self, namespace_id: str, source_id: str, *, enabled: bool) -> dict[str, Any]:
        """``PutSourceEventRescan`` — PUT .../sources/{sourceId}/event-rescan ``{enabled}`` (Glue only)."""
        return self.put(
            f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/event-rescan", {"enabled": bool(enabled)}
        )

    def get_source_table(self, namespace_id: str, source_id: str, table_id: str) -> dict[str, Any]:
        """``GetSourceTable`` — GET .../sources/{sourceId}/tables/{tableId}."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/tables/{_seg(table_id)}")

    def review_source_table(self, namespace_id: str, source_id: str, table_id: str, decision: str) -> dict[str, Any]:
        """``ReviewSourceTable`` — PUT .../tables/{tableId}/review. decision: APPROVED|REJECTED."""
        return self.put(
            f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/tables/{_seg(table_id)}/review",
            {"decision": decision},
        )

    def review_source_column(
        self, namespace_id: str, source_id: str, table_id: str, column_name: str, decision: str
    ) -> dict[str, Any]:
        """``ReviewSourceColumn`` — PUT .../tables/{tableId}/columns/{columnName}/review."""
        return self.put(
            f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/tables/{_seg(table_id)}/columns/{_seg(column_name)}/review",
            {"decision": decision},
        )

    def update_source_table_keys(
        self,
        namespace_id: str,
        source_id: str,
        table_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """``UpdateSourceTableKeys`` — PATCH .../tables/{tableId}/keys (an empty list clears a key)."""
        return self.patch(
            f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/tables/{_seg(table_id)}/keys",
            body,
        )

    def update_source_table_metadata(
        self,
        namespace_id: str,
        source_id: str,
        table_id: str,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        """``UpdateSourceTableMetadata`` — PATCH .../tables/{tableId}/metadata (partial override)."""
        return self.patch(
            f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/tables/{_seg(table_id)}/metadata",
            {"overrides": overrides},
        )

    def update_source_column_metadata(
        self,
        namespace_id: str,
        source_id: str,
        table_id: str,
        column_name: str,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        """``UpdateSourceColumnMetadata`` — PATCH .../columns/{columnName}/metadata (partial override)."""
        return self.patch(
            f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/tables/{_seg(table_id)}/columns/{_seg(column_name)}/metadata",
            {"overrides": overrides},
        )

    def delete_source(self, namespace_id: str, source_id: str) -> dict[str, Any]:
        """``DeleteSource`` — DELETE .../sources/{sourceId} (202, async: DELETING, then the row is removed)."""
        return self.delete(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}")

    def approve_source(self, namespace_id: str, source_id: str) -> dict[str, Any]:
        """``ApproveSource`` — POST .../sources/{sourceId}/approve (202, async)."""
        return self.post(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/approve")

    def reject_source(self, namespace_id: str, source_id: str) -> dict[str, Any]:
        """``RejectSource`` — POST .../sources/{sourceId}/reject (202, async)."""
        return self.post(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/reject")

    def get_source_scan_job(self, namespace_id: str, source_id: str, job_id: str) -> dict[str, Any]:
        """``GetSourceScanJob`` — GET .../sources/{sourceId}/scan/{jobId} (carries the job's ``errorMessage``)."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/scan/{_seg(job_id)}")

    def rescan_source(
        self,
        namespace_id: str,
        source_id: str,
        *,
        confirm_discard_open_review: bool | None = None,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        """``RescanSource`` — POST .../sources/{sourceId}/rescan (202; retry-safe with ``client_token``).

        In ``RESCAN_REVIEW`` COA answers 409 unless ``confirm_discard_open_review`` is true.
        """
        body = (
            {"confirmDiscardOpenReview": confirm_discard_open_review} if confirm_discard_open_review is not None else {}
        )
        return self._post_maybe_idempotent(
            f"/namespaces/{_seg(namespace_id)}/sources/{_seg(source_id)}/rescan", body, client_token
        )

    def get_source_upload_urls(self, namespace_id: str, files: list[dict[str, str]]) -> dict[str, Any]:
        """``GetSourceUploadUrls`` — POST .../sources/upload-urls. The URLs are bearer credentials."""
        return self.post(f"/namespaces/{_seg(namespace_id)}/sources/upload-urls", {"files": files})

    # ------------------------------------------------------------------
    # Induction (ontology-induction.smithy)
    # ------------------------------------------------------------------

    def start_induction(
        self, namespace_id: str, body: dict[str, Any], *, client_token: str | None = None
    ) -> dict[str, Any]:
        """``StartInduction`` — POST /namespaces/{namespaceId}/induce (202; retry-safe with ``client_token``)."""
        return self._post_maybe_idempotent(f"/namespaces/{_seg(namespace_id)}/induce", body, client_token)

    def get_induction_job(self, namespace_id: str, job_id: str, *, include: str | None = None) -> dict[str, Any]:
        """``GetInductionJob`` — GET .../induce/jobs/{jobId}."""
        q = _query_string({"include": include})
        return self.get(f"/namespaces/{_seg(namespace_id)}/induce/jobs/{_seg(job_id)}{q}")

    def list_induction_jobs(
        self, namespace_id: str, *, status: str | None = None, next_token: str | None = None, max_results: int = 50
    ) -> Any:
        """``ListInductionJobs`` — GET .../induce/jobs. Shape: object with items, or a bare array."""
        q = _query_string({"maxResults": max_results, "status": status, "nextToken": next_token})
        return self._request("GET", f"/namespaces/{_seg(namespace_id)}/induce/jobs{q}")

    def list_induced_datasources(
        self,
        namespace_id: str,
        *,
        next_token: str | None = None,
        max_results: int = LIST_INDUCED_DATASOURCES_MAX_RESULTS,
    ) -> dict[str, Any]:
        """``ListInducedDatasources`` — GET .../induce/datasources/induced (entry keys are mixed case)."""
        q = _query_string({"maxResults": max_results, "nextToken": next_token})
        return self.get(f"/namespaces/{_seg(namespace_id)}/induce/datasources/induced{q}")

    # ------------------------------------------------------------------
    # Proposals (ontology-induction.smithy)
    # ------------------------------------------------------------------

    def get_proposal(self, namespace_id: str, proposal_id: str) -> dict[str, Any]:
        """``GetProposal`` — GET .../proposals/{proposalId}."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}")

    def list_proposals(
        self,
        namespace_id: str,
        *,
        status: str | None = None,
        ontology_id: str | None = None,
        source_type: str | None = None,
        proposal_type: str | None = None,
        next_token: str | None = None,
        max_results: int = 50,
    ) -> Any:
        """``ListProposals`` — GET .../proposals. Object with items, or a bare array."""
        q = _query_string(
            {
                "maxResults": max_results,
                "status": status,
                "ontologyId": ontology_id,
                "source_type": source_type,
                "type": proposal_type,
                "nextToken": next_token,
            }
        )
        return self._request("GET", f"/namespaces/{_seg(namespace_id)}/proposals{q}")

    def validate_proposal(
        self,
        namespace_id: str,
        proposal_id: str,
        *,
        client_token: str | None = None,
        shacl_shapes_turtle: str | None = None,
        tiers: list[str] | None = None,
        competency_questions: list[str] | None = None,
    ) -> dict[str, Any]:
        """``ValidateProposal`` — POST .../proposals/{proposalId}/validate (202; retry-safe with a token).

        Fields go snake_case: the handler has no aliases and drops the Smithy camelCase names.
        """
        body: dict[str, Any] = {}
        if shacl_shapes_turtle:
            body["shacl_shapes_turtle"] = shacl_shapes_turtle
        if tiers:
            body["tiers"] = tiers
        if competency_questions:
            body["competency_questions"] = competency_questions
        return self._post_maybe_idempotent(
            f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/validate", body, client_token
        )

    def update_proposal(self, namespace_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``UpdateProposal`` — POST .../proposals/update (``proposalId`` goes in the body)."""
        return self.post(f"/namespaces/{_seg(namespace_id)}/proposals/update", body)

    def get_proposal_upload_urls(self, namespace_id: str, proposal_id: str) -> dict[str, Any]:
        """``GetProposalUploadUrls`` — POST .../upload-url. Create-only PUTs (``If-None-Match: *``); bearer URLs."""
        return self.post(f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/upload-url", {})

    def infer_constraints(
        self, namespace_id: str, proposal_id: str, *, client_token: str | None = None
    ) -> dict[str, Any]:
        """``InferConstraints`` — POST .../infer-constraints (202, async; retry-safe with a token) → ``{job_id, …}``."""
        return self._post_maybe_idempotent(
            f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/infer-constraints", {}, client_token
        )

    def get_infer_constraints_job(self, namespace_id: str, proposal_id: str, job_id: str) -> dict[str, Any]:
        """``GetInferConstraintsJob`` — GET .../infer-constraints/jobs/{jobId} (snake_case)."""
        return self.get(
            f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/infer-constraints/jobs/{_seg(job_id)}"
        )

    def compile_constraints(
        self, namespace_id: str, proposal_id: str, constraint_config: dict[str, Any], custom_turtle: str | None = None
    ) -> dict[str, Any]:
        """``CompileConstraints`` — POST .../compile-constraints (stateless) → ``{proposal_id, shapes_turtle}``."""
        body: dict[str, Any] = {"constraint_config": constraint_config}
        if custom_turtle:
            body["custom_turtle"] = custom_turtle
        return self.post(f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/compile-constraints", body)

    def repair_proposal_datatypes(self, namespace_id: str, proposal_id: str, *, dry_run: bool) -> dict[str, Any]:
        """``RepairProposalDatatypes`` — POST .../repair-datatypes.

        ``dry_run`` goes snake_case: the handler ignores ``dryRun``, so a camelCase preview would rewrite.
        """
        return self.post(
            f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/repair-datatypes",
            {"dry_run": bool(dry_run)},
        )

    def get_proposal_validation_job(self, namespace_id: str, proposal_id: str, job_id: str) -> dict[str, Any]:
        """``GetProposalValidationJob`` — GET .../proposals/{proposalId}/validate/jobs/{jobId}."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/validate/jobs/{_seg(job_id)}")

    def accept_proposal(self, namespace_id: str, proposal_id: str, *, ontology_id: str | None = None) -> dict[str, Any]:
        """``AcceptProposal`` — POST .../proposals/{proposalId}/accept (202, async)."""
        # snake_case: the handler drops the Smithy name "ontologyId".
        body = {"ontology_id": ontology_id} if ontology_id else {}
        return self.post(f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/accept", body)

    def reject_proposals(self, namespace_id: str, proposal_ids: list[str], reason: str) -> dict[str, Any]:
        """``RejectProposals`` — POST .../proposals/reject → ``{"results": [...]}`` (the handler sends an array)."""
        # snake_case: the handler 422s on the Smithy name "proposalIds".
        parsed = self._request(
            "POST",
            f"/namespaces/{_seg(namespace_id)}/proposals/reject",
            json_body={"proposal_ids": proposal_ids, "reason": reason},
        )
        if isinstance(parsed, list):
            return {"results": parsed}
        return self._as_object(parsed)

    def cancel_proposal(self, namespace_id: str, proposal_id: str) -> dict[str, Any]:
        """``CancelProposal`` — POST .../proposals/{proposalId}/cancel (synchronous)."""
        return self.post(f"/namespaces/{_seg(namespace_id)}/proposals/{_seg(proposal_id)}/cancel")

    # ------------------------------------------------------------------
    # Ontologies and graph (ontology-graph.smithy, ontology-induction.smithy
    # foundational catalog, serve.smithy DescribeSchema)
    # ------------------------------------------------------------------

    def list_ontologies(
        self,
        namespace_id: str,
        *,
        ontology_type: str | None = None,
        domain_tag: str | None = None,
        uri: str | None = None,
        source_registry: str | None = None,
        license_tier: str | None = None,
        next_token: str | None = None,
        max_results: int = 100,
    ) -> Any:
        """``ListOntologies`` — GET .../ontologies (paged; a page can be empty while ``nextToken`` is set)."""
        q = _query_string(
            {
                "maxResults": max_results,
                "ontology_type": ontology_type,
                "domain_tag": domain_tag,
                "uri": uri,
                "source_registry": source_registry,
                "license_tier": license_tier,
                "nextToken": next_token,
            }
        )
        return self._request("GET", f"/namespaces/{_seg(namespace_id)}/ontologies{q}")

    def get_ontology(self, namespace_id: str, ontology_id: str) -> dict[str, Any]:
        """``GetOntology`` — GET .../ontologies/{ontologyId} (the id is an IRI, sent as one encoded segment)."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/ontologies/{_seg(ontology_id)}")

    def get_ontology_download_url(self, namespace_id: str, ontology_id: str) -> dict[str, Any]:
        """``DownloadOntology`` — GET .../ontologies/{ontologyId}/download → ``{downloadUrl, expiresInSeconds}``."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/ontologies/{_seg(ontology_id)}/download")

    def delete_ontology(self, namespace_id: str, ontology_id: str) -> dict[str, Any]:
        """``DeleteOntology`` — DELETE .../ontologies?ontology_id=<IRI> (202).

        A blank id is refused: without ``ontology_id`` the route deletes every ontology in the namespace.
        """
        if not isinstance(ontology_id, str) or not ontology_id.strip():
            raise ValueError("delete_ontology needs a non-blank ontology_id (a blank one would wipe the namespace)")
        q = _query_string({"ontology_id": ontology_id})
        return self.delete(f"/namespaces/{_seg(namespace_id)}/ontologies{q}")

    def get_ontology_upload_url(
        self,
        namespace_id: str,
        *,
        filename: str,
        content_type: str,
        ontology_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """``GetOntologyUploadUrl`` — POST .../ontologies/upload-url → ``{uploadUrl, s3Key, ontologyId}``."""
        body: dict[str, Any] = {"filename": filename, "contentType": content_type}
        if ontology_id:
            body["ontologyId"] = ontology_id
        if title:
            body["title"] = title
        return self.post(f"/namespaces/{_seg(namespace_id)}/ontologies/upload-url", body)

    def put_presigned(
        self, url: str, data: bytes, content_type: str, *, extra_headers: dict[str, str] | None = None
    ) -> None:
        """PUT ``data`` to a presigned S3 URL; no bearer token, and the URL stays out of errors."""
        _require_https(url)
        try:
            resp = self._session.put(
                url,
                data=data,
                headers={"Content-Type": content_type, **(extra_headers or {})},
                auth=_no_auth,
                allow_redirects=False,
                timeout=_PRESIGNED_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            # requests' message embeds the URL (signature included); drop it.
            raise CoaApiError("PUT", "<presigned upload URL>", 0, type(exc).__name__) from None
        if resp.status_code >= 300:
            raise CoaApiError("PUT", "<presigned upload URL>", resp.status_code, _s3_error_code(resp.text))

    def download_presigned(self, url: str, max_bytes: int) -> bytes:
        """GET a presigned S3 URL (no COA bearer token), refusing a body over ``max_bytes``."""
        data, truncated = self._read_presigned(url, max_bytes)
        if truncated:
            raise ValueError(f"download exceeds the {max_bytes}-byte limit")
        return data

    def download_presigned_prefix(self, url: str, max_bytes: int, *, offset: int = 0) -> tuple[bytes, bool]:
        """``(data, truncated)``: up to ``max_bytes`` of a presigned S3 URL from byte ``offset``.

        A positive ``offset`` sends a ``Range`` GET.
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        return self._read_presigned(url, max_bytes, offset=offset)

    def _read_presigned(self, url: str, max_bytes: int, *, offset: int = 0) -> tuple[bytes, bool]:
        """Stream a presigned GET, stopping after ``max_bytes``; ``(data, more_remained)``."""
        _require_https(url)
        # One byte past the page, so a 206 still says whether more remains.
        headers = {"Range": f"bytes={offset}-{offset + max_bytes}"} if offset else None
        try:
            with self._session.get(
                url,
                stream=True,
                auth=_no_auth,
                allow_redirects=False,
                timeout=_PRESIGNED_TIMEOUT_S,
                headers=headers,
            ) as resp:
                if offset and resp.status_code == 416:
                    return b"", False
                if resp.status_code >= 300:
                    raise CoaApiError("GET", "<presigned download URL>", resp.status_code, _s3_error_code(resp.text))
                # A 200 to a Range request is the whole object: skip to offset.
                skip = offset if offset and resp.status_code == 200 else 0
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_content(chunk_size=min(_PRESIGNED_CHUNK_BYTES, max_bytes + 1)):
                    if skip:
                        dropped = min(skip, len(chunk))
                        chunk, skip = chunk[dropped:], skip - dropped
                        if not chunk:
                            continue
                    if size + len(chunk) > max_bytes:
                        chunks.append(chunk[: max_bytes - size])
                        return b"".join(chunks), True
                    size += len(chunk)
                    chunks.append(chunk)
        except requests.RequestException as exc:
            # requests' message embeds the URL (signature included); drop it.
            raise CoaApiError("GET", "<presigned download URL>", 0, type(exc).__name__) from None
        return b"".join(chunks), False

    def ingest_ontology_from_s3(
        self, namespace_id: str, ontology_id: str, body: dict[str, Any], *, client_token: str | None = None
    ) -> dict[str, Any]:
        """``IngestOntologyFromS3`` — POST .../ontologies/{ontologyId}/ingest-from-s3 (202; retry-safe with a token)."""
        return self._post_maybe_idempotent(
            f"/namespaces/{_seg(namespace_id)}/ontologies/{_seg(ontology_id)}/ingest-from-s3", body, client_token
        )

    def get_ingest_status(self, namespace_id: str, ontology_id: str, job_id: str) -> dict[str, Any]:
        """``GetIngestStatus`` — GET .../ontologies/{ontologyId}/ingest-status/{jobId} → ``{result: IngestJob}``."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/ontologies/{_seg(ontology_id)}/ingest-status/{_seg(job_id)}")

    def fetch_ontology(self, namespace_id: str, ontology_id: str, *, source_url: str | None = None) -> dict[str, Any]:
        """``FetchOntology`` — POST .../ontologies/{ontologyId}/fetch (synchronous re-fetch from the source URL)."""
        body = {"sourceUrl": source_url} if source_url else {}
        return self.post(f"/namespaces/{_seg(namespace_id)}/ontologies/{_seg(ontology_id)}/fetch", body)

    def list_foundational_ontologies(self, namespace_id: str) -> dict[str, Any]:
        """``ListFoundationalOntologies`` — GET .../ontology/foundational (a small fixed catalog, not paged)."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/ontology/foundational")

    def load_foundational_ontology(
        self, namespace_id: str, key: str, *, client_token: str | None = None
    ) -> dict[str, Any]:
        """``LoadFoundationalOntology`` — POST .../ontology/foundational/{key}/load (202; retry-safe with a token)."""
        return self._post_maybe_idempotent(
            f"/namespaces/{_seg(namespace_id)}/ontology/foundational/{_seg(key)}/load", {}, client_token
        )

    def graph_search(
        self,
        namespace_id: str,
        q: str,
        *,
        kind: str | None = None,
        ontology_id: str | None = None,
        exclude_ontology_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """``GraphSearch`` — GET .../graph/search → ``{hits, totalCount}``. An empty ``q`` is sent as ``*``."""
        query = _query_string(
            {
                "q": q or "*",
                "kind": kind,
                "ontology_id": ontology_id,
                "exclude_ontology_id": exclude_ontology_id,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.get(f"/namespaces/{_seg(namespace_id)}/graph/search{query}")

    def get_graph_vertex(self, namespace_id: str, kind: str, uri: str) -> dict[str, Any]:
        """``GetGraph{Class,ObjectProperty,DatatypeProperty}`` — GET .../graph/{kind}?uri=."""
        if kind not in _GRAPH_VERTEX_ROUTES:
            raise ValueError(f"unknown graph vertex kind {kind!r}; expected one of {sorted(_GRAPH_VERTEX_ROUTES)}")
        return self.get(f"/namespaces/{_seg(namespace_id)}/graph/{kind}{_query_string({'uri': uri})}")

    def get_ontology_overview(
        self,
        namespace_id: str,
        ontology_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        sample: bool = False,
    ) -> dict[str, Any]:
        """``GetOntologyOverview`` — GET .../graph/ontology-overview?ontology_id=<IRI>."""
        q = _query_string(
            {"ontology_id": ontology_id, "limit": limit, "offset": offset, "sample": "true" if sample else None}
        )
        return self.get(f"/namespaces/{_seg(namespace_id)}/graph/ontology-overview{q}")

    def describe_schema(
        self, namespace_id: str, *, include_properties: bool = False, max_results: int | None = None
    ) -> dict[str, Any]:
        """``DescribeSchema`` — GET .../schema → ``{classes, ontologyVersion}``."""
        q = _query_string({"includeProperties": "true" if include_properties else "false", "maxResults": max_results})
        return self.get(f"/namespaces/{_seg(namespace_id)}/schema{q}")

    # ------------------------------------------------------------------
    # Namespaces, health and roles (namespace.smithy, control-plane.smithy,
    # role.smithy). Bodies and responses are camelCase.
    # ------------------------------------------------------------------

    def create_namespace(self, body: dict[str, Any], *, client_token: str) -> dict[str, Any]:
        """``CreateNamespace`` — POST /namespaces (201; retry-safe with ``client_token``) → ``{namespace}``."""
        return self.post_idempotent("/namespaces", body, client_token)

    def get_namespace(self, namespace_id: str) -> dict[str, Any]:
        """``GetNamespace`` — GET /namespaces/{namespaceId} → ``{namespace}``; 404 once a delete finished."""
        return self.get(f"/namespaces/{_seg(namespace_id)}")

    def list_namespaces(
        self, *, next_token: str | None = None, max_results: int = LIST_NAMESPACES_MAX_RESULTS
    ) -> dict[str, Any]:
        """``ListNamespaces`` — GET /namespaces → ``{namespaces, nextToken?}``, filtered to the caller's grants."""
        q = _query_string({"maxResults": max_results, "nextToken": next_token})
        return self.get(f"/namespaces{q}")

    def update_namespace(self, namespace_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``UpdateNamespace`` — PUT /namespaces/{namespaceId} with ``displayName``/``description`` (``""`` clears)."""
        return self.put(f"/namespaces/{_seg(namespace_id)}", body)

    def update_namespace_status(self, namespace_id: str, status: str) -> dict[str, Any]:
        """``UpdateNamespaceStatus`` — PATCH .../status; only ACTIVE <-> ARCHIVED (else 409)."""
        return self.patch(f"/namespaces/{_seg(namespace_id)}/status", {"status": status})

    def delete_namespace(self, namespace_id: str) -> dict[str, Any]:
        """``DeleteNamespace`` — DELETE /namespaces/{namespaceId} (202; done when ``get_namespace`` 404s)."""
        return self.delete(f"/namespaces/{_seg(namespace_id)}")

    def health_check(self) -> dict[str, Any]:
        """``HealthCheck`` — GET /health → ``{status: "ok"}`` (unauthenticated mock)."""
        return self.get("/health")

    def get_system_health(self) -> dict[str, Any]:
        """``GetSystemHealth`` — GET /system-health → ``{status, dynamodb, neptuneKg, opensearch, …}``."""
        return self.get("/system-health")

    def list_platform_roles(self) -> dict[str, Any]:
        """``ListPlatformRoles`` — GET /roles → ``{roles}``."""
        return self.get("/roles")

    def list_namespace_roles(self, namespace_id: str) -> dict[str, Any]:
        """``ListNamespaceRoles`` — GET /namespaces/{namespaceId}/roles → ``{roles}``."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/roles")

    def get_namespace_role(self, namespace_id: str, role_id: str) -> dict[str, Any]:
        """``GetNamespaceRole`` — GET .../roles/{roleId} → ``{role}`` with its ``cedarPolicy``."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/roles/{_seg(role_id)}")

    # ------------------------------------------------------------------
    # Grants (grant.smithy)
    # ------------------------------------------------------------------

    def list_namespace_grants(
        self,
        namespace_id: str,
        *,
        principal: str | None = None,
        next_token: str | None = None,
        max_results: int = LIST_GRANTS_MAX_RESULTS,
    ) -> dict[str, Any]:
        """``ListNamespaceGrants`` — GET .../grants. ``principal`` is a raw key (``User::...``), not an email."""
        q = _query_string({"principal": principal, "maxResults": max_results, "nextToken": next_token})
        return self.get(f"/namespaces/{_seg(namespace_id)}/grants{q}")

    def create_grant(self, namespace_id: str, body: dict[str, Any], *, client_token: str) -> dict[str, Any]:
        """``CreateGrant`` — POST .../grants (201; retry-safe with ``client_token``) → ``{grant}``.

        Restriction keys must be camelCase: COA ignores snake_case ones and grants unrestricted.
        """
        return self.post_idempotent(f"/namespaces/{_seg(namespace_id)}/grants", body, client_token)

    def delete_grant(self, namespace_id: str, grant_id: str) -> dict[str, Any]:
        """``DeleteGrant`` — DELETE .../grants/{grantId} → ``{}`` (200 even when the grant does not exist)."""
        return self.delete(f"/namespaces/{_seg(namespace_id)}/grants/{_seg(grant_id)}")

    def list_my_grants(
        self, *, next_token: str | None = None, max_results: int = LIST_PRINCIPAL_GRANTS_MAX_RESULTS
    ) -> dict[str, Any]:
        """``ListPrincipalGrants`` — GET /principals/me/grants (platform grants have ``namespaceId: "GLOBAL"``)."""
        q = _query_string({"maxResults": max_results, "nextToken": next_token})
        return self.get(f"/principals/me/grants{q}")

    def list_platform_grants(
        self, *, principal: str | None = None, next_token: str | None = None, max_results: int = LIST_GRANTS_MAX_RESULTS
    ) -> dict[str, Any]:
        """``ListPlatformGrants`` — GET /grants → ``{grants, nextToken?}`` (platform-admin/viewer only)."""
        q = _query_string({"principal": principal, "maxResults": max_results, "nextToken": next_token})
        return self.get(f"/grants{q}")

    def create_platform_grant(self, body: dict[str, Any], *, client_token: str) -> dict[str, Any]:
        """``CreatePlatformGrant`` — POST /grants (201; retry-safe with ``client_token``) → ``{grant}``."""
        return self.post_idempotent("/grants", body, client_token)

    def delete_platform_grant(self, grant_id: str) -> dict[str, Any]:
        """``DeletePlatformGrant`` — DELETE /grants/{grantId} → ``{}`` (404 for an unknown id). platform-admin only."""
        return self.delete(f"/grants/{_seg(grant_id)}")

    # ------------------------------------------------------------------
    # Metrics (metric-service.smithy). Bodies and responses are camelCase.
    # A metric is identified by (namespaceId, name); COA never returns metricId.
    # ------------------------------------------------------------------

    def create_metric(self, namespace_id: str, body: dict[str, Any], *, client_token: str) -> dict[str, Any]:
        """``CreateMetric`` — POST .../metrics (201; retry-safe with ``client_token``) → ``{metric, warnings?}``."""
        return self.post_idempotent(f"/namespaces/{_seg(namespace_id)}/metrics", body, client_token)

    def get_metric(self, namespace_id: str, name: str) -> dict[str, Any]:
        """``GetMetric`` — GET .../metrics/{name} → ``{metric}``."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/metrics/{_seg(name)}")

    def list_metrics(
        self,
        namespace_id: str,
        *,
        data_source_id: str | None = None,
        source_table: str | None = None,
        next_token: str | None = None,
        max_results: int = LIST_METRICS_MAX_RESULTS,
    ) -> dict[str, Any]:
        """``ListMetrics`` — GET .../metrics → ``{metrics, nextToken?}`` (COA's order, by metric IRI)."""
        q = _query_string(
            {
                "maxResults": max_results,
                "nextToken": next_token,
                "dataSourceId": data_source_id,
                "sourceTable": source_table,
            }
        )
        return self.get(f"/namespaces/{_seg(namespace_id)}/metrics{q}")

    def update_metric(self, namespace_id: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """``UpdateMetric`` — PUT .../metrics/{name}: a FULL replace (an omitted optional field is cleared)."""
        return self.put(f"/namespaces/{_seg(namespace_id)}/metrics/{_seg(name)}", body)

    def delete_metric(self, namespace_id: str, name: str) -> dict[str, Any]:
        """``DeleteMetric`` — DELETE .../metrics/{name} (204; a repeat is a 404)."""
        return self.delete(f"/namespaces/{_seg(namespace_id)}/metrics/{_seg(name)}")

    def validate_metric(self, namespace_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``ValidateMetric`` — POST .../metrics/validate → ``{warnings}`` (persists nothing)."""
        return self.post(f"/namespaces/{_seg(namespace_id)}/metrics/validate", body)

    def bulk_delete_metrics_by_name(self, namespace_id: str, names: list[str]) -> dict[str, Any]:
        """``BulkDeleteMetrics`` with ``names`` — POST .../bulk-delete-metrics → ``{deleted, notFound?}``."""
        if not names or any(not isinstance(n, str) or not n for n in names):
            raise ValueError("bulk_delete_metrics_by_name needs a non-empty list of non-empty names")
        return self.post(f"/namespaces/{_seg(namespace_id)}/bulk-delete-metrics", {"names": names})

    def bulk_delete_metrics(
        self, namespace_id: str, *, criteria: dict[str, str] | None = None, delete_all: bool = False
    ) -> dict[str, Any]:
        """``BulkDeleteMetrics`` with ``filter`` XOR ``deleteAll`` → ``{deleted}``. No preview: confirm first."""
        if (criteria is None) == (not delete_all):
            raise ValueError("bulk_delete_metrics needs exactly one of criteria or delete_all=True")
        if criteria is not None and not any(isinstance(v, str) and v for v in criteria.values()):
            raise ValueError("bulk_delete_metrics criteria needs at least one non-empty field")
        body: dict[str, Any] = {"filter": criteria} if criteria is not None else {"deleteAll": True}
        return self.post(f"/namespaces/{_seg(namespace_id)}/bulk-delete-metrics", body)

    def get_osi_upload_url(self, namespace_id: str, filename: str) -> dict[str, Any]:
        """``GetOsiUploadUrl`` — POST .../import-osi/upload-url. ``uploadUrl`` is a bearer credential."""
        return self.post(f"/namespaces/{_seg(namespace_id)}/import-osi/upload-url", {"filename": filename})

    def import_osi(self, namespace_id: str, body: dict[str, Any], *, client_token: str) -> dict[str, Any]:
        """``ImportOsi`` — POST .../import-osi (``content`` XOR ``s3Key``; 202 ``{jobId}`` when deployed)."""
        return self.post_idempotent(f"/namespaces/{_seg(namespace_id)}/import-osi", body, client_token)

    def get_import_job(self, namespace_id: str, job_id: str) -> dict[str, Any]:
        """``GetImportJob`` — GET .../import-jobs/{jobId}."""
        return self.get(f"/namespaces/{_seg(namespace_id)}/import-jobs/{_seg(job_id)}")

    def export_osi(self, namespace_id: str, *, names: list[str] | None = None) -> dict[str, Any]:
        """``ExportOsi`` — GET .../export-osi?format=inline[&names=...] → ``{content}`` (OSI YAML)."""
        q = urlencode([("format", ExportFormat.INLINE.value), *(("names", n) for n in names or [])])
        return self.get(f"/namespaces/{_seg(namespace_id)}/export-osi?{q}")


def _no_auth(request: Any) -> Any:
    """Auth hook that adds nothing, so ``requests`` does not fall back to ``~/.netrc`` for S3.

    Redirects are off on the same calls: ``requests`` re-applies netrc on a redirect.
    """
    return request


def _s3_error_code(body: str) -> str:
    """Only the ``<Code>`` of an S3 error body: the rest can echo the signed request."""
    match = re.search(r"<Code>([^<]{1,100})</Code>", body or "")
    return match.group(1) if match else "S3 error"


def _require_https(url: str) -> None:
    """Refuse a presigned URL that is not https (it would send the payload in clear)."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("presigned URL must be https")
