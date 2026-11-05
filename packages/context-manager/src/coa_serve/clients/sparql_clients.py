# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-source SPARQL data-endpoint clients for ``RDF_SPARQL`` sources.

Two concerns live here and nowhere else:

* **Engine selection** — :func:`build_sparql_client` reads ``configuration.store`` and
  returns the client for that engine. ``RDF_SPARQL`` (the source sub-type) only decides
  THAT the SPARQL strategy runs; the store decides WHICH client talks to the endpoint.
  A new engine is one more ``match`` arm (plus its auth mode), never a new sub-type or
  strategy. Its registration-time twin is the verify Lambda's ``PROBES`` map.
* **Client lifecycle** — :class:`SparqlClientCache` keeps one long-lived client per
  source (pooled HTTP connection, cached signer) and closes it once it is dropped and
  no request still holds it.

Everything engine-specific — endpoint shape, auth, signing, port — stays inside the
client the factory builds. Query safety does not: it is the strategy's
``SPARQLFirewall``, applied identically to every engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

import structlog
from coa_common import resolve_region
from coa_common.sparql_config import SparqlSourceConfig, SparqlStore

from .neptune import NeptuneGraphClient
from .ttl_cache import TtlLruCache

logger = structlog.get_logger(__name__)

# One client per (namespace, source, configuration) — bounded so a long-lived process
# serving many sources cannot accumulate connection pools without limit.
CLIENT_CACHE_MAX_SIZE = 64
CLIENT_CACHE_TTL_S = 600.0
# The largest answer one graph-source query may return, as metric-graph bounds its bodies.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class SparqlEndpointClient(Protocol):
    """Read transport to one source's SPARQL data endpoint (the A-Box)."""

    async def query(self, sparql: str, *, max_results: int = ...) -> list[dict[str, Any]]:
        """Run a SELECT; one flat ``{var: value}`` dict per solution."""
        ...

    async def ask(self, sparql: str) -> bool:
        """Run an ASK."""
        ...

    async def construct(self, sparql: str, *, max_triples: int = ...) -> list[dict[str, str]]:
        """Run a CONSTRUCT; one ``{subject, predicate, object}`` dict per triple; at most ``max_triples``."""
        ...

    async def close(self) -> None:
        """Release pooled connections."""
        ...


class UnsupportedSparqlEngineError(ValueError):
    """The source's ``configuration.store`` has no client implementation."""


def build_sparql_client(config: SparqlSourceConfig, *, default_region: str | None = None) -> SparqlEndpointClient:
    """Return the data-endpoint client for the engine the source is configured with.

    Args:
        config: The source's parsed configuration (:mod:`coa_common.sparql_config`).
        default_region: SigV4 region when the configuration names none; defaults to
            the serve runtime's region.

    Raises:
        UnsupportedSparqlEngineError: No client exists for ``config.store``.
    """
    match config.store:
        case SparqlStore.NEPTUNE:
            # SigV4 with the serve role; no credential secret. The firewall gates the
            # query, so the client's keyword guard is off (see NeptuneGraphClient).
            return NeptuneGraphClient(
                endpoint=config.host,
                region=config.signing_region(default_region or resolve_region()),
                port=config.effective_port,
                enforce_read_only=False,
                retry_timeouts=False,
                max_body_bytes=MAX_RESPONSE_BYTES,
            )
        case other:
            raise UnsupportedSparqlEngineError(f"No SPARQL client for store {other!r}")


def _fingerprint(config: SparqlSourceConfig) -> str:
    return hashlib.sha256(json.dumps(config.to_record(), sort_keys=True).encode()).hexdigest()[:16]


class SparqlClientCache:
    """Long-lived data-endpoint clients, one per (namespace, source, configuration).

    Keyed on the configuration's fingerprint as well as the source, so an edited
    endpoint is picked up by the next request instead of after the TTL; the client
    for the old configuration ages out and is closed. Callers hold a client through
    :meth:`lease`. A dropped client (expired, LRU-evicted or replaced) is closed in
    the background as soon as no lease holds it, so a request never loses its
    connection pool mid-query; :meth:`aclose` closes the rest at shutdown.
    """

    def __init__(self, *, max_size: int = CLIENT_CACHE_MAX_SIZE, ttl_s: float = CLIENT_CACHE_TTL_S, factory=None):
        """Create an empty cache.

        Args:
            max_size: Maximum clients retained.
            ttl_s: Seconds a client is reused before it is rebuilt.
            factory: ``(config) -> SparqlEndpointClient``; defaults to
                :func:`build_sparql_client`. Injectable for tests.
        """
        self._factory = factory or build_sparql_client
        self._closing: set[asyncio.Task[None]] = set()
        self._leases: dict[int, int] = {}
        self._dropped_while_leased: dict[int, SparqlEndpointClient] = {}
        self._cache: TtlLruCache[tuple[str, str, str], SparqlEndpointClient] = TtlLruCache(
            ttl_s=ttl_s, max_size=max_size, on_evict=self._drop
        )

    def lease(
        self, namespace: str, data_source_id: str, config: SparqlSourceConfig
    ) -> AbstractContextManager[SparqlEndpointClient]:
        """Hold the cached client for this source and configuration, building it on first use.

        Use as ``with cache.lease(...) as client:``; the client stays open until the
        block exits, even if the cache drops it meanwhile.

        Raises:
            UnsupportedSparqlEngineError: From the factory, for an unknown store.
        """
        return self._held(self._client_for(namespace, data_source_id, config))

    def _client_for(self, namespace: str, data_source_id: str, config: SparqlSourceConfig) -> SparqlEndpointClient:
        key = (namespace, data_source_id, _fingerprint(config))
        client = self._cache.get(key)
        if client is None:
            client = self._factory(config)
            self._cache.set(key, client)
            logger.info(
                "sparql_client_created",
                namespace=namespace,
                data_source_id=data_source_id,
                store=config.store.value,
            )
        return client

    @contextmanager
    def _held(self, client: SparqlEndpointClient) -> Iterator[SparqlEndpointClient]:
        key = id(client)
        self._leases[key] = self._leases.get(key, 0) + 1
        try:
            yield client
        finally:
            self._leases[key] -= 1
            if not self._leases[key]:
                del self._leases[key]
                dropped = self._dropped_while_leased.pop(key, None)
                if dropped is not None:
                    self._close_in_background(dropped)

    def _drop(self, client: SparqlEndpointClient) -> None:
        if id(client) in self._leases:
            self._dropped_while_leased[id(client)] = client
        else:
            self._close_in_background(client)

    def _close_in_background(self, client: SparqlEndpointClient) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync teardown): nothing pooled can be awaited here
        task = loop.create_task(_close_quietly(client))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def aclose(self) -> None:
        """Close every cached or still-leased client and wait for background closes to finish."""
        dropped = list(self._dropped_while_leased.values())
        self._dropped_while_leased.clear()
        await asyncio.gather(*(_close_quietly(c) for c in [*self._cache.clear(), *dropped]), *self._closing)

    def __len__(self) -> int:
        """Number of clients currently cached."""
        return len(self._cache)


async def _close_quietly(client: SparqlEndpointClient) -> None:
    try:
        await client.close()
    except Exception as e:  # releasing a pool must never fail the request that evicted it
        logger.warning("sparql_client_close_failed", error=type(e).__name__)
