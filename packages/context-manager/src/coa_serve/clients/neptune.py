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
from rdflib import Graph

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
SERVER_TIMEOUT_MARGIN_MS = 1000


def read_timeout_s() -> float:
    """The client's read budget for one query (``NEPTUNE_TIMEOUT``, default 8 s)."""
    return float(os.environ.get("NEPTUNE_TIMEOUT", "8"))


def server_timeout_ms() -> int:
    """The per-query timeout the engine enforces: below the client's read budget, so the server gives up first."""
    read_ms = read_timeout_s() * 1000
    return int(max(read_ms - SERVER_TIMEOUT_MARGIN_MS, read_ms / 2))


class SPARQLValidationError(ValueError):
    """Raised when SPARQL contains disallowed write operations."""


class SPARQLResponseTooLargeError(ValueError):
    """The endpoint's answer passed the client's byte cap; it is dropped unread."""


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, max_bytes: int):
        self._stream = stream
        self._max_bytes = max_bytes

    async def __aiter__(self):
        size = 0
        async for chunk in self._stream:
            size += len(chunk)
            if size > self._max_bytes:
                raise SPARQLResponseTooLargeError(f"the SPARQL response exceeds {self._max_bytes} bytes")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _BoundedTransport(httpx.AsyncBaseTransport):
    """A transport whose response bodies stop at ``max_bytes``, counted as they arrive."""

    def __init__(self, inner: httpx.AsyncBaseTransport, max_bytes: int):
        self._inner = inner
        self._max_bytes = max_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_BoundedStream(response.stream, self._max_bytes),  # type: ignore[arg-type]
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


class NeptuneGraphClient:
    """Neptune DB SPARQL client with IAM SigV4 signing.

    Serves two roles. As the SHARED ontology client (T-Box assembly, URI validation,
    metric lookups) it keeps the keyword write-guard as defence in depth. As the
    per-source DATA client of an RDF_SPARQL source (built by
    ``sparql_clients.build_sparql_client``) it is transport only: the strategy's
    parse-based ``SPARQLFirewall`` gates every query before it gets here, and the
    keyword regex would wrongly reject a read whose literal merely contains a
    keyword (``CONTAINS(?name, "load")``).
    """

    def __init__(
        self,
        endpoint: str | None = None,
        region: str | None = None,
        port: int = 8182,
        *,
        enforce_read_only: bool = True,
        retry_timeouts: bool = True,
        max_body_bytes: int | None = None,
    ):
        """Configure the Neptune endpoint, region, and port for signed requests.

        Args:
            endpoint: Neptune cluster host. Defaults to the ``NEPTUNE_ENDPOINT`` env var.
            region: AWS region for SigV4 signing. Defaults to the resolved region.
            port: Neptune HTTPS port.
            enforce_read_only: Reject queries containing an update keyword. Disabled
                only for a data-endpoint client whose queries the firewall gates.
            retry_timeouts: Retry a timed-out request once. Off for a data-endpoint client,
                whose timed-out query would otherwise spend the caller's deadline twice.
            max_body_bytes: Refuse a response larger than this, uncompressed, while it streams. Set for a
                data-endpoint client, whose LLM-written query can ask for one huge row.

        Raises:
            ValueError: If no endpoint is provided via arg or env var.
        """
        self._endpoint = endpoint or os.environ.get("NEPTUNE_ENDPOINT", "")
        self._region = region or resolve_region()
        self._port = port
        self._enforce_read_only = enforce_read_only
        self._retry_timeouts = retry_timeouts
        self._max_body_bytes = max_body_bytes
        self._transport: httpx.AsyncBaseTransport | None = None
        if not self._endpoint:
            raise ValueError("Neptune endpoint must be provided via constructor arg or NEPTUNE_ENDPOINT env var")
        self._base_url = f"https://{self._endpoint}:{self._port}"
        self._session = Session()
        self._http: httpx.AsyncClient | None = None
        self._http_lock = asyncio.Lock()
        self._closed = False

    async def _get_http(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("the Neptune client is closed")
        if self._http is None:
            async with self._http_lock:
                if self._http is None:
                    # Sync customer path: fast fixed connect timeout + a bounded
                    # read budget (NEPTUNE_TIMEOUT, default 8s) so a hung graph
                    # query cannot tie up request threads.
                    read = read_timeout_s()
                    timeout = httpx.Timeout(connect=2.0, read=read, write=read, pool=read)
                    transport = self._transport
                    if self._max_body_bytes is not None:
                        transport = _BoundedTransport(transport or httpx.AsyncHTTPTransport(), self._max_body_bytes)
                    self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        return self._http

    async def close(self) -> None:
        """Close the pooled httpx client, if one was created; a closed client never opens another."""
        self._closed = True
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

    @instrumented("neptune")
    async def construct(self, sparql: str, *, max_triples: int = DEFAULT_MAX_RESULTS) -> list[dict[str, str]]:
        """Execute a CONSTRUCT or DESCRIBE query and return its triples as rows.

        Graph-returning forms answer in an RDF serialization, not SPARQL JSON
        results, so this asks for N-Triples and parses them. A LIMIT bounds
        solutions, not triples (a template of k triples yields up to k per
        solution), so the triples themselves are capped here.

        Args:
            sparql: The query text (write operations are rejected).
            max_triples: At most this many triples are returned, the first in
                (subject, predicate, object) N-Triples order.

        Returns:
            One ``{"subject", "predicate", "object"}`` dict per returned triple.
        """
        self._validate_sparql(sparql)
        resp = await self._post_query(sparql, accept="application/n-triples")
        graph = Graph()
        graph.parse(data=resp.text, format="nt")
        # By N-Triples text: terms themselves do not always compare (a NaN double against a decimal raises).
        triples = sorted(graph, key=lambda triple: tuple(term.n3() for term in triple))
        if len(triples) > max_triples:
            logger.warning("neptune_construct_truncated", triples=len(triples), max_triples=max_triples)
            triples = triples[:max_triples]
        return [{"subject": str(s), "predicate": str(p), "object": str(o)} for s, p, o in triples]

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
        resp = await self._post_query(sparql, accept=None)
        return resp.json()

    async def _post_query(self, sparql: str, *, accept: str | None) -> httpx.Response:
        url = f"{self._base_url}/sparql"
        body = urlencode({"query": sparql})
        headers = await self._sign_request("POST", url, body)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if accept:
            headers["Accept"] = accept
        if self._max_body_bytes is not None:
            headers["Accept-Encoding"] = "identity"  # the cap counts what the parser reads, not compressed bytes

        # Sync customer path: fail fast with a single retry, and only on
        # transient transport faults (connection reset / timeout). We do NOT
        # retry server errors here — the caller (resolution pipeline) has its
        # own tier fallback, and retrying 5xx would stack retries under load.
        @retryhttp.retry(
            max_attempt_number=2,
            retry_server_errors=False,
            retry_network_errors=True,
            retry_timeouts=self._retry_timeouts,
            retry_rate_limited=False,
        )
        async def _post() -> httpx.Response:
            client = await self._get_http()
            return await client.post(url, content=body, headers=headers)

        resp = await _post()
        if not resp.is_success:
            logger.error("neptune_sparql_error", status=resp.status_code, body=resp.text[:500])
        resp.raise_for_status()
        return resp

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
        if self._enforce_read_only and _SPARQL_WRITE_PATTERN.search(_SPARQL_DATA_TOKEN.sub(" ", sparql)):
            raise SPARQLValidationError("Only read-only SPARQL queries are allowed (SELECT, ASK, DESCRIBE, CONSTRUCT)")
