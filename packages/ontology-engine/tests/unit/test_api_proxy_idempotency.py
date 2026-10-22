# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Job-starting POST idempotency (StartInduction and friends), enforced in the API proxy Lambda.

The ECS service never sees the caller identity, so the proxy applies the
``clientToken`` guard before forwarding each POST in ``_IDEMPOTENT_POSTS``.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import boto3
import pytest
from coa_common import idempotency
from moto import mock_aws

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology-engine.ns:8001")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")


@pytest.fixture
def idem_table(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("IDEMPOTENCY_TABLE", "test-idempotency")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    idempotency.reset_store_cache()
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="test-idempotency",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
    idempotency.reset_store_cache()


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch):
    from coa_ontology import api_proxy_handler

    # The handler snapshots ALLOWED_ORIGIN at import time, and another test
    # module may have imported it first with the variable unset.
    monkeypatch.setitem(api_proxy_handler._CORS_HEADERS, "Access-Control-Allow-Origin", "https://app.example.com")
    return api_proxy_handler


def _ok(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.status = 202
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _induce_event(body: dict, *, method: str = "POST") -> dict:
    return {
        "httpMethod": method,
        "resource": "/namespaces/{namespaceId}/induce",
        "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
        "queryStringParameters": None,
        "headers": {"Content-Type": "application/json"},
        "requestContext": {"authorizer": {"principalId": "steward@example.com"}},
        "body": json.dumps(body),
    }


def test_start_induction_retry_with_token_forwards_once_and_replays(mod, idem_table: None) -> None:
    body = {"dataSourceIds": ["ds-1"], "clientToken": "induce-1"}
    with patch("urllib.request.urlopen", return_value=_ok(b'{"jobId": "job-1"}')) as urlopen:
        first = mod.handler(_induce_event(body), None)
        second = mod.handler(_induce_event(body), None)

    assert first["statusCode"] == 202
    assert (second["statusCode"], second["body"]) == (202, first["body"])
    assert second["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"
    urlopen.assert_called_once()
    forwarded = json.loads(urlopen.call_args.args[0].data)
    assert forwarded == {"dataSourceIds": ["ds-1"]}


def test_start_induction_backend_error_releases_token_for_retry(mod, idem_table: None) -> None:
    body = {"dataSourceIds": ["ds-1"], "clientToken": "induce-2"}
    err = HTTPError("http://x", 409, "Conflict", {}, io.BytesIO(b'{"detail": "job running"}'))
    with patch("urllib.request.urlopen", side_effect=[err, _ok(b'{"jobId": "job-2"}')]) as urlopen:
        first = mod.handler(_induce_event(body), None)
        second = mod.handler(_induce_event(body), None)

    assert first["statusCode"] == 409
    assert second["statusCode"] == 202
    assert urlopen.call_count == 2


def test_proxy_other_routes_bypass_idempotency(mod) -> None:
    event = _induce_event({"clientToken": "x"}, method="GET")
    event["resource"] = "/namespaces/{namespaceId}/induce/jobs"
    with (
        patch.object(idempotency, "_get_store") as get_store,
        patch("urllib.request.urlopen", return_value=_ok(b"[]")),
    ):
        mod.handler(event, None)
    get_store.assert_not_called()


# ── Every job-starting POST behind the proxy ─────────────────────────────────

_OPENAPI = (
    Path(__file__).resolve().parents[4]
    / "smithy-generated"
    / "openapi"
    / "openapi"
    / "ControlPlaneService.openapi.json"
)

_PATH_PARAMS = {
    "namespaceId": "11111111-1111-4111-8111-111111111111",
    "ontologyId": "urn:o",
    "key": "foaf",
    "proposalId": "p-1",
}


def _event_for(resource: str, body: dict, **path_overrides: str) -> dict:
    params = {name: _PATH_PARAMS[name] for name in re.findall(r"\{(\w+)\}", resource)}
    params.update(path_overrides)
    return {**_induce_event(body), "resource": resource, "pathParameters": params}


# Spelled out (not read from the module) because the proxy reads its endpoint
# from the environment at import time; the list is checked against the module's
# table below.
_WRAPPED_RESOURCES = [
    "/namespaces/{namespaceId}/induce",
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-from-s3",
    "/namespaces/{namespaceId}/ontology/foundational/{key}/load",
    "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints",
    "/namespaces/{namespaceId}/proposals/{proposalId}/validate",
]


def test_wrapped_resource_list_matches_the_proxy(mod) -> None:
    assert sorted(_WRAPPED_RESOURCES) == sorted(mod._IDEMPOTENT_POSTS)


@pytest.mark.parametrize("resource", _WRAPPED_RESOURCES)
def test_wrapped_post_retry_forwards_once_and_replays(mod, idem_table: None, resource: str) -> None:
    body = {"clientToken": "retry-1"}
    with patch("urllib.request.urlopen", return_value=_ok(b'{"job_id": "j-1"}')) as urlopen:
        first = mod.handler(_event_for(resource, body), None)
        second = mod.handler(_event_for(resource, body), None)

    assert (second["statusCode"], second["body"]) == (first["statusCode"], first["body"])
    urlopen.assert_called_once()
    assert "clientToken" not in json.loads(urlopen.call_args.args[0].data)


def test_token_reused_for_another_proposal_is_409(mod, idem_table: None) -> None:
    resource = "/namespaces/{namespaceId}/proposals/{proposalId}/validate"
    with patch("urllib.request.urlopen", return_value=_ok(b'{"job_id": "j-1"}')) as urlopen:
        mod.handler(_event_for(resource, {"clientToken": "t"}, proposalId="p-1"), None)
        other = mod.handler(_event_for(resource, {"clientToken": "t"}, proposalId="p-2"), None)

    assert other["statusCode"] == 409
    urlopen.assert_called_once()


def test_backend_timeout_releases_the_token(mod, idem_table: None) -> None:
    """The proxy answers 504 itself (before the Lambda dies), so the claim is released and a retry runs."""
    resource = "/namespaces/{namespaceId}/ontology/foundational/{key}/load"
    with patch("urllib.request.urlopen", side_effect=[TimeoutError(), _ok(b'{"result": {}}')]) as urlopen:
        first = mod.handler(_event_for(resource, {"clientToken": "t"}), None)
        second = mod.handler(_event_for(resource, {"clientToken": "t"}), None)

    assert (first["statusCode"], second["statusCode"]) == (504, 202)
    assert urlopen.call_count == 2
    assert {c.kwargs["timeout"] for c in urlopen.call_args_list} == {mod._IDEMPOTENT_BACKEND_TIMEOUT_SECONDS}
    assert mod._IDEMPOTENT_BACKEND_TIMEOUT_SECONDS < mod._BACKEND_TIMEOUT_SECONDS


def test_wrapped_posts_match_the_operations_that_declare_client_token() -> None:
    """The proxy table and the Smithy model agree on which engine POSTs take a ``clientToken``."""
    from coa_ontology import api_proxy_handler

    if not _OPENAPI.is_file():
        pytest.skip(f"{_OPENAPI} not found — run `make generate`")
    spec = json.loads(_OPENAPI.read_text())
    schemas = spec["components"]["schemas"]
    declared: dict[str, str] = {}
    for path in api_proxy_handler._PATH_MAP:
        op = spec["paths"].get(path, {}).get("post")
        ref = ((op or {}).get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema", {})
        props = schemas.get(ref.get("$ref", "").rsplit("/", 1)[-1], {}).get("properties", {})
        if "clientToken" in props:
            declared[path] = op["operationId"]
    assert declared == api_proxy_handler._IDEMPOTENT_POSTS
