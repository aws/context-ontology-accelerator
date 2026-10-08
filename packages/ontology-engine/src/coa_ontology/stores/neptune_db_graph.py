# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune DB graph store — SPARQL 1.1 over the Neptune DB cluster.

Uses the ``/sparql`` REST endpoint on the cluster writer. Authentication
is SigV4 via ``botocore.auth.SigV4Auth`` when IAM auth is enabled on the
cluster (which is required for most Amazon-internal Neptune DB clusters).

Schema mirrors the Neptune Analytics backend (``na_store.py``): every
ontology, class, and property is a named graph resource. We store them
in a single named graph per workbench so they are easy to list and
wipe.

Design notes:
  - We use RDF (not property graph) because the cluster engine supports
    both, and the induced artifacts are Turtle anyway.
  - We keep metadata (title, description, counts, etc.) as RDF literals
    in a ``workbench:`` namespace so there is no separate metadata store.
  - This implementation is deliberately minimal: it covers the methods
    on :class:`GraphStore` the induction pipeline actually hits.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import boto3
import botocore.auth
import botocore.awsrequest
import botocore.exceptions
import httpx
from coa_common import validate_namespace_name
from coa_common.constants import VOCAB_URI
from coa_common.opensearch import retry_call

from coa_ontology.stores.base import GraphStore

log = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────

NDB_ENDPOINT = os.getenv("NDB_ENDPOINT", "")  # https://<cluster>.neptune.amazonaws.com:8182
NDB_REGION = os.getenv("NDB_REGION", os.getenv("AWS_REGION", "us-east-1"))
NDB_IAM_AUTH = os.getenv("NDB_IAM_AUTH", "true").lower() == "true"
NDB_TIMEOUT = float(os.getenv("NDB_TIMEOUT", "30"))
DEFAULT_NAMESPACE = os.getenv("DYNAMODB_DEFAULT_NAMESPACE", "default")
# Subjects per DELETE when superseding annotations on append; a re-induced
# proposal can carry hundreds of classes + properties.
_SUPERSEDE_CHUNK = 200

# Neptune answers HTTP 500 ``MemoryLimitExceededException`` when query-execution
# memory is exhausted — often by OTHER concurrent requests, so a cheap query can
# fail alongside the heavy one. Neptune documents the error as retriable, and the
# failed request does not commit, so a retry is safe for queries, updates and GSP
# loads alike. Retries are short (a few seconds of backoff in total) because reads
# sit behind API Gateway's 29 s integration limit.
_NDB_OOM_CODE = "MemoryLimitExceededException"
_NDB_MAX_RETRIES = int(os.getenv("NDB_MAX_RETRIES", "2"))
_NDB_MAX_BACKOFF_S = float(os.getenv("NDB_MAX_BACKOFF_S", "4"))


class NeptuneMemoryLimitError(httpx.HTTPStatusError):
    """Neptune rejected a request with ``MemoryLimitExceededException``.

    Subclasses :class:`httpx.HTTPStatusError` so existing ``except`` sites keep
    working. ``main.py`` maps it to a 503 on routes that let it propagate (e.g.
    ``/graph/search``); routes with a broad ``except Exception`` still return 500.
    """


# ── Named graph URI scheme ──────────────────────────────────────────────
#
# There is no cross-ontology catalog graph. Every ontology in a namespace
# lives in its own named graph at::
#
#     {base}/{namespace}/{quote(ontology_id)}
#
# The ontology path segment is percent-encoded so that an IRI
# (``https://spec.edmcouncil.org/fibo/ontology/FND``) can be dropped in
# directly without breaking URI structure.
#
# ``NDB_GRAPH_URI_BASE`` is the preferred env var. For backward compat with
# deployments that set ``NDB_GRAPH_URI=<base>/graph/catalog`` (the legacy
# form), if ``NDB_GRAPH_URI_BASE`` is unset we derive the base by stripping
# the trailing ``/graph/<anything>`` segment from ``NDB_GRAPH_URI``.


def _resolve_graph_base() -> str:
    """Return the base URL under which per-namespace graph URIs live."""
    base = os.getenv("NDB_GRAPH_URI_BASE", "").strip().rstrip("/")
    if base:
        return base
    legacy = os.getenv("NDB_GRAPH_URI", "").strip()
    if legacy:
        # Strip trailing "/graph/<name>" if present, else drop the last path segment.
        if "/graph/" in legacy:
            legacy = legacy.rsplit("/graph/", 1)[0]
        return legacy.rstrip("/")
    return "https://ontology-workbench.local"


def _namespace_graph_prefix(namespace: str | None = None) -> str:
    """Return the ``{base}/{namespace}`` prefix the namespace's graphs live under."""
    ns = namespace or DEFAULT_NAMESPACE
    return f"{_resolve_graph_base()}/{ns}"


def _ontology_graph_uri(ontology_id: str, namespace: str | None = None) -> str:
    """Return the named graph URI for one ontology.

    ``ontology_id`` is the ontology's identifier within the namespace — in
    practice this is the ontology IRI (e.g. ``owl:Ontology`` subject). The
    value is percent-encoded so IRIs containing ``/`` and ``#`` produce a
    valid single path segment.
    """
    from urllib.parse import quote

    return f"{_namespace_graph_prefix(namespace)}/{quote(ontology_id, safe='')}"


WB = "https://ontology-workbench.local/vocab#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
OWL = "http://www.w3.org/2002/07/owl#"
DCT = "http://purl.org/dc/terms/"
_XSD = "http://www.w3.org/2001/XMLSchema#"

# SKOS mapping predicates + the SCL grounding marker the inducer emits on a
# grounded class (see inducer/strategies/table_to_ontology.py).
_SKOS = "http://www.w3.org/2004/02/skos/core#"
_SCL = VOCAB_URI

# Graph view's initial ("sample") overview: seed the canvas with the N
# largest-subtree root classes and cap how many of their descendants are
# materialized up front. The rest of the taxonomy is loaded on demand as the
# user expands nodes — so opening the Graph view is O(bounded), not O(classes).
_SAMPLE_ROOTS = 3
_SAMPLE_MAX_DESCENDANTS = 50

# Hard ceiling on a single search page. The API layer validates ``limit`` and
# rejects out-of-range values with a 400; this is the store's own backstop for
# callers that bypass the router, since the value reaches a SPARQL LIMIT clause.
# Keep in sync with ``_MAX_SEARCH_LIMIT`` in catalog/routers/graph.py.
_MAX_SEARCH_PAGE_SIZE = 500


# ── SigV4 helper ────────────────────────────────────────────────────────


def _sign(method: str, url: str, body: str | bytes, service: str = "neptune-db") -> dict:
    """Return SigV4 headers for a Neptune HTTP request."""
    if not NDB_IAM_AUTH:
        return {}
    creds = boto3.Session().get_credentials()
    if creds is None:
        raise RuntimeError("AWS credentials not available")
    frozen = creds.get_frozen_credentials()
    req = botocore.awsrequest.AWSRequest(method=method, url=url, data=body)
    botocore.auth.SigV4Auth(frozen, service, NDB_REGION).add_auth(req)
    return dict(req.headers.items())


# ── SPARQL transport ────────────────────────────────────────────────────


def _raise_for_neptune_status(r: httpx.Response) -> None:
    """``raise_for_status``, but raise :class:`NeptuneMemoryLimitError` for an OOM."""
    try:
        body = r.json()
    except ValueError:
        body = None
    # A JSON body that is not an object (list/str/null) must not raise
    # AttributeError here: /graph/search maps AttributeError to 501, which the
    # integ test treats as "unsupported" and skips — hiding the real error.
    code = body.get("code") if isinstance(body, dict) else None
    if code == _NDB_OOM_CODE:
        raise NeptuneMemoryLimitError(f"Neptune {_NDB_OOM_CODE} ({r.status_code})", request=r.request, response=r)
    r.raise_for_status()


def _with_oom_retry[T](op: str, fn: Callable[[], T]) -> T:
    """Run one Neptune HTTP call, retrying only ``MemoryLimitExceededException``."""
    return retry_call(
        op,
        fn,
        is_transient=lambda exc: isinstance(exc, NeptuneMemoryLimitError),
        max_retries=_NDB_MAX_RETRIES,
        max_backoff_s=_NDB_MAX_BACKOFF_S,
    )


def _sparql_query(query: str) -> dict:
    if not NDB_ENDPOINT:
        raise RuntimeError("NDB_ENDPOINT not set — configure your Neptune DB endpoint")
    import time as _t
    from urllib.parse import urlencode

    url = f"{NDB_ENDPOINT.rstrip('/')}/sparql"
    body = urlencode({"query": query})

    def _once() -> dict:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/sparql-results+json",
        }
        headers.update(_sign("POST", url, body))
        _t0 = _t.perf_counter()
        with httpx.Client(timeout=NDB_TIMEOUT) as c:
            r = c.post(url, content=body, headers=headers)
            elapsed_ms = (_t.perf_counter() - _t0) * 1000
            if r.status_code >= 400:
                log.error(
                    "SPARQL query failed (%d) %.0fms: %s\nquery=%s", r.status_code, elapsed_ms, r.text[:1500], query
                )
                _raise_for_neptune_status(r)
            log.info("SPARQL query %.0fms  (body=%d bytes)", elapsed_ms, len(body))
            return r.json()

    return _with_oom_retry("neptune sparql query", _once)


def _sparql_update(update: str) -> None:
    if not NDB_ENDPOINT:
        raise RuntimeError("NDB_ENDPOINT not set — configure your Neptune DB endpoint")
    import time as _t
    from urllib.parse import urlencode

    url = f"{NDB_ENDPOINT.rstrip('/')}/sparql"
    body = urlencode({"update": update})

    def _once() -> None:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        headers.update(_sign("POST", url, body))
        _t0 = _t.perf_counter()
        # Batched INSERTs (up to the per-flush byte budget) are a bulk-transfer
        # workload, not the per-item call the 30 s default was sized for. Use the
        # same floor the GSP turtle load uses so a large slow-committing flush
        # doesn't spuriously time out (and trip the fresh-ingest rollback).
        with httpx.Client(timeout=max(NDB_TIMEOUT, 120)) as c:
            r = c.post(url, content=body, headers=headers)
            elapsed_ms = (_t.perf_counter() - _t0) * 1000
            if r.status_code >= 400:
                log.error(
                    "SPARQL update failed (%d) %.0fms: %s\nupdate=%s", r.status_code, elapsed_ms, r.text[:1500], update
                )
                _raise_for_neptune_status(r)
            log.info("SPARQL update %.0fms  (body=%d bytes)", elapsed_ms, len(body))

    _with_oom_retry("neptune sparql update", _once)


def _gsp_post_turtle(graph_uri: str, turtle: str) -> dict:
    """POST a Turtle document into a named graph via Neptune's GSP endpoint.

    Uses the SPARQL Graph Store Protocol (``/sparql/gsp``). Neptune DB
    accepts ``text/turtle`` directly in the request body, which is the
    most efficient way to load a whole Turtle file without splitting it
    into SPARQL INSERT DATA batches (which hit an 8 KB query size limit).

    ``graph_uri`` goes into the ``?graph=`` query parameter. The special
    value ``"default"`` writes to the default graph.
    """
    if not NDB_ENDPOINT:
        raise RuntimeError("NDB_ENDPOINT not set — configure your Neptune DB endpoint")
    import time as _t

    if graph_uri == "default":
        url = f"{NDB_ENDPOINT.rstrip('/')}/sparql/gsp/?default"
    else:
        from urllib.parse import quote

        url = f"{NDB_ENDPOINT.rstrip('/')}/sparql/gsp/?graph={quote(graph_uri, safe='')}"
    body_bytes = turtle.encode("utf-8")

    def _once() -> float:
        headers = {"Content-Type": "text/turtle"}
        # SigV4 needs the body for signing.
        headers.update(_sign("POST", url, body_bytes))
        _t0 = _t.perf_counter()
        with httpx.Client(timeout=max(NDB_TIMEOUT, 120)) as c:
            r = c.post(url, content=body_bytes, headers=headers)
            elapsed_ms = (_t.perf_counter() - _t0) * 1000
            if r.status_code >= 400:
                log.error("GSP POST failed (%d) %.0fms: %s", r.status_code, elapsed_ms, r.text[:500])
                _raise_for_neptune_status(r)
        return elapsed_ms

    elapsed_ms = _with_oom_retry("neptune gsp post", _once)
    log.info("GSP POST %.0fms  (turtle=%d bytes, graph=%s)", elapsed_ms, len(body_bytes), graph_uri)
    return {
        "graph_uri": graph_uri,
        "bytes_loaded": len(body_bytes),
        "status": "ok",
    }


def _gsp_get_turtle(graph_uri: str) -> str | None:
    """GET a named graph as Turtle via Neptune's GSP endpoint.

    Counterpart to :func:`_gsp_post_turtle` — round-trips RDF in/out of
    the same named graph. Returns the Turtle body on 200 (which may be
    empty if the named graph contains no triples) or ``None`` when
    Neptune signals the graph does not exist (404). Other status codes
    raise.
    """
    if not NDB_ENDPOINT:
        raise RuntimeError("NDB_ENDPOINT not set — configure your Neptune DB endpoint")
    import time as _t

    if graph_uri == "default":
        url = f"{NDB_ENDPOINT.rstrip('/')}/sparql/gsp/?default"
    else:
        from urllib.parse import quote

        url = f"{NDB_ENDPOINT.rstrip('/')}/sparql/gsp/?graph={quote(graph_uri, safe='')}"

    def _once() -> str | None:
        headers = {"Accept": "text/turtle"}
        headers.update(_sign("GET", url, ""))
        _t0 = _t.perf_counter()
        with httpx.Client(timeout=max(NDB_TIMEOUT, 120)) as c:
            r = c.get(url, headers=headers)
            elapsed_ms = (_t.perf_counter() - _t0) * 1000
            if r.status_code == 404:
                log.info("GSP GET 404 %.0fms  (graph=%s)", elapsed_ms, graph_uri)
                return None
            if r.status_code >= 400:
                log.error("GSP GET failed (%d) %.0fms: %s", r.status_code, elapsed_ms, r.text[:500])
                _raise_for_neptune_status(r)
        log.info("GSP GET %.0fms  (turtle=%d bytes, graph=%s)", elapsed_ms, len(r.text), graph_uri)
        return r.text

    return _with_oom_retry("neptune gsp get", _once)


def _esc(s: str) -> str:
    """Escape a SPARQL string literal per SPARQL 1.1 §19.7."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


# Characters forbidden in an IRI reference per RFC 3987 §2.2 (IRI) and
# SPARQL 1.1 §19.6. If any of these appear in a URI we refuse to emit
# it as <...> because doing so would break out of the IRI position and
# inject arbitrary SPARQL.
_IRI_FORBIDDEN = re.compile(r'[\s<>"{}|\\^`]')


def _iri(s: str) -> str:
    """Render a string as a SPARQL IRI reference ``<uri>``.

    Rejects URIs containing characters that are not permitted in the
    SPARQL IRI_REF production (whitespace, angle brackets, braces, pipe,
    backslash, caret, backtick, double-quote). This prevents SPARQL
    injection via crafted URIs like ``http://x> ?s ?p ?o } UNION { ...``.
    """
    if not isinstance(s, str) or not s:
        raise ValueError("IRI must be a non-empty string")
    if _IRI_FORBIDDEN.search(s):
        raise ValueError(f"URI contains characters forbidden in IRI references: {s!r}")
    parsed = urlparse(s)
    if parsed.scheme not in ("http", "https", "urn"):
        raise ValueError(f"URI scheme must be http/https/urn, got {parsed.scheme!r}: {s!r}")
    if parsed.scheme in ("http", "https") and not parsed.netloc:
        raise ValueError(f"http(s) URI must have a host: {s!r}")
    return f"<{s}>"


def _is_storable_iri(s) -> bool:
    """True if ``s`` is a named IRI we can safely store as a SPARQL IRI ref.

    Returns False for RDF blank nodes (anonymous nodes that rdflib emits as
    bare node IDs like ``n934b37c…`` or ``_:b0`` with no URI scheme) and for
    anything ``_iri`` would otherwise reject. Foundational ontologies like
    FIBO declare classes with blank-node superclasses (anonymous
    ``owl:Restriction`` / ``owl:unionOf`` constructs); we can't ground
    against an anonymous node, so the caller skips these references rather
    than aborting the whole module ingest on the first one.
    """
    if not isinstance(s, str) or not s:
        return False
    try:
        _iri(s)
        return True
    except ValueError:
        return False


def _local_name(uri: str) -> str:
    """Reduce a full IRI to its short local name.

    Strips everything before the last ``#`` or ``/`` separator (e.g.
    ``…#Class`` → ``Class``). Falls back to the input when no separator
    exists.
    """
    for sep in ("#", "/"):
        idx = uri.rfind(sep)
        if idx >= 0 and idx < len(uri) - 1:
            return uri[idx + 1 :]
    return uri


# Mapping from ``rdf:type`` values to the abstract "kind" we report on
# neighbors. Order matters in :func:`_kind_specificity` below.
_KIND_BY_TYPE: dict[str, str] = {
    OWL + "Class": "class",
    OWL + "ObjectProperty": "object-property",
    OWL + "DatatypeProperty": "datatype-property",
    OWL + "AnnotationProperty": "annotation-property",
    OWL + "Ontology": "ontology",
    RDF + "Property": "property",
    RDFS + "Class": "class",
}


def _kind_from_type(type_uri: str) -> str | None:
    """Return the kind label for a given ``rdf:type`` URI.

    Returns None when the type is not recognised.
    """
    return _KIND_BY_TYPE.get(type_uri)


# Specificity ranking used when a neighbor has multiple ``rdf:type``
# values (e.g. ``rdf:Property`` + ``owl:DatatypeProperty``). Higher = more
# specific, wins.
_KIND_SPECIFICITY: dict[str, int] = {
    "class": 3,
    "object-property": 3,
    "datatype-property": 3,
    "annotation-property": 2,
    "ontology": 2,
    "property": 1,
    "unknown": 0,
}


def _kind_specificity(kind: str) -> int:
    return _KIND_SPECIFICITY.get(kind, 0)


# Reverse mapping: from the user-facing "kind" string to the set of
# rdf:type URIs that qualify. Used by ``search_entities`` to filter.
_KIND_TO_SPARQL_TYPES: dict[str, list[str]] = {
    "class": [OWL + "Class", RDFS + "Class"],
    "object-property": [OWL + "ObjectProperty"],
    "datatype-property": [OWL + "DatatypeProperty"],
}


# ── GraphStore impl ─────────────────────────────────────────────────────


class NeptuneDBGraphStore(GraphStore):
    """SPARQL-backed graph store with one named graph per ontology.

    There is no cross-ontology catalog graph. Each ontology lives in its
    own named graph at ``{base}/{namespace}/{quote(ontology_id)}``,
    which holds the ontology's metadata node, its projected schema
    (classes, properties, domain/range edges), and the raw Turtle
    uploaded by the user — all three sets of triples coexist in the
    same graph. Listing ontologies is delegated to the Dynamo registry,
    which is the authoritative index.
    """

    def __init__(self, endpoint: str | None = None, namespace: str | None = None):
        """Bind the store to a SPARQL endpoint and namespace.

        Args:
            endpoint: SPARQL endpoint URL; when set, overrides the module-level
                ``NDB_ENDPOINT`` used for all queries.
            namespace: Namespace whose per-ontology named graphs this store
                reads and writes.
        """
        global NDB_ENDPOINT
        if endpoint:
            NDB_ENDPOINT = endpoint
        self._namespace = namespace

    # ── Graph URI helper ──────────────────────────────────────────────
    def _graph_for(self, ontology_uri: str) -> str:
        return _ontology_graph_uri(ontology_uri, self._namespace)

    # ── Ontology CRUD ─────────────────────────────────────────────────

    def create_ontology(self, data: dict) -> dict:
        """Create an ontology's metadata node in its named graph.

        Args:
            data: Ontology attributes; must include ``uri`` and may include
                title, description, type, license, domain_tags, imports, etc.

        Returns:
            The created ontology as a dict (falls back to a minimal
            ``{"uri", "id"}`` if the read-back finds nothing).
        """
        uri = data["uri"]
        now = datetime.now(UTC).isoformat()
        graph_uri = self._graph_for(uri)
        triples = [
            f"{_iri(uri)} {_iri(RDF + 'type')} {_iri(OWL + 'Ontology')} .",
            f'{_iri(uri)} {_iri(DCT + "created")} "{now}" .',
            f'{_iri(uri)} {_iri(DCT + "modified")} "{now}" .',
        ]
        for k in ("title", "description", "ontology_type", "format", "license", "license_tier", "source_registry"):
            v = data.get(k)
            if v:
                pred = DCT + k if k in {"title", "description"} else WB + k
                triples.append(f'{_iri(uri)} {_iri(pred)} "{_esc(str(v))}" .')
        for tag in data.get("domain_tags") or []:
            triples.append(f'{_iri(uri)} {_iri(WB + "domainTag")} "{_esc(tag)}" .')
        for imp in data.get("imports") or []:
            triples.append(f"{_iri(uri)} {_iri(OWL + 'imports')} {_iri(imp)} .")
        self._emit_insert(graph_uri, triples)
        return self.get_ontology_by_uri(uri) or {"uri": uri, "id": uri}

    def get_ontology(self, oid: str) -> dict | None:
        """Fetch an ontology by id (aliases the ontology URI)."""
        return self.get_ontology_by_uri(oid)

    def get_ontology_by_uri(self, uri: str) -> dict | None:
        """Fetch an ontology's metadata from its named graph.

        Args:
            uri: The ontology URI to look up.

        Returns:
            The ontology attributes as a dict, or None when the URI is
            malformed or no triples exist for it.
        """
        try:
            iri = _iri(uri)
        except ValueError:
            return None
        graph_uri = self._graph_for(uri)
        q = f"""
        SELECT ?p ?o WHERE {{
          GRAPH {_iri(graph_uri)} {{ {iri} ?p ?o }}
        }}
        """
        rows = _sparql_query(q)["results"]["bindings"]
        if not rows:
            return None
        out: dict[str, Any] = {"uri": uri, "id": uri, "domain_tags": [], "imports": []}
        for b in rows:
            p, o = b["p"]["value"], b["o"]["value"]
            if p == WB + "domainTag":
                out["domain_tags"].append(o)
            elif p == OWL + "imports":
                out["imports"].append(o)
            elif p == RDF + "type":
                continue
            elif p.startswith(WB):
                out[p[len(WB) :]] = o
            elif p.startswith(DCT):
                out[p[len(DCT) :]] = o
        return out

    def update_ontology(self, uri: str, updates: dict) -> dict | None:
        """Replace workbench-namespaced attributes on an ontology.

        Args:
            uri: URI of the ontology to update.
            updates: Attribute name/value pairs to set; ``id``/``uri`` keys are
                ignored, and a ``None`` value clears the attribute.

        Returns:
            The updated ontology, or None if the ontology does not exist.
        """
        if not self.get_ontology_by_uri(uri):
            return None
        graph_uri = self._graph_for(uri)
        delete_parts, insert_parts = [], []
        for k, v in updates.items():
            if k in {"id", "uri"}:
                continue
            pred = WB + k
            delete_parts.append(f"{_iri(uri)} {_iri(pred)} ?old_{k} .")
            if v is not None:
                insert_parts.append(f'{_iri(uri)} {_iri(pred)} "{_esc(str(v))}" .')
        if not delete_parts:
            return self.get_ontology_by_uri(uri)
        where = " ".join(
            [f"OPTIONAL {{ {_iri(uri)} {_iri(WB + k)} ?old_{k} }} ." for k in updates if k not in {"id", "uri"}]
        )
        _sparql_update(f"""
        WITH {_iri(graph_uri)}
        DELETE {{ {" ".join(delete_parts)} }}
        INSERT {{ {" ".join(insert_parts)} }}
        WHERE {{ {where} }}
        """)
        return self.get_ontology_by_uri(uri)

    def delete_ontology(self, uri: str) -> bool:
        """Drop an ontology's entire named graph.

        Args:
            uri: URI of the ontology to delete.

        Returns:
            True if the ontology existed before the drop, False otherwise.
        """
        # Drop the entire per-ontology named graph — one SPARQL statement
        # wipes every triple the ontology ever wrote, no FILTER gymnastics
        # needed because nothing else lives in that graph.
        graph_uri = self._graph_for(uri)
        # Check first so we can report whether anything was actually there.
        existing = self.get_ontology_by_uri(uri)
        _sparql_update(f"DROP SILENT GRAPH {_iri(graph_uri)}")
        return existing is not None

    # ── Schema elements ───────────────────────────────────────────────

    # Batching bounds for INSERT DATA. A chunk is flushed when EITHER bound would
    # be exceeded by adding the next class/property, whichever comes first. Both
    # keep each SPARQL UPDATE well under Neptune's 150 MB per-request hard limit
    # (docs.aws.amazon.com/neptune/latest/userguide/limits.html — exceeding it
    # returns HTTP 400 BadRequestException). The byte bound is the real guard
    # (triple count is a poor proxy: one FIBO rdfs:comment can be 1-2 KB); 40 MB
    # of raw SPARQL leaves ample headroom for `update=` URL-encoding inflation
    # (~1.5-3x) under 150 MB. Chunking is per-class/per-property — a single
    # element's triples are never split across two INSERTs, so a mid-batch flush
    # failure can't leave a class half-projected (e.g. missing its coa:isMapped
    # marker). Tunable.
    _INSERT_BATCH_TRIPLES = 5000
    _INSERT_BATCH_BYTES = 40 * 1024 * 1024

    def _class_triples(self, ontology_uri, class_uri, labels, comments, super_classes, is_mapped) -> list[str]:
        """Serialize one class's projected triples (no I/O).

        Shared by :meth:`store_class` and :meth:`store_classes_batch` so the two
        paths emit byte-identical triples.
        """
        triples = [
            f"{_iri(class_uri)} {_iri(RDF + 'type')} {_iri(OWL + 'Class')} .",
            f"{_iri(class_uri)} {_iri(WB + 'definedBy')} {_iri(ontology_uri)} .",
        ]
        # Tier-2 answerability marker: True when the class is the rr:class of an
        # R2RML TriplesMap (SQL-backed), so Ontop / NL->SQL can answer queries
        # over it. The serve T-Box context filters on this triple so unmapped
        # (unstructured / foundational) classes don't pollute the structured
        # prompt. Absence of the triple == not mapped (serve treats absent as
        # "hide"); we therefore only emit it, never emit a "false".
        if is_mapped:
            triples.append(
                f"{_iri(class_uri)} {_iri(_SCL + 'isMapped')} "
                f'"true"^^{_iri("http://www.w3.org/2001/XMLSchema#boolean")} .'
            )
        for lbl in labels or []:
            triples.append(f'{_iri(class_uri)} {_iri(RDFS + "label")} "{_esc(lbl)}" .')
        for cmt in comments or []:
            triples.append(f'{_iri(class_uri)} {_iri(RDFS + "comment")} "{_esc(cmt)}" .')
        for sup in super_classes or []:
            # Skip blank-node superclasses (anonymous owl:Restriction /
            # owl:unionOf constructs common in FIBO). Storing the named
            # subClassOf edge is what matters for grounding; an anonymous
            # restriction can't be a grounding target anyway, and passing
            # its bare node ID to _iri() would throw and abort the whole
            # module ingest.
            if not _is_storable_iri(sup):
                continue
            triples.append(f"{_iri(class_uri)} {_iri(RDFS + 'subClassOf')} {_iri(sup)} .")
        return triples

    def _property_triples(self, ontology_uri, prop_uri, label, comment, domains, ranges) -> list[str]:
        """Serialize one property's projected triples (no I/O).

        Shared by :meth:`store_property` and :meth:`store_properties_batch`.
        """
        triples = [
            f"{_iri(prop_uri)} {_iri(WB + 'definedBy')} {_iri(ontology_uri)} .",
        ]
        if label:
            triples.append(f'{_iri(prop_uri)} {_iri(RDFS + "label")} "{_esc(label)}" .')
        if comment:
            triples.append(f'{_iri(prop_uri)} {_iri(RDFS + "comment")} "{_esc(comment)}" .')
        for d in domains or []:
            # Skip blank-node domains/ranges (anonymous union/restriction
            # class expressions) — see store_class for rationale.
            if not _is_storable_iri(d):
                continue
            triples.append(f"{_iri(prop_uri)} {_iri(RDFS + 'domain')} {_iri(d)} .")
        for r in ranges or []:
            if not _is_storable_iri(r):
                continue
            triples.append(f"{_iri(prop_uri)} {_iri(RDFS + 'range')} {_iri(r)} .")
        # Declare as a generic property unless the caller already picked a type
        triples.append(f"{_iri(prop_uri)} {_iri(RDF + 'type')} {_iri(RDF + 'Property')} .")
        return triples

    def _flush_triple_groups(self, graph_uri: str, groups: list[list[str]]) -> None:
        """INSERT per-element triple ``groups`` into ``graph_uri`` in bounded chunks.

        Each group is one class's or property's triples. The common case keeps a
        group whole — it is never split across two INSERT DATA calls when it fits
        the byte budget, so a normal mid-batch flush failure cannot leave a
        single class/property half-projected (which would e.g. drop its
        ``coa:isMapped`` marker). A new chunk is started when adding the next
        group would exceed ``_INSERT_BATCH_TRIPLES`` triples or
        ``_INSERT_BATCH_BYTES`` bytes, whichever comes first.

        A single group whose own triples exceed the byte budget CANNOT be emitted
        whole without risking Neptune's 150 MB request cap (HTTP 400, which aborts
        the whole ingest). Such a group is flushed on its own and split across
        multiple INSERTs by ``_emit_grouped`` — trading this one element's
        whole-group atomicity for a request that Neptune will actually accept.
        That relaxation is safe because the caller (``ingest_ontology``) now rolls
        the whole ontology back on any projection failure, so a partial split of
        one oversized element never survives. Byte size is measured as UTF-8 (what
        ``_sparql_update`` serializes and sends), not code-point count.
        """
        chunk: list[str] = []
        chunk_triples = 0
        chunk_bytes = 0
        for group in groups:
            if not group:
                continue
            group_bytes = sum(len(t.encode("utf-8")) for t in group)
            if group_bytes > self._INSERT_BATCH_BYTES:
                # Oversized element: flush what's pending, then emit this element
                # on its own, split into budget-sized sub-INSERTs (not whole).
                log.warning(
                    "ontology projection: a single element's %d triples are %d bytes, "
                    "exceeding the %d-byte per-INSERT budget; splitting it across multiple "
                    "INSERTs (per-triple, not whole) to stay under Neptune's 150 MB request limit.",
                    len(group),
                    group_bytes,
                    self._INSERT_BATCH_BYTES,
                )
                if chunk:
                    self._emit_insert(graph_uri, chunk)
                    chunk, chunk_triples, chunk_bytes = [], 0, 0
                self._emit_grouped(graph_uri, group)
                continue
            if chunk and (
                chunk_triples + len(group) > self._INSERT_BATCH_TRIPLES
                or chunk_bytes + group_bytes > self._INSERT_BATCH_BYTES
            ):
                self._emit_insert(graph_uri, chunk)
                chunk, chunk_triples, chunk_bytes = [], 0, 0
            chunk.extend(group)
            chunk_triples += len(group)
            chunk_bytes += group_bytes
        if chunk:
            self._emit_insert(graph_uri, chunk)

    def _emit_grouped(self, graph_uri: str, triples: list[str]) -> None:
        """Emit one over-budget element's ``triples`` across ≤budget sub-INSERTs.

        Only reached for a single element whose triples exceed
        ``_INSERT_BATCH_BYTES`` (see ``_flush_triple_groups``). Packs triples
        greedily up to the byte budget per INSERT. A lone triple that itself
        exceeds the budget is emitted on its own and logged — it cannot be split
        further, so if it also exceeds Neptune's request cap the INSERT will fail
        loud (and the caller's rollback wipes the partial ontology).
        """
        sub: list[str] = []
        sub_bytes = 0
        for t in triples:
            t_bytes = len(t.encode("utf-8"))
            if t_bytes > self._INSERT_BATCH_BYTES:
                log.warning(
                    "ontology projection: a single triple is %d bytes, exceeding the "
                    "%d-byte budget; emitting it alone (cannot split further).",
                    t_bytes,
                    self._INSERT_BATCH_BYTES,
                )
            if sub and sub_bytes + t_bytes > self._INSERT_BATCH_BYTES:
                self._emit_insert(graph_uri, sub)
                sub, sub_bytes = [], 0
            sub.append(t)
            sub_bytes += t_bytes
        if sub:
            self._emit_insert(graph_uri, sub)

    def _emit_insert(self, graph_uri: str, triples: list[str]) -> None:
        """Issue one ``INSERT DATA`` SPARQL UPDATE for ``triples``.

        Wraps transport failures with the chunk's shape (triple count + byte
        size) so a mid-batch flush failure names which INSERT died, instead of a
        bare httpx error with no batch context.
        """
        update = f"INSERT DATA {{ GRAPH {_iri(graph_uri)} {{\n  " + "\n  ".join(triples) + "\n}}"
        try:
            _sparql_update(update)
        except (httpx.HTTPError, botocore.exceptions.BotoCoreError) as e:
            chunk_bytes = sum(len(t.encode("utf-8")) for t in triples)
            raise RuntimeError(
                f"INSERT flush failed ({len(triples)} triples, {chunk_bytes} bytes) into graph {graph_uri}: {e}"
            ) from e

    def store_class(
        self,
        ontology_uri,
        class_uri,
        labels=None,
        comments=None,
        super_classes=None,
        is_mapped=False,
    ):
        """Project a class (and its labels, comments, superclasses) into a graph.

        Args:
            ontology_uri: URI of the owning ontology (selects the named graph).
            class_uri: URI of the class to store.
            labels: ``rdfs:label`` values for the class.
            comments: ``rdfs:comment`` values (definitions) for the class.
            super_classes: Parent class URIs; blank-node superclasses are skipped.
            is_mapped: When True, emit the ``coa:isMapped`` Tier-2 answerability
                marker (SQL-backed class); never emitted as false.

        Returns:
            A dict with the stored class URI and owning ontology id.
        """
        graph_uri = self._graph_for(ontology_uri)
        triples = self._class_triples(ontology_uri, class_uri, labels, comments, super_classes, is_mapped)
        self._emit_insert(graph_uri, triples)
        return {"uri": class_uri, "ontology_id": ontology_uri}

    def store_classes_batch(self, ontology_uri, classes: list[dict]) -> int:
        """Project many classes into the ontology graph in chunked INSERTs.

        All classes share one named graph (``ontology_uri``), so their triples
        are accumulated and flushed in a few SPARQL UPDATEs instead of one HTTP
        round-trip per class. Each ``classes`` item is a dict with keys
        ``class_uri`` and optional ``labels``/``comments``/``super_classes``/
        ``is_mapped`` (same semantics as :meth:`store_class`).

        Returns the number of classes projected.
        """
        graph_uri = self._graph_for(ontology_uri)
        groups = [
            self._class_triples(
                ontology_uri,
                c["class_uri"],
                c.get("labels"),
                c.get("comments"),
                c.get("super_classes"),
                c.get("is_mapped", False),
            )
            for c in classes
        ]
        self._flush_triple_groups(graph_uri, groups)
        return len(classes)

    def store_property(
        self,
        ontology_uri,
        prop_uri,
        label="",
        comment="",
        domains=None,
        ranges=None,
    ):
        """Project a property (label, comment, domains, ranges) into a graph.

        Args:
            ontology_uri: URI of the owning ontology (selects the named graph).
            prop_uri: URI of the property to store.
            label: ``rdfs:label`` value for the property.
            comment: ``rdfs:comment`` value for the property.
            domains: Domain class URIs; blank-node domains are skipped.
            ranges: Range URIs (class IRIs or datatypes); blank-node ranges are skipped.

        Returns:
            A dict with the stored property URI and owning ontology id.
        """
        graph_uri = self._graph_for(ontology_uri)
        triples = self._property_triples(ontology_uri, prop_uri, label, comment, domains, ranges)
        self._emit_insert(graph_uri, triples)
        return {"uri": prop_uri, "ontology_id": ontology_uri}

    def store_properties_batch(self, ontology_uri, properties: list[dict]) -> int:
        """Project many properties into the ontology graph in chunked INSERTs.

        The property counterpart to :meth:`store_classes_batch`. Each
        ``properties`` item is a dict with key ``prop_uri`` and optional
        ``label``/``comment``/``domains``/``ranges`` (same semantics as
        :meth:`store_property`). Returns the number of properties projected.
        """
        graph_uri = self._graph_for(ontology_uri)
        groups = [
            self._property_triples(
                ontology_uri,
                p["prop_uri"],
                p.get("label", ""),
                p.get("comment", ""),
                p.get("domains"),
                p.get("ranges"),
            )
            for p in properties
        ]
        self._flush_triple_groups(graph_uri, groups)
        return len(properties)

    # ── Bulk Turtle ingest ────────────────────────────────────────────

    def supersede_annotations(
        self,
        ontology_uri: str,
        subject_uris: Iterable[str],
        predicates: Mapping[str, str | None],
    ) -> int:
        """Retire the live ``predicates`` values of ``subject_uris`` in this ontology's graph.

        ``predicates`` maps each annotation predicate to the HISTORY predicate its
        displaced values move to (``coa:supersededComment`` etc.), or ``None`` to
        drop them outright. One ``DELETE/INSERT`` per chunk of subjects,
        constrained by ``VALUES`` on both the subject and the ``(predicate,
        history)`` pair, so nothing outside the named (subject, predicate) pairs
        — in particular the relationship axioms (``rdfs:range``,
        ``rdfs:subClassOf``, R2RML) and every other subject in the graph — can be
        touched. Neptune's SPARQL UPDATE response carries no affected-row count,
        so the count is taken with a ``SELECT COUNT`` first; it is the log signal
        that supersession fired (or found nothing to do).
        """
        subjects = [s for s in dict.fromkeys(subject_uris) if _is_storable_iri(s)]
        kept = {p: h for p, h in predicates.items() if h}
        dropped = [p for p, h in predicates.items() if not h]
        if not subjects or not predicates:
            return 0
        graph_uri = self._graph_for(ontology_uri)
        removed = 0
        # ponytail: VALUES-chunked so a large re-induced proposal (hundreds of
        # classes + properties) stays under Neptune's request size comfortably.
        for chunk_no, i in enumerate(range(0, len(subjects), _SUPERSEDE_CHUNK)):
            subj_values = " ".join(_iri(s) for s in subjects[i : i + _SUPERSEDE_CHUNK])
            all_preds = " ".join(_iri(p) for p in predicates)
            count_pattern = f"VALUES ?s {{ {subj_values} }} VALUES ?p {{ {all_preds} }} ?s ?p ?o ."
            try:
                count_res = _sparql_query(
                    f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH {_iri(graph_uri)} {{ {count_pattern} }} }}"
                )
            except Exception as e:
                raise RuntimeError(f"supersede count failed (chunk {chunk_no}, {len(subjects)} subjects): {e}") from e
            bindings = count_res.get("results", {}).get("bindings", [])
            n = int(bindings[0]["n"]["value"]) if bindings else 0
            if n == 0:
                continue
            # Move: the displaced literal is re-attached under its history
            # predicate so the earlier generation stays inspectable.
            # One statement per (live, history) pair with both predicates
            # inlined — the same shape update_ontology uses. Cost: one small
            # update per annotation predicate (4) per subject chunk.
            for live_pred, hist_pred in kept.items():
                try:
                    _sparql_update(
                        f"WITH {_iri(graph_uri)} "
                        f"DELETE {{ ?s {_iri(live_pred)} ?o }} INSERT {{ ?s {_iri(hist_pred)} ?o }} "
                        f"WHERE {{ VALUES ?s {{ {subj_values} }} ?s {_iri(live_pred)} ?o . }}"
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"supersede move failed (chunk {chunk_no}, predicate {live_pred} -> {hist_pred}): {e}"
                    ) from e
            if dropped:
                drop_values = " ".join(_iri(p) for p in dropped)
                try:
                    _sparql_update(
                        f"WITH {_iri(graph_uri)} DELETE {{ ?s ?p ?o }} "
                        f"WHERE {{ VALUES ?s {{ {subj_values} }} VALUES ?p {{ {drop_values} }} ?s ?p ?o . }}"
                    )
                except Exception as e:
                    raise RuntimeError(f"supersede drop failed (chunk {chunk_no}, predicates {dropped}): {e}") from e
            removed += n
        log.info(
            "supersede_annotations: retired %d triples (subjects=%d predicates=%d kept_as_history=%d graph=%s)",
            removed,
            len(subjects),
            len(predicates),
            len(kept),
            graph_uri,
        )
        return removed

    def load_turtle(
        self,
        ontology_uri: str,
        turtle: str,
        graph_uri: str | None = None,
    ) -> dict:
        """POST raw Turtle to the per-ontology named graph via GSP."""
        if not turtle or not turtle.strip():
            return {"status": "skipped", "reason": "empty turtle"}
        target = graph_uri or self._graph_for(ontology_uri)
        log.info("load_turtle: posting %d bytes to %s", len(turtle), target)
        return _gsp_post_turtle(target, turtle)

    def get_graph_turtle(self, ontology_id: str) -> str | None:
        """GET the per-ontology named graph as Turtle via GSP.

        ``ontology_id`` is mapped to the same named-graph URI scheme
        :meth:`load_turtle` writes to, so the operation round-trips
        cleanly. Returns the Turtle body (possibly empty if the graph
        has no triples), or ``None`` if Neptune reports 404 for the
        graph URI.
        """
        target = self._graph_for(ontology_id)
        log.info("get_graph_turtle: fetching %s", target)
        return _gsp_get_turtle(target)

    def get_vertex(
        self,
        vertex_uri: str,
        namespace: str | None = None,
    ) -> dict | None:
        """Cross-ontology vertex lookup scoped to one namespace.

        Issues three SPARQL queries against Neptune:

        1. All ``<vertex_uri> ?p ?o`` triples in any named graph whose
           URI starts with the namespace prefix (so neighbors from
           foundational ontologies are reachable from induced
           ontology vertices).
        2. ``rdfs:label`` for every URI neighbor discovered in (1),
           also namespace-scoped — a single query using a SPARQL
           ``VALUES`` clause.
        3. ``rdf:type`` for every URI neighbor — same shape as (2),
           used to derive the neighbor's "kind" field.

        Returns ``None`` when the vertex has no triples anywhere in
        the namespace.
        """
        ns_prefix = _namespace_graph_prefix(namespace).rstrip("/") + "/"

        # ── (1) outgoing triples ────────────────────────────────────
        q1 = f"""
        SELECT ?g ?p ?o WHERE {{
          GRAPH ?g {{
            {_iri(vertex_uri)} ?p ?o .
          }}
          FILTER(STRSTARTS(STR(?g), "{_esc(ns_prefix)}"))
        }}
        """
        bindings = _sparql_query(q1).get("results", {}).get("bindings", [])

        # ── (1b) incoming triples ───────────────────────────────────
        q1b = f"""
        SELECT ?g ?s ?p WHERE {{
          GRAPH ?g {{
            ?s ?p {_iri(vertex_uri)} .
          }}
          FILTER(STRSTARTS(STR(?g), "{_esc(ns_prefix)}"))
        }}
        """
        incoming_bindings = _sparql_query(q1b).get("results", {}).get("bindings", [])

        if not bindings and not incoming_bindings:
            log.info("get_vertex: no triples for %s in namespace %s", vertex_uri, namespace)
            return None

        types: list[str] = []
        labels: list[str] = []
        comments: list[str] = []
        alt_labels: list[str] = []
        superseded: dict[str, list[str]] = {}
        edges: list[dict] = []
        graph_uris: set[str] = set()
        neighbor_uris: set[str] = set()
        is_mapped = False

        rdf_type = RDF + "type"
        rdfs_label = RDFS + "label"
        rdfs_comment = RDFS + "comment"
        skos_alt_label = _SKOS + "altLabel"
        scl_is_mapped = _SCL + "isMapped"
        # History predicates: the previous generation of a description /
        # alt-label retired by an append-mode re-accept. Surfaced separately so
        # the class detail can show "what this used to say" without them being
        # mistaken for live content or for graph edges.
        history_preds = {
            _SCL + "supersededComment": "comments",
            _SCL + "supersededDefinition": "definitions",
            _SCL + "supersededAltLabel": "altLabels",
        }

        for b in bindings:
            g_val = b.get("g", {}).get("value")
            if g_val:
                graph_uris.add(g_val)
            p = b["p"]["value"]
            o_node = b["o"]
            o_value = o_node["value"]
            o_type = o_node["type"]  # 'uri' | 'literal' | 'bnode'

            if p == rdf_type and o_type == "uri":
                if o_value not in types:
                    types.append(o_value)
            elif p == rdfs_label and o_type == "literal":
                if o_value not in labels:
                    labels.append(o_value)
            elif p == rdfs_comment and o_type == "literal":
                if o_value not in comments:
                    comments.append(o_value)
            elif p == skos_alt_label and o_type == "literal":
                if o_value not in alt_labels:
                    alt_labels.append(o_value)
            elif p in history_preds and o_type == "literal":
                bucket = superseded.setdefault(history_preds[p], [])
                if o_value not in bucket:
                    bucket.append(o_value)
            elif p == scl_is_mapped:
                # Tier-2 R2RML-answerability marker (written by store_class as
                # ``"true"^^xsd:boolean``). Surfaced as a first-class boolean so
                # callers/tests can assert answerability without parsing edges.
                # Absent triple == unmapped == False (structured classes only).
                is_mapped = str(o_value).lower() == "true"
            else:
                edges.append({"p": p, "o": o_value, "o_type": o_type, "direction": "outgoing"})
                if o_type == "uri":
                    neighbor_uris.add(o_value)

        # Process incoming edges — the neighbor is the subject
        for b in incoming_bindings:
            g_val = b.get("g", {}).get("value")
            if g_val:
                graph_uris.add(g_val)
            s_node = b["s"]
            # Skip blank-node subjects. These arise when the vertex is the
            # object of a triple whose subject is anonymous — e.g. an RDF list
            # cons-cell (``owl:members ( ... )`` expands to rdf:first/rdf:rest
            # BNodes) or an ``owl:Restriction`` class expression. Their BNode
            # ids (e.g. "b35015439") have no IRI scheme, so feeding them to
            # ``_iri()`` in the neighbor-label/type batch queries below raises
            # ValueError and 500s the whole vertex lookup.
            if s_node.get("type") != "uri":
                continue
            s_value = s_node["value"]
            p = b["p"]["value"]
            # Skip rdf:type incoming (means "something is typed as this vertex" — noise)
            if p == rdf_type:
                continue
            edges.append({"p": p, "o": s_value, "o_type": "uri", "direction": "incoming"})
            neighbor_uris.add(s_value)

        # ── (2) labels for URI neighbors (batched queries) ──────────
        # Cap batch size to avoid exceeding Neptune's query size limit
        # (~10 MB). Each IRI is at most ~300 chars wrapped in <>, so
        # 500 IRIs ≈ 150 KB — well within limits while avoiding DoS
        # from vertices with thousands of neighbors.
        _VALUES_BATCH = 500
        neighbor_labels: dict[str, str] = {}
        if neighbor_uris:
            uri_list = list(neighbor_uris)
            for batch_start in range(0, len(uri_list), _VALUES_BATCH):
                batch = uri_list[batch_start : batch_start + _VALUES_BATCH]
                values_clause = " ".join(_iri(u) for u in batch)
                q2 = f"""
                PREFIX rdfs: <{RDFS}>
                SELECT ?o ?label WHERE {{
                  VALUES ?o {{ {values_clause} }}
                  GRAPH ?g {{ ?o rdfs:label ?label . }}
                  FILTER(STRSTARTS(STR(?g), "{_esc(ns_prefix)}"))
                }}
                """
                for r in _sparql_query(q2).get("results", {}).get("bindings", []):
                    neighbor_labels.setdefault(r["o"]["value"], r["label"]["value"])

        # ── (3) types for URI neighbors (batched queries) ───────────
        neighbor_kinds: dict[str, str] = {}
        if neighbor_uris:
            uri_list = list(neighbor_uris)
            for batch_start in range(0, len(uri_list), _VALUES_BATCH):
                batch = uri_list[batch_start : batch_start + _VALUES_BATCH]
                values_clause = " ".join(_iri(u) for u in batch)
                q3 = f"""
                PREFIX rdf: <{RDF}>
                SELECT ?o ?t WHERE {{
                  VALUES ?o {{ {values_clause} }}
                  GRAPH ?g {{ ?o rdf:type ?t . }}
                  FILTER(STRSTARTS(STR(?g), "{_esc(ns_prefix)}"))
                }}
                """
                for r in _sparql_query(q3).get("results", {}).get("bindings", []):
                    uri = r["o"]["value"]
                    t = r["t"]["value"]
                    kind = _kind_from_type(t)
                    if kind and (
                        uri not in neighbor_kinds or _kind_specificity(kind) > _kind_specificity(neighbor_kinds[uri])
                    ):
                        neighbor_kinds[uri] = kind

        # ── Assemble response ───────────────────────────────────────
        out_edges: list[dict] = []
        for e in edges:
            o_value = e["o"]
            o_type = e["o_type"]
            direction = e.get("direction", "outgoing")
            if o_type == "uri":
                neighbor = {
                    "uri": o_value,
                    "label": neighbor_labels.get(o_value),
                    "kind": neighbor_kinds.get(o_value, "unknown"),
                }
            elif o_type == "literal":
                # Literals are represented as their lexical form; their
                # "name" is the value itself.
                neighbor = {
                    "uri": o_value,
                    "label": o_value,
                    "kind": "literal",
                }
            else:  # bnode
                neighbor = {
                    "uri": o_value,
                    "label": None,
                    "kind": "blank-node",
                }
            out_edges.append(
                {
                    "predicate": e["p"],
                    "predicate_label": _local_name(e["p"]),
                    "direction": direction,
                    "neighbor": neighbor,
                }
            )

        return {
            "uri": vertex_uri,
            "graph_uris": sorted(graph_uris),
            "types": types,
            "labels": labels,
            "comments": comments,
            "alt_labels": alt_labels,
            "superseded": superseded,
            "is_mapped": is_mapped,
            "edges": out_edges,
        }

    # ── Entity search ─────────────────────────────────────────────────

    def _graph_scope_filter(
        self,
        namespace: str | None,
        ontology_id: str | None,
        exclude_ontology_id: str | None,
    ) -> str:
        """Build a SPARQL ``FILTER`` that pins ``?g`` to this namespace's graphs.

        This MUST be applied wherever ``?g`` is bound — including the outer query
        of the paged search — so results never leak across namespaces that happen
        to share an ontology IRI (a subject like ``foaf:Person`` exists in many
        namespaces' graphs).

        Args:
            namespace: Namespace whose named-graph prefix to scope to.
            ontology_id: When set, pin ``?g`` to that single ontology's graph
                instead of the whole namespace prefix.
            exclude_ontology_id: When set, additionally exclude that ontology's
                graph (e.g. governed-metrics).

        Returns:
            One or more newline-joined ``FILTER(...)`` clauses over ``?g``.
        """
        ns_prefix = _namespace_graph_prefix(namespace).rstrip("/") + "/"
        if ontology_id:
            graph_filter = f"FILTER(?g = {_iri(_ontology_graph_uri(ontology_id, namespace))})"
        else:
            graph_filter = f'FILTER(STRSTARTS(STR(?g), "{_esc(ns_prefix)}"))'
        if exclude_ontology_id:
            excluded_graph = _ontology_graph_uri(exclude_ontology_id, namespace)
            graph_filter += f"\n          FILTER(?g != {_iri(excluded_graph)})"
        return graph_filter

    def _search_where_clause(
        self,
        query: str,
        namespace: str | None,
        kind: str | None,
        ontology_id: str | None,
        exclude_ontology_id: str | None,
    ) -> str:
        """Build the shared WHERE body for the paged search and the count query.

        Using the identical body for both guarantees the page window and its
        reported total are derived from the same match set. Binds ``?s``
        (entity), ``?g`` (named graph) and, via OPTIONAL, ``?label`` (used by
        the text filter).
        """
        graph_filter = self._graph_scope_filter(namespace, ontology_id, exclude_ontology_id)

        # Build the type filter
        type_filter = ""
        if kind:
            type_uris = _KIND_TO_SPARQL_TYPES.get(kind)
            if type_uris:
                values = " ".join(f"<{u}>" for u in type_uris)
                type_filter = f"VALUES ?requiredType {{ {values} }}\n          GRAPH ?g {{ ?s a ?requiredType . }}"

        # Escape the query for SPARQL CONTAINS (case-insensitive via LCASE)
        escaped_q = _esc(query.lower())

        # A bare ``*`` (or empty string) means "match everything" — used by
        # the UI to auto-load a namespace's entities without a search term.
        # Treat it as a wildcard by dropping the substring CONTAINS filter
        # rather than searching for a literal asterisk (which matches no IRI).
        #
        # The substring is matched against the entity's LOCAL NAME (the segment
        # after the last ``#`` or ``/``), not the whole IRI — otherwise a query
        # like ``Product`` spuriously matches every class under a
        # ``.../ProductsAndServices/...`` path even when its own name is
        # unrelated (e.g. ``.../ProductsAndServices/Broker``). ``REPLACE`` strips
        # everything up to and including the last separator; the label is matched
        # too so human-readable names still hit.
        match_all = query.strip() in ("", "*")
        text_filter = (
            ""
            if match_all
            else f"""FILTER(
            CONTAINS(LCASE(REPLACE(STR(?s), "^.*[#/]", "")), "{escaped_q}")
            || CONTAINS(LCASE(STR(COALESCE(?label, ""))), "{escaped_q}")
          )"""
        )

        # With a kind, the type pattern alone binds ?s and ?g: a subject typed in
        # ?g is by definition in ?g, so an extra ``?s ?_p ?_o`` adds no matches.
        # It does add cost: with ?g unbound it can match every quad in the cluster
        # (all namespaces) before the STRSTARTS filter applies, and it multiplies
        # rows per subject ahead of GROUP BY. The bound-predicate type pattern
        # touches only typed subjects (still cluster-wide, but far fewer rows).
        subject_pattern = type_filter or "GRAPH ?g {\n            ?s ?_p ?_o .\n          }"

        return f"""
          {subject_pattern}
          {graph_filter}
          OPTIONAL {{ GRAPH ?g {{ ?s rdfs:label ?label . }} }}
          {text_filter}"""

    def count_entities(
        self,
        query: str,
        namespace: str | None = None,
        kind: str | None = None,
        ontology_id: str | None = None,
        exclude_ontology_id: str | None = None,
    ) -> int:
        """Total number of distinct entities matching a substring search.

        Same match set as :meth:`search_entities` (independent of limit/offset),
        so a client can render a known page count. Returns 0 on an empty result.
        """
        where = self._search_where_clause(query, namespace, kind, ontology_id, exclude_ontology_id)
        sparql = f"""
        PREFIX rdfs: <{RDFS}>
        PREFIX rdf: <{RDF}>
        SELECT (COUNT(DISTINCT ?s) AS ?total) WHERE {{{where}
        }}
        """
        bindings = _sparql_query(sparql).get("results", {}).get("bindings", [])
        if not bindings:
            return 0
        try:
            return int(bindings[0].get("total", {}).get("value", 0))
        except (TypeError, ValueError):
            return 0

    def search_entities(
        self,
        query: str,
        namespace: str | None = None,
        kind: str | None = None,
        ontology_id: str | None = None,
        exclude_ontology_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Substring search over entity IRIs and labels in a namespace.

        Returns a list of dicts with keys: uri, label, kind, graph_uris.
        Searches across all named graphs in the namespace (or a single
        ontology graph when ``ontology_id`` is provided).

        Results are returned in a deterministic total order (case-insensitive
        label — falling back to the IRI — then IRI as a unique tiebreaker), so
        ``limit`` / ``offset`` yield stable, non-overlapping page windows over
        the full match set. Pair with :meth:`count_entities` for the total.

        Args:
            query: Case-insensitive substring matched against IRIs and labels;
                ``""`` or ``"*"`` matches everything.
            namespace: Namespace whose graphs to search; defaults to the store's.
            kind: Restrict to a kind (``class``/``object-property``/
                ``datatype-property``) when provided.
            ontology_id: Restrict to a single ontology's named graph.
            exclude_ontology_id: If set, excludes results from this ontology's
                named graph (e.g., exclude governed-metrics classes).
            limit: Maximum number of results to return (the page size).
            offset: Number of leading (ordered) distinct entities to skip.

        Returns:
            List of dicts with keys: uri, label, kind, graph_uris.
        """
        where = self._search_where_clause(query, namespace, kind, ontology_id, exclude_ontology_id)
        safe_offset = max(0, offset)
        # Both bounds are interpolated into the SPARQL query below, so they are
        # clamped here as well as validated at the API edge: a non-positive LIMIT
        # is invalid SPARQL (a 500 from Neptune rather than a 400), and an
        # unbounded one is an unbounded scan.
        safe_limit = max(1, min(limit, _MAX_SEARCH_PAGE_SIZE))
        # The outer query re-binds ?g, so it MUST carry the same namespace scope
        # as the inner subquery — otherwise a subject that also exists in another
        # namespace's graph (e.g. a shared ontology IRI like foaf:) would bind ?g
        # to that foreign graph and leak its graph_uri/labels across namespaces.
        outer_graph_filter = self._graph_scope_filter(namespace, ontology_id, exclude_ontology_id)

        # Inner subquery selects exactly this page's distinct entities in a
        # stable order; the outer pattern re-fetches each one's labels/types so
        # the best-label / most-specific-kind dedup below still applies. Ordering
        # is repeated in the outer query because SPARQL does not guarantee an
        # inner ORDER BY survives the join.
        sparql = f"""
        PREFIX rdfs: <{RDFS}>
        PREFIX rdf: <{RDF}>
        SELECT ?s ?label ?t ?g ?sortKey WHERE {{
          {{
            SELECT ?s (MIN(LCASE(COALESCE(STR(?label), STR(?s)))) AS ?sortKey) WHERE {{{where}
            }}
            GROUP BY ?s
            ORDER BY ?sortKey ?s
            LIMIT {safe_limit} OFFSET {safe_offset}
          }}
          GRAPH ?g {{ ?s ?_p2 ?_o2 . }}
          {outer_graph_filter}
          OPTIONAL {{ GRAPH ?g {{ ?s rdfs:label ?label . }} }}
          OPTIONAL {{ GRAPH ?g {{ ?s rdf:type ?t . }} }}
        }}
        ORDER BY ?sortKey ?s
        """

        bindings = _sparql_query(sparql).get("results", {}).get("bindings", [])

        # Deduplicate by URI, pick best label and kind. Insertion order follows
        # the query's ORDER BY, so the paged order is preserved.
        hits: dict[str, dict] = {}
        # Per-URI accumulator of every named graph the entity was seen in. A
        # class IRI asserted in more than one ontology (e.g. a shared IRI
        # referenced by both schema.org and FIBO) yields several rows here — one
        # per graph. We collapse to one hit per URI but keep ALL its graphs so
        # the list view can show every source ontology, not just the first seen.
        graphs_for: dict[str, list[str]] = {}

        def _add_graph(uri: str, b: dict) -> None:
            g = b.get("g", {}).get("value")
            if not g:
                return
            lst = graphs_for.setdefault(uri, [])
            if g not in lst:
                lst.append(g)

        for b in bindings:
            uri = b["s"]["value"]
            _add_graph(uri, b)
            if uri in hits:
                # Update label if we got one
                if "label" in b and b["label"]["type"] == "literal" and not hits[uri].get("label"):
                    hits[uri]["label"] = b["label"]["value"]
                # Update kind if more specific
                if "t" in b:
                    new_kind = _kind_from_type(b["t"]["value"])
                    if new_kind and _kind_specificity(new_kind) > _kind_specificity(hits[uri].get("kind", "unknown")):
                        hits[uri]["kind"] = new_kind
                continue
            entry: dict = {
                "uri": uri,
                "label": b.get("label", {}).get("value") if "label" in b else None,
                "kind": _kind_from_type(b.get("t", {}).get("value", "")) or "unknown",
            }
            hits[uri] = entry

        for uri, entry in hits.items():
            entry["graph_uris"] = graphs_for.get(uri, [])

        return list(hits.values())

    # ── Health ────────────────────────────────────────────────────────

    def health_check(self) -> dict:
        """Probe the SPARQL endpoint with a trivial query.

        Returns:
            A status dict: ``{"status": "unconfigured"}`` when no endpoint is
            set, ``{"status": "ok"}`` on success, or ``{"status": "error",
            "error": ...}`` when the probe query fails.
        """
        if not NDB_ENDPOINT:
            return {"status": "unconfigured"}
        try:
            _sparql_query("SELECT (1 AS ?health) WHERE {}")
            return {"status": "ok"}
        except Exception as e:  # pragma: no cover - best-effort
            return {"status": "error", "error": str(e)}

    def count_classes_and_properties(self, ontology_id: str) -> dict | None:
        """Count distinct owl:Class and property instances in the ontology's named graph."""
        graph_uri = _ontology_graph_uri(ontology_id, self._namespace)
        q = (
            f"SELECT (COUNT(DISTINCT ?c) AS ?classes) (COUNT(DISTINCT ?p) AS ?props) WHERE {{ "
            f"GRAPH <{graph_uri}> {{ "
            f"{{ ?c a <{OWL}Class> }} UNION {{ ?p a <{OWL}DatatypeProperty> }} UNION {{ ?p a <{OWL}ObjectProperty> }} "
            f"}} }}"
        )
        try:
            result = _sparql_query(q)
            bindings = result.get("results", {}).get("bindings", [])
            if bindings:
                return {
                    "classes": int(bindings[0].get("classes", {}).get("value", 0)),
                    "properties": int(bindings[0].get("props", {}).get("value", 0)),
                }
        except Exception:
            log.warning("count_classes_and_properties failed", exc_info=True)
        return None

    # ── Ontology overview ─────────────────────────────────────────────

    @staticmethod
    def _overview_class_query(gi: str) -> str:
        """Build SPARQL enumerating a graph's classes with the overview fields.

        Surfaces label, comment, grounding, and subClassOf parents for each
        class. ``coa:groundedTo`` (+ skos:exactMatch / skos:closeMatch) is emitted by
        the inducer on a class grounded to a previously-loaded class; absent →
        the class is novel (net-new this induction).
        """
        return f"""
        PREFIX rdf: <{RDF}>
        PREFIX rdfs: <{RDFS}>
        PREFIX owl: <{OWL}>
        PREFIX skos: <{_SKOS}>
        PREFIX coa: <{_SCL}>
        SELECT ?c ?label ?comment ?groundedTo ?exact ?close ?parent WHERE {{
          GRAPH {gi} {{
            ?c rdf:type ?t .
            VALUES ?t {{ owl:Class rdfs:Class }}
            OPTIONAL {{ ?c rdfs:label ?label }}
            OPTIONAL {{ ?c rdfs:comment ?comment }}
            OPTIONAL {{ ?c coa:groundedTo ?groundedTo }}
            OPTIONAL {{ ?c skos:exactMatch ?exact }}
            OPTIONAL {{ ?c skos:closeMatch ?close }}
            OPTIONAL {{ ?c rdfs:subClassOf ?parent }}
          }}
        }}
        """

    @staticmethod
    def _accumulate_class_row(classes: dict[str, dict], b: dict) -> None:
        """Fold one class-query binding into the per-URI class summary map.

        A class yields one row per (label × comment × parent × …) combination,
        so collapse to a single entry: first label/comment/grounding wins;
        named-IRI subClassOf parents accumulate (deduped, order-preserving).
        Blank-node parents (owl:Restriction cardinality axioms) are skipped —
        they aren't taxonomy edges.
        """
        uri = b["c"]["value"]
        entry = classes.setdefault(
            uri,
            {
                "uri": uri,
                "label": None,
                "comment": None,
                "grounded_to": None,
                "match_type": None,
                "sub_class_of": [],
            },
        )
        if "parent" in b and b["parent"]["type"] == "uri":
            parent = b["parent"]["value"]
            if parent not in entry["sub_class_of"]:
                entry["sub_class_of"].append(parent)
        if entry["label"] is None and "label" in b:
            entry["label"] = b["label"]["value"]
        if entry["comment"] is None and "comment" in b:
            entry["comment"] = b["comment"]["value"]
        # Grounding target: prefer coa:groundedTo, else a skos exact/close
        # mapping. Record the match type so the UI can distinguish exact/close.
        if entry["grounded_to"] is None:
            if "groundedTo" in b and b["groundedTo"]["type"] == "uri":
                entry["grounded_to"] = b["groundedTo"]["value"]
            elif "exact" in b and b["exact"]["type"] == "uri":
                entry["grounded_to"] = b["exact"]["value"]
            elif "close" in b and b["close"]["type"] == "uri":
                entry["grounded_to"] = b["close"]["value"]
        if entry["match_type"] is None:
            if "exact" in b and b["exact"]["type"] == "uri":
                entry["match_type"] = "exact"
            elif "close" in b and b["close"]["type"] == "uri":
                entry["match_type"] = "close"

    def _sample_overview(self, ontology_id: str, graph_uri: str, gi: str) -> dict:
        """Bounded taxonomy seed for the Graph view's initial render.

        Enumerates the ontology's full class set once (the class scan is cheap;
        the old bottleneck was one per-class detail call on the client, not this
        query), then in Python picks the ``_SAMPLE_ROOTS`` roots with the
        largest subtrees and walks their descendants breadth-first up to
        ``_SAMPLE_MAX_DESCENDANTS``. Only edges *within* the returned set are
        kept, so a root whose real parent is a foundational class outside the
        sample still reports as a root on the client. Properties are omitted —
        the Graph view fetches a node's relationships on selection.
        """
        classes: dict[str, dict] = {}
        for b in _sparql_query(self._overview_class_query(gi)).get("results", {}).get("bindings", []):
            self._accumulate_class_row(classes, b)

        # child → parents (within this ontology graph) and parent → children.
        children_of: dict[str, list[str]] = {uri: [] for uri in classes}
        for uri, entry in classes.items():
            for parent in entry["sub_class_of"]:
                if parent in classes:
                    children_of[parent].append(uri)

        # Roots = classes with no parent inside this graph. Rank by subtree size
        # (descendants, transitive) so the seed shows the richest hierarchies.
        def subtree_size(root: str) -> int:
            seen: set[str] = set()
            stack = [root]
            while stack:
                cur = stack.pop()
                for child in children_of.get(cur, ()):
                    if child not in seen:
                        seen.add(child)
                        stack.append(child)
            return len(seen)

        roots = [uri for uri, entry in classes.items() if not any(p in classes for p in entry["sub_class_of"])]
        roots.sort(key=lambda r: (-subtree_size(r), (classes[r]["label"] or r).lower()))
        seed_roots = roots[:_SAMPLE_ROOTS]

        # BFS descendants of the seed roots, capped. Roots themselves are always
        # included and do not count against the descendant budget.
        included: dict[str, None] = {r: None for r in seed_roots}
        frontier = list(seed_roots)
        while frontier and len(included) - len(seed_roots) < _SAMPLE_MAX_DESCENDANTS:
            nxt: list[str] = []
            for uri in frontier:
                for child in children_of.get(uri, ()):
                    if child in included:
                        continue
                    included[child] = None
                    nxt.append(child)
                    if len(included) - len(seed_roots) >= _SAMPLE_MAX_DESCENDANTS:
                        break
                if len(included) - len(seed_roots) >= _SAMPLE_MAX_DESCENDANTS:
                    break
            frontier = nxt

        sampled: list[dict] = []
        for uri in included:
            entry = dict(classes[uri])
            # Keep only subclass edges that stay inside the sample, so seed roots
            # remain roots (their real foundational parents are out of scope).
            entry["sub_class_of"] = [p for p in entry["sub_class_of"] if p in included]
            sampled.append(entry)
        sampled.sort(key=lambda c: (c["label"] or c["uri"]).lower())

        return {
            "ontology_id": ontology_id,
            "graph_uri": graph_uri,
            "classes": sampled,
            "object_properties": [],
            "datatype_properties": [],
        }

    def get_ontology_overview(
        self,
        ontology_id: str,
        namespace: str | None = None,
        *,
        sample: bool = False,
    ) -> dict | None:
        """Enumerate the classes + properties persisted in one named graph.

        Two SPARQL queries against the ontology's named graph: one for
        classes (``owl:Class`` / ``rdfs:Class``) and one for properties
        (``owl:ObjectProperty`` / ``owl:DatatypeProperty`` / generic
        ``rdf:Property``). Rows are aggregated by subject in Python so a
        subject with multiple labels/domains/ranges collapses to a single
        summary (first value wins). Generic ``rdf:Property`` rows are
        bucketed by range: an ``xsd:``/``rdfs:Literal`` range → datatype
        property, anything else → object property (relationship).

        When ``sample`` is set, return only a bounded taxonomy seed instead of
        the full ontology: the ``_SAMPLE_ROOTS`` largest-subtree root classes
        plus up to ``_SAMPLE_MAX_DESCENDANTS`` of their descendants, and NO
        properties. This backs the Graph view's initial render, which
        materializes a bounded set of nodes and expands the rest on demand —
        so a multi-thousand-class ontology no longer fans out one detail call
        per class. ``subClassOf`` edges pointing outside the sampled set are
        dropped so the returned roots stay roots on the client.
        """
        graph_uri = _ontology_graph_uri(ontology_id, self._namespace)
        gi = _iri(graph_uri)

        if sample:
            return self._sample_overview(ontology_id, graph_uri, gi)

        # ── Classes ──────────────────────────────────────────────────
        class_q = self._overview_class_query(gi)
        classes: dict[str, dict] = {}
        for b in _sparql_query(class_q).get("results", {}).get("bindings", []):
            self._accumulate_class_row(classes, b)

        # ── Properties (object + datatype + generic) ─────────────────
        prop_q = f"""
        PREFIX rdf: <{RDF}>
        PREFIX rdfs: <{RDFS}>
        PREFIX owl: <{OWL}>
        SELECT ?p ?type ?label ?domain ?range WHERE {{
          GRAPH {gi} {{
            ?p rdf:type ?type .
            VALUES ?type {{ owl:ObjectProperty owl:DatatypeProperty rdf:Property owl:AnnotationProperty }}
            OPTIONAL {{ ?p rdfs:label ?label }}
            OPTIONAL {{ ?p rdfs:domain ?domain }}
            OPTIONAL {{ ?p rdfs:range ?range }}
          }}
        }}
        """
        props: dict[str, dict] = {}
        for b in _sparql_query(prop_q).get("results", {}).get("bindings", []):
            uri = b["p"]["value"]
            entry = props.setdefault(
                uri,
                {"uri": uri, "label": None, "domain": None, "range": None, "types": set()},
            )
            entry["types"].add(b["type"]["value"])
            if entry["label"] is None and "label" in b:
                entry["label"] = b["label"]["value"]
            # Only keep named-IRI domains/ranges (skip blank-node class expressions).
            if entry["domain"] is None and "domain" in b and b["domain"]["type"] == "uri":
                entry["domain"] = b["domain"]["value"]
            if entry["range"] is None and "range" in b and b["range"]["type"] == "uri":
                entry["range"] = b["range"]["value"]

        object_properties: list[dict] = []
        datatype_properties: list[dict] = []
        for entry in props.values():
            types = entry.pop("types")
            is_object = OWL + "ObjectProperty" in types
            is_datatype = OWL + "DatatypeProperty" in types
            # Annotation properties (e.g. the grounding `matchConfidence` marker)
            # are metadata, not relationships or attributes. An ingested
            # annotation property carries both owl:AnnotationProperty and a
            # generic rdf:Property stamp (store_property); with no range it would
            # otherwise fall through to object_properties, overcounting the
            # "Relationships" badge by one with an empty domain/range row (#33).
            # Exclude it from BOTH buckets unless it is also explicitly declared
            # an object or datatype property.
            if OWL + "AnnotationProperty" in types and not is_object and not is_datatype:
                continue
            rng = entry.get("range") or ""
            is_literal_range = rng.startswith(_XSD) or rng == RDFS + "Literal"
            if is_datatype or (not is_object and is_literal_range):
                datatype_properties.append(entry)
            else:
                object_properties.append(entry)

        return {
            "ontology_id": ontology_id,
            "graph_uri": graph_uri,
            "classes": sorted(classes.values(), key=lambda c: (c["label"] or c["uri"]).lower()),
            "object_properties": sorted(object_properties, key=lambda p: (p["label"] or p["uri"]).lower()),
            "datatype_properties": sorted(datatype_properties, key=lambda p: (p["label"] or p["uri"]).lower()),
        }

    def get_namespace_schema(
        self,
        namespace: str,
        *,
        max_results: int = 100,
        include_properties: bool = True,
    ) -> dict:
        """List all OWL classes and properties across all ontologies in a namespace.

        Queries all named graphs under the namespace prefix.
        """
        validate_namespace_name(namespace)
        ns_prefix = _namespace_graph_prefix(namespace)

        # Classes
        class_q = f"""
        PREFIX rdf: <{RDF}>
        PREFIX rdfs: <{RDFS}>
        PREFIX owl: <{OWL}>
        SELECT ?classUri ?classLabel ?classDescription ?parentClass WHERE {{
          GRAPH ?g {{
            ?classUri rdf:type ?t .
            VALUES ?t {{ owl:Class rdfs:Class }}
            OPTIONAL {{ ?classUri rdfs:label ?classLabel }}
            OPTIONAL {{ ?classUri rdfs:comment ?classDescription }}
            OPTIONAL {{ ?classUri rdfs:subClassOf ?parentClass }}
          }}
          FILTER(STRSTARTS(STR(?g), "{ns_prefix}"))
        }}
        ORDER BY ?classLabel
        LIMIT {max_results}
        """

        classes_raw = _sparql_query(class_q).get("results", {}).get("bindings", [])
        classes = [
            {
                "uri": b["classUri"]["value"],
                "label": b.get("classLabel", {}).get("value", ""),
                "description": b.get("classDescription", {}).get("value", ""),
                "parentClass": b.get("parentClass", {}).get("value", ""),
                "properties": [],
            }
            for b in classes_raw
        ]

        # Properties (grouped by domain)
        if include_properties and classes:
            prop_q = f"""
            PREFIX rdf: <{RDF}>
            PREFIX rdfs: <{RDFS}>
            PREFIX owl: <{OWL}>
            SELECT ?propUri ?propLabel ?domain ?range ?propDescription WHERE {{
              GRAPH ?g {{
                ?propUri rdf:type owl:DatatypeProperty .
                OPTIONAL {{ ?propUri rdfs:label ?propLabel }}
                OPTIONAL {{ ?propUri rdfs:domain ?domain }}
                OPTIONAL {{ ?propUri rdfs:range ?range }}
                OPTIONAL {{ ?propUri rdfs:comment ?propDescription }}
              }}
              FILTER(STRSTARTS(STR(?g), "{ns_prefix}"))
            }}
            ORDER BY ?propLabel
            LIMIT {max_results * 5}
            """
            props_raw = _sparql_query(prop_q).get("results", {}).get("bindings", [])

            # Group by domain
            props_by_domain: dict[str, list[dict]] = {}
            for b in props_raw:
                domain = b.get("domain", {}).get("value", "")
                props_by_domain.setdefault(domain, []).append(
                    {
                        "uri": b["propUri"]["value"],
                        "label": b.get("propLabel", {}).get("value", ""),
                        "range": b.get("range", {}).get("value", ""),
                        "description": b.get("propDescription", {}).get("value", ""),
                    }
                )

            # Attach properties to classes
            for cls in classes:
                cls["properties"] = props_by_domain.get(cls["uri"], [])

        return {
            "classes": classes,
            "namespace": namespace,
            "ontologyVersion": "",
        }
