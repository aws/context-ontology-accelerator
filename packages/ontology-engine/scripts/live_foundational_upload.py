#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Upload a foundational ontology (Turtle file) into a namespace.

Usage:
    python3 scripts/live_foundational_upload.py <path-to-turtle-file> \
        [--namespace NS] [--ontology-id IRI]

If --ontology-id is not provided, it is derived from the owl:Ontology
subject in the Turtle file.

The ontology IRI follows the pattern:
    https://live-test.coa.amazon.com/{namespace}/{ontology_id}
when --ontology-id is a plain name (no scheme). If it already looks like
a full IRI (http/https/urn), it is used as-is.

Prereqs:
    - Required env (or a .env in this package's root):
        NDB_ENDPOINT, OSS_ENDPOINT, DYNAMODB_TABLE, AWS_REGION
      (same as live_induction_run.py)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

# ── Make the workbench importable ───────────────────────────────────────
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# ── Load .env ──────────────────────────────────────────────────────────
env_file = ROOT / ".env"
if env_file.is_file():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("WORKBENCH_BACKEND", "opensearch_neptune")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("NDB_REGION", os.environ["AWS_REGION"])
os.environ.setdefault("OSS_REGION", os.environ["AWS_REGION"])
os.environ.setdefault("NDB_IAM_AUTH", "true")
os.environ.setdefault("OSS_INDEX", "ontology-workbench-embeddings-live")
os.environ.setdefault("OSS_DIMENSIONS", "1024")
os.environ.setdefault("BEDROCK_REGION", os.environ["AWS_REGION"])
os.environ.setdefault("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
os.environ.setdefault("BEDROCK_EMBED_DIMENSIONS", "1024")
os.environ.setdefault("NA_REGION", os.environ["AWS_REGION"])

BACKEND = os.environ["WORKBENCH_BACKEND"]

if BACKEND == "opensearch_neptune":
    REQUIRED = ["NDB_ENDPOINT", "OSS_ENDPOINT", "DYNAMODB_TABLE"]
elif BACKEND == "na_only":
    REQUIRED = ["NA_GRAPH_ID", "DYNAMODB_TABLE"]
else:
    sys.exit(f"Unsupported WORKBENCH_BACKEND: {BACKEND!r}")

missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    sys.exit(f"Missing required env for backend {BACKEND!r}: {missing}")

# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-40s %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
for noisy in ("urllib3", "httpx", "botocore", "boto3", "opensearch", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("foundational-upload")


def _resolve_ontology_iri(raw: str | None, namespace: str) -> str | None:
    """If raw is a plain name, wrap it as https://live-test.coa.amazon.com/{ns}/{name}."""
    if raw is None:
        return None
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https", "urn"):
        return raw
    return f"https://live-test.coa.amazon.com/{namespace}/{raw}"


def _delete_ontology(ontology_id: str, namespace: str) -> int:
    """Delete an ontology from graph store, vector store, and DynamoDB registry."""
    from coa_ontology import dynamo_store
    from coa_ontology.stores import build_stores

    graph_store, vector_store = build_stores(namespace=namespace)

    log.info("Deleting ontology %s from namespace %s", ontology_id, namespace)

    # Graph store
    try:
        graph_store.delete_ontology(ontology_id)
        log.info("  graph store: deleted")
    except Exception as e:
        log.warning("  graph store: %s", e)

    # Vector store — delete embeddings for this ontology
    try:
        vector_store.delete_embeddings_for_ontology(ontology_id)
        log.info("  vector store: deleted")
    except Exception as e:
        log.warning("  vector store: %s", e)

    # DynamoDB registry
    deleted = dynamo_store.delete_ontology_registry(namespace, ontology_id)
    log.info("  dynamo registry: %s", "deleted" if deleted else "not found")

    print(f"\n✓ Deleted ontology {ontology_id} from namespace {namespace}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a foundational ontology Turtle file.")
    parser.add_argument("turtle_file", nargs="?", help="Path to the .ttl file")
    parser.add_argument("--namespace", default="default", help="Target namespace (default: 'default')")
    parser.add_argument("--ontology-id", default=None, help="Ontology IRI or plain name (derived from file if omitted)")
    parser.add_argument("--delete", action="store_true", help="Delete the ontology instead of uploading")
    args = parser.parse_args()

    namespace = args.namespace

    if args.delete:
        if not args.ontology_id:
            sys.exit("--ontology-id is required with --delete")
        ontology_id = _resolve_ontology_iri(args.ontology_id, namespace)
        return _delete_ontology(ontology_id, namespace)

    if not args.turtle_file:
        sys.exit("turtle_file is required for upload")

    turtle_path = Path(args.turtle_file)
    if not turtle_path.is_file():
        sys.exit(f"File not found: {turtle_path}")

    namespace = args.namespace
    ontology_id = _resolve_ontology_iri(args.ontology_id, namespace)

    turtle_content = turtle_path.read_text(encoding="utf-8")
    log.info("Read %d bytes from %s", len(turtle_content), turtle_path)

    # ── Build stores ────────────────────────────────────────────────────
    from coa_ontology.stores import build_stores

    graph_store, vector_store = build_stores(namespace=namespace)

    gh = graph_store.health_check()
    vh = vector_store.health_check()
    log.info("graph health: %s", gh)
    log.info("vector health: %s", vh)

    # ── Ingest ──────────────────────────────────────────────────────────
    from coa_ontology.catalog.ingest import IngestMetadata, ingest_ontology

    metadata = IngestMetadata(
        ontology_id=ontology_id,
        ontology_type="foundational",
        source="upload",
        platform_foundational=True,
        domain_tags=["foundational"],
        file_path=str(turtle_path),
    )

    t0 = time.perf_counter()
    result = ingest_ontology(
        graph_store,
        vector_store,
        turtle_content,
        namespace=namespace,
        metadata=metadata,
    )
    dt = time.perf_counter() - t0

    # ── Summary ─────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  Foundational ontology upload complete")
    print("═" * 60)
    print(f"  file:           {turtle_path}")
    print(f"  namespace:      {namespace}")
    print(f"  ontology_id:    {result['ontology_id']}")
    print(f"  graph_uri:      {result['graph_uri']}")
    print(f"  classes:        {result['class_count']}")
    print(f"  properties:     {result['property_count']}")
    print(f"  axioms:         {result['axiom_count']}")
    print(f"  embeddings:     {result.get('embeddings', {}).get('count', 0)}")
    print(f"  turtle_load:    {result['turtle_load']['status']} ({result['turtle_load'].get('bytes_loaded', 0)} bytes)")
    print(f"  time:           {dt * 1000:.0f} ms")
    print("═" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
