# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lambda client for direct invocation of backend Lambdas.

Builds synthetic API Gateway v1.0 proxy integration events and invokes
backend Lambdas directly, bypassing API Gateway.
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = structlog.get_logger(__name__)

_LAMBDA_INVOKE_TIMEOUT_S = int(os.environ.get("LAMBDA_INVOKE_TIMEOUT_S", "30"))


class LambdaInvokeError(Exception):
    """Raised when Lambda invocation itself fails (function error, timeout, throttle)."""


class LambdaApiError(Exception):
    """Raised when the Lambda returns a non-2xx statusCode in its API GW response."""

    def __init__(self, status_code: int, body: str, message: str = ""):
        """Capture the failing status code and response body."""
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"Lambda returned {status_code}: {body[:200]}")


class LambdaClient:
    """Invoke backend Lambdas with synthetic API Gateway proxy events."""

    def __init__(self, region: str):
        """Initialize the boto3 Lambda client with fixed timeout and retry config."""
        # Sync customer path: explicit connect/read timeouts and fail-fast
        # (max_attempts=1 == no retries) — the caller decides when to retry.
        self._lambda_client = boto3.client(
            "lambda",
            region_name=region,
            config=BotoConfig(
                read_timeout=_LAMBDA_INVOKE_TIMEOUT_S,
                connect_timeout=5,
                retries={"max_attempts": 1},
            ),
        )
        logger.info("lambda_client_initialized", region=region)

    def _build_event(
        self,
        method: str,
        resource: str,
        path_params: dict,
        query_params: dict | None,
        body: dict | None,
        bearer_token: str,
        authorizer_context: dict | None = None,
    ) -> dict:
        """Build a synthetic API Gateway v1.0 proxy integration event."""
        resolved_path = resource
        for key, value in path_params.items():
            resolved_path = resolved_path.replace(f"{{{key}}}", value)

        request_context: dict = {"stage": "mcp", "requestId": str(uuid4())}
        if authorizer_context:
            request_context["authorizer"] = authorizer_context

        return {
            "httpMethod": method,
            "resource": resource,
            "path": resolved_path,
            "pathParameters": path_params,
            "queryStringParameters": query_params or None,
            "headers": {
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
            },
            "body": json.dumps(body) if body else None,
            "isBase64Encoded": False,
            "requestContext": request_context,
        }

    def _invoke_sync(self, function_name: str, payload: bytes) -> dict:
        """Synchronous Lambda invoke (called via asyncio.to_thread)."""
        return self._lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=payload,
        )

    async def invoke_api(
        self,
        function_name: str,
        method: str,
        resource: str,
        path_params: dict,
        query_params: dict | None = None,
        body: dict | None = None,
        bearer_token: str = "",
        authorizer_context: dict | None = None,
    ) -> dict:
        """Invoke Lambda, parse API GW proxy response, return body as dict.

        Raises LambdaInvokeError when the invocation itself fails and
        LambdaApiError when the Lambda returns a non-2xx status code.
        """
        event = self._build_event(method, resource, path_params, query_params, body, bearer_token, authorizer_context)
        payload = json.dumps(event).encode()

        try:
            response = await asyncio.to_thread(self._invoke_sync, function_name, payload)
        except ClientError as exc:
            logger.error("lambda_invoke_client_error", function=function_name, error=str(exc))
            raise LambdaInvokeError(f"Lambda invoke failed: {exc}") from exc

        if "FunctionError" in response:
            error_payload = response["Payload"].read().decode()
            logger.error(
                "lambda_function_error",
                function=function_name,
                error=error_payload[:500],
            )
            raise LambdaInvokeError(f"Lambda function error: {error_payload[:500]}")

        response_body = json.loads(response["Payload"].read().decode())
        status_code = response_body.get("statusCode", 200)

        if status_code >= 400:
            body_str = response_body.get("body", "")
            logger.warning(
                "lambda_api_error",
                function=function_name,
                status=status_code,
                body=body_str[:200],
            )
            raise LambdaApiError(status_code=status_code, body=body_str)

        raw_body = response_body.get("body", "{}")
        if isinstance(raw_body, str):
            return json.loads(raw_body)
        return raw_body
