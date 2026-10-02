# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``AossVectorClient`` — the shared AOSS vector client.

See the package docstring (``coa_common.opensearch``) for the
engine/filtering contract and the AOSS quirks this client encapsulates.

The client is transport-pluggable:

- **direct** (default): a SigV4-signed opensearch-py client, live credentials,
  full data plane (create / bulk / search / count / delete). Used by
  ontology-engine and metric-service.
- **proxy**: a caller-supplied ``transport`` callable that executes a *search*
  body elsewhere (e.g. the serve AgentCore search-proxy Lambda). Search/count
  only; write/index-admin ops raise. Used by the serve retrieval tiers.

Filtering is always server-side: :meth:`knn_search` and :meth:`count` build a
``bool``/``filter`` from the given ``term``/``exists`` clauses and push it into
the k-NN query (or a plain count query).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

import boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection
from opensearchpy.exceptions import RequestError

from coa_common.opensearch.retry import bulk_with_retry, oss_retry

log = logging.getLogger(__name__)

_AOSS_SERVICE = "aoss"  # OpenSearch Serverless — NOT "es"

# ``delete_by_term`` re-verifies to zero against AOSS's eventually-consistent
# search (search lags the index, so an empty *search* page is NOT proof the
# deletes are complete). ``_DELETE_VERIFY_POLL_S`` is the gap between
# delete-then-recount passes; ``_DELETE_VERIFY_STALL_TIMEOUT_S`` is a
# *no-progress* deadline (resets whenever the loop deletes ≥1 more doc), NOT a
# wall-clock cap — a large term-set whose docs keep disappearing takes as long
# as it needs, but a genuinely wedged index bails out. Mirrors ingest.py's
# embedding-readiness wait semantics.
_DELETE_VERIFY_POLL_S = float(os.getenv("OSS_DELETE_VERIFY_POLL_S", "2"))
_DELETE_VERIFY_STALL_TIMEOUT_S = float(os.getenv("OSS_DELETE_VERIFY_STALL_TIMEOUT_S", "60"))


def _is_index_not_found(exc: Exception) -> bool:
    """True if ``exc`` indicates the index doesn't exist.

    For a *deletion* verify loop a missing index means the matching docs are
    definitively gone. Matches on class name / message so it works regardless of
    which opensearch-py exception subclass surfaces the 404.
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    return (
        name in ("NotFoundError", "IndexNotFoundError")
        or "index_not_found" in msg
        or "no such index" in msg
        or "resource_not_found" in msg
    )


# Fields the shared ontology/metric vector index stores. keyword → exact
# match/exists filtering server-side; text → not filterable (prompt/display only).
_KEYWORD_FIELDS = (
    "entity_uri",
    "ontology_id",
    "entity_type",
    "embedding_type",
    "model_id",
    "namespace",
    # R2RML data-source provenance — written only for structured (Tier-2-capable)
    # classes. Explicit keyword so serve NL→SQL can gate on it server-side
    # (exists/term inside the k-NN filter) reliably on new indexes. Older indexes
    # that predate this only have it dynamically mapped, where ``exists`` still works.
    "data_source_id",
)
_TEXT_FIELDS = (
    # Raw text fed to the embedder — kept verbatim for re-examination / re-embed.
    "text",
    # Richer schema context for the serve NL→SQL prompt (e.g. column allowed
    # values). NOT embedded — stored for prompt use only.
    "context_text",
)


# Engines that support filtered k-NN. NMSLIB does NOT — a method-less index
# that resolves to NMSLIB fails every filtered query with
# "Engine [NMSLIB] does not support filters" (see #174).
_FILTER_CAPABLE_ENGINES = frozenset({"faiss", "lucene"})

# Explicit Faiss/HNSW method block. Requested by default so index creation does
# not depend on the server's (observed-unstable) method-less default engine.
_FAISS_HNSW_METHOD = {"name": "hnsw", "engine": "faiss", "space_type": "l2"}


class IncompatibleEngineError(RuntimeError):
    """A vector index resolved to an ANN engine that cannot serve filtered k-NN.

    Raised by :meth:`AossVectorClient.ensure_index` after reading back the
    created index's mapping, when the ``embedding`` field's resolved engine is
    not in :data:`_FILTER_CAPABLE_ENGINES`. This turns the #174 failure —
    silently getting an NMSLIB index and only discovering it when a filtered
    query 400s — into a loud, deploy-time failure.
    """


def build_index_mapping(dims: int, *, method: bool = True) -> dict:
    """Canonical vector-index mapping — the single source of truth.

    ``embedding`` is a ``knn_vector``. By default (``method=True``) it carries
    an explicit Faiss/HNSW ``method`` block, because the service's method-less
    default engine is NOT stable and has been observed to resolve to NMSLIB
    (which cannot serve filtered k-NN — see #174). ``method=False`` emits the
    legacy method-less mapping, used as a fallback by :meth:`ensure_index` when
    the service rejects an explicit ``method.engine`` at creation.

    All filterable fields are ``keyword`` so server-side ``term``/``exists``
    filters match exactly.
    """
    props: dict[str, Any] = {f: {"type": "keyword"} for f in _KEYWORD_FIELDS}
    props.update({f: {"type": "text"} for f in _TEXT_FIELDS})
    embedding: dict[str, Any] = {"type": "knn_vector", "dimension": dims}
    if method:
        embedding["method"] = dict(_FAISS_HNSW_METHOD)
    props["embedding"] = embedding
    return {"settings": {"index": {"knn": True}}, "mappings": {"properties": props}}


def _rejects_explicit_method(e: Exception) -> bool:
    """True if a create RequestError means "explicit knn_vector method not allowed".

    Some NEXTGEN AOSS generations reject an explicit ``method.engine`` block at
    index creation (observed live as HTTP 400 ``illegal_argument_exception`` /
    "Field parameter 'engine' is not supported"; see #174). We detect it by the
    error code and message rather than status alone, so a genuine 400 for some
    OTHER reason still propagates instead of silently falling back.
    """
    code = (getattr(e, "error", "") or "").lower()
    msg = str(getattr(e, "info", "") or "").lower() + " " + str(e).lower()
    if "illegal_argument_exception" in code or "mapper_parsing_exception" in code:
        return True
    return any(
        s in msg
        for s in (
            "engine' is not supported",
            "engine] is not supported",
            "does not support parameter",
            "unknown parameter [method]",
            "unsupported parameter",
            "parameter 'engine'",
        )
    )


def _resolved_engine(mapping: dict, index: str) -> str | None:
    """Extract the resolved ANN engine for ``embedding`` from a get_mapping response.

    ``indices.get_mapping`` returns ``{index: {"mappings": {"properties":
    {"embedding": {"method": {"engine": ...}}}}}``. Faiss/Lucene report the
    engine under ``method.engine``; a method-less NMSLIB index may report no
    ``method`` block at all. Returns the lowercased engine string, or None when
    no engine is present in the mapping.
    """
    try:
        props = mapping[index]["mappings"]["properties"]
        emb = props["embedding"]
        engine = emb.get("method", {}).get("engine")
        return engine.lower() if isinstance(engine, str) else None
    except (KeyError, AttributeError, TypeError):
        return None


# Transport for the proxy path: given (action, index, body) returns the raw
# opensearch response dict. Only "search" is exercised through this today.
Transport = Callable[[str, str, dict], dict]


class _RetryingIndices:
    """Proxy over ``client.indices`` that retries transient AOSS faults."""

    def __init__(self, indices: Any):
        self._indices = indices

    def __getattr__(self, name: str) -> Callable[..., Any]:
        target = getattr(self._indices, name)

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return oss_retry(f"indices.{name}", lambda: target(*args, **kwargs))

        return _wrapped


class _RetryingClient:
    """Retry-wrapping proxy over an ``OpenSearch`` client for transient AOSS faults.

    Retries transient AOSS faults on
    every operation (index / search / delete / indices.*), so a single 429/5xx
    blip doesn't fail the whole read or write. Non-transient errors (4xx,
    RequestError) propagate immediately. The underlying client is exposed as
    ``.raw`` for helpers (e.g. ``opensearchpy.helpers.bulk``) that need it.
    """

    def __init__(self, client: OpenSearch):
        self.raw = client
        self.indices = _RetryingIndices(client.indices)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        target = getattr(self.raw, name)
        if not callable(target):
            return target

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return oss_retry(name, lambda: target(*args, **kwargs))

        return _wrapped


def _build_signed_client(endpoint: str, region: str, timeout: int) -> OpenSearch:
    host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials not available")
    # LIVE credentials object (not a frozen snapshot): AWSV4SignerAuth re-signs
    # each request with current creds, so a cached client survives Fargate
    # task-role token rotation. Service must be "aoss" (defaults to "es").
    auth = AWSV4SignerAuth(credentials, region, _AOSS_SERVICE)
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=timeout,
        # Size to the WIDEST inducer ThreadPoolExecutor that shares this client so
        # it doesn't exhaust the default maxsize=10 pool ("Connection pool is full,
        # discarding connection"). Both induction passes hit the same client: the
        # first-pass grounding recall fans out INDUCER_GROUNDING_WORKERS-wide
        # (default 24) and the second-pass column match INDUCER_COLUMN_MATCH_WORKERS
        # (default 16). Sizing only to the column-match width starved the wider
        # grounding pass.
        pool_maxsize=max(
            int(os.getenv("INDUCER_COLUMN_MATCH_WORKERS", "16")),
            int(os.getenv("INDUCER_GROUNDING_WORKERS", "24")),
            10,
        ),
    )


def _filter_clause(filters: list[dict] | None) -> dict | None:
    """Wrap a list of leaf filter clauses in a ``bool``/``filter``, or None."""
    return {"bool": {"filter": filters}} if filters else None


class AossVectorClient:
    """Shared AOSS vector client. See the module docstring.

    Args:
        endpoint: AOSS collection endpoint (``https://…aoss.<region>.on.aws``).
            Required for the direct transport; ignored for the proxy transport.
        region: AWS region for SigV4 signing (direct transport).
        dimensions: knn_vector dimension for ``build_index_mapping``.
        timeout: opensearch-py request timeout (seconds), direct transport.
        transport: optional search-only proxy callable. When set, the client
            never builds a signed opensearch-py client and write/admin ops raise.
    """

    def __init__(
        self,
        *,
        endpoint: str = "",
        region: str = "us-east-1",
        dimensions: int = 1024,
        timeout: int = 60,
        transport: Transport | None = None,
    ):
        """Configure the AOSS client for either direct or proxy transport (see class Args)."""
        self.endpoint = endpoint
        self.region = region
        self.dimensions = dimensions
        self.timeout = timeout
        self._transport = transport
        self._client: _RetryingClient | None = None
        self._ensured_indices: set[str] = set()
        # Engine strategy, classified ONCE per collection then cached (#174 long-term):
        #   None  = not yet probed
        #   True  = this collection accepts an explicit knn_vector method.engine
        #           (the normal NEXTGEN case) → always create explicit Faiss/HNSW,
        #           and get_mapping MUST echo a filter-capable engine.
        #   False = this collection REJECTS an explicit method at create time →
        #           create method-less and let the service resolve the engine;
        #           an unreported engine in get_mapping is then tolerated (the
        #           service does not always echo a server-resolved default) while
        #           a reported non-filtering engine (NMSLIB) is still fatal.
        # Caching the classification removes the per-index guessing that caused
        # the F1 false-reject risk: after the first ensure_index, every later
        # index takes the known-good path deterministically.
        self._explicit_method_supported: bool | None = None

    # ── transport ──────────────────────────────────────────────────────

    @property
    def is_proxy(self) -> bool:
        """True when configured for search-only proxy transport (no direct client)."""
        return self._transport is not None

    def _c(self) -> _RetryingClient:
        """Retry-wrapped direct client. Raises if this client is proxy-only."""
        if self._transport is not None:
            raise RuntimeError("AossVectorClient is proxy-only; direct client operations are unavailable")
        if self._client is None:
            if not self.endpoint:
                raise RuntimeError("AOSS endpoint not set — configure the collection endpoint")
            self._client = _RetryingClient(_build_signed_client(self.endpoint, self.region, self.timeout))
        return self._client

    def raw_client(self):
        """Return the retry-wrapped opensearch-py client for low-level/debug access.

        Intended for ad-hoc ``.get`` / ``.count`` in scripts. Raises for proxy-only
        clients. Prefer the typed methods (knn_search / count / …) in app code.
        """
        return self._c()

    def _search(self, index: str, body: dict) -> dict:
        """Execute a search body via the proxy transport or the direct client.

        A missing index reads as "no data" rather than an error: every read
        helper funnels through here, so this one guard lets callers query an
        index that was never created (or was torn down with its namespace)
        without first calling :meth:`ensure_index`. That matters because an
        ``ensure_index`` on the READ path resurrects an empty index for a
        deleted namespace, and nothing ever reclaims it — AOSS caps a
        collection at 1000 indexes, so leaked shells eventually break every
        write with ``index_limit_breached``.
        """
        try:
            if self._transport is not None:
                return self._transport("search", index, body)
            return self._c().search(index=index, body=body)
        except Exception as e:
            if _is_index_not_found(e):
                log.debug("search on missing index %s — returning empty result", index)
                return {"hits": {"hits": [], "total": {"value": 0}}}
            raise

    # ── index admin (direct only) ──────────────────────────────────────

    def ensure_index(self, index: str) -> str:
        """Create ``index`` with the canonical mapping if it doesn't exist, engine-verified.

        Creation strategy (see #174 — the resolved ANN engine is a server-side
        default that has been observed unstable across accounts/dates):

        1. Try create with an EXPLICIT Faiss/HNSW ``method`` block.
        2. If the service rejects the explicit ``method`` at creation (some
           NEXTGEN generations return ``illegal_argument_exception`` /
           "Field parameter 'engine' is not supported"), retry create
           METHOD-LESS and let the service resolve the engine.
        3. Either way, read the mapping back and VERIFY the resolved engine is
           filter-capable (Faiss/Lucene). If it resolved to a non-filtering
           engine (NMSLIB), raise :class:`IncompatibleEngineError` NOW — so an
           index that cannot serve a filtered query is caught at creation, not
           at first query. The resolved engine is always logged at INFO.
        """
        if index in self._ensured_indices:
            return index
        c = self._c()
        # Strict verification (an unreported engine is fatal) applies ONLY to an
        # index THIS call created with an explicit method — the one case where we
        # know what was asked for. A pre-existing index was created by another
        # process or by code that predates the explicit method, so how it was
        # created is unknown: method-less creation is valid and on some
        # collections never echoes an engine in get_mapping. Treating it as
        # explicit (the previous default, and the cached flag on a collection
        # that accepts explicit methods) rejected every pre-existing index on a
        # fresh process — failing the readiness probe and proposal-accept ingest.
        created_with_explicit_method = False
        if not c.indices.exists(index=index):
            created_with_explicit_method = self._create_with_engine_fallback(c, index)
        # Verify the resolved engine regardless of who created it — a
        # pre-existing index created before this check could be NMSLIB, and a
        # REPORTED non-filtering engine is fatal on every path.
        self._verify_filter_capable_engine(c, index, explicit_method=created_with_explicit_method)
        self._ensured_indices.add(index)
        return index

    def _create_with_engine_fallback(self, c: Any, index: str) -> bool:
        """Create ``index`` explicit-Faiss-first, method-less on rejection.

        Returns True if the index was created (or is being verified) via the
        EXPLICIT-method path, False if it went via the METHOD-LESS fallback —
        the caller uses this to choose engine-verification strictness.

        The explicit/method-less classification is cached on the instance
        (``_explicit_method_supported``) after the first create, so every later
        index on the same collection takes the known-good path with no re-probe
        and no per-index guessing. A concurrent-create race
        (``resource_already_exists_exception``) does not change the cache; it
        reports the strategy this collection is known/assumed to use.
        """
        # If we already classified this collection, go straight to the known path.
        if self._explicit_method_supported is False:
            try:
                c.indices.create(index=index, body=build_index_mapping(self.dimensions, method=False))
                log.info("created OpenSearch index %s (dims=%d, method=less/server-resolved)", index, self.dimensions)
            except RequestError as e:
                if e.error == "resource_already_exists_exception":
                    return False
                raise
            return False

        try:
            c.indices.create(index=index, body=build_index_mapping(self.dimensions, method=True))
            log.info("created OpenSearch index %s (dims=%d, method=explicit-faiss/hnsw)", index, self.dimensions)
            self._explicit_method_supported = True
            return True
        except RequestError as e:
            if e.error == "resource_already_exists_exception":
                # Race: keep whatever classification we have (default: explicit).
                return self._explicit_method_supported is not False
            # Some NEXTGEN generations reject an explicit method.engine at
            # creation. Classify the collection as method-less-only, fall back,
            # and let the service resolve the engine (which
            # _verify_filter_capable_engine then checks, tolerating an
            # unreported engine on this path).
            if _rejects_explicit_method(e):
                log.warning(
                    "OpenSearch rejected explicit knn_vector method on %s (%s); "
                    "classifying collection as method-less and verifying the resolved engine",
                    index,
                    getattr(e, "error", e),
                )
                self._explicit_method_supported = False
                try:
                    c.indices.create(index=index, body=build_index_mapping(self.dimensions, method=False))
                    log.info(
                        "created OpenSearch index %s (dims=%d, method=less/server-resolved)",
                        index,
                        self.dimensions,
                    )
                    return False
                except RequestError as e2:
                    if e2.error == "resource_already_exists_exception":
                        return False
                    raise
            raise

    def _verify_filter_capable_engine(self, c: Any, index: str, *, explicit_method: bool = True) -> None:
        """Read the index mapping back and raise if the ANN engine can't filter.

        Filter-capable engines are Faiss/Lucene; NMSLIB cannot serve filtered
        k-NN. Logs the resolved engine at INFO so a deployment can finally read
        which engine its indices got (#174 — "nothing reported the engine").

        Path-aware strictness (resolves F1):

        - ``explicit_method=True`` (default/normal path): we asked for Faiss, so
          the mapping MUST confirm a filter-capable engine. A reported NMSLIB
          OR an UNREPORTED engine is fatal — if the service accepted our explicit
          method it must echo it back; silence means something is wrong.
        - ``explicit_method=False`` (method-less fallback path): the service
          resolves the engine and does NOT always echo a server-resolved default
          in get_mapping. A reported NMSLIB is still fatal, but an UNREPORTED
          (None) engine is TOLERATED with a WARNING — raising here would
          false-reject a valid Faiss index on the exact generation the fallback
          exists to serve. The residual "unreported-but-secretly-NMSLIB" risk is
          caught by the live filtered-kNN integration test, not by inference.
        """
        try:
            mapping = c.indices.get_mapping(index=index)
        except Exception as e:  # noqa: BLE001 — verification failure must not be silently ignored
            # A verification read failure is itself a signal — do not proceed as
            # if the engine were fine. Re-raise so the caller sees it.
            raise IncompatibleEngineError(
                f"could not read back mapping for index {index!r} to verify its ANN engine: {e}"
            ) from e
        engine = _resolved_engine(mapping, index)
        log.info("OpenSearch index %s resolved ANN engine: %s", index, engine or "<none reported>")

        if engine is not None and engine not in _FILTER_CAPABLE_ENGINES:
            # A reported non-filtering engine (NMSLIB) is fatal on BOTH paths.
            raise IncompatibleEngineError(
                f"index {index!r} resolved to ANN engine {engine!r}, which cannot serve filtered k-NN "
                f"(need one of {sorted(_FILTER_CAPABLE_ENGINES)}). Filtered vector search would fail with "
                f"'Engine [{engine.upper()}] does not support filters'. See #174."
            )
        if engine is None:
            if explicit_method:
                # We sent an explicit filter-capable method; the service must echo
                # it. Silence means the create did not apply what we asked for.
                raise IncompatibleEngineError(
                    f"index {index!r} was created with an explicit filter-capable method but its mapping "
                    f"reports no ANN engine; refusing to trust an unverifiable index. See #174."
                )
            # Method-less fallback: tolerate an unreported engine (see docstring).
            log.warning(
                "OpenSearch index %s reports no ANN engine after method-less creation; "
                "trusting the service default. Filter-capability is confirmed by the live "
                "filtered-kNN integration test, not by this mapping read. See #174.",
                index,
            )

    def delete_index(self, index: str) -> bool:
        """Delete ``index``. Returns True if it existed."""
        c = self._c()
        if not c.indices.exists(index=index):
            return False
        c.indices.delete(index=index)
        self._ensured_indices.discard(index)
        log.info("deleted OpenSearch index %s", index)
        return True

    # ── writes (direct only) ────────────────────────────────────────────

    def index_document(self, index: str, doc: dict) -> None:
        """Append a single document (AOSS auto-assigns ``_id``)."""
        self._c().index(index=index, body=doc)

    def bulk_index(self, index: str, docs: list[dict]) -> None:
        """Bulk-append documents with per-item transient-fault retry."""
        if not docs:
            return
        actions = [{"_op_type": "index", "_index": index, **d} for d in docs]
        bulk_with_retry(self._c().raw, actions)

    def delete_by_term(
        self,
        index: str,
        field: str,
        value: str | Sequence[str],
        page: int = 500,
        and_filters: list[dict] | None = None,
    ) -> int:
        """Delete every doc where ``field == value`` (or ``field IN values``), verifying the match reaches zero.

        ``value`` may be a single string or a sequence of strings; the latter
        runs ONE ``terms`` query and one verify loop for the whole set, instead
        of paying the refresh-lag wait once per value (append-mode ingest retires
        the stale embeddings of every subject in the incoming proposal at once).

        ``and_filters`` optionally narrows the delete with additional AND clauses
        (``{"term": {...}}``, ``{"terms": {...}}``, etc.) — used by the embedding
        retirement path to scope by ``ontology_id`` so that shared IRIs in a
        different ontology are not touched. When set, the term/terms query is
        wrapped in a ``bool.must`` alongside the extra filters; when omitted the
        query is the bare term/terms as before.

        AOSS has no ``_delete_by_query`` on VECTORSEARCH, so we search for
        matching docs and delete each by its auto-assigned ``_id``. AOSS
        ``search`` is also NOT read-your-writes consistent (near-real-time
        refresh lag), so a naive "delete visible hits until a search returns
        empty" loop can stop early: an intermediate search returns 0 hits from a
        stale index view and breaks the loop, leaving a tail of docs that were
        never issued a delete. (Observed live: property embeddings orphaned after
        an ontology delete.)

        To be correct against that lag, we loop delete-then-recount until the
        term-query ``count`` is a *stable* zero, tracking a no-progress stall
        deadline (resets whenever we delete ≥1 more doc, mirrors ingest.py's
        readiness wait). On a genuine stall we log and stop rather than spin
        forever — the caller verifies teardown and retries. Returns the number of
        docs deleted.
        """
        c = self._c()
        if isinstance(value, str):
            primary: dict = {"term": {field: value}}
        else:
            # Dedup and drop empties/None: a `terms` clause of [""] is a valid
            # query that matches nothing, but an all-empty input is a caller bug
            # and should cost no round-trip.
            values = [v for v in dict.fromkeys(value) if v]
            if not values:
                return 0
            primary = {"terms": {field: values}}

        if and_filters:
            query = {"bool": {"must": [primary, *and_filters]}}
            # For `count()`, wrap in the same shape so the terminal check
            # matches exactly what we deleted.
            count_filters = [query]
        else:
            query = primary
            count_filters = [primary]
        deleted = 0
        stall_deadline = time.perf_counter() + _DELETE_VERIFY_STALL_TIMEOUT_S

        while True:
            # 1. Fetch a page of matching doc ids and delete them. Goes through
            #    _search so a missing index reads as zero hits (nothing to
            #    delete) instead of raising — callers no longer ensure_index on
            #    the delete path, since doing so resurrected empty shells.
            resp = self._search(
                index,
                {"size": page, "_source": False, "query": query},
            )
            hits = _hits(resp)
            deleted_this_pass = 0
            for h in hits:
                try:
                    c.delete(index=index, id=h["_id"])
                    deleted += 1
                    deleted_this_pass += 1
                except Exception:  # noqa: BLE001 — best-effort per-doc; recount is the gate
                    pass

            # 2. Terminal check via count (does NOT rely on this page being
            #    empty — that empty could be a stale view). Only a real zero
            #    count means the term matches nothing left. A missing index →
            #    done (0); any other (transient) count error → -1 so the stall
            #    timer governs whether we keep retrying.
            try:
                remaining = self.count(index, filters=count_filters)
            except Exception as e:  # noqa: BLE001
                remaining = 0 if _is_index_not_found(e) else -1

            if remaining == 0:
                return deleted

            # 3. Progress / stall bookkeeping. "Progress" = we actually deleted
            #    at least one doc THIS pass (count alone can lag). Real progress
            #    resets the no-progress deadline; a pass that deletes nothing and
            #    still sees count > 0 counts against the deadline, so a persistent
            #    search-empty-but-count-nonzero disagreement can't spin forever.
            if deleted_this_pass > 0:
                stall_deadline = time.perf_counter() + _DELETE_VERIFY_STALL_TIMEOUT_S
            elif time.perf_counter() >= stall_deadline:
                log.warning(
                    "delete_by_term(%s=%s) stalled — deleted %d, ~%s still matching after %.0fs "
                    "no-progress; giving up this pass (caller will retry / verify)",
                    field,
                    value if isinstance(value, str) else f"<{len(primary['terms'][field])} values>",
                    deleted,
                    remaining if remaining >= 0 else "unknown",
                    _DELETE_VERIFY_STALL_TIMEOUT_S,
                )
                return deleted

            time.sleep(_DELETE_VERIFY_POLL_S)

    # ── reads (direct or proxy) ─────────────────────────────────────────

    def knn_search(
        self,
        index: str,
        vector: list[float],
        *,
        top_k: int = 10,
        filters: list[dict] | None = None,
        source: list[str] | bool | None = None,
    ) -> list[dict]:
        """Server-side filtered k-NN. Returns raw hit dicts (``_source`` + ``_score``).

        ``filters`` is a list of leaf clauses (``{"term": {...}}`` /
        ``{"exists": {...}}``) ANDed inside the k-NN ``filter`` — the engine
        returns the true top-``top_k`` within the filtered subset, no over-fetch.
        """
        if len(vector) < self.dimensions:
            vector = list(vector) + [0.0] * (self.dimensions - len(vector))
        knn: dict[str, Any] = {"vector": vector, "k": top_k}
        clause = _filter_clause(filters)
        if clause is not None:
            knn["filter"] = clause
        body: dict[str, Any] = {"size": top_k, "query": {"knn": {"embedding": knn}}}
        if source is not None:
            body["_source"] = source
        resp = self._search(index, body)
        return [{**h.get("_source", {}), "_score": h.get("_score"), "_id": h.get("_id")} for h in _hits(resp)]

    def filter_search(
        self,
        index: str,
        filters: list[dict],
        *,
        size: int = 1000,
        source: list[str] | bool | None = None,
    ) -> list[dict]:
        """Non-vector bool/filter search. Returns raw ``_source`` dicts."""
        body: dict[str, Any] = {"size": size, "query": _filter_clause(filters) or {"match_all": {}}}
        if source is not None:
            body["_source"] = source
        resp = self._search(index, body)
        return [h.get("_source", {}) for h in _hits(resp)]

    def search_raw(self, index: str, body: dict) -> dict:
        """Execute a raw search ``body``; return the raw opensearch response.

        Escape hatch for query shapes the typed helpers don't cover (e.g. a
        hybrid BM25 + k-NN ``bool.should``). Goes through the same transport
        (direct or proxy) as every other search.
        """
        return self._search(index, body)

    def count(self, index: str, *, filters: list[dict] | None = None) -> int:
        """Count docs matching optional filters (a ``size:0`` search)."""
        query = _filter_clause(filters) or {"match_all": {}}
        body = {"size": 0, "query": query, "track_total_hits": True}
        resp = self._search(index, body)
        return resp.get("hits", {}).get("total", {}).get("value", 0)

    def iter_source_field(self, index: str, filters: list[dict], field: str, page: int = 10_000) -> list[Any]:
        """Project one ``field`` across all matching docs.

        Pages with ``search_after`` (AOSS has no scroll API). Sorts on ``field`` (keyword).
        Goes through :meth:`_search`, so a missing index yields ``[]``.
        """
        query = _filter_clause(filters) or {"match_all": {}}
        values: list[Any] = []
        search_after: list[Any] | None = None
        while True:
            body: dict[str, Any] = {
                "size": page,
                "_source": [field],
                "query": query,
                "sort": [{field: "asc"}],
            }
            if search_after is not None:
                body["search_after"] = search_after
            resp = self._search(index, body)
            hits = _hits(resp)
            if not hits:
                break
            for h in hits:
                v = h["_source"].get(field)
                if v is not None:
                    values.append(v)
            if len(hits) < page:
                break
            search_after = hits[-1].get("sort")
            if not search_after:
                break
        return values

    # ── health ──────────────────────────────────────────────────────────

    def health_check(self, index: str) -> dict:
        """Probe liveness by ensuring the index exists.

        AOSS 404s on ``/`` so ``client.info()`` is unusable;
        ensuring the index exists is the cheapest round-trip that works.
        """
        try:
            if self._transport is not None:
                self._transport("health_check", index, {})
            else:
                self.ensure_index(index)
            return {"status": "ok", "index": index}
        except Exception as e:  # noqa: BLE001 — health probe must not raise
            return {"status": "error", "error": str(e)}


def _hits(resp: dict) -> list[dict]:
    return resp.get("hits", {}).get("hits", [])
