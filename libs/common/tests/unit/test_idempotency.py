# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.idempotency (moto-backed DynamoDB)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_common import idempotency
from coa_common.idempotency import (
    IN_PROGRESS_TIMEOUT_SECONDS,
    MAX_STORED_BODY_BYTES,
    IdempotencyStore,
    IdempotencyStoreError,
    idempotent,
    idempotent_create,
    reset_store_cache,
)
from coa_common.testing import mock_idempotency_table

pytestmark = pytest.mark.unit

_TABLE = "test-idempotency"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv(idempotency.IDEMPOTENCY_TABLE_ENV, _TABLE)
    monkeypatch.setattr(idempotency, "_COMPLETE_RETRY_DELAY_SECONDS", 0)
    reset_store_cache()


@pytest.fixture
def table() -> Iterator[Any]:
    with mock_idempotency_table(_TABLE) as tbl:
        yield tbl


def _event(
    body: dict[str, Any] | None, *, namespace_id: str | None = "ns-1", caller: str | None = "alice"
) -> dict[str, Any]:
    return {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/grants",
        "pathParameters": {"namespaceId": namespace_id} if namespace_id else None,
        "requestContext": {"authorizer": {"principalId": caller} if caller else {}},
        "body": json.dumps(body) if body is not None else None,
    }


class _Create:
    """Counting fake create that records the body it was given."""

    def __init__(self, status: int = 201, body: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = status
        self.body = body if body is not None else {"grant": {"grantId": "g-1"}}

    def __call__(self, event: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(json.loads(event["body"]) if event.get("body") else {})
        return {
            "statusCode": self.status,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "x"},
            "body": json.dumps({**self.body, "n": len(self.calls)}),
        }


def _items(tbl: Any) -> list[dict[str, Any]]:
    return tbl.scan()["Items"]


def test_idempotent_create_no_token_passes_through_without_table_access() -> None:
    create = _Create()
    event = _event({"principalId": "bob"})
    with patch.object(idempotency, "_get_store") as get_store:
        resp = idempotent_create(event, create, operation="CreateGrant")
    get_store.assert_not_called()
    assert resp["statusCode"] == 201
    assert create.calls == [{"principalId": "bob"}]


def test_idempotent_create_null_token_is_stripped_and_skips_table() -> None:
    create = _Create()
    with patch.object(idempotency, "_get_store") as get_store:
        idempotent_create(_event({"principalId": "bob", "clientToken": None}), create, operation="CreateGrant")
    get_store.assert_not_called()
    assert create.calls == [{"principalId": "bob"}]


def test_idempotent_create_first_call_stores_completed_and_strips_token(table: Any) -> None:
    create = _Create()
    resp = idempotent_create(_event({"principalId": "bob", "clientToken": "tok-1"}), create, operation="CreateGrant")

    assert resp["statusCode"] == 201
    assert create.calls == [{"principalId": "bob"}]
    [item] = _items(table)
    assert item["status"] == "COMPLETED"
    assert item["operation"] == "CreateGrant"
    assert int(item["status_code"]) == 201
    assert item["body"] == resp["body"]
    assert item["headers"] == {"Content-Type": "application/json"}
    assert int(item["ttl"]) - int(item["created_at"]) == idempotency.RECORD_TTL_SECONDS
    # Neither the token nor the principal appears in clear in the key.
    assert "tok-1" not in item["PK"] and "alice" not in item["PK"]
    assert len(item["PK"]) == 64


def test_idempotent_create_replay_returns_identical_response(table: Any) -> None:
    create = _Create()
    body = {"principalId": "bob", "clientToken": "tok-1"}
    first = idempotent_create(_event(body), create, operation="CreateGrant")
    second = idempotent_create(_event(body), create, operation="CreateGrant")

    assert len(create.calls) == 1
    assert second["statusCode"] == first["statusCode"]
    assert second["body"] == first["body"]
    assert second["headers"]["X-Idempotent-Replay"] == "true"
    assert second["headers"]["Access-Control-Expose-Headers"] == "X-Idempotent-Replay"
    assert second["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"


def test_idempotent_create_body_key_order_does_not_change_fingerprint(table: Any) -> None:
    create = _Create()
    idempotent_create(_event({"a": 1, "b": 2, "clientToken": "t"}), create, operation="Op")
    resp = idempotent_create(_event({"clientToken": "t", "b": 2, "a": 1}), create, operation="Op")
    assert len(create.calls) == 1
    assert resp["headers"]["X-Idempotent-Replay"] == "true"


def test_idempotent_create_different_params_returns_409(table: Any) -> None:
    create = _Create()
    idempotent_create(_event({"principalId": "bob", "clientToken": "tok-1"}), create, operation="CreateGrant")
    resp = idempotent_create(_event({"principalId": "carol", "clientToken": "tok-1"}), create, operation="CreateGrant")

    assert resp["statusCode"] == 409
    assert json.loads(resp["body"]) == {"message": "clientToken was already used with different request parameters"}
    assert len(create.calls) == 1


def test_idempotent_create_token_scoped_per_caller_and_namespace(table: Any) -> None:
    create = _Create()
    body = {"principalId": "bob", "clientToken": "tok-1"}
    idempotent_create(_event(body, caller="alice"), create, operation="CreateGrant")
    idempotent_create(_event(body, caller="mallory"), create, operation="CreateGrant")
    idempotent_create(_event(body, namespace_id="ns-2"), create, operation="CreateGrant")
    idempotent_create(_event(body, namespace_id=None), create, operation="CreatePlatformGrant")
    assert len(create.calls) == 4
    assert len(_items(table)) == 4


def test_idempotent_create_in_progress_returns_409(table: Any) -> None:
    body = {"principalId": "bob", "clientToken": "tok-1"}
    outcomes: list[dict[str, Any]] = []

    def create(event: dict[str, Any]) -> dict[str, Any]:
        # A concurrent duplicate arrives while the first is still running.
        outcomes.append(idempotent_create(_event(body), _Create(), operation="CreateGrant"))
        return _Create()(event)

    idempotent_create(_event(body), create, operation="CreateGrant")
    assert outcomes[0]["statusCode"] == 409
    assert json.loads(outcomes[0]["body"])["message"] == "a request with this clientToken is still in progress"


def test_idempotent_create_abandoned_in_progress_is_taken_over(table: Any) -> None:
    body = {"principalId": "bob", "clientToken": "tok-1"}
    key = idempotency.record_key(operation="CreateGrant", principal="alice", scope="ns-1", token="tok-1")
    fp = idempotency.request_fingerprint({"principalId": "bob"}, {"namespaceId": "ns-1"})
    with patch.object(idempotency.time, "time", return_value=1_000_000):
        assert IdempotencyStore(_TABLE).claim(key, operation="CreateGrant", fingerprint=fp, now=1_000_000)

    create = _Create()
    with patch.object(idempotency.time, "time", return_value=1_000_000 + IN_PROGRESS_TIMEOUT_SECONDS - 1):
        assert idempotent_create(_event(body), create, operation="CreateGrant")["statusCode"] == 409
    with patch.object(idempotency.time, "time", return_value=1_000_000 + IN_PROGRESS_TIMEOUT_SECONDS):
        resp = idempotent_create(_event(body), create, operation="CreateGrant")

    assert resp["statusCode"] == 201
    assert len(create.calls) == 1
    [item] = _items(table)
    assert item["status"] == "COMPLETED"
    assert int(item["created_at"]) == 1_000_000 + IN_PROGRESS_TIMEOUT_SECONDS


def test_idempotent_create_expired_record_is_reused(table: Any) -> None:
    create = _Create()
    body = {"principalId": "bob", "clientToken": "tok-1"}
    with patch.object(idempotency.time, "time", return_value=1_000_000):
        idempotent_create(_event(body), create, operation="CreateGrant")
    # TTL deletion is lazy: an expired-but-present record must not replay or block.
    with patch.object(idempotency.time, "time", return_value=1_000_000 + idempotency.RECORD_TTL_SECONDS):
        resp = idempotent_create(_event({**body, "principalId": "carol"}), create, operation="CreateGrant")
    assert resp["statusCode"] == 201
    assert len(create.calls) == 2


def test_idempotent_create_non_2xx_deletes_record(table: Any) -> None:
    create = _Create(status=404, body={"message": "Role not found"})
    body = {"principalId": "bob", "clientToken": "tok-1"}
    first = idempotent_create(_event(body), create, operation="CreateGrant")
    assert first["statusCode"] == 404
    assert _items(table) == []
    second = idempotent_create(_event(body), create, operation="CreateGrant")
    assert second["statusCode"] == 404
    assert len(create.calls) == 2


def test_idempotent_create_exception_deletes_record_and_reraises(table: Any) -> None:
    def boom(event: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("downstream failure")

    with pytest.raises(RuntimeError, match="downstream failure"):
        idempotent_create(_event({"principalId": "bob", "clientToken": "tok-1"}), boom, operation="CreateGrant")
    assert _items(table) == []


def test_idempotent_create_table_error_returns_503_without_running_create() -> None:
    fake_table = MagicMock()
    fake_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "PutItem"
    )
    create = _Create()
    resp = idempotent_create(
        _event({"principalId": "bob", "clientToken": "tok-1"}),
        create,
        operation="CreateGrant",
        store=IdempotencyStore(_TABLE, table=fake_table),
    )
    assert resp["statusCode"] == 503
    assert json.loads(resp["body"]) == {"message": "idempotency store unavailable, retry"}
    assert create.calls == []


def test_idempotent_create_missing_table_env_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(idempotency.IDEMPOTENCY_TABLE_ENV)
    create = _Create()
    resp = idempotent_create(_event({"principalId": "bob", "clientToken": "tok-1"}), create, operation="CreateGrant")
    assert resp["statusCode"] == 503
    assert create.calls == []


@pytest.mark.parametrize("token", ["", "has space", "x" * 65, 123, "café", ["a"]])
def test_idempotent_create_invalid_token_returns_400(token: Any) -> None:
    create = _Create()
    with patch.object(idempotency, "_get_store") as get_store:
        resp = idempotent_create(
            _event({"principalId": "bob", "clientToken": token}), create, operation="CreateSource", error_field="error"
        )
    get_store.assert_not_called()
    assert resp["statusCode"] == 400
    assert "clientToken" in json.loads(resp["body"])["error"]
    assert create.calls == []


def test_idempotent_create_accepts_64_char_printable_token(table: Any) -> None:
    create = _Create()
    resp = idempotent_create(_event({"clientToken": "~" * 64}), create, operation="Op")
    assert resp["statusCode"] == 201


def test_idempotent_create_oversize_body_not_cached_and_replay_409(table: Any) -> None:
    create = _Create(body={"blob": "x" * (MAX_STORED_BODY_BYTES + 1)})
    body = {"clientToken": "tok-1"}
    assert idempotent_create(_event(body), create, operation="Op")["statusCode"] == 201
    [item] = _items(table)
    assert item["status"] == "COMPLETED" and item["body_omitted"] is True and "body" not in item
    resp = idempotent_create(_event(body), create, operation="Op")
    assert resp["statusCode"] == 409
    assert len(create.calls) == 1


def test_idempotent_create_custom_headers_used_for_errors(table: Any) -> None:
    resp = idempotent_create(
        _event({"clientToken": " bad"}),
        _Create(),
        operation="StartInduction",
        headers=lambda: {"Content-Type": "application/json", "X-Custom": "1"},
    )
    assert resp["statusCode"] == 400
    assert resp["headers"] == {"Content-Type": "application/json", "X-Custom": "1"}


def test_idempotent_create_base64_body_is_decoded_and_stripped(table: Any) -> None:
    import base64

    create = _Create()
    event = _event(None)
    event["body"] = base64.b64encode(json.dumps({"a": 1, "clientToken": "t"}).encode()).decode()
    event["isBase64Encoded"] = True
    idempotent_create(event, create, operation="Op")
    assert create.calls == [{"a": 1}]
    assert len(_items(table)) == 1


def test_idempotent_create_unknown_caller_with_token_returns_503_without_running_create(table: Any) -> None:
    create = _Create()
    resp = idempotent_create(_event({"clientToken": "tok-1"}, caller=None), create, operation="CreateGrant")
    assert resp["statusCode"] == 503
    assert "caller identity" in json.loads(resp["body"])["message"]
    assert create.calls == []
    assert _items(table) == []


def test_idempotent_create_unknown_caller_without_token_passes_through() -> None:
    create = _Create()
    with patch.object(idempotency, "_get_store") as get_store:
        resp = idempotent_create(_event({"principalId": "bob"}, caller=None), create, operation="CreateGrant")
    get_store.assert_not_called()
    assert resp["statusCode"] == 201


def test_idempotent_create_complete_retried_after_transient_failure(table: Any) -> None:
    create = _Create()
    real_complete = IdempotencyStore.complete
    calls: list[bool] = []

    def flaky(self: IdempotencyStore, *args: Any, **kwargs: Any) -> bool:
        calls.append(kwargs.get("omit_body", False))
        if len(calls) == 1:
            raise IdempotencyStoreError("throttled")
        return real_complete(self, *args, **kwargs)

    with patch.object(IdempotencyStore, "complete", flaky):
        resp = idempotent_create(_event({"clientToken": "tok-1"}), create, operation="Op")

    assert resp["statusCode"] == 201
    assert calls == [False, False]
    [item] = _items(table)
    assert item["status"] == "COMPLETED" and item["body"] == resp["body"]


def test_idempotent_create_complete_exhausted_falls_back_to_body_omitted(table: Any) -> None:
    create = _Create()
    real_complete = IdempotencyStore.complete

    def full_body_fails(self: IdempotencyStore, *args: Any, **kwargs: Any) -> bool:
        if not kwargs.get("omit_body"):
            raise IdempotencyStoreError("throttled")
        return real_complete(self, *args, **kwargs)

    with patch.object(IdempotencyStore, "complete", full_body_fails):
        first = idempotent_create(_event({"clientToken": "tok-1"}), create, operation="Op")

    assert first["statusCode"] == 201
    [item] = _items(table)
    assert item["status"] == "COMPLETED" and item["body_omitted"] is True and "body" not in item
    # Even long after the IN_PROGRESS timeout, a retry is told the create already
    # succeeded instead of running it again.
    later = int(item["created_at"]) + IN_PROGRESS_TIMEOUT_SECONDS + 1
    with patch.object(idempotency.time, "time", return_value=later):
        retry = idempotent_create(_event({"clientToken": "tok-1"}), create, operation="Op")
    assert retry["statusCode"] == 409
    assert len(create.calls) == 1


def test_idempotent_create_complete_and_fallback_fail_still_returns_response() -> None:
    fake_table = MagicMock()
    fake_table.update_item.side_effect = ClientError({"Error": {"Code": "InternalServerError"}}, "UpdateItem")
    create = _Create()
    resp = idempotent_create(
        _event({"clientToken": "tok-1"}), create, operation="Op", store=IdempotencyStore(_TABLE, table=fake_table)
    )
    assert resp["statusCode"] == 201
    assert fake_table.update_item.call_count == idempotency._MAX_COMPLETE_ATTEMPTS + 1


def test_idempotent_create_record_taken_over_before_complete_is_not_retried() -> None:
    fake_table = MagicMock()
    fake_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    resp = idempotent_create(
        _event({"clientToken": "tok-1"}), _Create(), operation="Op", store=IdempotencyStore(_TABLE, table=fake_table)
    )
    assert resp["statusCode"] == 201
    fake_table.update_item.assert_called_once()


def test_in_progress_timeout_covers_longest_handler_timeout() -> None:
    assert IN_PROGRESS_TIMEOUT_SECONDS > idempotency.MAX_HANDLER_TIMEOUT_SECONDS


def test_idempotent_decorator_wraps_handler_and_passes_context(table: Any) -> None:
    seen: list[tuple[dict[str, Any], Any]] = []

    @idempotent(operation="CreateThing", error_field="error")
    def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
        """Docstring kept."""
        seen.append((json.loads(event["body"]), context))
        return {"statusCode": 201, "headers": {}, "body": json.dumps({"n": len(seen)})}

    ctx = object()
    first = handler(_event({"a": 1, "clientToken": "t"}), ctx)
    second = handler(_event({"a": 1, "clientToken": "t"}), ctx)
    bad = handler(_event({"clientToken": " "}), ctx)

    assert seen == [({"a": 1}, ctx)]
    assert second["body"] == first["body"]
    assert bad["statusCode"] == 400 and "error" in json.loads(bad["body"])
    assert handler.__doc__ == "Docstring kept."
    assert handler.__wrapped__(_event({"a": 2}), ctx)["statusCode"] == 201  # type: ignore[attr-defined]


def test_reset_store_cache_drops_cached_stores(table: Any) -> None:
    idempotent_create(_event({"clientToken": "t"}), _Create(), operation="Op")
    assert idempotency._STORES
    reset_store_cache()
    assert not idempotency._STORES


def test_claim_that_keeps_losing_to_a_release_is_a_contention_conflict() -> None:
    store = MagicMock(spec=IdempotencyStore)
    store.claim.return_value = False
    store.get.return_value = None  # released between every put and read

    outcome = idempotency._claim(store, "k", operation="CreateThing", fingerprint="f")

    assert isinstance(outcome, idempotency._Conflict)
    assert outcome.reason == "claim_contention"
    assert store.claim.call_count == idempotency._MAX_CLAIM_ATTEMPTS
