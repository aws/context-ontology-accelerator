# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Caller-supplied idempotency tokens for API Gateway create operations.

A create operation becomes safe to retry when the caller sends an optional
``clientToken`` in the JSON request body (the Smithy ``@idempotencyToken``
member). :func:`idempotent_create` wraps the operation's existing
"build an API Gateway proxy response" function; :func:`idempotent` is the
decorator form for a Lambda ``handler(event, context)``:

* No ``clientToken`` -> the wrapped function runs exactly as before; the
  idempotency table is never touched.
* Malformed token -> 400.
* Token present but the caller identity cannot be resolved -> 503 (fail
  closed): keying on a shared "unknown" principal would let unrelated callers
  replay each other's responses.
* First use -> a record is written ``IN_PROGRESS`` with a conditional put, the
  create runs, and a 2xx response is stored (``COMPLETED``) for replay. A
  non-2xx response or an exception deletes the record, so a retry re-runs the
  create and surfaces the real error again.
* Same token, same request, ``COMPLETED`` -> the stored response is replayed
  verbatim (same status code and body).
* Same token, different request -> 409.
* Same token while the first request is still ``IN_PROGRESS`` -> 409, unless
  the record is older than :data:`IN_PROGRESS_TIMEOUT_SECONDS`, in which case it
  is treated as abandoned and taken over with a conditional update on its
  ``created_at``.
* The idempotency table is unconfigured, unavailable or throttled -> 503
  (fail closed): running the create without the guard could duplicate it,
  which is exactly what the caller asked us to prevent.

Records are keyed by ``sha256(operation, caller principal, scope, token)`` so
neither the token nor the principal is stored in clear in the key, and two
callers (or two namespaces) can never collide on the same token. The
``clientToken`` field is removed from the body before the wrapped function
sees it, so existing validation and persistence never see or store it.

Responses larger than :data:`MAX_STORED_BODY_BYTES` are not cached (a DynamoDB
item is capped at 400 KB). The record is still marked ``COMPLETED`` so a retry
cannot repeat the create; such a retry gets a 409 explaining the original
request already succeeded. None of the current create responses come close.
The same "completed, not replayable" record is written as a fallback when
storing the full response keeps failing after a successful create.

Residual duplicate window: if the Lambda is killed (timeout, out of memory)
after the create's side effects commit but before the record is marked
``COMPLETED``, the record stays ``IN_PROGRESS``. A retry within
:data:`IN_PROGRESS_TIMEOUT_SECONDS` gets 409; a retry after it takes the
record over and runs the create again. Creates that already guard their own
writes (a conditional put on the resource key) answer that second run with
their usual "already exists" 409 rather than duplicating.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from structlog.typing import FilteringBoundLogger

from coa_common.aws_config import sync_boto_config
from coa_common.config import resolve_region
from coa_common.response import UNKNOWN_CALLER, cors_headers, get_caller_identity

logger = structlog.get_logger(__name__)

CLIENT_TOKEN_FIELD = "clientToken"
IDEMPOTENCY_TABLE_ENV = "IDEMPOTENCY_TABLE"
# Scope used for operations whose route carries no namespaceId (platform grants).
PLATFORM_SCOPE = "PLATFORM"
CLIENT_TOKEN_PATTERN = re.compile(r"[\x21-\x7E]{1,64}")
RECORD_TTL_SECONDS = 24 * 60 * 60
# Longest timeout of a Lambda serving an idempotent route (the ontology API
# proxy, 60 s; the namespace, grants, metric and sources API Lambdas are 10-30 s).
# Keep in step with infra/lib/stacks/services/*-stack.ts.
MAX_HANDLER_TIMEOUT_SECONDS = 60
# An IN_PROGRESS record older than this cannot belong to a live invocation. The
# margin absorbs clock skew between Lambda hosts; keeping it short bounds how
# long a caller whose invocation was killed mid-create is blocked from retrying.
IN_PROGRESS_TIMEOUT_SECONDS = MAX_HANDLER_TIMEOUT_SECONDS + 60
# DynamoDB items are capped at 400 KB; leave headroom for the other attributes.
MAX_STORED_BODY_BYTES = 350 * 1024
REPLAY_HEADER = "X-Idempotent-Replay"

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"

_MSG_INVALID_TOKEN = "clientToken must be 1-64 printable ASCII characters with no spaces"
_MSG_MISMATCH = "clientToken was already used with different request parameters"
_MSG_IN_PROGRESS = "a request with this clientToken is still in progress"
_MSG_UNAVAILABLE = "idempotency store unavailable, retry"
_MSG_NO_IDENTITY = "caller identity unavailable; clientToken requests cannot be processed"
_MSG_COMPLETED_NOT_REPLAYABLE = (
    "a request with this clientToken already completed successfully; its response is too large to replay"
)
# Headers worth replaying. CORS headers are regenerated for the current request.
_REPLAYED_HEADER_EXCLUDE_PREFIX = "access-control-"
# Bounded retries when the record vanishes between the conditional put and the read.
_MAX_CLAIM_ATTEMPTS = 3
# Bounded retries of the COMPLETED write after a successful create; the delay
# doubles after each failed attempt.
_MAX_COMPLETE_ATTEMPTS = 3
_COMPLETE_RETRY_DELAY_SECONDS = 0.1

# Every attribute is aliased so no expression trips over a DynamoDB reserved word.
_ATTR_NAMES = {
    "#st": "status",
    "#op": "operation",
    "#fp": "fingerprint",
    "#ca": "created_at",
    "#ttl": "ttl",
    "#sc": "status_code",
    "#b": "body",
    "#h": "headers",
    "#bo": "body_omitted",
}

ProxyResponse = dict[str, Any]
CreateFn = Callable[[dict[str, Any]], ProxyResponse]
HeadersFn = Callable[[], dict[str, str]]
LambdaHandler = Callable[[dict[str, Any], Any], ProxyResponse]


class IdempotencyStoreError(Exception):
    """The idempotency table could not be read or written."""


def _default_headers() -> dict[str, str]:
    return {"Content-Type": "application/json", **cors_headers()}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class IdempotencyStore:
    """Thin DynamoDB wrapper for idempotency records (PK-only table, TTL on ``ttl``).

    Every DynamoDB/botocore failure other than an expected conditional-check
    failure is raised as :class:`IdempotencyStoreError` so the caller can fail
    closed. The client uses the fail-fast synchronous retry profile.
    """

    def __init__(self, table_name: str, *, table: Any | None = None) -> None:
        """Bind to ``table_name``; ``table`` injects a boto3 Table resource (tests)."""
        self.table_name = table_name
        if table is None:
            resource = boto3.resource("dynamodb", region_name=resolve_region(), config=sync_boto_config())
            table = resource.Table(table_name)
        self._table = table

    @staticmethod
    def _call(action: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any] | None:
        """Run one table call.

        Returns the call's response, or None when its condition expression
        failed. Any other DynamoDB/botocore error is raised as
        :class:`IdempotencyStoreError`.
        """
        try:
            return action(**kwargs)
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return None
            raise IdempotencyStoreError(str(exc)) from exc
        except BotoCoreError as exc:
            raise IdempotencyStoreError(str(exc)) from exc

    def get(self, key: str) -> dict[str, Any] | None:
        """Strongly-consistent read of the record for ``key``."""
        response = self._call(self._table.get_item, Key={"PK": key}, ConsistentRead=True)
        return (response or {}).get("Item")

    def claim(self, key: str, *, operation: str, fingerprint: str, now: int) -> bool:
        """Create an IN_PROGRESS record. Returns False if one already exists."""
        item = {
            "PK": key,
            "operation": operation,
            "status": STATUS_IN_PROGRESS,
            "fingerprint": fingerprint,
            "created_at": now,
            "ttl": now + RECORD_TTL_SECONDS,
        }
        response = self._call(self._table.put_item, Item=item, ConditionExpression="attribute_not_exists(PK)")
        return response is not None

    def take_over(self, key: str, *, operation: str, fingerprint: str, old_created_at: int, now: int) -> bool:
        """Replace an abandoned/expired record, guarded on its ``created_at``.

        Returns False when another request changed the record first.
        """
        response = self._call(
            self._table.update_item,
            Key={"PK": key},
            UpdateExpression="SET #st = :in_progress, #op = :op, #fp = :fp, #ca = :now, #ttl = :ttl "
            "REMOVE #sc, #b, #h, #bo",
            ConditionExpression="#ca = :old",
            ExpressionAttributeNames=_ATTR_NAMES,
            ExpressionAttributeValues={
                ":in_progress": STATUS_IN_PROGRESS,
                ":op": operation,
                ":fp": fingerprint,
                ":now": now,
                ":ttl": now + RECORD_TTL_SECONDS,
                ":old": old_created_at,
            },
        )
        return response is not None

    def complete(self, key: str, *, created_at: int, response: ProxyResponse, omit_body: bool = False) -> bool:
        """Mark the record COMPLETED and store the response for replay.

        The body is not stored (``body_omitted``) when it is too large for a
        DynamoDB item or ``omit_body`` is set; a retry then gets a 409 saying
        the original request already succeeded. Returns False when the record
        is no longer ours (taken over after :data:`IN_PROGRESS_TIMEOUT_SECONDS`).
        """
        body = response.get("body") or ""
        if not isinstance(body, str):
            body = json.dumps(body, default=str)
        headers = {
            k: str(v)
            for k, v in (response.get("headers") or {}).items()
            if not k.lower().startswith(_REPLAYED_HEADER_EXCLUDE_PREFIX)
        }
        values: dict[str, Any] = {
            ":completed": STATUS_COMPLETED,
            ":code": int(response.get("statusCode", 200)),
            ":headers": headers,
            ":mine": created_at,
        }
        names: tuple[str, ...]
        if omit_body or len(body.encode("utf-8")) > MAX_STORED_BODY_BYTES:
            expr = "SET #st = :completed, #sc = :code, #h = :headers, #bo = :omitted REMOVE #b"
            names = ("#st", "#sc", "#h", "#bo", "#b", "#ca")
            values[":omitted"] = True
        else:
            expr = "SET #st = :completed, #sc = :code, #h = :headers, #b = :body"
            names = ("#st", "#sc", "#h", "#b", "#ca")
            values[":body"] = body
        result = self._call(
            self._table.update_item,
            Key={"PK": key},
            UpdateExpression=expr,
            ConditionExpression="#ca = :mine",
            ExpressionAttributeNames={alias: _ATTR_NAMES[alias] for alias in names},
            ExpressionAttributeValues=values,
        )
        return result is not None

    def release(self, key: str, *, created_at: int) -> None:
        """Delete our IN_PROGRESS record so the caller can retry (no-op if no longer ours)."""
        self._call(
            self._table.delete_item,
            Key={"PK": key},
            ConditionExpression="#ca = :mine",
            ExpressionAttributeNames={"#ca": "created_at"},
            ExpressionAttributeValues={":mine": created_at},
        )


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


_STORES: dict[str, IdempotencyStore] = {}


def _get_store(table_name: str) -> IdempotencyStore:
    store = _STORES.get(table_name)
    if store is None:
        store = IdempotencyStore(table_name)
        _STORES[table_name] = store
    return store


def reset_store_cache() -> None:
    """Drop the cached per-table stores (tests that swap the backing table or mock)."""
    _STORES.clear()


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedRequest:
    """The request body with ``clientToken`` split out."""

    token: Any
    body: dict[str, Any]


def _parse_body(event: dict[str, Any]) -> _ParsedRequest | None:
    """Return the parsed body when it is a JSON object carrying ``clientToken``.

    Anything else (no body, non-JSON, non-object, no token) returns None so
    the wrapped handler runs unchanged and produces its own error, if any.
    """
    raw = event.get("body")
    if not raw or not isinstance(raw, str):
        return None
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict) or CLIENT_TOKEN_FIELD not in body:
        return None
    token = body.pop(CLIENT_TOKEN_FIELD)
    return _ParsedRequest(token=token, body=body)


def _event_without_token(event: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    return {**event, "body": json.dumps(body), "isBase64Encoded": False}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def record_key(*, operation: str, principal: str, scope: str, token: str) -> str:
    """Hashed record key; the token and principal never appear in clear."""
    return hashlib.sha256(_canonical([operation, principal, scope, token]).encode("utf-8")).hexdigest()


def request_fingerprint(body: dict[str, Any], path_parameters: dict[str, Any] | None) -> str:
    """sha256 of the canonical token-free body plus the path parameters."""
    payload = {"body": body, "pathParameters": path_parameters or {}}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def idempotent_create(
    event: dict[str, Any],
    create: CreateFn,
    *,
    operation: str,
    error_field: str = "message",
    headers: HeadersFn | None = None,
    store: IdempotencyStore | None = None,
) -> ProxyResponse:
    """Run ``create(event)`` at most once per caller-supplied ``clientToken``.

    Args:
        event: The API Gateway proxy event.
        create: Builds the proxy response for the create. Receives the event
            with ``clientToken`` removed from the body.
        operation: Stable operation name (e.g. ``"CreateGrant"``); part of the
            record key so tokens never collide across operations.
        error_field: Body key the calling handler uses for error text
            (``"message"`` for most handlers, ``"error"`` for sources).
        headers: Factory for the base response headers (Content-Type + CORS)
            used on errors and replays. Defaults to the shared
            :func:`coa_common.response.api_response` headers.
        store: Injected store (tests); otherwise built from ``IDEMPOTENCY_TABLE``.

    Returns:
        The proxy response: the create's own response, a replay of it, or an
        idempotency error (400 / 409 / 503).
    """
    parsed = _parse_body(event)
    if parsed is None:
        return create(event)

    stripped_event = _event_without_token(event, parsed.body)
    if parsed.token is None:
        # An explicit JSON null is the same as omitting the optional member.
        return create(stripped_event)

    headers_fn = headers or _default_headers

    def error(status: int, message: str) -> ProxyResponse:
        return {"statusCode": status, "headers": headers_fn(), "body": json.dumps({error_field: message})}

    token = parsed.token
    if not isinstance(token, str) or not CLIENT_TOKEN_PATTERN.fullmatch(token):
        return error(400, _MSG_INVALID_TOKEN)

    principal = get_caller_identity(event)
    if principal == UNKNOWN_CALLER:
        # Every identity-less caller would share one key space in this scope.
        logger.error("idempotency_caller_identity_missing", operation=operation)
        return error(503, _MSG_NO_IDENTITY)

    path_params = event.get("pathParameters") or {}
    scope = path_params.get("namespaceId") or PLATFORM_SCOPE
    key = record_key(operation=operation, principal=principal, scope=scope, token=token)
    fingerprint = request_fingerprint(parsed.body, path_params)
    log: FilteringBoundLogger = logger.bind(operation=operation, idempotency_key=key)

    resolved = _resolve_store(store, log)
    if resolved is None:
        return error(503, _MSG_UNAVAILABLE)

    try:
        outcome = _claim(resolved, key, operation=operation, fingerprint=fingerprint)
    except IdempotencyStoreError as exc:
        log.error("idempotency_store_unavailable", phase="claim", error=str(exc))
        return error(503, _MSG_UNAVAILABLE)

    if isinstance(outcome, _Conflict):
        log.info("idempotency_conflict", reason=outcome.reason)
        return error(409, outcome.message)
    if isinstance(outcome, _Replay):
        log.info("idempotency_replay", status_code=outcome.response["statusCode"])
        return _build_replay(outcome, headers_fn)

    try:
        response = create(stripped_event)
    except Exception:
        _release_quietly(resolved, key, outcome.created_at, log)
        raise
    _finish(resolved, key, outcome.created_at, response, log)
    return response


def idempotent(
    *,
    operation: str,
    error_field: str = "message",
    headers: HeadersFn | None = None,
) -> Callable[[LambdaHandler], LambdaHandler]:
    """Decorator form of :func:`idempotent_create` for a Lambda ``handler(event, context)``.

    Example::

        @idempotent(operation="CreateGrant")
        def handler(event, context):
            ...

    The undecorated function stays reachable as ``handler.__wrapped__``.
    """

    def decorate(fn: LambdaHandler) -> LambdaHandler:
        @functools.wraps(fn)
        def wrapper(event: dict[str, Any], context: Any) -> ProxyResponse:
            return idempotent_create(
                event,
                lambda ev: fn(ev, context),
                operation=operation,
                error_field=error_field,
                headers=headers,
            )

        return wrapper

    return decorate


def _resolve_store(store: IdempotencyStore | None, log: FilteringBoundLogger) -> IdempotencyStore | None:
    """Injected store, else the cached store for ``IDEMPOTENCY_TABLE``; None if unconfigured."""
    if store is not None:
        return store
    table_name = os.environ.get(IDEMPOTENCY_TABLE_ENV, "")
    if not table_name:
        log.error("idempotency_table_not_configured", env_var=IDEMPOTENCY_TABLE_ENV)
        return None
    return _get_store(table_name)


def _build_replay(outcome: _Replay, headers_fn: HeadersFn) -> ProxyResponse:
    replay_headers = {**headers_fn(), **outcome.headers}
    # Exposed explicitly so a browser client can read it on the CORS response.
    replay_headers[REPLAY_HEADER] = "true"
    replay_headers["Access-Control-Expose-Headers"] = REPLAY_HEADER
    return {**outcome.response, "headers": replay_headers}


def _finish(
    store: IdempotencyStore,
    key: str,
    created_at: int,
    response: ProxyResponse,
    log: FilteringBoundLogger,
) -> None:
    """Record the create's outcome: COMPLETED for a 2xx, otherwise release the claim."""
    status = int(response.get("statusCode", 500))
    if not 200 <= status < 300:
        _release_quietly(store, key, created_at, log)
        return

    # The create already happened, so the caller gets its response whatever
    # happens here; failing now would make it retry into a duplicate. Retry the
    # write a few times, then fall back to a body-less COMPLETED record so a
    # retry gets "already completed" (409) instead of re-running the create
    # once the IN_PROGRESS record is considered abandoned.
    delay = _COMPLETE_RETRY_DELAY_SECONDS
    for attempt in range(1, _MAX_COMPLETE_ATTEMPTS + 1):
        try:
            if not store.complete(key, created_at=created_at, response=response):
                log.warning("idempotency_record_lost", status_code=status)
            return
        except IdempotencyStoreError as exc:
            log.warning("idempotency_complete_retry", attempt=attempt, status_code=status, error=str(exc))
            if attempt < _MAX_COMPLETE_ATTEMPTS:
                time.sleep(delay)
                delay *= 2
    try:
        store.complete(key, created_at=created_at, response=response, omit_body=True)
        log.error("idempotency_complete_body_omitted", status_code=status)
    except IdempotencyStoreError as exc:
        # The IN_PROGRESS record blocks retries until IN_PROGRESS_TIMEOUT_SECONDS.
        log.error("idempotency_complete_failed", status_code=status, error=str(exc))


def _release_quietly(store: IdempotencyStore, key: str, created_at: int, log: FilteringBoundLogger) -> None:
    try:
        store.release(key, created_at=created_at)
    except IdempotencyStoreError as exc:
        log.error("idempotency_release_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Claim state machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Claimed:
    created_at: int


@dataclass(frozen=True)
class _Conflict:
    reason: str
    message: str


@dataclass(frozen=True)
class _Replay:
    response: ProxyResponse
    headers: dict[str, str]


def _claim(store: IdempotencyStore, key: str, *, operation: str, fingerprint: str) -> _Claimed | _Conflict | _Replay:
    for _ in range(_MAX_CLAIM_ATTEMPTS):
        now = int(time.time())
        if store.claim(key, operation=operation, fingerprint=fingerprint, now=now):
            return _Claimed(created_at=now)

        existing = store.get(key)
        if existing is None:
            # Released between our put and our read — try to claim again.
            continue

        created = int(existing.get("created_at", 0))
        expired = int(existing.get("ttl", 0)) <= now  # TTL deletion is lazy
        abandoned = existing.get("status") == STATUS_IN_PROGRESS and now - created >= IN_PROGRESS_TIMEOUT_SECONDS

        if not expired:
            if existing.get("fingerprint") != fingerprint:
                return _Conflict("fingerprint_mismatch", _MSG_MISMATCH)
            if existing.get("status") == STATUS_COMPLETED:
                return _replay_from(existing)
            if not abandoned:
                return _Conflict("in_progress", _MSG_IN_PROGRESS)

        if store.take_over(key, operation=operation, fingerprint=fingerprint, old_created_at=created, now=now):
            logger.warning("idempotency_record_taken_over", idempotency_key=key, expired=expired, operation=operation)
            return _Claimed(created_at=now)
        return _Conflict("in_progress", _MSG_IN_PROGRESS)

    # Every attempt found the record released between our put and our read: a
    # concurrent request on the same key keeps claiming and releasing it. Store
    # failures never reach here (they raise IdempotencyStoreError -> 503).
    return _Conflict("claim_contention", _MSG_IN_PROGRESS)


def _replay_from(record: dict[str, Any]) -> _Replay | _Conflict:
    if record.get("body_omitted"):
        return _Conflict("completed_not_replayable", _MSG_COMPLETED_NOT_REPLAYABLE)
    stored_headers = {str(k): str(v) for k, v in (record.get("headers") or {}).items()}
    response = {"statusCode": int(record.get("status_code", 200)), "body": record.get("body", "")}
    return _Replay(response=response, headers=stored_headers)
