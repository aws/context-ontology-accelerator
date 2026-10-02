# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune Analytics graph-native storage backend.

Graph model:
  (Ontology)-[:DEFINES]->(Class)-[:HAS_EMBEDDING]->(Embedding)
  (Ontology)-[:DEFINES]->(Property)-[:HAS_EMBEDDING]->(Embedding)
  (Class)-[:SUBCLASS_OF]->(Class)
  (Property)-[:HAS_DOMAIN]->(Class)
  (Property)-[:HAS_RANGE]->(Class)
  (Ontology)-[:IMPORTS]->(Ontology)

Node IDs:
  Ontology  → ontology URI
  Class     → class URI
  Property  → property URI
  Embedding → emb/{entity_uri}/{embedding_type}/{model_id}

Client configuration (env vars):
  NA_GRAPH_ID          - Graph identifier (required)
  NA_ENDPOINT          - Override endpoint URL (local container only)
  NA_REGION            - AWS region (default: us-east-1)
  NA_AUTH_DISABLED      - Set "true" to disable SigV4 + host prefix (local)
  VSS_DIMENSION        - Vector index dimension (default: 1024)
  ONTOLOGY_STORAGE_PATH - Local file storage for uploaded Turtle files
"""

import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

NA_GRAPH_ID = os.getenv("NA_GRAPH_ID", "local")
NA_ENDPOINT = os.getenv("NA_ENDPOINT", "")
NA_REGION = os.getenv("NA_REGION", "us-east-1")
NA_AUTH_DISABLED = os.getenv("NA_AUTH_DISABLED", "false").lower() == "true"
ONTOLOGY_STORAGE_PATH = os.getenv("ONTOLOGY_STORAGE_PATH", "./data/ontologies")
VSS_DIMENSION = int(os.getenv("VSS_DIMENSION", "1024"))
NA_MAX_RETRIES = int(os.getenv("NA_MAX_RETRIES", "5"))
# Bound the per-request read so a hung Neptune Analytics query can't block the
# accept worker forever (#456/#466: the daemon thread otherwise hangs with no
# timeout). 0 / unset-to-0 restores the old unbounded behavior if ever needed.
NA_READ_TIMEOUT = int(os.getenv("NA_READ_TIMEOUT", "120"))
DEFAULT_NAMESPACE = os.getenv("DYNAMODB_DEFAULT_NAMESPACE", "default")

log = logging.getLogger(__name__)

_client = None


def _ns(namespace: str | None) -> str:
    """Resolve namespace value (None → default)."""
    return namespace or DEFAULT_NAMESPACE


def _nid(original_id: str, namespace: str | None) -> str:
    """Return the namespaced graph ID for a logical ID.

    For the default namespace the original ID is used unchanged (backward
    compat with existing graphs). For non-default namespaces the ID is
    prefixed with ``{ns}::`` so collisions with other namespaces are
    impossible even at the vertex-identity level.
    """
    ns = _ns(namespace)
    if ns == "default":
        return original_id
    return f"{ns}::{original_id}"


def _strip_nid(nid: str, namespace: str | None) -> str:
    """Reverse of :func:`_nid` — return the logical ID for a namespaced ID."""
    ns = _ns(namespace)
    if ns == "default":
        return nid
    prefix = f"{ns}::"
    if nid.startswith(prefix):
        return nid[len(prefix) :]
    return nid


def _get_client():
    global _client
    if _client is not None:
        return _client

    kwargs = {"service_name": "neptune-graph", "region_name": NA_REGION}
    if NA_ENDPOINT:
        kwargs["endpoint_url"] = NA_ENDPOINT
    # AWS best practice: disable SDK retries for execute_query — NA rejects
    # retried SDK requests with UnprocessableException. We retry ourselves.
    # read_timeout is bounded (NA_READ_TIMEOUT, default 120s) so a hung query
    # surfaces as a timeout the accept worker can fail/retry, rather than
    # blocking the daemon thread forever (#456/#466). Set NA_READ_TIMEOUT=0 to
    # restore the previous unbounded behavior.
    boto_cfg = {
        "retries": {"total_max_attempts": 1, "mode": "standard"},
        "read_timeout": NA_READ_TIMEOUT if NA_READ_TIMEOUT > 0 else None,
    }
    if NA_AUTH_DISABLED:
        from botocore import UNSIGNED

        boto_cfg["signature_version"] = UNSIGNED
    kwargs["config"] = BotoConfig(**boto_cfg)

    _client = boto3.client(**kwargs)

    # Local container doesn't support the SDK's host-prefix routing
    # (graphId.endpoint), so strip it.
    if NA_AUTH_DISABLED:

        def _strip_host_prefix(request, **_):
            prefix = f"{NA_GRAPH_ID}."
            proto, rest = request.url.split("://", 1)
            if rest.startswith(prefix):
                request.url = f"{proto}://{rest[len(prefix) :]}"

        _client.meta.events.register("before-send.neptune-graph.*", _strip_host_prefix)

    return _client


def _oc_query(query: str, params: dict | None = None) -> dict:
    kwargs: dict[str, Any] = {
        "graphIdentifier": NA_GRAPH_ID,
        "queryString": " ".join(query.split()),
        "language": "OPEN_CYPHER",
    }
    if params:
        kwargs["parameters"] = params
    client = _get_client()
    for attempt in range(NA_MAX_RETRIES):
        try:
            r = client.execute_query(**kwargs)
            return json.loads(r["payload"].read())
        except ClientError as e:
            code = e.response["Error"].get("Code", "")
            if code in ("UnprocessableException", "ConflictException") and attempt < NA_MAX_RETRIES - 1:
                wait = 0.1 * (2**attempt)
                log.warning("NA %s (attempt %d/%d), retrying in %.1fs", code, attempt + 1, NA_MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Neptune Analytics query failed after {NA_MAX_RETRIES} attempts")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pad_vector(vec: list[float]) -> list[float]:
    if len(vec) >= VSS_DIMENSION:
        return vec[:VSS_DIMENSION]
    return vec + [0.0] * (VSS_DIMENSION - len(vec))


# ── Ontology CRUD ────────────────────────────────────────────────────────────

_ONT_PROPS = [
    "title",
    "description",
    "ontology_type",
    "version",
    "version_iri",
    "license",
    "license_tier",
    "source_registry",
    "source_url",
    "format",
    "class_count",
    "property_count",
    "axiom_count",
    "parse_status",
    "file_path",
    "created_at",
    "updated_at",
    "namespace",
]


def _ont_return_clause(alias: str = "n") -> str:
    fields = ", ".join(f"{alias}.{p} AS {p}" for p in _ONT_PROPS)
    return f"id({alias}) AS uri, {fields}"


def _ont_from_row(r: dict, namespace: str | None = None) -> dict:
    logical_uri = _strip_nid(r["uri"], namespace)
    return {
        "id": logical_uri,
        "uri": logical_uri,
        **{p: r.get(p) for p in _ONT_PROPS},
        "domain_tags": [],
        "imports": [],
    }


def _fill_ont_edges(ont: dict, namespace: str | None = None) -> dict:
    ns = _ns(namespace)
    nid = _nid(ont["uri"], namespace)
    tags_res = _oc_query(
        "MATCH (n:Ontology)-[r:TAGGED]->(t:Tag) WHERE n.`~id` = $nid AND n.namespace = $ns"
        " AND r.namespace = $ns AND t.namespace = $ns RETURN id(t) AS tag",
        {"nid": nid, "ns": ns},
    )
    ont["domain_tags"] = [_strip_nid(r["tag"], namespace) for r in tags_res.get("results", [])]
    imp_res = _oc_query(
        "MATCH (n:Ontology)-[r:IMPORTS]->(o:Ontology) WHERE n.`~id` = $nid AND n.namespace = $ns"
        " AND r.namespace = $ns AND o.namespace = $ns RETURN id(o) AS imp",
        {"nid": nid, "ns": ns},
    )
    ont["imports"] = [_strip_nid(r["imp"], namespace) for r in imp_res.get("results", [])]
    return ont


def create_ontology(data: dict, namespace: str | None = None) -> dict:
    """Create an Ontology node (plus tag and import edges) and return the stored record.

    Args:
        data: Ontology fields (``uri`` and ``title`` required; ``domain_tags`` and
            ``imports`` become TAGGED / IMPORTS edges).
        namespace: The namespace to create the ontology under.

    Returns:
        The stored ontology as read back from the graph.

    Raises:
        RuntimeError: If the ontology cannot be read back after the write.
    """
    ns = _ns(namespace)
    uri = data["uri"]
    nid = _nid(uri, namespace)
    now = _now_iso()
    _oc_query(
        "CREATE (n:Ontology {`~id`: $nid, namespace: $ns, title: $title, description: $desc,"
        " ontology_type: $otype, version: $ver, version_iri: $viri,"
        " license: $lic, license_tier: $ltier, source_registry: $sreg,"
        " source_url: $surl, format: $fmt, parse_status: $ps,"
        " created_at: $now, updated_at: $now})",
        {
            "nid": nid,
            "ns": ns,
            "title": data["title"],
            "desc": data.get("description"),
            "otype": data.get("ontology_type", "foundational"),
            "ver": data.get("version"),
            "viri": data.get("version_iri"),
            "lic": data.get("license"),
            "ltier": data.get("license_tier"),
            "sreg": data.get("source_registry"),
            "surl": data.get("source_url"),
            "fmt": data.get("format"),
            "ps": "pending",
            "now": now,
        },
    )
    for tag in data.get("domain_tags", []):
        tag_nid = _nid(tag, namespace)
        _oc_query(
            "MERGE (t:Tag {`~id`: $tag_nid, namespace: $ns})"
            " WITH t MATCH (n:Ontology {`~id`: $nid, namespace: $ns})"
            " MERGE (n)-[r:TAGGED]->(t) SET r.namespace = $ns",
            {"tag_nid": tag_nid, "nid": nid, "ns": ns},
        )
    for imp in data.get("imports", []):
        imp_nid = _nid(imp, namespace)
        _oc_query(
            "MATCH (n:Ontology {`~id`: $nid, namespace: $ns}), (o:Ontology {`~id`: $imp_nid, namespace: $ns})"
            " MERGE (n)-[r:IMPORTS]->(o) SET r.namespace = $ns",
            {"nid": nid, "imp_nid": imp_nid, "ns": ns},
        )
    result = get_ontology_by_uri(uri, namespace=namespace)
    if result is None:
        raise RuntimeError(f"create_ontology: ontology {uri!r} not found after write")
    return result


def get_ontology(oid: str, namespace: str | None = None) -> dict | None:
    """Fetch an ontology by id (its URI) within a namespace.

    Args:
        oid: The ontology id/URI to fetch.
        namespace: The namespace to look in.

    Returns:
        The ontology record, or None when it does not exist.
    """
    return get_ontology_by_uri(oid, namespace=namespace)


def get_ontology_by_uri(uri: str, namespace: str | None = None) -> dict | None:
    """Fetch an ontology by URI, including its tag and import edges.

    Args:
        uri: The ontology URI to fetch.
        namespace: The namespace to look in.

    Returns:
        The ontology record with ``domain_tags`` and ``imports`` filled, or None
        when no such ontology exists.
    """
    ns = _ns(namespace)
    nid = _nid(uri, namespace)
    res = _oc_query(
        f"MATCH (n:Ontology) WHERE n.`~id` = $nid AND n.namespace = $ns RETURN {_ont_return_clause()}",
        {"nid": nid, "ns": ns},
    )
    rows = res.get("results", [])
    if not rows:
        return None
    return _fill_ont_edges(_ont_from_row(rows[0], namespace), namespace)


def list_ontologies(
    ontology_type=None,
    uri=None,
    source_registry=None,
    license_tier=None,
    domain_tag=None,
    skip=0,
    limit=50,
    namespace: str | None = None,
) -> list[dict]:
    """List ontologies in a namespace, filtered and paginated.

    Args:
        ontology_type: Optional filter by ontology type.
        uri: Optional filter to a single ontology URI.
        source_registry: Optional filter by source registry.
        license_tier: Optional filter by license tier.
        domain_tag: Optional filter to ontologies carrying this domain tag.
        skip: Number of results to skip (pagination offset).
        limit: Maximum number of results to return.
        namespace: The namespace to list within.

    Returns:
        The matching ontologies, each with tag/import edges filled.
    """
    ns = _ns(namespace)
    wheres, params = ["n.namespace = $ns"], {"ns": ns}
    if ontology_type:
        wheres.append("n.ontology_type = $otype")
        params["otype"] = ontology_type
    if uri:
        wheres.append("n.`~id` = $nid")
        params["nid"] = _nid(uri, namespace)
    if source_registry:
        wheres.append("n.source_registry = $sreg")
        params["sreg"] = source_registry
    if license_tier:
        wheres.append("n.license_tier = $ltier")
        params["ltier"] = license_tier

    match = "MATCH (n:Ontology)"
    if domain_tag:
        match = "MATCH (n:Ontology)-[:TAGGED]->(t:Tag)"
        wheres.append("t.`~id` = $dtag_nid AND t.namespace = $ns")
        params["dtag_nid"] = _nid(domain_tag, namespace)

    where = " WHERE " + " AND ".join(wheres)
    res = _oc_query(
        f"{match}{where} RETURN {_ont_return_clause()} ORDER BY n.`~id` SKIP {skip} LIMIT {limit}",
        params,
    )
    return [_fill_ont_edges(_ont_from_row(r, namespace), namespace) for r in res.get("results", [])]


def update_ontology(uri: str, updates: dict, namespace: str | None = None) -> dict | None:
    """Apply property, tag, and import updates to an ontology.

    The ``namespace`` property is never mutated; ``domain_tags`` and ``imports``
    are replaced wholesale (existing edges deleted, then re-created).

    Args:
        uri: The ontology URI to update.
        updates: The fields to set; may include ``domain_tags`` and ``imports``.
        namespace: The namespace the ontology lives in.

    Returns:
        The updated ontology record, or None when it does not exist.
    """
    if not updates:
        return get_ontology_by_uri(uri, namespace=namespace)
    ns = _ns(namespace)
    nid = _nid(uri, namespace)
    sets = ["n.updated_at = $now"]
    params: dict = {"nid": nid, "ns": ns, "now": _now_iso()}

    tags = updates.pop("domain_tags", None)
    imports = updates.pop("imports", None)

    for k, v in updates.items():
        # Refuse to mutate the namespace property via update
        if k == "namespace":
            continue
        pkey = f"p_{k}"
        sets.append(f"n.{k} = ${pkey}")
        params[pkey] = v

    _oc_query(
        f"MATCH (n:Ontology) WHERE n.`~id` = $nid AND n.namespace = $ns SET {', '.join(sets)}",
        params,
    )
    if tags is not None:
        _oc_query(
            "MATCH (n:Ontology {`~id`: $nid, namespace: $ns})-[r:TAGGED]->() DELETE r",
            {"nid": nid, "ns": ns},
        )
        for tag in tags:
            tag_nid = _nid(tag, namespace)
            _oc_query(
                "MERGE (t:Tag {`~id`: $tag_nid, namespace: $ns})"
                " WITH t MATCH (n:Ontology {`~id`: $nid, namespace: $ns})"
                " MERGE (n)-[r:TAGGED]->(t) SET r.namespace = $ns",
                {"tag_nid": tag_nid, "nid": nid, "ns": ns},
            )
    if imports is not None:
        _oc_query(
            "MATCH (n:Ontology {`~id`: $nid, namespace: $ns})-[r:IMPORTS]->() DELETE r",
            {"nid": nid, "ns": ns},
        )
        for imp in imports:
            imp_nid = _nid(imp, namespace)
            _oc_query(
                "MATCH (n:Ontology {`~id`: $nid, namespace: $ns}),"
                " (o:Ontology {`~id`: $imp_nid, namespace: $ns})"
                " MERGE (n)-[r:IMPORTS]->(o) SET r.namespace = $ns",
                {"nid": nid, "imp_nid": imp_nid, "ns": ns},
            )
    return get_ontology_by_uri(uri, namespace=namespace)


def delete_ontology(uri: str, namespace: str | None = None) -> bool:
    """Delete an ontology and its defined classes/properties and embeddings.

    Args:
        uri: The ontology URI to delete.
        namespace: The namespace the ontology lives in.

    Returns:
        True if the Ontology node was deleted, False if it did not exist.
    """
    ns = _ns(namespace)
    nid = _nid(uri, namespace)
    # Delete embeddings linked via DEFINES edges (namespace-scoped)
    _oc_query(
        "MATCH (n:Ontology {`~id`: $nid, namespace: $ns})-[:DEFINES]->(e)"
        " -[:HAS_EMBEDDING]->(emb:Embedding)"
        " WHERE emb.namespace = $ns DETACH DELETE emb",
        {"nid": nid, "ns": ns},
    )
    _oc_query(
        "MATCH (n:Ontology {`~id`: $nid, namespace: $ns})-[:DEFINES]->(e) WHERE e.namespace = $ns DETACH DELETE e",
        {"nid": nid, "ns": ns},
    )
    # Delete embeddings linked by ontology_id property (batch-stored)
    _oc_query(
        "MATCH (emb:Embedding) WHERE emb.ontology_id = $uri AND emb.namespace = $ns DETACH DELETE emb",
        {"uri": uri, "ns": ns},
    )
    res = _oc_query(
        "MATCH (n:Ontology) WHERE n.`~id` = $nid AND n.namespace = $ns DETACH DELETE n RETURN count(n) AS c",
        {"nid": nid, "ns": ns},
    )
    return (res.get("results", [{}])[0].get("c", 0)) > 0


# ── TBox graph construction ──────────────────────────────────────────────────


def store_class(
    ontology_uri: str,
    class_uri: str,
    labels: list[str] | None = None,
    comments: list[str] | None = None,
    superclass_uris: list[str] | None = None,
    namespace: str | None = None,
) -> None:
    """Upsert a Class node, link it to its ontology, and create SUBCLASS_OF edges.

    Args:
        ontology_uri: URI of the ontology that defines the class.
        class_uri: URI of the class to store.
        labels: Optional labels; the first is stored as the class label.
        comments: Optional comments; the first is stored as the class comment.
        superclass_uris: Optional parent class URIs to link via SUBCLASS_OF.
        namespace: The namespace to store the class under.
    """
    ns = _ns(namespace)
    c_nid = _nid(class_uri, namespace)
    o_nid = _nid(ontology_uri, namespace)
    _oc_query(
        "MERGE (c:Class {`~id`: $c_nid, namespace: $ns})"
        " SET c.label = $label, c.comment = $comment"
        " WITH c MATCH (o:Ontology {`~id`: $o_nid, namespace: $ns})"
        " MERGE (o)-[r:DEFINES]->(c) SET r.namespace = $ns",
        {"c_nid": c_nid, "o_nid": o_nid, "ns": ns, "label": (labels or [""])[0], "comment": (comments or [""])[0]},
    )
    for sc in superclass_uris or []:
        sc_nid = _nid(sc, namespace)
        _oc_query(
            "MATCH (c:Class {`~id`: $c_nid, namespace: $ns}),"
            " (s:Class {`~id`: $sc_nid, namespace: $ns})"
            " MERGE (c)-[r:SUBCLASS_OF]->(s) SET r.namespace = $ns",
            {"c_nid": c_nid, "sc_nid": sc_nid, "ns": ns},
        )


def store_property(
    ontology_uri: str,
    prop_uri: str,
    label: str = "",
    comment: str = "",
    domain_uris: list[str] | None = None,
    range_uris: list[str] | None = None,
    namespace: str | None = None,
) -> None:
    """Upsert a Property node, link it to its ontology, and create domain/range edges.

    Args:
        ontology_uri: URI of the ontology that defines the property.
        prop_uri: URI of the property to store.
        label: The property label.
        comment: The property comment.
        domain_uris: Optional class URIs to link via HAS_DOMAIN.
        range_uris: Optional class URIs to link via HAS_RANGE.
        namespace: The namespace to store the property under.
    """
    ns = _ns(namespace)
    p_nid = _nid(prop_uri, namespace)
    o_nid = _nid(ontology_uri, namespace)
    _oc_query(
        "MERGE (p:Property {`~id`: $p_nid, namespace: $ns})"
        " SET p.label = $label, p.comment = $comment"
        " WITH p MATCH (o:Ontology {`~id`: $o_nid, namespace: $ns})"
        " MERGE (o)-[r:DEFINES]->(p) SET r.namespace = $ns",
        {"p_nid": p_nid, "o_nid": o_nid, "ns": ns, "label": label, "comment": comment},
    )
    for d in domain_uris or []:
        d_nid = _nid(d, namespace)
        _oc_query(
            "MATCH (p:Property {`~id`: $p_nid, namespace: $ns}),"
            " (c:Class {`~id`: $d_nid, namespace: $ns})"
            " MERGE (p)-[r:HAS_DOMAIN]->(c) SET r.namespace = $ns",
            {"p_nid": p_nid, "d_nid": d_nid, "ns": ns},
        )
    for r_uri in range_uris or []:
        r_nid = _nid(r_uri, namespace)
        _oc_query(
            "MATCH (p:Property {`~id`: $p_nid, namespace: $ns}),"
            " (c:Class {`~id`: $r_nid, namespace: $ns})"
            " MERGE (p)-[r:HAS_RANGE]->(c) SET r.namespace = $ns",
            {"p_nid": p_nid, "r_nid": r_nid, "ns": ns},
        )


# ── Embedding operations ────────────────────────────────────────────────────


def _emb_node_id(entity_uri: str, embedding_type: str, model_id: str) -> str:
    return f"emb/{entity_uri}/{embedding_type}/{model_id}"


def store_embedding(data: dict, namespace: str | None = None) -> dict:
    """Create an Embedding node, upsert its vector, and link it to its entity node.

    Args:
        data: Embedding fields including ``entity_uri``, ``entity_type``,
            ``embedding_type``, ``model_id``, ``ontology_id``, and ``vector``.
            An item-level ``namespace`` overrides the argument.
        namespace: The default namespace to store under.

    Returns:
        The stored embedding's metadata (node id, dimensions, timestamps, etc.).
    """
    ns = _ns(namespace)
    # Allow namespace to be supplied via the data dict too (for batch callers
    # that put namespace on each item).
    if "namespace" in data and data["namespace"]:
        ns = data["namespace"]
    logical_nid = _emb_node_id(data["entity_uri"], data["embedding_type"], data["model_id"])
    nid = _nid(logical_nid, ns)
    entity_nid = _nid(data["entity_uri"], ns)
    now = _now_iso()
    # Infer dimensions if not supplied (OSS-compatible callers omit it).
    dims = data.get("dimensions") if "dimensions" in data else len(data.get("vector") or [])
    _oc_query(
        "CREATE (e:Embedding {`~id`: $nid, namespace: $ns, ontology_id: $oid, entity_uri: $euri,"
        " entity_type: $etype, embedding_type: $embtype, model_id: $mid,"
        " dimensions: $dims, created_at: $now})",
        {
            "nid": nid,
            "ns": ns,
            "oid": data["ontology_id"],
            "euri": data["entity_uri"],
            "etype": data["entity_type"],
            "embtype": data["embedding_type"],
            "mid": data["model_id"],
            "dims": dims,
            "now": now,
        },
    )
    padded = _pad_vector(data["vector"])
    vec_str = ",".join(str(v) for v in padded)
    _oc_query(
        f"MATCH (e) WHERE id(e) = $nid CALL neptune.algo.vectors.upsert(e, [{vec_str}]) YIELD success RETURN success",
        {"nid": nid},
    )
    # Link to entity node if it exists (namespace-scoped)
    label = "Class" if data["entity_type"] == "class" else "Property"
    _oc_query(
        f"OPTIONAL MATCH (n:{label}) WHERE n.`~id` = $entity_nid AND n.namespace = $ns"
        " WITH n WHERE n IS NOT NULL"
        " MATCH (e:Embedding) WHERE id(e) = $nid AND e.namespace = $ns"
        " MERGE (n)-[r:HAS_EMBEDDING]->(e) SET r.namespace = $ns",
        {"nid": nid, "entity_nid": entity_nid, "ns": ns},
    )
    return {
        "node_id": nid,
        "namespace": ns,
        "ontology_id": data["ontology_id"],
        "entity_uri": data["entity_uri"],
        "entity_type": data["entity_type"],
        "embedding_type": data["embedding_type"],
        "model_id": data["model_id"],
        "dimensions": dims,
        "created_at": now,
    }


def store_embeddings_batch(items: list[dict], namespace: str | None = None) -> list[dict]:
    """Batch-create Embedding nodes, upsert their vectors, and link them to entities.

    Nodes are created via ``UNWIND``, vectors upserted in chunks, and
    HAS_EMBEDDING edges created per entity label. Each item's own ``namespace``
    is honored, falling back to the argument.

    Args:
        items: Embedding dicts (same shape as :func:`store_embedding`'s ``data``).
        namespace: The default namespace for items that don't carry one.

    Returns:
        The stored embeddings' metadata, one per input item.
    """
    if not items:
        return []
    now = _now_iso()
    # Group by item-level namespace so each item's namespace is honored;
    # fall back to the function-level namespace when an item doesn't carry one.
    rows = []
    default_ns = _ns(namespace)
    for d in items:
        item_ns = d.get("namespace") or default_ns
        logical_nid = _emb_node_id(d["entity_uri"], d["embedding_type"], d["model_id"])
        # Infer dimensions from vector length if not supplied by caller.
        # OSS doesn't need this field, so callers that target both backends
        # omit it. NA needs it for the Embedding node property.
        dims = d.get("dimensions") if "dimensions" in d else len(d.get("vector") or [])
        rows.append(
            {
                "nid": _nid(logical_nid, item_ns),
                "ns": item_ns,
                "oid": d["ontology_id"],
                "euri": d["entity_uri"],
                "etype": d["entity_type"],
                "embtype": d["embedding_type"],
                "mid": d["model_id"],
                "dims": dims,
                "now": now,
            }
        )
    # Batch node creation via UNWIND — namespace read from each row
    _oc_query(
        "UNWIND $rows AS r"
        " CREATE (e:Embedding {`~id`: r.nid, namespace: r.ns, ontology_id: r.oid, entity_uri: r.euri,"
        " entity_type: r.etype, embedding_type: r.embtype, model_id: r.mid,"
        " dimensions: r.dims, created_at: r.now})",
        {"rows": rows},
    )
    # Batch vector upserts with inline UNWIND (NA needs literal lists, not param refs)
    CHUNK = 20
    for i in range(0, len(items), CHUNK):
        chunk = [
            (r["nid"], _pad_vector(d["vector"]))
            for d, r in zip(items[i : i + CHUNK], rows[i : i + CHUNK], strict=False)
        ]
        entries = ",".join('{id:"' + nid + '",embedding:[' + ",".join(str(v) for v in vec) + "]}" for nid, vec in chunk)
        _oc_query(
            f"UNWIND [{entries}] AS entry"
            " MATCH (e) WHERE id(e) = entry.id"
            " WITH e, entry.embedding AS embedding"
            " CALL neptune.algo.vectors.upsert(e, embedding)"
            " YIELD success RETURN success",
            {},
        )
    # Create HAS_EMBEDDING edges (namespace-scoped) from source classes/properties
    # to their Embedding nodes, so namespace-aware graph traversals can reach
    # the vectors without a second property-based lookup.
    edge_rows = [
        {
            "ns": r["ns"],
            "entity_nid": _nid(d["entity_uri"], r["ns"]),
            "emb_nid": r["nid"],
            "label": "Class" if d["entity_type"] == "class" else "Property",
        }
        for d, r in zip(items, rows, strict=False)
    ]
    # Cypher's MATCH doesn't bind a label from a variable, so run one query per label.
    for lbl in {er["label"] for er in edge_rows}:
        lbl_rows = [er for er in edge_rows if er["label"] == lbl]
        _oc_query(
            "UNWIND $rows AS r"
            f" MATCH (n:{lbl}) WHERE n.`~id` = r.entity_nid AND n.namespace = r.ns"
            " WITH n, r MATCH (e:Embedding) WHERE id(e) = r.emb_nid AND e.namespace = r.ns"
            " MERGE (n)-[h:HAS_EMBEDDING]->(e) SET h.namespace = r.ns",
            {"rows": lbl_rows},
        )
    return [
        {
            "node_id": r["nid"],
            "namespace": r["ns"],
            "ontology_id": d["ontology_id"],
            "entity_uri": d["entity_uri"],
            "entity_type": d["entity_type"],
            "embedding_type": d["embedding_type"],
            "model_id": d["model_id"],
            "dimensions": r["dims"],
            "created_at": now,
        }
        for d, r in zip(items, rows, strict=False)
    ]


def get_embeddings_for_entity(
    entity_uri: str, entity_type=None, embedding_type=None, model_id=None, namespace: str | None = None
) -> list[dict]:
    """Return all embeddings for an entity URI, each hydrated with its vector.

    Args:
        entity_uri: The entity whose embeddings to fetch.
        entity_type: Optional filter by entity type (class/property).
        embedding_type: Optional filter by embedding type (lexical/structural).
        model_id: Optional filter by embedding model.
        namespace: The namespace to query.

    Returns:
        Matching embedding records, each including its (trimmed) vector.
    """
    ns = _ns(namespace)
    wheres = ["e.entity_uri = $euri", "e.namespace = $ns"]
    params: dict = {"euri": entity_uri, "ns": ns}
    if entity_type:
        wheres.append("e.entity_type = $etype")
        params["etype"] = entity_type
    if embedding_type:
        wheres.append("e.embedding_type = $embtype")
        params["embtype"] = embedding_type
    if model_id:
        wheres.append("e.model_id = $mid")
        params["mid"] = model_id
    where = " AND ".join(wheres)
    res = _oc_query(
        f"MATCH (e:Embedding) WHERE {where}"
        " RETURN id(e) AS nid, e.namespace AS namespace, e.ontology_id AS ontology_id,"
        " e.entity_uri AS entity_uri, e.entity_type AS entity_type,"
        " e.embedding_type AS embedding_type, e.model_id AS model_id,"
        " e.dimensions AS dimensions, e.created_at AS created_at",
        params,
    )
    results = []
    for r in res.get("results", []):
        vec_res = _oc_query(
            "MATCH (e) WHERE id(e) = $nid CALL neptune.algo.vectors.get(e) YIELD embedding RETURN embedding",
            {"nid": r["nid"]},
        )
        vec = vec_res.get("results", [{}])[0].get("embedding", [])
        results.append({**r, "vector": vec[: r.get("dimensions", len(vec))]})
    return results


def list_embeddings_for_ontology(
    ontology_id: str,
    entity_type=None,
    embedding_type=None,
    model_id=None,
    skip=0,
    limit=100,
    namespace: str | None = None,
) -> list[dict]:
    """List an ontology's embeddings (metadata only), filtered and paginated.

    Args:
        ontology_id: The ontology whose embeddings to list.
        entity_type: Optional filter by entity type (class/property).
        embedding_type: Optional filter by embedding type (lexical/structural).
        model_id: Optional filter by embedding model.
        skip: Number of results to skip (pagination offset).
        limit: Maximum number of results to return.
        namespace: The namespace to query.

    Returns:
        Matching embedding metadata rows (without vectors).
    """
    ns = _ns(namespace)
    wheres = ["e.ontology_id = $oid", "e.namespace = $ns"]
    params: dict = {"oid": ontology_id, "ns": ns}
    if entity_type:
        wheres.append("e.entity_type = $etype")
        params["etype"] = entity_type
    if embedding_type:
        wheres.append("e.embedding_type = $embtype")
        params["embtype"] = embedding_type
    if model_id:
        wheres.append("e.model_id = $mid")
        params["mid"] = model_id
    where = " AND ".join(wheres)
    res = _oc_query(
        f"MATCH (e:Embedding) WHERE {where}"
        " RETURN e.namespace AS namespace, e.ontology_id AS ontology_id, e.entity_uri AS entity_uri,"
        " e.entity_type AS entity_type, e.embedding_type AS embedding_type,"
        " e.model_id AS model_id, e.dimensions AS dimensions,"
        " e.created_at AS created_at"
        f" ORDER BY e.entity_uri SKIP {skip} LIMIT {limit}",
        params,
    )
    return res.get("results", [])


def list_searchable_entity_uris(ontology_id: str, namespace: str | None = None) -> list[str]:
    """Return the entity_uri of every embedding searchable for an ontology.

    URI-only projection for the embedding-readiness wait: the openCypher
    RETURN never includes the ``~embedding`` vector property, so only IDs
    cross the wire. No SKIP/LIMIT — the wait needs the complete set.
    """
    ns = _ns(namespace)
    res = _oc_query(
        "MATCH (e:Embedding) WHERE e.ontology_id = $oid AND e.namespace = $ns"
        " RETURN e.entity_uri AS entity_uri ORDER BY e.entity_uri",
        {"oid": ontology_id, "ns": ns},
    )
    return [row["entity_uri"] for row in res.get("results", []) if row.get("entity_uri")]


def _oc_filter(prop: str, value: str) -> str:
    """Build an openCypher map literal for a vertexFilter equals clause.

    Escapes single quotes and backslashes to prevent openCypher injection.
    """
    safe_value = value.replace("\\", "\\\\").replace("'", "\\'")
    safe_prop = prop.replace("\\", "\\\\").replace("'", "\\'")
    return f"{{equals: {{property: '{safe_prop}', value: '{safe_value}'}}}}"


def search_nearest(
    vector: list[float],
    embedding_type: str,
    model_id: str,
    entity_type: str = "class",
    ontology_id: str | None = None,
    top_k: int = 10,
    namespace: str | None = None,
) -> list[dict]:
    """k-NN search using Neptune Analytics' indexed ``topK.byEmbedding``.

    Returns the ``top_k`` nearest ``Embedding`` nodes that match the
    supplied filters. The returned ``score`` is **raw cosine similarity
    in ``[-1, 1]``**, converted client-side from ``topK.byEmbedding``'s
    raw output (which is always squared L2 distance, regardless of
    metric settings).

    Conversion (valid because ``BedrockEmbeddingClient`` produces
    L2-normalized unit vectors)::

        cos(theta) = 1 - L2_squared / 2

    The ``vertexFilter`` clause restricts the algorithm to ``Embedding``
    nodes that match every supplied filter (``embedding_type``,
    ``model_id``, ``entity_type``, ``namespace``, and optionally
    ``ontology_id``) so we never compare across unrelated tenants or
    embedding models.

    Note: the AWS docs explicitly forbid ``MATCH (n)`` or ``WITH`` as
    the prefix of a ``CALL ... topK.byEmbedding(...)`` — every node
    matched would invoke a separate topK run. So the ``CALL`` MUST
    come first.
    """
    ns = _ns(namespace)
    padded = _pad_vector(vector)
    vec_str = ",".join(str(v) for v in padded)

    # Compose the vertex filter: an andAll over equals clauses for
    # every restriction. Includes the ``Embedding`` label filter so
    # topK only considers our embedding nodes (NA topK is global by
    # default — without a label filter it would consider every vector
    # in the graph).
    filter_parts: list[str] = [
        _oc_filter("~label", "Embedding"),
        _oc_filter("embedding_type", embedding_type),
        _oc_filter("entity_type", entity_type),
        _oc_filter("model_id", model_id),
        _oc_filter("namespace", ns),
    ]
    if ontology_id is not None:
        filter_parts.append(_oc_filter("ontology_id", ontology_id))
    vertex_filter = "{andAll: [" + ", ".join(filter_parts) + "]}"

    # CALL must come FIRST per the AWS docs (no MATCH/WITH prefix).
    res = _oc_query(
        f"CALL neptune.algo.vectors.topK.byEmbedding("
        f" {{embedding: [{vec_str}], topK: {top_k}, "
        f"vertexFilter: {vertex_filter}, concurrency: 4}})"
        f" YIELD node, score"
        f" RETURN id(node) AS node_id,"
        f" node.namespace AS namespace, node.entity_uri AS entity_uri,"
        f" node.ontology_id AS ontology_id, node.entity_type AS entity_type,"
        f" node.embedding_type AS embedding_type, node.model_id AS model_id,"
        f" node.dimensions AS dimensions,"
        # Convert L2-squared distance to cosine similarity client-side.
        # Valid because input vectors are unit-normalized (Bedrock embedders
        # ``normalize: True``). For unit vectors:
        #   ‖a − b‖² = 2 − 2·cos(theta), so cos = 1 − L2² / 2.
        # We compute the conversion in-query to avoid a round-trip
        # through the Python layer for the per-row math.
        f" (1 - score / 2) AS score"
        f" ORDER BY score DESC",
    )
    return res.get("results", [])


def get_vector(node_id: str) -> list[float]:
    """Fetch the stored embedding vector for an Embedding node by id.

    Args:
        node_id: The Embedding node's graph id.

    Returns:
        The vector, or an empty list when the id is empty or has no vector.
    """
    if not node_id:
        return []
    res = _oc_query(
        "MATCH (e) WHERE id(e) = $nid CALL neptune.algo.vectors.get(e) YIELD embedding RETURN embedding",
        {"nid": node_id},
    )
    rows = res.get("results", [])
    return rows[0]["embedding"] if rows else []


def delete_embeddings_for_entities(
    entity_uris: list[str],
    ontology_id: str,
    namespace: str | None = None,
) -> int:
    """Delete the Embedding nodes for a set of entity URIs within a namespace + ontology.

    Args:
        entity_uris: The entities whose embeddings to delete.
        ontology_id: Scope the delete to embeddings owned by this ontology only.
            Shared IRIs across ontologies (foundational reloads,
            explicit-target merges) share the same ``entity_uri`` under a
            different ``ontology_id``; without this scope the delete takes
            the sibling ontology's embedding down with it.
        namespace: The namespace to delete within.

    Returns:
        The number of Embedding nodes deleted.
    """
    if not entity_uris or not ontology_id:
        return 0
    ns = _ns(namespace)
    res = _oc_query(
        "MATCH (e:Embedding) "
        "WHERE e.entity_uri IN $uris AND e.namespace = $ns AND e.ontology_id = $oid "
        "DETACH DELETE e RETURN count(e) AS c",
        {"uris": list(entity_uris), "ns": ns, "oid": ontology_id},
    )
    rows = res.get("results", [])
    return int(rows[0].get("c", 0)) if rows else 0


def delete_embeddings_for_ontology(ontology_id: str, namespace: str | None = None) -> int:
    """Delete all Embedding nodes for an ontology within a namespace.

    Args:
        ontology_id: The ontology whose embeddings to delete.
        namespace: The namespace to delete within.

    Returns:
        The number of Embedding nodes deleted.
    """
    ns = _ns(namespace)
    res = _oc_query(
        "MATCH (e:Embedding) WHERE e.ontology_id = $oid AND e.namespace = $ns DETACH DELETE e RETURN count(e) AS c",
        {"oid": ontology_id, "ns": ns},
    )
    return res.get("results", [{}])[0].get("c", 0)


# ── Health ───────────────────────────────────────────────────────────────────


def health_check() -> dict:
    """Use a cheap query to verify connectivity through the SDK."""
    res = _oc_query("RETURN 1 AS ok")
    return {"status": "healthy"} if res.get("results") else {"status": "unhealthy"}
