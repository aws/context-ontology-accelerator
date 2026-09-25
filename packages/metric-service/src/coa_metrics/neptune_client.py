# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune SPARQL client for :GovernedMetric subclass CRUD.

Each metric is an owl:Class that is rdfs:subClassOf :GovernedMetric in the
namespace's ontology named graph — the same named graph used by the
ontology-engine for classes and properties.

Graph URI scheme (identical to ontology-engine):
  {NDB_GRAPH_URI_BASE}/{namespace}/{percent-encoded ontology_id}

The ontology_id for governed metrics is the shared constant
``GOVERNED_METRICS_ONTOLOGY_ID`` from ``coa_common``.

Uses SigV4 IAM auth and httpx for transport.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import boto3
import botocore.auth
import botocore.awsrequest
import httpx
import structlog
from coa_common import GOVERNED_METRICS_ONTOLOGY_ID
from coa_common.constants import URN_PREFIX

logger = structlog.get_logger(__name__)

# ── Configuration ───────────────────────────────────────────────────────

NEPTUNE_ENDPOINT = os.getenv("NEPTUNE_ENDPOINT", "")
NEPTUNE_REGION = os.getenv("AWS_REGION", "us-east-1")
NEPTUNE_TIMEOUT = float(os.getenv("NEPTUNE_TIMEOUT", "30"))
# Split timeout: fast connect (TCP/TLS handshake), NEPTUNE_TIMEOUT read budget.
NEPTUNE_HTTP_TIMEOUT = httpx.Timeout(connect=2.0, read=NEPTUNE_TIMEOUT, write=NEPTUNE_TIMEOUT, pool=NEPTUNE_TIMEOUT)


# ── Namespace prefixes ──────────────────────────────────────────────────

COA = f"urn:{URN_PREFIX}:"
OWL = "http://www.w3.org/2002/07/owl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
XSD = "http://www.w3.org/2001/XMLSchema#"
COA_VOCAB = f"urn:{URN_PREFIX}:vocab#"

# ── Graph URI resolution (mirrors ontology-engine neptune_db_graph.py) ──

DEFAULT_NAMESPACE = os.getenv("DYNAMODB_DEFAULT_NAMESPACE", "default")


def _resolve_graph_base() -> str:
    """Return the base URL under which per-namespace graph URIs live."""
    base = os.getenv("NDB_GRAPH_URI_BASE", "").strip().rstrip("/")
    if base:
        return base
    legacy = os.getenv("NDB_GRAPH_URI", "").strip()
    if legacy:
        if "/graph/" in legacy:
            legacy = legacy.rsplit("/graph/", 1)[0]
        return legacy.rstrip("/")
    return "https://ontology-workbench.local"


def _named_graph(namespace: str) -> str:
    """Return the named graph URI for the governed-metrics ontology in a namespace.

    Uses the same scheme as ontology-engine:
    {base}/{namespace}/{percent-encoded ontology_id}
    """
    ns = namespace or DEFAULT_NAMESPACE
    return f"{_resolve_graph_base()}/{ns}/{quote(GOVERNED_METRICS_ONTOLOGY_ID, safe='')}"


def _metric_uri(namespace: str, name: str) -> str:
    """Return the URI for a metric within a namespace."""
    return f"{COA}{namespace}:metric:{name}"


# ── IRI safety ──────────────────────────────────────────────────────────

_IRI_FORBIDDEN = re.compile(r'[\s<>"{}|\\^`]')


class InvalidMetricDefinitionError(ValueError):
    """A metric cannot be represented safely as deterministic SPARQL."""


def _iri(s: str) -> str:
    """Render a string as a SPARQL IRI reference <uri> with injection protection."""
    if not isinstance(s, str) or not s:
        raise InvalidMetricDefinitionError("IRI must be a non-empty string")
    if _IRI_FORBIDDEN.search(s):
        raise InvalidMetricDefinitionError(f"URI contains characters forbidden in IRI references: {s!r}")
    return f"<{s}>"


def _resolved_class_uri(value: object, binding_type: object) -> str:
    """Validate one Neptune URI binding against the metric write-path contract."""
    if binding_type != "uri" or not isinstance(value, str):
        raise ValueError("ontology resolver class binding must contain a URI value")
    parsed = urlparse(value)
    is_http_uri = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    urn_parts = parsed.path.split(":", 1) if parsed.scheme == "urn" else []
    is_urn = len(urn_parts) == 2 and all(urn_parts)
    if not is_http_uri and not is_urn:
        raise ValueError("ontology resolver returned an unsupported class URI")
    try:
        _iri(value)
    except InvalidMetricDefinitionError as exc:
        raise ValueError("ontology resolver returned a malformed class URI") from exc
    return value


def _esc(s: str) -> str:
    """Escape a SPARQL string literal per SPARQL 1.1 §19.7."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


# ── SigV4 helper ────────────────────────────────────────────────────────


def _sign(method: str, url: str, body: str | bytes) -> dict[str, str]:
    """Return SigV4 headers for a Neptune HTTP request."""
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials not available")
    creds = credentials.get_frozen_credentials()
    req = botocore.awsrequest.AWSRequest(method=method, url=url, data=body)
    botocore.auth.SigV4Auth(creds, "neptune-db", NEPTUNE_REGION).add_auth(req)
    return dict(req.headers.items())


# ── SPARQL transport ────────────────────────────────────────────────────


def _sparql_query(query: str) -> dict[str, Any]:
    """Execute a SPARQL SELECT/ASK query and return the JSON result."""
    if not NEPTUNE_ENDPOINT:
        raise RuntimeError("NEPTUNE_ENDPOINT not set")
    url = f"{NEPTUNE_ENDPOINT.rstrip('/')}/sparql"
    body = urlencode({"query": query})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/sparql-results+json",
    }
    headers.update(_sign("POST", url, body))
    t0 = time.perf_counter()
    with httpx.Client(timeout=NEPTUNE_HTTP_TIMEOUT) as client:
        resp = client.post(url, content=body, headers=headers)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code >= 400:
            logger.error(
                "SPARQL query failed", status=resp.status_code, elapsed_ms=round(elapsed_ms), response=resp.text[:1000]
            )
            resp.raise_for_status()
        logger.debug("SPARQL query", elapsed_ms=round(elapsed_ms), body_bytes=len(body))
        return resp.json()


def _sparql_update(update: str) -> None:
    """Execute a SPARQL UPDATE (INSERT/DELETE)."""
    if not NEPTUNE_ENDPOINT:
        raise RuntimeError("NEPTUNE_ENDPOINT not set")
    url = f"{NEPTUNE_ENDPOINT.rstrip('/')}/sparql"
    body = urlencode({"update": update})
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    headers.update(_sign("POST", url, body))
    t0 = time.perf_counter()
    with httpx.Client(timeout=NEPTUNE_HTTP_TIMEOUT) as client:
        resp = client.post(url, content=body, headers=headers)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code >= 400:
            logger.error(
                "SPARQL update failed", status=resp.status_code, elapsed_ms=round(elapsed_ms), response=resp.text[:1000]
            )
            resp.raise_for_status()
        logger.debug("SPARQL update", elapsed_ms=round(elapsed_ms), body_bytes=len(body))


# ── Data classes ────────────────────────────────────────────────────────


@dataclass
class MetricDialect:
    """A single dialect + expression pair."""

    dialect: str
    expression: str


@dataclass
class MetricAiContext:
    """AI context for metric discoverability."""

    synonyms: list[str] = field(default_factory=list)
    instructions: str = ""
    examples: list[str] = field(default_factory=list)


@dataclass
class MetricDefinition:
    """Full metric definition as stored in Neptune."""

    name: str
    description: str
    expression_dialects: list[MetricDialect]
    data_source_id: str
    source_table: str
    default_time_grain: str | None = None
    unit: str | None = None
    return_type: str | None = None
    ai_context: MetricAiContext | None = None
    ontology_concepts: list[str] = field(default_factory=list)
    defined_by: str | None = None
    effective_from: str | None = None


# ── Neptune Metric Client ───────────────────────────────────────────────


class MetricNeptuneClient:
    """SPARQL client for metric CRUD operations in Neptune.

    Metrics live in the same per-ontology named graph scheme as
    ontology-engine classes/properties. The ontology_id is
    ``GOVERNED_METRICS_ONTOLOGY_ID``.
    """

    def __init__(self) -> None:
        """Initialize the client with an empty per-namespace seeding cache."""
        self._seeded_namespaces: set[str] = set()

    def _ensure_base_vocabulary(self, namespace: str) -> None:
        """Ensure the :GovernedMetric base OWL class exists in the namespace graph."""
        if namespace in self._seeded_namespaces:
            return

        graph = _named_graph(namespace)
        governed_metric_uri = COA_VOCAB + "GovernedMetric"

        ask_query = f"""
        ASK {{
          GRAPH {_iri(graph)} {{
            {_iri(governed_metric_uri)} {_iri(RDF + "type")} {_iri(OWL + "Class")} .
          }}
        }}
        """
        result = _sparql_query(ask_query)
        if result.get("boolean", False):
            self._seeded_namespaces.add(namespace)
            return

        seed_triples = [
            f"{_iri(governed_metric_uri)} {_iri(RDF + 'type')} {_iri(OWL + 'Class')} .",
            f'{_iri(governed_metric_uri)} {_iri(RDFS + "label")} "Governed Metric" .',
            f'{_iri(governed_metric_uri)} {_iri(RDFS + "comment")} "Base class for all governed business metrics." .',
            # Declare governedMetricFor as an owl:ObjectProperty
            f"{_iri(COA_VOCAB + 'governedMetricFor')} {_iri(RDF + 'type')} {_iri(OWL + 'ObjectProperty')} .",
            f'{_iri(COA_VOCAB + "governedMetricFor")} {_iri(RDFS + "label")} "governed metric for" .',
            (
                f"{_iri(COA_VOCAB + 'governedMetricFor')} {_iri(RDFS + 'comment')}"
                f' "Links a GovernedMetric to the OWL class it measures." .'
            ),
            f"{_iri(COA_VOCAB + 'governedMetricFor')} {_iri(RDFS + 'domain')} {_iri(governed_metric_uri)} .",
            f"{_iri(COA_VOCAB + 'governedMetricFor')} {_iri(RDFS + 'range')} {_iri(OWL + 'Class')} .",
        ]
        sparql = f"INSERT DATA {{ GRAPH {_iri(graph)} {{\n  {chr(10).join(seed_triples)}\n}} }}"
        _sparql_update(sparql)
        self._seeded_namespaces.add(namespace)
        logger.info("Seeded GovernedMetric base class", namespace=namespace)

    def resolve_class_uris(self, namespace: str, concept_names: list[str]) -> list[str]:
        """Resolve class names/labels to full URIs by querying all namespace graphs.

        For each concept name: if it's already a valid IRI (starts with http/https/urn),
        use it as-is. Otherwise, batch-search for owl:Class entities with matching
        rdfs:label across all named graphs under this namespace prefix.

        Returns resolved URIs (unresolvable names are dropped with a warning).
        """
        if not concept_names:
            return []

        resolved: list[str] = []
        to_resolve: list[str] = []

        for concept in concept_names:
            if not concept:
                continue
            if concept.startswith(("http://", "https://", "urn:")):
                _iri(concept)
                resolved.append(concept)
            else:
                to_resolve.append(concept)

        if not to_resolve:
            return resolved

        # Batch resolution via VALUES clause. Query/response failures must
        # propagate so callers do not checkpoint a definition with silently
        # dropped ontology links during a Neptune outage.
        ns_prefix = f"{_resolve_graph_base()}/{namespace}/"
        values = " ".join(f'"{_esc(label)}"' for label in to_resolve)
        query = f"""
        SELECT ?label ?cls WHERE {{
          VALUES ?label {{ {values} }}
          GRAPH ?g {{
            ?cls {_iri(RDF + "type")} {_iri(OWL + "Class")} .
            ?cls {_iri(RDFS + "label")} ?label .
          }}
          FILTER(STRSTARTS(STR(?g), "{_esc(ns_prefix)}"))
        }}
        """
        result = _sparql_query(query)
        bindings = result["results"]["bindings"]
        if not isinstance(bindings, list):
            raise ValueError("ontology concept query bindings must be a list")
        found: dict[str, str] = {}
        for binding in bindings:
            label = binding["label"]["value"]
            class_binding = binding["cls"]
            if not isinstance(class_binding, dict):
                raise ValueError("ontology resolver class binding must be an object")
            class_uri = _resolved_class_uri(class_binding.get("value"), class_binding.get("type"))
            if label not in found:
                found[label] = class_uri
        for label in to_resolve:
            if label in found:
                resolved.append(found[label])
            else:
                logger.warning("ontology_concept_not_found", concept=label, namespace=namespace)

        return resolved

    def validate_metric_definition(self, namespace: str, metric: MetricDefinition) -> None:
        """Validate the complete local write shape without performing Neptune I/O."""
        self._build_triples(namespace, metric)

    def create_metric(self, namespace: str, metric: MetricDefinition) -> None:
        """Write metric triples to Neptune via INSERT DATA."""
        graph = _named_graph(namespace)
        triples = self._build_triples(namespace, metric)
        self._ensure_base_vocabulary(namespace)
        sparql = f"INSERT DATA {{ GRAPH {_iri(graph)} {{\n  {chr(10).join(triples)}\n}} }}"
        _sparql_update(sparql)
        logger.info("Metric created", namespace=namespace, name=metric.name)

    def get_metric(self, namespace: str, name: str) -> MetricDefinition | None:
        """Read a single metric from Neptune. Returns None if not found."""
        graph = _named_graph(namespace)
        uri = _metric_uri(namespace, name)
        query = f"""
        SELECT ?p ?o WHERE {{
          GRAPH {_iri(graph)} {{
            {_iri(uri)} ?p ?o .
          }}
        }}
        """
        result = _sparql_query(query)
        bindings = result.get("results", {}).get("bindings", [])
        if not bindings:
            return None
        return self._parse_metric(name, bindings)

    def list_metrics(
        self,
        namespace: str,
        data_source_id: str | None = None,
        source_table: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MetricDefinition]:
        """List metrics in a namespace with optional filters."""
        graph = _named_graph(namespace)
        filters: list[str] = []
        if data_source_id:
            filters.append(f'?s {_iri(COA_VOCAB + "dataSourceId")} "{_esc(data_source_id)}" .')
        if source_table:
            filters.append(f'?s {_iri(COA_VOCAB + "sourceTable")} "{_esc(source_table)}" .')
        filter_clause = "\n    ".join(filters)

        query = f"""
        SELECT DISTINCT ?s WHERE {{
          GRAPH {_iri(graph)} {{
            ?s {_iri(RDF + "type")} {_iri(OWL + "Class")} .
            ?s {_iri(RDFS + "subClassOf")} {_iri(COA_VOCAB + "GovernedMetric")} .
            {filter_clause}
          }}
        }}
        ORDER BY ?s
        LIMIT {limit}
        OFFSET {offset}
        """
        result = _sparql_query(query)
        bindings = result.get("results", {}).get("bindings", [])

        metrics: list[MetricDefinition] = []
        for binding in bindings:
            metric_uri_val = binding["s"]["value"]
            name = metric_uri_val.rsplit(":", 1)[-1]
            metric = self.get_metric(namespace, name)
            if metric:
                metrics.append(metric)
        return metrics

    def get_metrics_bulk(self, namespace: str, names: list[str] | None = None) -> list[MetricDefinition]:
        """Fetch full metric definitions in a single SPARQL round-trip.

        Unlike ``list_metrics()`` + per-metric ``get_metric()`` (N+1 — one
        query for URIs, then one additional query per metric to fetch its
        properties), this issues ONE query that returns every ``?p ?o``
        triple for every matching metric subject, then groups the results
        by subject client-side. At 10-50ms per Neptune round-trip (see LLD),
        the N+1 path is what causes export timeouts at 100+ metrics — this
        collapses N+1 round-trips into 1.

        Pass ``names`` to fetch a specific subset (used for scoped exports);
        omit it to fetch every governed metric in the namespace.

        Intentionally separate from ``list_metrics()``, which other callers
        (paginated list, bulk-delete-by-filter) use unchanged for URI-only
        listing with LIMIT/OFFSET semantics.
        """
        graph = _named_graph(namespace)
        values_clause = ""
        if names:
            values = " ".join(_iri(_metric_uri(namespace, n)) for n in names)
            values_clause = f"VALUES ?s {{ {values} }}"

        query = f"""
        SELECT ?s ?p ?o WHERE {{
          GRAPH {_iri(graph)} {{
            {values_clause}
            ?s {_iri(RDF + "type")} {_iri(OWL + "Class")} .
            ?s {_iri(RDFS + "subClassOf")} {_iri(COA_VOCAB + "GovernedMetric")} .
            ?s ?p ?o .
          }}
        }}
        ORDER BY ?s
        """
        result = _sparql_query(query)
        bindings = result.get("results", {}).get("bindings", [])

        grouped: dict[str, list[dict[str, Any]]] = {}
        for binding in bindings:
            subject = binding["s"]["value"]
            grouped.setdefault(subject, []).append(binding)

        metrics: list[MetricDefinition] = []
        for uri, subject_bindings in grouped.items():
            name = uri.rsplit(":", 1)[-1]
            metrics.append(self._parse_metric(name, subject_bindings))
        return metrics

    def update_metric(self, namespace: str, name: str, metric: MetricDefinition) -> None:
        """Atomic delete + insert for a metric (full replacement)."""
        graph = _named_graph(namespace)
        uri = _metric_uri(namespace, name)
        triples = self._build_triples(namespace, metric)

        sparql = f"""
        WITH {_iri(graph)}
        DELETE {{ {_iri(uri)} ?p ?o }}
        INSERT {{
          {chr(10).join(triples)}
        }}
        WHERE {{ {_iri(uri)} ?p ?o }}
        """
        _sparql_update(sparql)
        logger.info("Metric updated", namespace=namespace, name=name)

    def delete_metric(self, namespace: str, name: str) -> bool:
        """Delete all triples for a metric. Returns True if metric existed."""
        graph = _named_graph(namespace)
        uri = _metric_uri(namespace, name)

        exists = self.get_metric(namespace, name) is not None
        if not exists:
            return False

        sparql = f"""
        WITH {_iri(graph)}
        DELETE {{ {_iri(uri)} ?p ?o }}
        WHERE {{ {_iri(uri)} ?p ?o }}
        """
        _sparql_update(sparql)
        logger.info("Metric deleted", namespace=namespace, name=name)
        return True

    def delete_all_metrics_for_namespace(self, namespace: str) -> int:
        """Delete ALL governed-metric triples from the namespace's metric graph.

        Authoritative, namespace-scoped deletion — does NOT depend on enumerating
        metric names from DynamoDB or calling get_metric() per metric. Idempotent:
        safe to call even if the graph is already empty or the namespace never had
        metrics.

        Returns: count of metric subjects deleted (0 if graph was already empty).
        """
        graph = _named_graph(namespace)

        # First count how many metrics exist
        count_query = f"""
        SELECT (COUNT(DISTINCT ?s) as ?count) WHERE {{
          GRAPH {_iri(graph)} {{
            ?s {_iri(RDF + "type")} {_iri(OWL + "Class")} .
            ?s {_iri(RDFS + "subClassOf")} {_iri(COA_VOCAB + "GovernedMetric")} .
          }}
        }}
        """
        result = _sparql_query(count_query)
        bindings = result.get("results", {}).get("bindings", [])
        count = int(bindings[0]["count"]["value"]) if bindings else 0

        if count == 0:
            logger.info("No metrics to delete", namespace=namespace)
            return 0

        # Delete all triples where subject is a GovernedMetric
        delete_sparql = f"""
        WITH {_iri(graph)}
        DELETE {{ ?s ?p ?o }}
        WHERE {{
          ?s {_iri(RDF + "type")} {_iri(OWL + "Class")} .
          ?s {_iri(RDFS + "subClassOf")} {_iri(COA_VOCAB + "GovernedMetric")} .
          ?s ?p ?o .
        }}
        """
        _sparql_update(delete_sparql)
        logger.info("All metrics deleted", namespace=namespace, count=count)
        return count

    # ── Private helpers ─────────────────────────────────────────────────

    def _build_triples(self, namespace: str, metric: MetricDefinition) -> list[str]:
        """Build the RDF triples for a metric definition."""
        uri = _metric_uri(namespace, metric.name)
        triples = [
            f"{_iri(uri)} {_iri(RDF + 'type')} {_iri(OWL + 'Class')} .",
            f"{_iri(uri)} {_iri(RDFS + 'subClassOf')} {_iri(COA_VOCAB + 'GovernedMetric')} .",
            f'{_iri(uri)} {_iri(RDFS + "label")} "{_esc(metric.name)}" .',
            f'{_iri(uri)} {_iri(RDFS + "comment")} "{_esc(metric.description)}" .',
            f'{_iri(uri)} {_iri(COA_VOCAB + "dataSourceId")} "{_esc(metric.data_source_id)}" .',
            f'{_iri(uri)} {_iri(COA_VOCAB + "sourceTable")} "{_esc(metric.source_table)}" .',
        ]

        # Expression dialects — stored as JSON string
        dialects_json = json.dumps(
            [{"dialect": d.dialect, "expression": d.expression} for d in metric.expression_dialects]
        )
        triples.append(f'{_iri(uri)} {_iri(COA_VOCAB + "expressionDialects")} "{_esc(dialects_json)}" .')

        if metric.default_time_grain:
            triples.append(f'{_iri(uri)} {_iri(COA_VOCAB + "defaultTimeGrain")} "{_esc(metric.default_time_grain)}" .')
        if metric.unit:
            triples.append(f'{_iri(uri)} {_iri(COA_VOCAB + "unit")} "{_esc(metric.unit)}" .')
        if metric.return_type:
            triples.append(f'{_iri(uri)} {_iri(COA_VOCAB + "returnType")} "{_esc(metric.return_type)}" .')
        if metric.defined_by:
            triples.append(f'{_iri(uri)} {_iri(COA_VOCAB + "definedBy")} "{_esc(metric.defined_by)}" .')
        if metric.effective_from:
            effective_literal = f'"{_esc(metric.effective_from)}"^^{_iri(XSD + "date")}'
            triples.append(f"{_iri(uri)} {_iri(COA_VOCAB + 'effectiveFrom')} {effective_literal} .")

        # AI context — stored as JSON string
        if metric.ai_context:
            ai_json = json.dumps(
                {
                    "synonyms": metric.ai_context.synonyms,
                    "instructions": metric.ai_context.instructions,
                    "examples": metric.ai_context.examples,
                }
            )
            triples.append(f'{_iri(uri)} {_iri(COA_VOCAB + "aiContext")} "{_esc(ai_json)}" .')

        # Object property edges to referenced OWL classes (IRI → IRI)
        for class_uri in metric.ontology_concepts:
            if class_uri and class_uri.startswith(("http://", "https://", "urn:")):
                triples.append(f"{_iri(uri)} {_iri(COA_VOCAB + 'governedMetricFor')} {_iri(class_uri)} .")

        return triples

    def _parse_metric(self, name: str, bindings: list[dict[str, Any]]) -> MetricDefinition:
        """Parse SPARQL SELECT bindings into a MetricDefinition."""
        props: dict[str, Any] = {}
        governed_for: list[str] = []

        for binding in bindings:
            p = binding["p"]["value"]
            o_val = binding["o"]["value"]

            if p == RDFS + "comment":
                props["description"] = o_val
            elif p == COA_VOCAB + "dataSourceId":
                props["data_source_id"] = o_val
            elif p == COA_VOCAB + "sourceTable":
                props["source_table"] = o_val
            elif p == COA_VOCAB + "expressionDialects":
                dialects_raw = json.loads(o_val)
                props["expression_dialects"] = [
                    MetricDialect(dialect=d["dialect"], expression=d["expression"]) for d in dialects_raw
                ]
            elif p == COA_VOCAB + "defaultTimeGrain":
                props["default_time_grain"] = o_val
            elif p == COA_VOCAB + "unit":
                props["unit"] = o_val
            elif p == COA_VOCAB + "returnType":
                props["return_type"] = o_val
            elif p == COA_VOCAB + "definedBy":
                props["defined_by"] = o_val
            elif p == COA_VOCAB + "effectiveFrom":
                props["effective_from"] = o_val
            elif p == COA_VOCAB + "aiContext":
                ai_raw = json.loads(o_val)
                props["ai_context"] = MetricAiContext(
                    synonyms=ai_raw.get("synonyms", []),
                    instructions=ai_raw.get("instructions", ""),
                    examples=ai_raw.get("examples", []),
                )
            elif p == COA_VOCAB + "governedMetricFor":
                # Object property — value is an IRI
                governed_for.append(o_val)

        return MetricDefinition(
            name=name,
            description=props.get("description", ""),
            expression_dialects=props.get("expression_dialects", []),
            data_source_id=props.get("data_source_id", ""),
            source_table=props.get("source_table", ""),
            default_time_grain=props.get("default_time_grain"),
            unit=props.get("unit"),
            return_type=props.get("return_type"),
            ai_context=props.get("ai_context"),
            ontology_concepts=governed_for,
            defined_by=props.get("defined_by"),
            effective_from=props.get("effective_from"),
        )
