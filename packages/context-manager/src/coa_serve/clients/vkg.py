# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG Service client — SPARQL-to-SQL translation over HTTP(S).

Calls the VKG Service's /sparql/translate endpoint to translate SPARQL
queries into SQL using the virtual knowledge graph mapping.

Retry policy (via retryhttp): 3 attempts with exponential backoff + jitter on:
  - HTTP 503 (VKG mid-reload)
  - Connection errors (rolling deploy, DNS propagation)
  - Timeouts (task startup, network blip)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx
import retryhttp
import structlog

from .base import instrumented

logger = structlog.get_logger(__name__)

DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class SparqlProjection:
    """SPARQL projection metadata from Ontop reformulation.

    Maps SPARQL SELECT variables to SQL output column aliases, enabling
    correct projection of raw SQL results to the SPARQL solution.
    """

    select_vars: list[str]
    var_to_column: dict[str, str]
    distinct: bool = False


@dataclass(frozen=True)
class VKGResult:
    """Result of SPARQL-to-SQL translation from VKG Service."""

    sql: str
    dialect: str
    ontology_version: str
    source_table_refs: list[str] = field(default_factory=list)
    datasource_routing: dict[str, dict[str, str]] = field(default_factory=dict)
    projection: SparqlProjection | None = None


class VKGTranslationError(Exception):
    """Raised when VKG Service returns an error or unexpected response."""


class VKGClient:
    """Async HTTP client for VKG Service SPARQL-to-SQL translation."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_RETRIES,
    ):
        """Configure the VKG Service base URL, request timeout, and retry budget.

        Args:
            base_url: VKG Service base URL. Defaults to the ``VKG_SERVICE_URL`` env var.
            timeout: Per-request timeout in seconds.
            max_retries: Maximum retry attempts for transient errors.

        Raises:
            ValueError: If no base URL is provided via arg or env var.
        """
        self._base_url = (base_url or os.environ.get("VKG_SERVICE_URL") or "").rstrip("/")
        if not self._base_url:
            raise ValueError("VKG Service URL must be provided via constructor arg or VKG_SERVICE_URL env var")
        self._timeout = timeout
        self._max_retries = max_retries
        self._http: httpx.AsyncClient | None = None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self._http

    async def close(self) -> None:
        """Close the pooled httpx client, if one was created."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _post_with_retry(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """POST request with retry on transient errors (503, connection, timeout)."""

        @retryhttp.retry(
            max_attempt_number=self._max_retries,
            retry_server_errors=True,
            retry_network_errors=True,
            retry_timeouts=True,
            retry_rate_limited=False,
            server_error_codes=(503,),
        )
        async def _do_post() -> httpx.Response:
            client = await self._get_http()
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp

        return await _do_post()

    @instrumented("vkg")
    async def translate(self, sparql: str, *, namespace: str) -> VKGResult:
        """Translate SPARQL to SQL via VKG Service.

        Raises:
            VKGTranslationError: On non-retryable errors or exhausted retries.
        """
        url = "/sparql/translate"
        payload = {"sparql": sparql, "namespace": namespace, "targetDialect": "trino"}

        try:
            resp = await self._post_with_retry(url, payload)
        except httpx.HTTPStatusError as e:
            detail = e.response.text.replace("\n", " ")
            raise VKGTranslationError(f"VKG returned {e.response.status_code}: {detail}") from e
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise VKGTranslationError(
                f"VKG request failed after {self._max_retries} attempts: {type(e).__name__}"
            ) from e
        except httpx.HTTPError as e:
            raise VKGTranslationError(f"VKG request failed: {e}") from e

        try:
            data = resp.json()
            projection = None
            if "projection" in data:
                proj_data = data["projection"]
                projection = SparqlProjection(
                    select_vars=proj_data.get("selectVars", []),
                    var_to_column=proj_data.get("varToColumn", {}),
                    distinct=proj_data.get("distinct", False),
                )
            return VKGResult(
                sql=data["sql"],
                dialect=data.get("dialect", "postgresql"),
                ontology_version=data.get("ontologyVersion", "unknown"),
                source_table_refs=data.get("sourceTableRefs", []),
                datasource_routing=data.get("datasourceRouting", {}),
                projection=projection,
            )
        except (KeyError, ValueError) as e:
            raise VKGTranslationError(f"VKG response parse error: {e}") from e

    async def health_check(self) -> dict[str, Any]:
        """Probe the VKG Service ``/health`` endpoint; returns a status dict, never raises."""
        try:
            client = await self._get_http()
            resp = await client.get("/health")
            return {"status": "ok" if resp.status_code == 200 else "degraded"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}
