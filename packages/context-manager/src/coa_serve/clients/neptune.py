# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune SPARQL client — GraphClient implementation.

Uses IAM SigV4 authentication via botocore. Consistent with the
ontology-engine's NeptuneDBGraphStore pattern but exposes only the
read-only SPARQL interface needed by the resolution pipeline.

Error handling: Protocol methods raise on failure. Only health_check()
is exception-safe and returns a status dict.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any
from urllib.parse import urlencode

import httpx
import retryhttp
import structlog
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session
from coa_common import resolve_region

from .base import instrumented

logger = structlog.get_logger(__name__)

_SPARQL_WRITE_PATTERN = re.compile(
    r"\b(INSERT|DELETE|DROP|CREATE|LOAD|CLEAR|COPY|MOVE|ADD)\b",
    re.IGNORECASE,
)

# Tokens whose text is data, not SPARQL syntax: string literals (long and short,
# with escapes), IRI refs, and comments. They are blanked before the write-keyword
# check, which otherwise fires on any IRI or literal that merely contains one of
# those words -- e.g. a class IRI ``<https://clear.example/ns#Account>`` or a label
# ``"Load Balancer"`` -- and turns every read that names it into an error.
# One left-to-right scan, so a quote inside an IRI or a ``#`` inside a string is
# consumed by the token it belongs to. An unterminated literal matches nothing and
# stays visible, and an IRI token never spans whitespace, so it cannot swallow an
# update keyword together with its operand.
_SPARQL_DATA_TOKEN = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""'
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|<[^<>\s]*>"
    r"|#[^\r\n]*"
)

DEFAULT_MAX_RESULTS = 10_000


class SPARQLValidationError(ValueError):
    """Raised when SPARQL contains disallowed write operations."""


class NeptuneGraphClient:
    """Neptune DB SPARQL client with IAM SigV4 signing."""

    def __init__(
        self,
        endpoint: str | None = None,
        region: str | None = None,
        port: int = 8182,
    ):
        """Configure the Neptune endpoint, region, and port for signed requests.

        Args:
            endpoint: Neptune cluster host. Defaults to the ``NEPTUNE_ENDPOINT`` env var.
            region: AWS region for SigV4 signing. Defaults to the resolved region.
            port: Neptune HTTPS port.

        Raises:
            ValueError: If no endpoint is provided via arg or env var.
        """
        self._endpoint = endpoint or os.environ.get("NEPTUNE_ENDPOINT", "")
        self._region = region or resolve_region()
        self._port = port
        if not self._endpoint:
            raise ValueError("Neptune endpoint must be provided via constructor arg or NEPTUNE_ENDPOINT env var")
        self._base_url = f"https://{self._endpoint}:{self._port}"
        self._session = Session()
        self._http: httpx.AsyncClient | None = None
        self._http_lock = asyncio.Lock()

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            async with self._http_lock:
                if self._http is None:
                    # Sync customer path: fast fixed connect timeout + a bounded
                    # read budget (NEPTUNE_TIMEOUT, default 8s) so a hung graph
                    # query cannot tie up request threads.
                    read = float(os.environ.get("NEPTUNE_TIMEOUT", "8"))
                    timeout = httpx.Timeout(connect=2.0, read=read, write=read, pool=read)
                    self._http = httpx.AsyncClient(timeout=timeout)
        return self._http

    async def close(self) -> None:
        """Close the pooled httpx client, if one was created."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @instrumented("neptune")
    async def query(self, sparql: str, *, max_results: int = DEFAULT_MAX_RESULTS) -> list[dict[str, Any]]:
        """Execute a read-only SPARQL SELECT, appending a LIMIT if none is present.

        Args:
            sparql: The SPARQL query text (write operations are rejected).
            max_results: LIMIT applied when the query has none.

        Returns:
            The result bindings as a list of flat ``{var: value}`` dicts.

        Raises:
            SPARQLValidationError: If the query contains a write operation.
        """
        self._validate_sparql(sparql)
        if "LIMIT" not in sparql.upper():
            sparql = f"{sparql.rstrip().rstrip(';')} LIMIT {int(max_results)}"
        data = await self._sparql_post(sparql)
        bindings = data.get("results", {}).get("bindings", [])
        return [
            {k: (v.get("value", "") if isinstance(v, dict) else str(v)) for k, v in row.items()} for row in bindings
        ]

    @instrumented("neptune")
    async def ask(self, sparql: str) -> bool:
        """Execute a read-only SPARQL ASK query.

        Args:
            sparql: The SPARQL ASK query text (write operations are rejected).

        Returns:
            The boolean result of the ASK.

        Raises:
            SPARQLValidationError: If the query contains a write operation.
        """
        self._validate_sparql(sparql)
        data = await self._sparql_post(sparql)
        return data.get("boolean", False)

    async def health_check(self) -> dict[str, Any]:
        """Probe the Neptune ``/status`` endpoint; returns a status dict, never raises."""
        try:
            url = f"{self._base_url}/status"
            headers = await self._sign_request("GET", url)
            client = await self._get_http()
            resp = await client.get(url, headers=headers)
            return {"status": "ok" if resp.status_code == 200 else "degraded"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    async def _sparql_post(self, sparql: str) -> dict[str, Any]:
        url = f"{self._base_url}/sparql"
        body = urlencode({"query": sparql})
        headers = await self._sign_request("POST", url, body)
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        # Sync customer path: fail fast with a single retry, and only on
        # transient transport faults (connection reset / timeout). We do NOT
        # retry server errors here — the caller (resolution pipeline) has its
        # own tier fallback, and retrying 5xx would stack retries under load.
        @retryhttp.retry(
            max_attempt_number=2,
            retry_server_errors=False,
            retry_network_errors=True,
            retry_timeouts=True,
            retry_rate_limited=False,
        )
        async def _post() -> httpx.Response:
            client = await self._get_http()
            return await client.post(url, content=body, headers=headers)

        resp = await _post()
        if not resp.is_success:
            logger.error("neptune_sparql_error", status=resp.status_code, body=resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    async def _sign_request(self, method: str, url: str, body: str = "") -> dict[str, str]:
        loop = asyncio.get_running_loop()

        def _sign() -> dict[str, str]:
            credentials = self._session.get_credentials()
            if credentials is None:
                raise ValueError("AWS credentials not found — ensure IAM role or env vars are configured")
            frozen_creds = credentials.get_frozen_credentials()
            request = AWSRequest(method=method, url=url, data=body)
            SigV4Auth(frozen_creds, "neptune-db", self._region).add_auth(request)
            return dict(request.headers)

        return await loop.run_in_executor(None, _sign)

    def _validate_sparql(self, sparql: str) -> None:
        if _SPARQL_WRITE_PATTERN.search(_SPARQL_DATA_TOKEN.sub(" ", sparql)):
            raise SPARQLValidationError("Only read-only SPARQL queries are allowed (SELECT, ASK, DESCRIBE, CONSTRUCT)")
