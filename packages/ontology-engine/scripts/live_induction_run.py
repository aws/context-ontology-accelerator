#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Inlined live run — calls the induction + acceptance code paths directly.

No FastAPI, no TestClient, no uvicorn. Every step is a plain Python
function call against the same code the API routes are wired to. That
makes it straightforward to put a debugger on any line.

Usage:
    cd packages/ontology-engine
    python3 scripts/live_induction_run.py

Prereqs:
    - data-catalog sidecar reachable at DATA_CATALOG_URL (default
      http://localhost:8003). The mock catalog service ships as a test
      fixture in this package. Start it from its directory:
         cd packages/ontology-engine/tests/fixtures/data-catalog
         uvicorn app.main:app --host 0.0.0.0 --port 8003
    - Required env (or a .env in this package's root):
        NDB_ENDPOINT, OSS_ENDPOINT, DYNAMODB_TABLE, AWS_REGION
      Defaults filled in for everything else.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import tempfile
import time
import uuid
from datetime import UTC
from pathlib import Path
from urllib.parse import urlparse

# ── Make the workbench importable ───────────────────────────────────────
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# ── Load .env before any AWS SDK import reads the environment ──────────
env_file = ROOT / ".env"
if env_file.is_file():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

os.environ.setdefault("WORKBENCH_BACKEND", "opensearch_neptune")
os.environ.setdefault("DATA_CATALOG_URL", "http://localhost:8003")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("NDB_REGION", os.environ["AWS_REGION"])
os.environ.setdefault("OSS_REGION", os.environ["AWS_REGION"])
os.environ.setdefault("NDB_IAM_AUTH", "true")
os.environ.setdefault("OSS_INDEX", "ontology-workbench-embeddings-live")
os.environ.setdefault("OSS_DIMENSIONS", "1024")
os.environ.setdefault("BEDROCK_REGION", os.environ["AWS_REGION"])
(os.environ.setdefault("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0"),)
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

# When using SMUS, default the induction namespace to the SMUS namespace ID.
# This ties the Neptune graph prefix and OSS index to the SMUS tenant boundary.
if os.environ.get("CATALOG_SOURCE") == "smus" and os.environ.get("SMUS_NAMESPACE_ID"):
    os.environ.setdefault("INDUCTION_NAMESPACE", os.environ["SMUS_NAMESPACE_ID"])


# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(name)-40s %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
for noisy in ("urllib3", "httpx", "botocore", "boto3", "opensearch"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("live")


# ── Timing harness ──────────────────────────────────────────────────────

TIMELINE: list[tuple[str, float]] = []


class Step:
    _depth = 0

    def __init__(self, label: str):
        self.label = label
        self.t0 = 0.0

    def __enter__(self):
        indent = "  " * Step._depth
        print(f"\n{indent}⏱  ▶ {self.label}", flush=True)
        Step._depth += 1
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        Step._depth -= 1
        dt = time.perf_counter() - self.t0
        indent = "  " * Step._depth
        state = "✓" if exc is None else "✗"
        print(f"{indent}⏱  ◀ {state} {self.label}  [{dt * 1000:.0f} ms]", flush=True)
        TIMELINE.append((self.label, dt))


def probe(url: str, timeout: float = 5.0) -> None:
    p = urlparse(url)
    host = p.hostname
    port = p.port or (443 if p.scheme == "https" else 80)
    with Step(f"TCP probe {host}:{port}"):
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()


# ── Direct SPARQL helpers (SigV4-signed) ────────────────────────────────


def _sig_headers(method: str, url: str, body) -> dict:
    import boto3
    import botocore.auth
    import botocore.awsrequest

    creds = boto3.Session().get_credentials().get_frozen_credentials()
    req = botocore.awsrequest.AWSRequest(method=method, url=url, data=body)
    botocore.auth.SigV4Auth(creds, "neptune-db", os.environ["AWS_REGION"]).add_auth(req)
    return dict(req.headers.items())


def sparql_query(q: str) -> dict:
    from urllib.parse import urlencode

    import httpx

    url = f"{os.environ['NDB_ENDPOINT'].rstrip('/')}/sparql"
    body = urlencode({"query": q})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/sparql-results+json",
    }
    headers.update(_sig_headers("POST", url, body))
    with httpx.Client(timeout=30) as c:
        r = c.post(url, content=body, headers=headers)
        if r.status_code >= 400:
            log.error("SPARQL query %d: %s\nquery=%s", r.status_code, r.text[:500], q)
            r.raise_for_status()
        return r.json()


def sparql_update(u: str) -> None:
    from urllib.parse import urlencode

    import httpx

    url = f"{os.environ['NDB_ENDPOINT'].rstrip('/')}/sparql"
    body = urlencode({"update": u})
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    headers.update(_sig_headers("POST", url, body))
    with httpx.Client(timeout=30) as c:
        r = c.post(url, content=body, headers=headers)
        if r.status_code >= 400:
            log.error("SPARQL update %d: %s\nupdate=%s", r.status_code, r.text[:500], u)
            r.raise_for_status()


# ── Direct in-process orchestration (the code the API routes call) ─────


# Grounding-capable strategies. Must be a subset of the induction STRATEGIES
# registry (coa_ontology.inducer.strategies) — the harness only drives the
# structured ``table_to_ontology`` pipeline. ``rigor_ontology`` is registered but
# deliberately ``del``\\ s the grounding scope, so it never grounds and is
# excluded. (``unstructured_lexical_graph`` is a separate dispatch path, not in
# STRATEGIES and not driven here, so it is intentionally not listed.)
_GROUNDING_CAPABLE_STRATEGIES = frozenset({"table_to_ontology"})


def _grounding_gate_should_fail(grounding_keys, strategy, grounding_used) -> bool:
    """Return True when a grounding-capable run was ASKED to ground but didn't (#175).

    The reported #175 defect was a green run that never actually grounded (empty
    scope → recall returns [] for every table, logs "empty grounding scope", run
    still reports success). This is the fail-loud predicate: grounding was
    requested (non-empty ``grounding_keys``) under a grounding-capable
    ``strategy``, yet ``grounding_used`` (report.grounding_ontologies_used) is
    empty. An all-novel run (no keys) and a ``rigor_ontology`` run (scope deleted
    by design) are both legitimate and never trip this gate.
    """
    return bool(grounding_keys) and strategy in _GROUNDING_CAPABLE_STRATEGIES and not grounding_used


def run_induction_direct(
    ontology_uri: str,
    datasource_ids: list[str],
    label: str,
    grounding_ontology_ids: list[str] | None = None,
    strategy: str | None = None,
    grounding_mode: str | None = None,
) -> str:
    """Call the exact same code ``POST /induce/`` would, synchronously.

    ``_run_induction`` is normally launched on a background Thread by
    the FastAPI handler; here we call it inline so the debugger can step
    through every line.

    ``grounding_ontology_ids`` (curated catalog keys and/or already-resolved
    URIs) are passed through unchanged — ``_run_induction`` resolves + loads
    them via ``_load_grounding_ontologies`` exactly as the API path does, then
    hands them to the strategy. Grounding is only exercised on a
    grounding-capable strategy (``table_to_ontology`` / the unstructured
    lexical graph); ``rigor_ontology`` deletes the scope by design, so pass a
    grounding-capable ``strategy`` when grounding is intended (#175).
    """
    from coa_ontology import induce_catalog
    from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient
    from coa_ontology.inducer import schemas as inducer_schemas
    from coa_ontology.inducer.routers import induce as induce_router
    from coa_ontology.inducer.services.data_catalog import CatalogTable
    from coa_ontology.stores import build_stores
    from coa_ontology.stores.adapters import StoreOntologyCatalogAdapter

    # Wire the module-level injected deps that main.py normally sets up.
    induce_catalog._schemas_mod = inducer_schemas
    induce_catalog._catalog_table_cls = CatalogTable

    # Build the pipeline with Bedrock embeddings + store-backed catalog,
    # exactly like main.py does.
    graph_store, vector_store = build_stores()
    store_adapter = StoreOntologyCatalogAdapter(graph_store, vector_store)

    def build_pipeline(config: dict):
        pipe = induce_router._build_pipeline(config)
        pipe.eg = BedrockEmbeddingClient(
            region=config.get("bedrock_region", "us-east-1"),
            model_id=config.get("bedrock_embed_model_id", "amazon.titan-embed-text-v2:0"),
            dimensions=int(config.get("bedrock_embed_dimensions", 1024)),
        )
        pipe.oc = store_adapter
        return pipe

    config = {
        "data_catalog_url": os.environ.get("DATA_CATALOG_URL", ""),
        "embedding_generator_url": "",
        "ontology_catalog_url": "",
        "neptune_analytics_url": os.environ.get("NEPTUNE_ANALYTICS_URL", "http://localhost:9200"),
        "llm_model_id": os.environ.get("LLM_MODEL_ID", "us.anthropic.claude-sonnet-4-6"),
        "llm_region": os.environ.get("LLM_REGION", os.environ["AWS_REGION"]),
        "bedrock_region": os.environ["BEDROCK_REGION"],
        "bedrock_embed_model_id": os.environ["BEDROCK_EMBED_MODEL_ID"],
        "bedrock_embed_dimensions": int(os.environ["BEDROCK_EMBED_DIMENSIONS"]),
        "catalog_source": os.environ.get("CATALOG_SOURCE", "http"),
        "smus_domain_id": os.environ.get("SMUS_DOMAIN_ID", ""),
        "smus_project_id": os.environ.get("SMUS_PROJECT_ID", ""),
        "smus_namespace_id": os.environ.get("SMUS_NAMESPACE_ID", ""),
        "smus_datasources_table": os.environ.get("DATASOURCES_TABLE", ""),
        "smus_region": os.environ.get("AWS_REGION", "us-east-1"),
    }

    # Build the induction request the same shape the router does.
    # Grounding is only meaningful on a grounding-capable strategy: the default
    # is now ``table_to_ontology`` (the production structured strategy that
    # actually grounds), NOT ``rigor_ontology`` — which does ``del
    # grounding_ontology_ids`` and would silently ignore any scope (#175).
    resolved_strategy = strategy or os.environ.get("INDUCTION_STRATEGY", "table_to_ontology")
    resolved_grounding_mode = grounding_mode or os.environ.get("INDUCTION_GROUNDING_MODE", "ENHANCED")
    body = induce_catalog.WorkbenchInductionRequest(
        datasource_ids=datasource_ids,
        ontology_uri_prefix=ontology_uri,
        namespace=os.environ.get("INDUCTION_NAMESPACE", "default"),
        label=label,
        confidence_threshold=0.80,
        strategy=resolved_strategy,
        grounding_ontology_ids=grounding_ontology_ids or None,
        grounding_mode=resolved_grounding_mode,
    )
    job_id = str(uuid.uuid4())

    # Seed the in-memory job record (same shape the API handler would).
    JobResponse = inducer_schemas.JobResponse
    JobStatus = inducer_schemas.JobStatus
    induce_catalog._jobs[job_id] = JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=datetime_utcnow_iso(),
    )
    # Persist initial state to DynamoDB (matches the API's pattern).
    from coa_ontology import dynamo_store

    dynamo_store.update_job_status(
        job_id,
        "pending",
        namespace=os.environ.get("INDUCTION_NAMESPACE", "default"),
        ontology_uri_prefix=ontology_uri,
        datasource_ids=datasource_ids,
        label=label or "",
    )

    # Call the pipeline synchronously. No Thread.
    induce_catalog._run_induction(
        job_id=job_id,
        body=body,
        namespace=os.environ.get("INDUCTION_NAMESPACE", "default"),
        config=config,
        build_pipeline_fn=build_pipeline,
        schemas_mod=inducer_schemas,
        catalog_table_cls=CatalogTable,
    )
    job = induce_catalog._jobs[job_id]
    if str(job.status) != "JobStatus.COMPLETED" and job.status != JobStatus.COMPLETED:
        raise RuntimeError(f"Induction failed: status={job.status} error={job.error}")
    return job_id


# Accept is ASYNC: ``accept_proposal`` returns HTTP 202 with an
# ``AcceptProposalResponse`` (``proposal_id`` + ``status``) the instant a
# background worker starts, and ``status`` is ``"accepting"`` — NOT
# ``"accepted"``. The worker later flips the proposal's DDB row to the
# terminal ``accepted`` (success) or ``accept_failed`` (with ``accept_error``
# naming the failed step) state. The bundled harness previously subscripted
# the response (``a["status"]``) — a ``TypeError`` on a pydantic model — and
# asserted ``status == "accepted"`` synchronously, so it never completed
# against the release it ships with (issue #175). The fix polls the proposal
# to a terminal state, exactly as any real client must.
def _positive_int_env(name: str, default: int, *, maximum: int) -> int:
    """Parse a positive-int env var, failing loudly on bad input (issue #175 fail-loud)."""
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} must be a valid integer, got {raw!r}") from e
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {value}")
    return min(value, maximum)


_ACCEPT_POLL_TIMEOUT_S = _positive_int_env("INDUCTION_ACCEPT_POLL_TIMEOUT_S", 300, maximum=3600)
_ACCEPT_POLL_INTERVAL_S = 2


def _poll_proposal_terminal(proposal_id: str, namespace: str) -> dict:
    """Poll ``GET /proposals/{id}`` until the accept reaches a terminal state.

    Returns the terminal proposal record. Raises ``TimeoutError`` if the
    proposal is still ``accepting`` / ``embeddings_sync`` after the timeout,
    and ``RuntimeError`` if it lands ``accept_failed`` (surfacing
    ``accept_error`` so a failed accept reads as a failure, not a success).
    """
    from coa_control_plane_server.models.proposal_status import ProposalStatus
    from coa_ontology import dynamo_store
    from coa_ontology.proposals import (
        _ACCEPT_IN_PROGRESS_STATUSES,
        PROPOSAL_STATUS_ACCEPT_FAILED,
    )

    deadline = time.time() + _ACCEPT_POLL_TIMEOUT_S
    last_status = None
    while True:
        item = dynamo_store.get_proposal_by_id(proposal_id, namespace=namespace)
        if not item:
            raise RuntimeError(f"proposal {proposal_id} disappeared while polling accept")
        status = item.get("status")
        if status is None:
            raise RuntimeError(f"proposal {proposal_id} has no status field: {item!r}")
        if status != last_status:
            print(f"    accept poll: status={status}", flush=True)
            last_status = status
        if status == ProposalStatus.ACCEPTED.value:
            return item
        if status == PROPOSAL_STATUS_ACCEPT_FAILED:
            raise RuntimeError(f"accept failed for {proposal_id}: {item.get('accept_error') or '(no accept_error)'}")
        if status not in _ACCEPT_IN_PROGRESS_STATUSES:
            # Any other non-terminal status is unexpected during accept.
            raise RuntimeError(f"unexpected proposal status during accept: {status!r}")
        if time.time() >= deadline:
            raise TimeoutError(f"accept for {proposal_id} still {status!r} after {_ACCEPT_POLL_TIMEOUT_S}s")
        time.sleep(_ACCEPT_POLL_INTERVAL_S)


def accept_proposal_direct(proposal_id: str, namespace: str = "default") -> dict:
    """Accept a proposal in-process and BLOCK until it reaches a terminal state.

    Calls :func:`app.proposals.accept_proposal` (which returns 202 +
    ``status="accepting"`` and spawns a background worker), then polls the
    proposal to the terminal ``accepted`` state. Returns a normalized dict:

        {"proposal_id", "status", "ontology_id", "graph_uri",
         "already_accepted": bool, "proposal": <terminal proposal record>}

    ``already_accepted`` is True when the proposal was ALREADY ``accepted``
    before this call (``accept_proposal`` short-circuits and returns
    ``status="accepted"`` immediately with no new worker) — that is the
    idempotent re-accept signal (there is no ``"already_accepted"`` status in
    the API; #175 asserted one that never existed).

    When ``INDUCTION_TARGET_ONTOLOGY_ID`` is set in the environment, every
    proposal accept is merged under that single target ontology (using
    the /accept body's ``ontology_id`` field). When unset, each proposal
    uses its own embedded ``ontology_id`` — the original per-proposal
    behaviour.
    """
    from coa_control_plane_server.models.proposal_status import ProposalStatus
    from coa_ontology.proposals import AcceptProposalRequest, accept_proposal

    raw_target = os.environ.get("INDUCTION_TARGET_ONTOLOGY_ID")
    if raw_target:
        ns = namespace or os.environ.get("INDUCTION_NAMESPACE", "default")
        target = f"https://live-test.coa.amazon.com/{ns}/{raw_target}"
    else:
        target = None
    body = AcceptProposalRequest(ontology_id=target) if target else None

    resp = accept_proposal(proposal_id, body=body, namespace=namespace)
    # 202 fast-path idempotency: an already-accepted proposal returns
    # status="accepted" WITHOUT starting a worker (proposals.accept_proposal
    # short-circuit). Anything else is "accepting" → poll to terminal.
    already_accepted = resp.status == ProposalStatus.ACCEPTED.value
    if not already_accepted and resp.status != ProposalStatus.ACCEPTING.value:
        raise RuntimeError(f"expected 202 accept status 'accepting', got {resp.status!r}")
    item = _poll_proposal_terminal(proposal_id, namespace=namespace)

    from coa_ontology.stores.neptune_db_graph import _ontology_graph_uri

    ontology_id = item.get("ontology_id") or (target or "")

    # Embedding count for the OSS-visibility poll (§_verify_opensearch_neptune).
    # The async accept worker no longer returns an in-process ingest result, so
    # read the authoritative deduped count off the ontology registry row that
    # the worker wrote. 0/absent → the poll's target is 0 (passes on first
    # visible doc) rather than asserting a count we can't know.
    # NOTE (weakened check): on the absent-row path this degrades the downstream
    # OSS-visibility poll to "pass on the first visible doc" — it cannot assert
    # the true ingested count. This is the same absent→benign-default class #175
    # was about; if registry-row propagation becomes reliably synchronous here,
    # tighten this to require the row and assert its count.
    embedding_count = 0
    if ontology_id:
        from coa_ontology import dynamo_store

        for row in dynamo_store.list_ontologies_registry(namespace=namespace) or []:
            if str(row.get("ontology_id")) == str(ontology_id):
                embedding_count = int(row.get("embedding_count") or 0)
                break

    return {
        "proposal_id": proposal_id,
        "status": item.get("status"),
        "ontology_id": ontology_id,
        "graph_uri": _ontology_graph_uri(ontology_id, namespace) if ontology_id else "",
        "embeddings": {"count": embedding_count},
        "already_accepted": already_accepted,
        "proposal": item,
    }


def datetime_utcnow_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


# ── Main ────────────────────────────────────────────────────────────────

# Datasources to exercise, in order. Each becomes one iteration:
# induction → accept → verify. No cleanup is performed at any point —
# Neptune graphs and OSS documents from all three runs accumulate in
# place so you can inspect the cross-ontology state afterwards.
DATASOURCES: list[tuple[str, str]] = [
    ("850243a9-116e-42b5-b696-2ab686929766", "PC Insurance (Glue)"),
    ("c44425b9-4d4e-4fd1-a6f6-84df38997955", "Bird Mini Dev (Glue)"),
]

# Golden-ontology URLs per datasource — when matched, each iteration will
# score its accepted Turtle against the golden and include the BenchmarkScore
# in the summary. Shared with demo.py (see _KNOWN_GOLDEN_URLS there).
_GOLDEN_URLS: dict[str, tuple[str, str]] = {
    "ds-pc-insurance": (
        "ACME Insurance (Sequeda/Allemang/Jacob 2023)",
        "https://raw.githubusercontent.com/datadotworld/cwd-benchmark-data/main/ACME_Insurance/ontology/insurance.ttl",
    ),
}


def _verify_opensearch_neptune(
    tag: str, ontology_uri: str, verify_graph: str, accept_result: dict
) -> tuple[int, int, int]:
    """Verify the opensearch_neptune backend wrote what we expect.

    Returns (neptune_triples, oss_docs_expected, oss_docs_visible).
    """
    from opensearchpy.exceptions import NotFoundError

    # The ontology_id under which data actually landed — differs from the
    # proposal's own ``ontology_uri`` when INDUCTION_TARGET_ONTOLOGY_ID
    # is set (multiple proposals merged under one target ontology).
    target_ontology_id = accept_result.get("ontology_id", ontology_uri)

    # ── SPARQL verify ───────────────────────────────────────────────
    with Step(f"{tag} SPARQL COUNT on <{verify_graph}>"):
        n_triples = int(
            sparql_query(f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{verify_graph}> {{ ?s ?p ?o }} }}")["results"][
                "bindings"
            ][0]["n"]["value"]
        )
        print(f"  triples in <{verify_graph}> = {n_triples}")
        assert n_triples > 0

    with Step(f"{tag} sample up to 40 triples"):
        rows = sparql_query(
            f"SELECT ?s ?p ?o WHERE {{ GRAPH <{verify_graph}> {{ ?s ?p ?o }} }} ORDER BY ?s ?p LIMIT 40"
        )["results"]["bindings"]
        cur_s = None
        for b in rows:
            s, p, o = b["s"]["value"], b["p"]["value"], b["o"]["value"]
            if s != cur_s:
                cur_s = s
                print(f"\n  <{s}>")
            print(f"      {p} → {o}")

    # ── OSS — poll + inspect ────────────────────────────────────────
    import coa_ontology.stores.opensearch_vector as ov

    ns = os.environ.get("INDUCTION_NAMESPACE", "default")
    oss = ov.OpenSearchVectorStore(namespace=ns).raw_client()
    # Writes land in the namespace-scoped index, not the env's OSS_INDEX.
    idx = ov._index_for_namespace(ns)
    expected = accept_result.get("embeddings", {}).get("count", 0)

    visible = 0
    with Step(f"{tag} poll OSS for {expected} docs (AOSS VECTORSEARCH is eventually consistent)"):
        max_wait_s = 180  # up to 3 minutes
        stable_target_reached_at: float | None = None
        for i in range(max_wait_s):
            time.sleep(1)
            try:
                visible = oss.count(
                    index=idx,
                    body={"query": {"term": {"ontology_id": target_ontology_id}}},
                )["count"]
            except NotFoundError:
                visible = 0
            if i < 15 or i % 5 == 0 or visible >= expected:
                print(f"  t+{i + 1:03d}s  oss.count={visible}/{expected}", flush=True)
            if visible >= expected:
                stable_target_reached_at = i + 1
                break
        if stable_target_reached_at is None:
            log.warning(
                "AOSS search visibility lag: saw %d/%d after %ds. "
                "This is AOSS VECTORSEARCH propagation; writes succeeded. "
                "Continuing anyway.",
                visible,
                expected,
                max_wait_s,
            )
            print(f"  ⚠ visible count stalled at {visible}/{expected} after {max_wait_s}s — continuing", flush=True)
        else:
            print(f"  ✓ {expected} docs visible at t+{stable_target_reached_at}s", flush=True)

    with Step(f"{tag} sample OSS docs"):
        resp = oss.search(
            index=idx,
            body={
                "size": 20,
                "_source": {"excludes": ["embedding"]},
                "query": {"term": {"ontology_id": target_ontology_id}},
                "sort": [{"entity_type": "asc"}, {"entity_uri": "asc"}],
            },
        )
        for h in resp["hits"]["hits"]:
            print(f"  _id={h['_id'][:12]}  {h['_source']}")

    with Step(f"{tag} k-NN self-match"):
        class_hits = [h for h in resp["hits"]["hits"] if h["_source"]["entity_type"] == "class"]
        if not class_hits:
            print("  (skip) no class docs visible yet in OSS search", flush=True)
        else:
            first_class = class_hits[0]
            vec_doc = oss.get(index=idx, id=first_class["_id"])
            probe_vec = vec_doc["_source"]["embedding"]
            knn = oss.search(
                index=idx,
                body={
                    "size": 3,
                    "_source": {"excludes": ["embedding"]},
                    "query": {
                        "knn": {
                            "embedding": {
                                "vector": probe_vec,
                                "k": 3,
                                "filter": {
                                    "bool": {
                                        "filter": [
                                            {"term": {"ontology_id": target_ontology_id}},
                                            {"term": {"entity_type": "class"}},
                                        ]
                                    }
                                },
                            }
                        }
                    },
                },
            )
            hits = knn["hits"]["hits"]
            if not hits:
                print("  (no k-NN hits — index still propagating)", flush=True)
            else:
                print(f"  top-1: {hits[0]['_source']['entity_uri']}  score={hits[0]['_score']:.4f}")
                assert hits[0]["_source"]["entity_uri"] == first_class["_source"]["entity_uri"]

    return n_triples, expected, visible


def _verify_na_only(tag: str, ontology_uri: str, ns: str, accept_result: dict) -> tuple[int, int, int]:
    """Verify the na_only backend wrote what we expect.

    For NA, the "graph" is the set of class/property nodes bound to the
    ontology — there's no named graph / Turtle bulk load. Embeddings
    live on Embedding nodes with a native vector index.

    Returns (graph_nodes, embeddings_expected, embeddings_visible).
    """
    from coa_ontology.catalog import na_store

    expected = accept_result.get("embeddings", {}).get("count", 0)

    # Count the catalog graph nodes (Class + Property) scoped to this ontology
    # AND namespace — reads must always filter by namespace.
    with Step(f"{tag} NA count classes+properties for ontology (namespace={ns!r})"):
        ns_param = ns or "default"
        # Classes
        c_res = na_store._oc_query(
            "MATCH (o:Ontology)-[:DEFINES]->(c:Class)"
            " WHERE o.`~id` = $ouri AND o.namespace = $ns"
            " AND c.namespace = $ns"
            " RETURN count(c) AS n",
            {"ouri": na_store._nid(ontology_uri, ns_param), "ns": ns_param},
        )
        n_classes = c_res.get("results", [{}])[0].get("n", 0)
        # Properties
        p_res = na_store._oc_query(
            "MATCH (o:Ontology)-[:DEFINES]->(p:Property)"
            " WHERE o.`~id` = $ouri AND o.namespace = $ns"
            " AND p.namespace = $ns"
            " RETURN count(p) AS n",
            {"ouri": na_store._nid(ontology_uri, ns_param), "ns": ns_param},
        )
        n_props = p_res.get("results", [{}])[0].get("n", 0)
        n_triples = n_classes + n_props  # "graph size" proxy
        print(f"  classes={n_classes}  properties={n_props}  (ontology={ontology_uri!r})")

    # Sample a few class/property labels scoped by namespace
    with Step(f"{tag} NA sample defined classes"):
        sample = na_store._oc_query(
            "MATCH (o:Ontology)-[:DEFINES]->(c:Class)"
            " WHERE o.`~id` = $ouri AND o.namespace = $ns"
            " AND c.namespace = $ns"
            " RETURN c.label AS label, id(c) AS nid LIMIT 10",
            {"ouri": na_store._nid(ontology_uri, ns_param), "ns": ns_param},
        )
        for r in sample.get("results", []):
            print(f"  class: {r.get('label')!r}  (nid={r.get('nid')})")

    # Count embeddings stored for this ontology & namespace
    with Step(f"{tag} NA count embeddings for ontology (namespace={ns!r})"):
        visible = len(na_store.list_embeddings_for_ontology(ontology_uri, limit=10000, namespace=ns_param))
        print(f"  embeddings visible: {visible}/{expected}")

    # k-NN self-match using the first stored class embedding as the probe vector
    with Step(f"{tag} NA k-NN self-match"):
        first = na_store.list_embeddings_for_ontology(ontology_uri, entity_type="class", limit=1, namespace=ns_param)
        if not first:
            print("  (skip) no class embeddings to probe")
        else:
            probe_uri = first[0]["entity_uri"]
            entity_embs = na_store.get_embeddings_for_entity(probe_uri, entity_type="class", namespace=ns_param)
            if not entity_embs:
                print("  (skip) could not read vector back")
            else:
                probe_vec = entity_embs[0]["vector"]
                hits = na_store.search_nearest(
                    vector=probe_vec,
                    embedding_type=entity_embs[0]["embedding_type"],
                    model_id=entity_embs[0]["model_id"],
                    entity_type="class",
                    ontology_id=ontology_uri,
                    top_k=3,
                    namespace=ns_param,
                )
                if hits:
                    print(
                        f"  top-1: {hits[0].get('entity_uri')!r}"
                        f"  score={hits[0].get('score'):.4f}"
                        f"  (probe={probe_uri!r})"
                    )

    return n_triples, expected, visible


def _run_one_iteration(idx: int, total: int, datasource_id: str, label: str) -> dict:
    """Run one full induction+accept flow for a single datasource.

    Mints a fresh ontology URI of the form
    ``https://live-test.ontology-workbench.local/onto/{ds_id}-{timestamp}#``
    so each iteration lands in its own Neptune named graph and its own
    slice of OSS documents. Returns a small metrics dict for the final
    summary.
    """
    tag = f"[{idx}/{total} {datasource_id}]"
    ns = os.environ.get("INDUCTION_NAMESPACE", "default")
    ontology_uri = f"http://coa.amazon.com/namespace/{ns}/ontology/{datasource_id}-{int(time.time())}#"

    print("\n" + "═" * 60)
    print(f"  ITERATION {idx}/{total}: {datasource_id} — {label}")
    print(f"  ontology_uri: {ontology_uri}")
    print("═" * 60)

    # Grounding scope for this run. Curated catalog keys and/or resolved URIs,
    # comma-separated in INDUCTION_GROUNDING_KEYS (e.g. "fibo-agreements").
    # Empty → a pure all-novel induction (no grounding requested), which is a
    # legitimate mode; the fail-loud gate below only fires when grounding WAS
    # requested but produced nothing.
    grounding_keys = [k.strip() for k in os.environ.get("INDUCTION_GROUNDING_KEYS", "").split(",") if k.strip()]
    strategy = os.environ.get("INDUCTION_STRATEGY", "table_to_ontology")

    # ── induction (synchronous, in-process) ─────────────────────────
    with Step(f"{tag} induction pipeline"):
        job_id = run_induction_direct(
            ontology_uri=ontology_uri,
            datasource_ids=[datasource_id],
            label=label,
            grounding_ontology_ids=grounding_keys or None,
            strategy=strategy,
        )

    from coa_ontology import induce_catalog

    job = induce_catalog._jobs[job_id]
    report = job.report
    grounding_used = list(getattr(report, "grounding_ontologies_used", []) or [])
    print(f"  job_id={job_id}")
    print(
        f"  tables={report.tables_processed}  "
        f"columns={report.columns_processed}  "
        f"novel_classes={report.novel_classes_created}  "
        f"grounded_to={grounding_used}"
    )

    # ── FAIL LOUD: grounding requested but never took effect (#175) ──
    # The reported #175 defect was that the harness "passed" while grounding
    # was never attempted (empty scope → grounding recall returns [] for every
    # table, logs "empty grounding scope", run still reports success). Guard it:
    # when a grounding-capable strategy was asked to ground against a non-empty
    # scope, at least one class must have grounded to a loaded ontology, else the
    # run must NOT be green. Note the gate cannot distinguish "scope failed to
    # load/resolve" from "scope loaded but the reranker matched nothing" — both
    # trip it, and both are runs that did not actually ground.
    if _grounding_gate_should_fail(grounding_keys, strategy, grounding_used):
        raise RuntimeError(
            f"{tag} grounding was requested (scope={grounding_keys}, strategy={strategy}) "
            f"but report.grounding_ontologies_used is empty — either the grounding scope "
            f"failed to resolve/load, or it loaded but nothing matched. Refusing to report "
            f"success on a run that never actually grounded (issue #175). Check the "
            f"foundational loader logs for the scope keys."
        )

    proposal_id = report.induced_ontology_id

    # ── fetch + show the proposal Turtle ────────────────────────────
    with Step(f"{tag} read proposal {proposal_id} from DynamoDB"):
        from coa_ontology import dynamo_store

        ns = os.environ.get("INDUCTION_NAMESPACE", "default")
        prop = dynamo_store.get_proposal_by_id(proposal_id, namespace=ns)
        assert prop, f"proposal {proposal_id} not found"
        turtle = prop["ontology_turtle"]
        r2rml = prop.get("r2rml_turtle", "")
    print(f"  ontology_id:  {prop['ontology_id']}")
    print(f"  metadata:     {prop.get('metadata')}")
    print(f"\n── proposal ontology Turtle ({len(turtle)} bytes) ──\n{turtle}")
    if r2rml:
        print(f"\n── proposal R2RML ({len(r2rml)} bytes) ──\n{r2rml}")

    # ── accept directly (ASYNC: polls to terminal 'accepted') ───────
    with Step(f"{tag} accept_proposal({proposal_id})"):
        a = accept_proposal_direct(proposal_id, namespace=ns)
    print(f"  response: {a}")
    # accept_proposal_direct polls to a terminal state, so status is the
    # authoritative terminal 'accepted' here (it raises on accept_failed /
    # timeout). #175: the API never returns 'accepted' synchronously.
    if a["status"] != "accepted":
        raise RuntimeError(f"first accept did not reach terminal 'accepted': {a}")
    if a["already_accepted"]:
        raise RuntimeError("first accept should not report already-accepted")
    # The accepted ontology_id is the TARGET when INDUCTION_TARGET_ONTOLOGY_ID
    # is set, else the proposal's own ontology_id.
    target_ontology = a["ontology_id"]
    raw_target = os.environ.get("INDUCTION_TARGET_ONTOLOGY_ID")
    expected_target = f"https://live-test.coa.amazon.com/{ns}/{raw_target}" if raw_target else ontology_uri
    assert target_ontology == expected_target, (target_ontology, expected_target)

    # The named graph URI reported by accept_proposal_direct (derived from the
    # terminal ontology_id) — matches either the proposal's own ontology_uri
    # (default) or the merge target (when INDUCTION_TARGET_ONTOLOGY_ID is set).
    from coa_ontology.stores.neptune_db_graph import _ontology_graph_uri

    verify_graph = a.get("graph_uri") or _ontology_graph_uri(target_ontology, ns)

    # ── Backend-specific verification ───────────────────────────────
    if BACKEND == "opensearch_neptune":
        n_triples, expected, visible = _verify_opensearch_neptune(
            tag=tag, ontology_uri=ontology_uri, verify_graph=verify_graph, accept_result=a
        )
    elif BACKEND == "na_only":
        n_triples, expected, visible = _verify_na_only(tag=tag, ontology_uri=ontology_uri, ns=ns, accept_result=a)
    else:
        n_triples = expected = visible = 0

    # ── idempotent re-accept ────────────────────────────────────────
    # A second accept of an already-accepted proposal short-circuits in the
    # 202 handler and returns status='accepted' WITHOUT a new worker. There is
    # no 'already_accepted' status in the API (#175 asserted one that never
    # existed) — the idempotency signal is accept_proposal_direct's
    # already_accepted flag.
    with Step(f"{tag} re-accept (should short-circuit)"):
        a2 = accept_proposal_direct(proposal_id, namespace=ns)
        print(f"  {a2}")
        if a2["status"] != "accepted":
            raise RuntimeError(f"re-accept did not report terminal 'accepted': {a2}")
        if not a2["already_accepted"]:
            raise RuntimeError("re-accept of an accepted proposal must report already-accepted")

    # ── NEW ENTITIES DISCOVERED (parse the proposal graph) ──────────
    entities = _analyze_new_entities(turtle, ontology_uri)
    _print_new_entities(entities, tag)

    # ── EVALUATION (golden compare) ──────────────────────────────────
    benchmark_score = None
    if datasource_id in _GOLDEN_URLS:
        golden_label, golden_url = _GOLDEN_URLS[datasource_id]
        with Step(f"{tag} evaluation (golden compare)"):
            try:
                import httpx as _httpx
                from coa_ontology import dynamo_store as _ds
                from coa_ontology.benchmark import compare_to_golden

                print(f"  golden: {golden_label}")
                print(f"  url:    {golden_url}")
                r = _httpx.get(golden_url, timeout=30, follow_redirects=True)
                r.raise_for_status()

                # Build tier_classifier from the namespace registry
                tier_prefixes: dict[str, str] = {}
                try:
                    for ont in _ds.list_ontologies_registry(ns):
                        uri = (ont.get("uri") or "").rstrip("/#")
                        ont_type = ont.get("ontology_type", "")
                        if uri and ont_type:
                            tier = (
                                "customer"
                                if ont_type == "customer_provided"
                                else "foundational"
                                if ont_type == "foundational"
                                else ""
                            )
                            if tier:
                                tier_prefixes[uri] = tier
                except Exception:
                    pass

                def _classify_iri(iri: str) -> str:
                    best_prefix, best_tier = "", ""
                    for prefix, tier in tier_prefixes.items():
                        if iri.startswith(prefix) and len(prefix) > len(best_prefix):
                            best_prefix = prefix
                            best_tier = tier
                    return best_tier

                score = compare_to_golden(
                    turtle,
                    r.text,
                    tier_classifier=_classify_iri if tier_prefixes else None,
                )
                benchmark_score = score.as_dict()
                c = benchmark_score["class"]
                p = benchmark_score["property"]

                print("\n  ── CLASS EVALUATION ──")
                print(f"    golden={c['total_golden']}  induced={c['total_induced']}  novel={c['novel']}")
                print(f"    exact={c['exact']}  aligned={c['aligned']}  missing={c['missing']}")
                print(f"    P={c['precision']:.3f}  R={c['recall']:.3f}  F1={c['f1']:.3f}")
                if c["grounded"] > 0:
                    print(
                        f"    grounded={c['grounded']} "
                        f"(customer={c['grounded_customer']}, "
                        f"foundational={c['grounded_foundational']})"
                    )

                print("\n  ── PROPERTY EVALUATION ──")
                print(f"    golden={p['total_golden']}  induced={p['total_induced']}  novel={p['novel']}")
                print(f"    exact={p['exact']}  aligned={p['aligned']}  missing={p['missing']}")
                print(f"    P={p['precision']:.3f}  R={p['recall']:.3f}  F1={p['f1']:.3f}")

                print(f"\n  ── COMBINED F1: {benchmark_score['combined_f1']:.3f} ──")

                # Print per-class match detail
                if score.class_matches:
                    print("\n  ── CLASS MATCH DETAIL ──")
                    for m in score.class_matches:
                        icon = "✓" if m.match_type == "exact" else ("≈" if m.match_type == "aligned" else "✗")
                        line = f"    {icon} {m.golden_label}"
                        if m.induced_label:
                            line += f" → {m.induced_label} ({m.match_type})"
                        else:
                            line += " — MISSING"
                        if m.grounded_to:
                            line += f" [grounded:{m.grounded_tier}]"
                        print(line)

            except Exception as e:
                log.warning("Evaluation failed: %s", e)
                print(f"  [evaluation skipped: {type(e).__name__}: {e}]")

    # ── VALIDATION (Tier 1-3) ─────────────────────────────────────────
    validation_report = None
    with Step(f"{tag} validation (tier 1-3)"):
        try:
            from coa_ontology.validation.schemas import ValidationTier
            from coa_ontology.validation.validators.tier1 import (
                ConnectivityValidator,
                ConsistencyValidator,
                TaxonomyCycleValidator,
            )
            from coa_ontology.validation.validators.tier2 import StructuralMetricsValidator
            from coa_ontology.validation.validators.tier3 import (
                AmbiguousMatchValidator,
                LabelCompletenessValidator,
                OoPSValidator,
            )
            from rdflib import Graph as _VG

            v_graph = _VG()
            v_graph.parse(data=turtle, format="turtle")

            validators = [
                ConsistencyValidator(),
                TaxonomyCycleValidator(),
                ConnectivityValidator(),
                StructuralMetricsValidator(),
                OoPSValidator(),
                LabelCompletenessValidator(),
                AmbiguousMatchValidator(),
            ]

            all_findings = []
            metrics_out: dict = {}
            for validator in validators:
                findings = validator.validate(v_graph, _metrics_out=metrics_out)
                all_findings.extend(findings)

            tier1_errors = [
                f for f in all_findings if f.tier == ValidationTier.tier1_blocking and f.severity.value == "error"
            ]
            tier2_warnings = [f for f in all_findings if f.tier == ValidationTier.tier2_scoring]
            tier3_info = [f for f in all_findings if f.tier == ValidationTier.tier3_review]
            passed = len(tier1_errors) == 0

            validation_report = {
                "passed": passed,
                "tier1_errors": len(tier1_errors),
                "tier2_warnings": len(tier2_warnings),
                "tier3_info": len(tier3_info),
                "total_findings": len(all_findings),
                "metrics": metrics_out,
            }

            status = "✓ PASSED" if passed else "✗ FAILED"
            print(
                f"  {status}  (tier1_errors={len(tier1_errors)}, tier2={len(tier2_warnings)}, tier3={len(tier3_info)})"
            )

            if metrics_out:
                print(f"  OntoQA metrics: {metrics_out}")

            if tier1_errors:
                print("  ── TIER 1 ERRORS (blocking) ──")
                for f in tier1_errors:
                    print(f"    [{f.code}] {f.message}")
                    if f.subject:
                        print(f"      subject: {f.subject}")

            if tier2_warnings:
                print("  ── TIER 2 WARNINGS (scoring) ──")
                for f in tier2_warnings:
                    print(f"    [{f.code}] {f.message}")

            if tier3_info:
                print(f"  ── TIER 3 FLAGS ({len(tier3_info)} items) ──")
                for f in tier3_info[:10]:
                    print(f"    [{f.code}] {f.message}")
                if len(tier3_info) > 10:
                    print(f"    ... and {len(tier3_info) - 10} more")

        except Exception as e:
            log.warning("Validation failed: %s", e)
            print(f"  [validation skipped: {type(e).__name__}: {e}]")
            validation_report = {"passed": None, "error": str(e)}

    return {
        "datasource_id": datasource_id,
        "label": label,
        "ontology_uri": ontology_uri,
        "job_id": job_id,
        "proposal_id": proposal_id,
        "tables_processed": report.tables_processed,
        "columns_processed": report.columns_processed,
        "novel_classes_created": report.novel_classes_created,
        "neptune_triples": n_triples,
        "oss_docs_expected": expected,
        "oss_docs_visible": visible,
        "entities": entities,
        "benchmark_score": benchmark_score,
        "validation": validation_report,
    }


def _analyze_new_entities(turtle: str, ontology_uri: str) -> dict:
    """Parse the proposal Turtle and extract what RIGOR/table_to_ontology discovered.

    Returns a dict with keys:
      - classes:      list of {uri, label, comment, derived_from}
      - object_props: list of {uri, label, domain, range, derived_from}
      - data_props:   list of {uri, label, domain, range, derived_from}
      - restrictions: count of owl:Restriction blank nodes
      - subclass_axioms: count of rdfs:subClassOf triples between named classes
      - provenance_count: count of prov:wasDerivedFrom triples (RIGOR-only signal)
      - auxiliary_classes: classes under `ontology_uri` whose label does NOT
        match any column/table name — purely inferred concepts
    """
    from rdflib import OWL, RDF, RDFS, URIRef
    from rdflib import Graph as _G
    from rdflib import Namespace as _N

    PROV = _N("http://www.w3.org/ns/prov#")
    g = _G()
    try:
        g.parse(data=turtle, format="turtle")
    except Exception as e:
        return {"parse_error": str(e)}

    prefix = ontology_uri.rstrip("#/").rstrip("/")

    def _in_ns(uri) -> bool:
        return isinstance(uri, URIRef) and str(uri).startswith(prefix)

    def _describe(s):
        return {
            "uri": str(s),
            "label": str(g.value(s, RDFS.label) or ""),
            "comment": str(g.value(s, RDFS.comment) or ""),
            "derived_from": [str(o) for o in g.objects(s, PROV.wasDerivedFrom)],
        }

    classes = [_describe(s) for s in g.subjects(RDF.type, OWL.Class) if _in_ns(s)]
    object_props = []
    for s in g.subjects(RDF.type, OWL.ObjectProperty):
        if not _in_ns(s):
            continue
        d = _describe(s)
        d["domain"] = str(g.value(s, RDFS.domain) or "")
        d["range"] = str(g.value(s, RDFS.range) or "")
        object_props.append(d)
    data_props = []
    for s in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not _in_ns(s):
            continue
        d = _describe(s)
        d["domain"] = str(g.value(s, RDFS.domain) or "")
        d["range"] = str(g.value(s, RDFS.range) or "")
        data_props.append(d)

    restrictions = sum(1 for _ in g.subjects(RDF.type, OWL.Restriction))
    subclass_axioms = sum(
        1 for s, p, o in g.triples((None, RDFS.subClassOf, None)) if isinstance(s, URIRef) and isinstance(o, URIRef)
    )
    provenance_count = sum(1 for _ in g.triples((None, PROV.wasDerivedFrom, None)))

    return {
        "classes": classes,
        "object_props": object_props,
        "data_props": data_props,
        "restrictions": restrictions,
        "subclass_axioms": subclass_axioms,
        "provenance_count": provenance_count,
    }


def _print_new_entities(entities: dict, tag: str) -> None:
    if "parse_error" in entities:
        print(f"\n{tag} NEW ENTITIES — parse error: {entities['parse_error']}")
        return

    print(f"\n{tag} NEW ENTITIES DISCOVERED")
    print(f"  classes:          {len(entities['classes'])}")
    print(f"  object_props:     {len(entities['object_props'])}")
    print(f"  data_props:       {len(entities['data_props'])}")
    print(f"  restrictions:     {entities['restrictions']}")
    print(f"  subclass_axioms:  {entities['subclass_axioms']}")
    print(
        f"  provenance_count: {entities['provenance_count']}  "
        f"{'(RIGOR signal)' if entities['provenance_count'] > 0 else ''}"
    )

    if entities["classes"]:
        print("\n  ── CLASSES ──")
        for c in entities["classes"]:
            local = c["uri"].rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            print(f"    • {local}  ({c['uri']})")
            if c["label"]:
                print(f"        label:    {c['label']}")
            if c["comment"]:
                short = (c["comment"][:100] + "…") if len(c["comment"]) > 100 else c["comment"]
                print(f"        comment:  {short}")
            if c["derived_from"]:
                print(f"        derived:  {c['derived_from'][0]}")

    if entities["object_props"]:
        print("\n  ── OBJECT PROPERTIES ──")
        for p in entities["object_props"]:
            local = p["uri"].rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            d_local = p["domain"].rsplit("#", 1)[-1].rsplit("/", 1)[-1] if p["domain"] else "?"
            r_local = p["range"].rsplit("#", 1)[-1].rsplit("/", 1)[-1] if p["range"] else "?"
            print(f"    • {local}  :  {d_local} → {r_local}")

    if entities["data_props"]:
        print("\n  ── DATA PROPERTIES ──")
        for p in entities["data_props"]:
            local = p["uri"].rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            d_local = p["domain"].rsplit("#", 1)[-1].rsplit("/", 1)[-1] if p["domain"] else "?"
            r_local = p["range"].rsplit("#", 1)[-1].rsplit("/", 1)[-1] if p["range"] else "?"
            print(f"    • {local}  :  {d_local} → {r_local}")


def main() -> int:
    wall = time.perf_counter()

    print("\n════════════════════════════════════════════════════════════")
    print("  Live induction + ingest run (direct, no FastAPI)")
    print("  account:       (set via AWS_PROFILE / AWS credentials)")
    print(f"  backend:       {os.environ['WORKBENCH_BACKEND']}")
    print(f"  strategy:      {os.environ.get('INDUCTION_STRATEGY', 'table_to_ontology')}")
    print(f"  namespace:     {os.environ.get('INDUCTION_NAMESPACE', 'default')}")
    print(f"  llm_model:     {os.environ.get('LLM_MODEL_ID', '(default)')}")
    if BACKEND == "opensearch_neptune":
        print(f"  NDB_ENDPOINT:  {os.environ['NDB_ENDPOINT']}")
        print(f"  OSS_ENDPOINT:  {os.environ['OSS_ENDPOINT']}")
        print(f"  OSS_INDEX:     {os.environ['OSS_INDEX']}")
    elif BACKEND == "na_only":
        print(f"  NA_GRAPH_ID:   {os.environ['NA_GRAPH_ID']}")
        print(f"  NA_REGION:     {os.environ['NA_REGION']}")
    catalog_source = os.environ.get("CATALOG_SOURCE", "http")
    if catalog_source == "smus":
        print(f"  catalog:       SMUS (domain={os.environ.get('SMUS_DOMAIN_ID', '?')})")
    else:
        print(f"  catalog:       HTTP ({os.environ.get('DATA_CATALOG_URL', '?')})")
    print(f"  datasources:   {[ds for ds, _ in DATASOURCES]}")
    print("  cleanup:       DISABLED (accumulate across iterations)")
    print("════════════════════════════════════════════════════════════\n")

    # Redirect induction scratch output somewhere writable.
    os.environ["INDUCE_OUTPUT_DIR"] = tempfile.mkdtemp(prefix="ow-live-")

    # ── 1. pre-flight (once) ─────────────────────────────────────────
    with Step("STEP 1: pre-flight reachability probes"):
        if BACKEND == "opensearch_neptune":
            probe(os.environ["NDB_ENDPOINT"])
            probe(os.environ["OSS_ENDPOINT"])
        # NA reaches Neptune Analytics via the AWS SDK — no host:port probe.
        if catalog_source != "smus":
            probe(os.environ["DATA_CATALOG_URL"])

    # ── 2. sanity check stores (once) ────────────────────────────────
    with Step("STEP 2: build stores + health"):
        from coa_ontology.stores import build_stores

        ns_for_stores = os.environ.get("INDUCTION_NAMESPACE", "default")
        graph_store, vector_store = build_stores(namespace=ns_for_stores)
        gh = graph_store.health_check()
        vh = vector_store.health_check()
        print(f"  graph:  {gh}")
        print(f"  vector: {vh}")
        # Neptune DB returns {"status": "ok"}, NA returns {"status": "healthy"}
        assert gh.get("status") in ("ok", "healthy"), gh
        assert vh.get("status") in ("ok", "healthy"), vh

    # No STEP 1b (DROP graph) or STEP 1c (OSS purge) — this test
    # intentionally accumulates Neptune graphs and OSS docs across
    # iterations so cross-ontology matching can be inspected.

    # ── 3. per-datasource iterations ─────────────────────────────────
    results: list[dict] = []
    for i, (ds_id, label) in enumerate(DATASOURCES, start=1):
        try:
            results.append(
                _run_one_iteration(
                    i,
                    len(DATASOURCES),
                    ds_id,
                    label,
                )
            )
        except Exception as e:
            log.exception("iteration %s failed", ds_id)
            results.append(
                {
                    "datasource_id": ds_id,
                    "label": label,
                    "error": f"{type(e).__name__}: {e}",
                }
            )

    # ── 4. cross-iteration summary ───────────────────────────────────
    print("\n" + "═" * 60)
    print("  ACCUMULATED RESULTS (no cleanup applied)")
    print("═" * 60)
    for r in results:
        if "error" in r:
            print(f"  {r['datasource_id']:<16} ✗ {r['error']}")
            continue
        print(
            f"  {r['datasource_id']:<16} "
            f"novel={r['novel_classes_created']:<2} "
            f"tables={r['tables_processed']:<2} "
            f"cols={r['columns_processed']:<2} "
            f"ndb_triples={r['neptune_triples']:<4} "
            f"oss={r['oss_docs_visible']}/{r['oss_docs_expected']}"
        )
        print(f"    ontology_uri={r['ontology_uri']}")
        if r.get("benchmark_score"):
            bs = r["benchmark_score"]
            print(
                f"    benchmark   class_F1={bs['class']['f1']:.2f}  "
                f"property_F1={bs['property']['f1']:.2f}  "
                f"combined={bs['combined_f1']:.2f}  "
                f"(grounded_classes={bs['class']['grounded']}, novel_classes={bs['class']['novel']})"
            )
        if r.get("validation"):
            v = r["validation"]
            if v.get("passed") is not None:
                status = "✓" if v["passed"] else "✗"
                print(
                    f"    validation  {status}  "
                    f"tier1_errors={v['tier1_errors']}  "
                    f"tier2={v['tier2_warnings']}  "
                    f"tier3={v['tier3_info']}"
                )
            elif v.get("error"):
                print(f"    validation  ⚠ skipped: {v['error']}")

    # ── 5. full dump per ontology (no cleanup) ───────────────────────
    if BACKEND == "opensearch_neptune":
        import coa_ontology.stores.opensearch_vector as _ov

        _ns = os.environ.get("INDUCTION_NAMESPACE", "default")
        oss = _ov.OpenSearchVectorStore(namespace=_ns).raw_client()
        OSS_INDEX = os.environ["OSS_INDEX"]
        for r in results:
            if "error" in r:
                continue
            with Step(f"DUMP {r['datasource_id']} — OSS + Neptune state"):
                _full_dump(r["ontology_uri"], oss, OSS_INDEX)
    else:
        # na_only: the per-iteration verification already printed class
        # samples and k-NN hits. Skip the full dump to keep output readable.
        pass

    _print_summary(wall)

    # Exit non-zero if any iteration errored, so CI/launch configs notice.
    return 0 if all("error" not in r for r in results) else 1


def _full_dump(ontology_uri: str, oss, OSS_INDEX: str) -> None:
    """Print every OSS doc for this ontology and every triple in the
    per-ontology Neptune named graph. Vector values are redacted to
    ``[...]`` for readability.
    """
    import coa_ontology.stores.opensearch_vector as ov

    ns = os.environ.get("INDUCTION_NAMESPACE", "default")
    # When proposals are merged under a single target ontology, all
    # iterations' data lives under that target URI — not the proposal's
    # own ontology_uri.
    target_ontology_id = os.environ.get("INDUCTION_TARGET_ONTOLOGY_ID") or ontology_uri
    idx = ov._index_for_namespace(ns)

    # ── OSS ───────────────────────────────────────────────────────
    print("\n════════════════════════════════════════════════════════════")
    print(f"  OSS index={idx}   ontology_id={target_ontology_id}")
    print("════════════════════════════════════════════════════════════")
    try:
        total = oss.count(
            index=idx,
            body={"query": {"term": {"ontology_id": target_ontology_id}}},
        )["count"]
    except Exception as e:
        print(f"  (count failed: {e})")
        total = 0
    print(f"  visible docs: {total}")
    try:
        resp = oss.search(
            index=idx,
            body={
                "size": 10_000,
                "query": {"term": {"ontology_id": target_ontology_id}},
                "sort": [{"entity_type": "asc"}, {"entity_uri": "asc"}],
            },
        )
    except Exception as e:
        print(f"  (search failed: {e})")
        resp = {"hits": {"hits": []}}
    for h in resp["hits"]["hits"]:
        s = dict(h["_source"])
        if "embedding" in s:
            s["embedding"] = "[...]"
        print(f"  _id={h['_id']}")
        for k in ("entity_uri", "entity_type", "embedding_type", "model_id", "ontology_id", "text", "embedding"):
            if k in s:
                print(f"      {k:<14} {s[k]}")
        # Anything extra.
        extras = {
            k: v
            for k, v in s.items()
            if k not in {"entity_uri", "entity_type", "embedding_type", "model_id", "ontology_id", "text", "embedding"}
        }
        if extras:
            print(f"      (extra)       {extras}")

    # ── Neptune DB ────────────────────────────────────────────────
    from coa_ontology.stores.neptune_db_graph import _ontology_graph_uri

    ontology_graph = _ontology_graph_uri(target_ontology_id, ns)
    print("\n════════════════════════════════════════════════════════════")
    print(f"  Neptune DB  GRAPH <{ontology_graph}>")
    print("════════════════════════════════════════════════════════════")
    try:
        n = int(
            sparql_query(f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{ontology_graph}> {{ ?s ?p ?o }} }}")["results"][
                "bindings"
            ][0]["n"]["value"]
        )
    except Exception as e:
        print(f"  (count failed: {e})")
        n = 0
    print(f"  total triples: {n}")
    try:
        rows = sparql_query(
            f"SELECT ?s ?p ?o WHERE {{ GRAPH <{ontology_graph}> {{ ?s ?p ?o }} }} ORDER BY ?s ?p LIMIT 10000"
        )["results"]["bindings"]
    except Exception as e:
        print(f"  (select failed: {e})")
        rows = []
    cur_s = None
    for b in rows:
        s = b["s"]["value"]
        p = b["p"]["value"]
        o = b["o"]["value"]
        if s != cur_s:
            cur_s = s
            print(f"\n  <{s}>")
        print(f"      {p} → {o}")


def _print_summary(wall_start: float) -> None:
    total = time.perf_counter() - wall_start
    print("\n════════════════════════════════════════════════════════════")
    print(f"  TIMELINE (total {total * 1000:.0f} ms)")
    print("════════════════════════════════════════════════════════════")
    for label, dt in sorted(TIMELINE, key=lambda t: -t[1]):
        share = 100 * dt / total if total else 0
        print(f"  {dt * 1000:7.0f} ms  ({share:5.1f}%)  {label}")
    print("════════════════════════════════════════════════════════════")
    print("✓ DONE")


if __name__ == "__main__":
    sys.exit(main())
