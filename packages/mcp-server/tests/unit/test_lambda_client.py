# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for LambdaClient."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_common.constants import RESOURCE_PREFIX
from coa_mcp.clients.lambda_client import (
    LambdaApiError,
    LambdaClient,
    LambdaInvokeError,
)


@pytest.fixture
def mock_boto3_client():
    """Patch boto3.client and return the mock Lambda client."""
    with (
        patch("coa_mcp.clients.lambda_client.boto3") as mock_boto3,
        patch("coa_mcp.clients.lambda_client.BotoConfig"),
    ):
        mock_lambda = MagicMock()
        mock_boto3.client.return_value = mock_lambda
        yield mock_lambda


@pytest.fixture
def lambda_client(mock_boto3_client):
    """Create a LambdaClient with mocked boto3."""
    return LambdaClient(region="us-east-1")


def _make_lambda_response(status_code: int = 200, body: dict | None = None) -> dict:
    """Build a mock Lambda invoke response with API GW proxy format."""
    proxy_response = {
        "statusCode": status_code,
        "body": json.dumps(body or {}),
        "headers": {"Content-Type": "application/json"},
    }
    payload_stream = MagicMock()
    payload_stream.read.return_value = json.dumps(proxy_response).encode()
    return {"Payload": payload_stream, "StatusCode": 200}


def _make_function_error_response(error_message: str = "Something broke") -> dict:
    """Build a mock Lambda response with FunctionError."""
    payload_stream = MagicMock()
    payload_stream.read.return_value = json.dumps({"errorMessage": error_message}).encode()
    return {"Payload": payload_stream, "StatusCode": 200, "FunctionError": "Unhandled"}


@pytest.mark.unit
async def test_happy_path_returns_parsed_body(lambda_client, mock_boto3_client):
    """Successful invocation returns parsed JSON body."""
    expected_body = {"metrics": [{"name": "revenue"}]}
    mock_boto3_client.invoke.return_value = _make_lambda_response(200, expected_body)

    result = await lambda_client.invoke_api(
        function_name=f"{RESOURCE_PREFIX}-dev-metric-api",
        method="GET",
        resource="/namespaces/{namespaceId}/metrics",
        path_params={"namespaceId": "test-ns"},
        bearer_token="my-token",
    )

    assert result == expected_body


@pytest.mark.unit
async def test_function_error_raises_lambda_invoke_error(lambda_client, mock_boto3_client):
    """Lambda FunctionError raises LambdaInvokeError."""
    mock_boto3_client.invoke.return_value = _make_function_error_response("timeout")

    with pytest.raises(LambdaInvokeError, match="Lambda function error"):
        await lambda_client.invoke_api(
            function_name=f"{RESOURCE_PREFIX}-dev-metric-api",
            method="GET",
            resource="/namespaces/{namespaceId}/metrics",
            path_params={"namespaceId": "ns1"},
        )


@pytest.mark.unit
async def test_non_200_status_raises_lambda_api_error(lambda_client, mock_boto3_client):
    """Non-2xx statusCode raises LambdaApiError with status and body."""
    mock_boto3_client.invoke.return_value = _make_lambda_response(404, {"message": "Not found"})

    with pytest.raises(LambdaApiError) as exc_info:
        await lambda_client.invoke_api(
            function_name=f"{RESOURCE_PREFIX}-dev-metric-api",
            method="GET",
            resource="/namespaces/{namespaceId}/metrics",
            path_params={"namespaceId": "missing"},
        )

    assert exc_info.value.status_code == 404
    assert "Not found" in exc_info.value.body


@pytest.mark.unit
async def test_client_error_raises_lambda_invoke_error(lambda_client, mock_boto3_client):
    """boto3 ClientError (timeout, throttle) raises LambdaInvokeError."""
    mock_boto3_client.invoke.side_effect = ClientError(
        {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}},
        "Invoke",
    )

    with pytest.raises(LambdaInvokeError, match="Lambda invoke failed"):
        await lambda_client.invoke_api(
            function_name=f"{RESOURCE_PREFIX}-dev-metric-api",
            method="GET",
            resource="/namespaces/{namespaceId}/metrics",
            path_params={"namespaceId": "ns1"},
        )


@pytest.mark.unit
async def test_bearer_token_in_headers(lambda_client, mock_boto3_client):
    """Bearer token appears in the synthetic event's Authorization header."""
    mock_boto3_client.invoke.return_value = _make_lambda_response(200, {})

    await lambda_client.invoke_api(
        function_name=f"{RESOURCE_PREFIX}-dev-metric-api",
        method="GET",
        resource="/namespaces/{namespaceId}/metrics",
        path_params={"namespaceId": "ns1"},
        bearer_token="secret-jwt-token",
    )

    call_kwargs = mock_boto3_client.invoke.call_args[1]
    event = json.loads(call_kwargs["Payload"])
    assert event["headers"]["Authorization"] == "Bearer secret-jwt-token"


@pytest.mark.unit
async def test_body_is_json_serialized(lambda_client, mock_boto3_client):
    """Request body is JSON-serialized in the synthetic event."""
    mock_boto3_client.invoke.return_value = _make_lambda_response(200, {})

    await lambda_client.invoke_api(
        function_name=f"{RESOURCE_PREFIX}-dev-data-layer-api",
        method="POST",
        resource="/namespaces/{namespaceId}/query",
        path_params={"namespaceId": "ns1"},
        body={"query": "What is revenue?"},
    )

    call_kwargs = mock_boto3_client.invoke.call_args[1]
    event = json.loads(call_kwargs["Payload"])
    assert json.loads(event["body"]) == {"query": "What is revenue?"}


@pytest.mark.unit
async def test_query_params_passed_through(lambda_client, mock_boto3_client):
    """Query parameters are included in the synthetic event."""
    mock_boto3_client.invoke.return_value = _make_lambda_response(200, {})

    await lambda_client.invoke_api(
        function_name=f"{RESOURCE_PREFIX}-dev-metric-api",
        method="GET",
        resource="/namespaces/{namespaceId}/metrics",
        path_params={"namespaceId": "ns1"},
        query_params={"maxResults": "50", "includeProperties": "true"},
    )

    call_kwargs = mock_boto3_client.invoke.call_args[1]
    event = json.loads(call_kwargs["Payload"])
    assert event["queryStringParameters"] == {"maxResults": "50", "includeProperties": "true"}


@pytest.mark.unit
async def test_path_params_substitute_into_path(lambda_client, mock_boto3_client):
    """Path parameters are substituted into the resolved path."""
    mock_boto3_client.invoke.return_value = _make_lambda_response(200, {})

    await lambda_client.invoke_api(
        function_name=f"{RESOURCE_PREFIX}-dev-ontology-api-proxy",
        method="GET",
        resource="/namespaces/{namespaceId}/graph/schema",
        path_params={"namespaceId": "my-namespace"},
    )

    call_kwargs = mock_boto3_client.invoke.call_args[1]
    event = json.loads(call_kwargs["Payload"])
    assert event["resource"] == "/namespaces/{namespaceId}/graph/schema"
    assert event["path"] == "/namespaces/my-namespace/graph/schema"
    assert event["pathParameters"] == {"namespaceId": "my-namespace"}


@pytest.mark.unit
async def test_no_body_sends_null(lambda_client, mock_boto3_client):
    """When no body is provided, event body is None."""
    mock_boto3_client.invoke.return_value = _make_lambda_response(200, {})

    await lambda_client.invoke_api(
        function_name=f"{RESOURCE_PREFIX}-dev-metric-api",
        method="GET",
        resource="/namespaces/{namespaceId}/metrics",
        path_params={"namespaceId": "ns1"},
    )

    call_kwargs = mock_boto3_client.invoke.call_args[1]
    event = json.loads(call_kwargs["Payload"])
    assert event["body"] is None


@pytest.mark.unit
async def test_no_query_params_sends_null(lambda_client, mock_boto3_client):
    """When no query params are provided, queryStringParameters is None."""
    mock_boto3_client.invoke.return_value = _make_lambda_response(200, {})

    await lambda_client.invoke_api(
        function_name=f"{RESOURCE_PREFIX}-dev-metric-api",
        method="GET",
        resource="/namespaces/{namespaceId}/metrics",
        path_params={"namespaceId": "ns1"},
    )

    call_kwargs = mock_boto3_client.invoke.call_args[1]
    event = json.loads(call_kwargs["Payload"])
    assert event["queryStringParameters"] is None
