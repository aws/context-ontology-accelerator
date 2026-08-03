# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Workbench /induce router — accepts datasource_ids instead of table_ids.

Fetches catalog metadata from the data-catalog service, extracts concepts,
embeds via Bedrock Titan, matches against existing ontology classes, then
produces a proposal ontology plus R2RML mappings. Both are saved to disk.
"""

import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Thread
from typing import Any

import httpx
from coa_common.bedrock_metrics import (
    CostTracker,
    emit_induction_heartbeat_metrics,
    emit_induction_job_metrics,
)
from fastapi import APIRouter, HTTPException, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from coa_ontology import dynamo_store
from coa_ontology.inducer.strategies import get_strategy

router = APIRouter()
_log = logging.getLogger(__name__)


@contextlib.contextmanager
def _stage_timer(stage: str, tracker: CostTracker) -> Iterator[None]:
    """Record ``{stage}DurationMs`` into ``tracker`` on exit, however the block leaves.

    Records via the per-job ``CostTracker`` (single value per stage) so the
    duration is published through the same PutMetricData emit that carries cost
    — NOT EMF-stdout, which is never extracted on this Fargate task. Fires on
    normal fall-through, an early ``return``, or an exception propagating out.
    """
    t0 = time.perf_counter()
    try:
        yield
    finally:
        tracker.record_stage_duration(stage, (time.perf_counter() - t0) * 1000)


# Cosmetic SQL-type spelling variants → a canonical family token. Only collapses
# differences that are pure dialect/spelling (Postgres introspection vs Glue,
# case, length specifiers) — genuinely different families stay distinct so a
# real type change still produces a new fingerprint.
_TYPE_FAMILY = {
    "int": "int",
    "integer": "int",
    "bigint": "int",
    "smallint": "int",
    "tinyint": "int",
    "serial": "int",
    "bigserial": "int",
    "float": "float",
    "double": "float",
    "double precision": "float",
    "real": "float",
    "decimal": "decimal",
    "numeric": "decimal",
    "money": "decimal",
    "varchar": "string",
    "character varying": "string",
    "char": "string",
    "character": "string",
    "text": "string",
    "string": "string",
    "uuid": "string",
    "boolean": "bool",
    "bool": "bool",
    "date": "date",
    "timestamp": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp",
    "datetime": "timestamp",
    "time": "time",
    "time without time zone": "time",
}


def _canonical_type(data_type: str) -> str:
    """Collapse cosmetic SQL-type spelling differences to a family token.

    ``character varying`` ≡ ``VARCHAR`` ≡ ``varchar(255)`` → ``string``;
    ``integer`` ≡ ``INT`` → ``int``. Unknown types fall back to the lowercased
    base (sans length specifier) so they stay distinct rather than all
    collapsing to one bucket.
    """
    base = (data_type or "").split("(")[0].strip().lower()
    return _TYPE_FAMILY.get(base, base or "unknown")


def _compute_structural_fingerprint(tables) -> str:
    """Deterministic hash of table structure (names, columns, canonical types, FKs).

    Canonicalized so two scans of the SAME logical schema dedupe even when the
    connector reports types/constraints differently:

      - **dataType** is normalized to a family token via ``_canonical_type``,
        so ``character varying`` (Postgres introspection) and ``VARCHAR``
        (Glue) hash identically while genuinely different families differ.
      - **constraint** is excluded: column-level constraint *detection* varies
        by connector (a JDBC scan may report ``constraint=None`` where a Glue
        scan reports ``PRIMARY_KEY``) and isn't part of the class's structural
        identity. FK *relationships* are still hashed (below) since they shape
        the ontology's object properties.
      - **FK referredColumns** are reduced to their ``table.column`` tail so a
        fully-qualified ``db.customers.id`` matches a bare ``customers.id``.
    """
    import hashlib

    def _norm_ref(ref: str) -> str:
        # Keep only the trailing table.column (drop catalog/db qualifiers), using
        # the one shared parser so the fingerprint agrees with every consumer.
        from coa_ontology.inducer.services.data_catalog import parse_referred_column

        table, column = parse_referred_column(ref)
        return f"{table}.{column}" if column is not None else table

    parts = []
    for t in sorted(tables, key=lambda x: x.name):
        cols = sorted([(c.name, _canonical_type(c.dataType)) for c in t.columns])
        fks = []
        if t.tableConstraints:
            for tc in t.tableConstraints:
                if tc.constraintType == "FOREIGN_KEY" and tc.columns and tc.referredColumns:
                    fks.append((tuple(sorted(tc.columns)), tuple(sorted(_norm_ref(r) for r in tc.referredColumns))))
        parts.append((t.name, tuple(cols), tuple(sorted(fks))))
    return hashlib.sha256(str(parts).encode()).hexdigest()[:16]


def _floats_to_decimal(obj):
    """Recursively convert float values to Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        import math

        if math.isnan(obj) or math.isinf(obj):
            return None
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(i) for i in obj]
    return obj


class NamespaceNotFoundError(ValueError):
    """Raised when a namespace doesn't exist in the namespaces table."""


class NamespaceMissingProjectError(ValueError):
    """Raised when a namespace exists but has no DataZone project provisioned."""


_jobs: dict = {}
_job_namespaces: dict[str, str] = {}  # job_id → namespace

OUTPUT_DIR = "./output/induce"  # default; read at call time via _output_dir()


def _output_dir() -> str:
    return os.getenv("INDUCE_OUTPUT_DIR", OUTPUT_DIR)


class WorkbenchInductionRequest(BaseModel):
    """Kick off ontology induction from datasource catalog(s).

    The namespace is bound from the request's ``?namespace=`` query param,
    not the body — keep it off this schema so a caller can't pass a
    different namespace than the one their token authorized.
    """

    # Accept BOTH snake_case (this hand-written schema's names) and the Smithy
    # contract's camelCase wire names, so a request built from the generated
    # client (groundingMode / groundingOntologyIds) binds correctly instead of
    # silently falling back to defaults. populate_by_name keeps the snake_case
    # callers working.
    model_config = ConfigDict(populate_by_name=True)

    datasource_ids: list[str] = []
    ontology_uri_prefix: str
    label: str | None = None
    embedding_backend: str | None = None
    confidence_threshold: float = 0.80
    grounding_ontology_ids: list[str] | None = Field(
        default=None, validation_alias=AliasChoices("grounding_ontology_ids", "groundingOntologyIds")
    )
    scoring_strategy: str = "lexical"
    structural_weight: float = 0.05
    strategy: str = "table_to_ontology"  # "table_to_ontology" | "rigor_ontology" | "unstructured_lexical_graph"
    grounding_mode: str = Field(
        default="ENHANCED", validation_alias=AliasChoices("grounding_mode", "groundingMode")
    )  # "NONE" | "STANDARD" | "ENHANCED"
    graph_arn: str | None = None

    @field_validator("ontology_uri_prefix")
    @classmethod
    def _normalize_uri_prefix(cls, v: str) -> str:
        """Ensure the prefix ends in ``#`` or ``/`` so consumers mint the same IRIs.

        ``is_mapped`` is derived by string-matching the induced ontology's class
        IRI against the R2RML ``rr:class`` IRI (ingest.py). The two are minted by
        different code paths: ``build_r2rml`` (base.py) normalizes a bare prefix
        by appending ``#``, but ``_build_proposal_ontology`` (table_to_ontology.py)
        does ``Namespace(uri_prefix)`` verbatim. A caller passing a bare prefix
        (``http://x/onto``) would therefore mint ``http://x/ontoOrders`` for the
        class but ``http://x/onto#Orders`` for the rr:class → they never match →
        EVERY class is marked unmapped → the whole namespace goes dark to Tier-2.
        The same value is also stored as the ontology_id / graph URI, so
        normalizing once here keeps all downstream consumers consistent.
        """
        v = v.strip()
        if v and not v.endswith(("#", "/")):
            v += "#"
        return v


def _fetch_catalog(data_catalog_url: str, datasource_id: str) -> dict:
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{data_catalog_url}/api/v1/catalogs/{datasource_id}")
        resp.raise_for_status()
        return resp.json()


def _resolve_datazone_project_id(namespace_id: str, namespaces_table: str, region: str) -> tuple[str, str]:
    """Look up the DataZone project ID and resolved namespace UUID.

    The namespace row stores ``dataZoneProjectId`` — provisioned at namespace
    creation time. Accepts either a namespace UUID or name (resolves via
    the shared ``resolve_namespace_id`` helper).

    Returns:
        Tuple of (project_id, resolved_namespace_id) where resolved_namespace_id
        is always the UUID regardless of whether name or UUID was passed.
    """
    import boto3
    from coa_common import resolve_namespace_id

    table = boto3.resource("dynamodb", region_name=region).Table(namespaces_table)
    try:
        namespace_id = resolve_namespace_id(namespace_id, table)
    except ValueError as e:
        raise NamespaceNotFoundError(str(e)) from e

    resp = table.get_item(Key={"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    item = resp.get("Item")
    if not item:
        raise NamespaceNotFoundError(f"Namespace '{namespace_id}' not found in {namespaces_table}")
    project_id = item.get("dataZoneProjectId")
    if not project_id:
        raise NamespaceMissingProjectError(
            f"Namespace '{namespace_id}' has no dataZoneProjectId — not provisioned with a DataZone project"
        )
    return project_id, namespace_id


def _fetch_catalog_from_smus(config: dict, datasource_id: str, namespace: str) -> dict:
    """Fetch approved catalog metadata for a datasource from DataZone.

    Looks up the DataZone project ID from the namespaces DDB table, then
    reads the approved-asset metadata for the datasource via DataZone APIs.
    Wraps namespace-resolution errors as HTTP responses for FastAPI route
    callers; the typed exceptions propagate to non-route callers (background
    threads call ``_resolve_datazone_project_id`` directly if they need to
    handle them differently).
    """
    from coa_common.metadata_store.catalog_reader import read_approved_catalog

    if not namespace:
        raise HTTPException(400, "namespace is required")
    namespaces_table = config.get("namespaces_table") or os.environ.get("NAMESPACES_TABLE", "")
    if not namespaces_table:
        raise HTTPException(500, "NAMESPACES_TABLE env var not configured")
    region = config.get("smus_region") or os.environ.get("AWS_REGION", "us-east-1")
    if not region:
        raise HTTPException(500, "AWS_REGION env var not configured")

    try:
        project_id, resolved_ns_id = _resolve_datazone_project_id(namespace, namespaces_table, region)
    except NamespaceNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except NamespaceMissingProjectError as e:
        raise HTTPException(409, str(e)) from e

    result = read_approved_catalog(
        domain_id=config["smus_domain_id"],
        project_id=project_id,
        namespace_id=resolved_ns_id,
        data_source_ids=[datasource_id],
        datasources_table=(
            config.get("sources_table")
            or os.environ.get("SOURCES_TABLE", "")
            or config.get("smus_datasources_table")
            or os.environ.get("DATASOURCES_TABLE", "")
        ),
        region=region,
    )
    for source in result.get("sources", []):
        if source.get("datasourceId") == datasource_id:
            return source
    return {"databases": []}


def _fetch_catalog_from_fixture(datasource_id: str) -> dict:
    """Load catalog metadata from a bundled JSON fixture.

    Used for mock/test datasources whose IDs end in '-mock'. Reads from
    /app/fixtures/sample_data/<datasource>.json (Docker layout) or the
    local source tree fallback.
    """
    import json
    from pathlib import Path

    fixture_root = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "data-catalog" / "sample_data"
    candidates = [
        Path("/app/fixtures/sample_data/pc_insurance.json"),
        fixture_root / "pc_insurance.json",
    ]
    for p in candidates:
        if p.exists():
            data = json.loads(p.read_text())
            for source in data.get("sources", []):
                if source.get("datasourceId") in (datasource_id, datasource_id.replace("-mock", "")):
                    return source
            return {"databases": []}
    return {"databases": []}


def _catalog_to_tables(catalog: dict) -> list[dict]:
    """Flatten catalog → databases → tables into CatalogTable-compatible dicts."""
    tables = []
    for db in catalog.get("databases", []):
        db_name = db.get("name", "")
        for tbl in db.get("tables", []):
            bm = tbl.get("businessMetadata", {})
            columns = []
            for col in tbl.get("columns", []):
                cbm = col.get("businessMetadata", {})
                columns.append(
                    {
                        "name": col["name"],
                        "dataType": col.get("type", ""),
                        "description": cbm.get("description"),
                        "synonyms": cbm.get("synonyms", []),
                        # Sampled distinct values (low-cardinality categorical cols)
                        # flow into the induced ontology for serve NL→SQL enum hints.
                        "distinctValues": col.get("distinctValues", []),
                        "constraint": "PRIMARY_KEY"
                        if col["name"] in ((tbl.get("primaryKey") or {}).get("columns") or [])
                        else None,
                    }
                )
            # ``relationshipType`` carries the FK provenance tag
            # (DETERMINISTIC / AI_INFERRED / STEWARD_SPECIFIED / …) emitted by
            # ``catalog_reader.py`` from ``ForeignKey.source``. The inducer
            # reads this off ``CatalogConstraint.relationshipType`` to emit
            # ``coa:fkProvenance`` annotations on the OWL property — which
            # lets reviewers tell auto-detected FKs apart from LLM-inferred
            # ones in the proposal UI.
            fk_constraints = [
                {
                    "constraintType": "FOREIGN_KEY",
                    "columns": [fk["column"]],
                    "referredColumns": [f"{fk['targetTable']}.{fk['targetColumn']}"],
                    "relationshipType": fk.get("source") or None,
                }
                for fk in tbl.get("foreignKeys", [])
            ]
            pk = tbl.get("primaryKey")
            pk_constraints = (
                [
                    {
                        "constraintType": "PRIMARY_KEY",
                        "columns": pk["columns"],
                        "relationshipType": pk.get("source") or None,
                    }
                ]
                if pk
                else []
            )

            tables.append(
                {
                    "id": f"{db_name}.{tbl['name']}",
                    "name": tbl["name"],
                    "fullyQualifiedName": f"{db_name}.{tbl['name']}",
                    "description": bm.get("description"),
                    "synonyms": bm.get("synonyms", []),
                    "columns": columns,
                    "tableConstraints": pk_constraints + fk_constraints or None,
                }
            )
    return tables


# ── Foundational lazy-load (shared by structured + unstructured) ─────────
def resolve_grounding_ontology_ids(
    *,
    grounding_keys: list[str],
    namespace: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Resolve grounding selections to loaded-ontology URIs.

    Shared by BOTH induction pipelines (structured ``table_to_ontology``
    and unstructured ``lexical_graph``) so they ground against loaded
    ontologies identically. For each selection:

    1. Resolve a curated-catalog KEY ("fibo-agreements") to its URI, or
       accept an already-resolved URI as-is.
    2. If the namespace registry already has that URI with a non-empty
       embedding index, reuse it.
    3. Otherwise fire the foundational loader (curated entries only) so
       embeddings exist before grounding recall runs.

    Returns the list of resolved URIs suitable to pass as
    ``ontology_ids`` to the grounding service / ``match_concepts``. Skips
    selections that don't resolve or fail to load (logged warning) and
    never raises on individual failures — one broken foundational must not
    fail the whole induction. Idempotent: an already-embedded registry row
    is never re-loaded.

    Args:
        grounding_keys: FE-supplied curated keys and/or URIs.
        namespace: The induction namespace.
        on_progress: Optional ``(loaded_count, total)`` callback invoked
            after each selection is resolved, so each caller can surface
            the ``loading_grounding_ontologies`` stage in its own
            ``source_type`` (structured vs unstructured) without this pure
            resolver knowing about job records.
    """
    from coa_ontology.catalog.routers.foundational import _BY_KEY, _load_foundational_and_wait

    log = logging.getLogger(__name__)
    total = len(grounding_keys)
    if on_progress is not None:
        on_progress(0, total)

    # Index existing namespace ontologies by URI to detect "already loaded".
    existing = dynamo_store.list_ontologies_registry(namespace=namespace) or []
    by_uri = {row.get("uri"): row for row in existing if row.get("uri")}

    resolved: list[str] = []
    loaded_count = 0
    for key in grounding_keys:
        # Frontend may send either curated keys ("fibo-agreements") or
        # already-resolved URIs. Detect by lookup in _BY_KEY.
        entry = _BY_KEY.get(key)
        if entry is not None:
            uri = entry.uri
        elif key.startswith(("http://", "https://", "urn:")):
            uri = key
        else:
            log.warning("grounding_key_unresolved", extra={"key": key, "namespace": namespace})
            continue

        existing_row = by_uri.get(uri)
        already_loaded = bool(existing_row and (existing_row.get("embedding_count") or 0) > 0)
        if already_loaded:
            resolved.append(uri)
            loaded_count += 1
            if on_progress is not None:
                on_progress(loaded_count, total)
            continue

        # Not loaded — load the foundational SYNCHRONOUSLY and wait until its
        # embeddings are searchable BEFORE appending it to the grounding pool.
        # The async load_foundational_ontology endpoint returns before embeddings
        # exist, so appending the URI off the back of it would ground against an
        # empty index (silent 0-hit grounding). Only append on a COMPLETED load.
        # Tolerate per-key failures so one broken foundational doesn't kill the
        # induction.
        try:
            if entry is not None:
                if _load_foundational_and_wait(entry, namespace=namespace):
                    resolved.append(uri)
                    loaded_count += 1
                else:
                    log.warning(
                        "grounding_load_incomplete key=%s uri=%s ns=%s — skipping (not appended to pool)",
                        key,
                        uri,
                        namespace,
                    )
                    continue
            else:
                # User passed a URI directly without a curated catalog
                # entry — we can't auto-fetch in that case; skip with a
                # warning so the user sees no grounding match against it.
                log.warning(
                    "grounding_uri_not_in_catalog",
                    extra={"uri": uri, "namespace": namespace},
                )
                continue
        except Exception as exc:
            # Inline the key/uri/error in the message itself rather than
            # extra={...} -- the default uvicorn stream handler drops
            # extra fields, which made past failures invisible. Keep
            # extra populated so structured-logging back-ends still see
            # the dimensions.
            log.warning(
                "grounding_load_failed key=%s uri=%s error=%r",
                key,
                uri,
                exc,
                exc_info=True,
                extra={"key": key, "uri": uri, "error": str(exc)},
            )
            continue

        if on_progress is not None:
            on_progress(loaded_count, total)

    return resolved


def _load_grounding_ontologies(
    *,
    grounding_keys: list[str],
    namespace: str,
    job_id: str,
    schemas_mod,
    job,
) -> list[str]:
    """Structured-pipeline wrapper over :func:`resolve_grounding_ontology_ids`.

    Preserves the structured job-status side effects (sets the job to
    ``LOADING_GROUNDING_ONTOLOGIES`` and writes ``grounding_loaded`` /
    ``grounding_total`` progress under ``source_type="STRUCTURED"``) while
    delegating the actual key→URI resolution + lazy load to the shared
    resolver.
    """
    job.status = schemas_mod.JobStatus.LOADING_GROUNDING_ONTOLOGIES

    def _progress(loaded: int, total: int) -> None:
        dynamo_store.update_job_status(
            job_id,
            "loading_grounding_ontologies",
            namespace=namespace,
            grounding_loaded=loaded,
            grounding_total=total,
            source_type="STRUCTURED",
        )

    return resolve_grounding_ontology_ids(
        grounding_keys=grounding_keys,
        namespace=namespace,
        on_progress=_progress,
    )


# ── Background worker ───────────────────────────────────────────────────
# Watchdog heartbeat interval — the worker bumps the job's ``updated_at`` this
# often while induce()/build_r2rml/Store run, so a long-but-alive run is never
# mistaken for an orphan by the stale sweep. Coarse on purpose (R3: keep DDB
# write volume low); 45s << the 1h stale window with wide margin.
_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("INDUCTION_HEARTBEAT_SECONDS", "45"))

# A watchdog tick failure is best-effort (must not kill the heartbeat), but a
# PERSISTENTLY failing touch freezes the liveness stamp and risks a false
# stale-sweep of a live run — so surface it, rate-limited to the first failure
# and then every Nth consecutive one (not one log line per tick).
_WATCHDOG_LOG_EVERY_N_FAILURES = 10


def _watchdog_loop(job_id: str, namespace: str, stop_event: Event, interval: float, region: str | None = None) -> None:
    """Bump the job's liveness timestamp every ``interval`` seconds until stopped.

    ``stop_event.wait(interval)`` is the clock: it returns True the instant the
    event is set (clean, immediate shutdown — no waiting out the full interval)
    and False on timeout (a heartbeat tick). Touch-only — ``touch_job`` never
    writes ``status`` — so a tick that races a terminal write can only bump
    ``updated_at``, never resurrect a finished job. Best-effort: a transient DDB
    error on one tick must not kill the heartbeat for the rest of the run, but a
    persistently-wedged heartbeat is logged (rate-limited) for visibility.

    Each tick also emits ``InductionHeartbeat`` + ``InductionWorkerMemoryBytes``
    to ``COA/Ontology`` (out-of-process OOM observability, #784): a run
    SIGKILLed by the OOM-killer never reaches the terminal metric emit in
    ``finally``, so the per-tick trail is the only signal that dates the crash
    and captures the pre-OOM memory ramp. The emit is best-effort on its own
    (swallows its errors), and is additionally guarded here so a raising client
    can't break the touch heartbeat.
    """
    consecutive_failures = 0
    while not stop_event.wait(interval):
        try:
            dynamo_store.touch_job(job_id, namespace)
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures == 1 or consecutive_failures % _WATCHDOG_LOG_EVERY_N_FAILURES == 0:
                _log.warning(
                    "watchdog touch_job failed for job %s (namespace %s): %d consecutive failure(s)",
                    job_id,
                    namespace,
                    consecutive_failures,
                    exc_info=(consecutive_failures == 1),
                )
        # Best-effort OOM-observability heartbeat — belt-and-suspenders guard so
        # even a helper that somehow raises can't kill the liveness loop.
        with contextlib.suppress(Exception):
            emit_induction_heartbeat_metrics(namespace, region=region)


def _start_watchdog(job_id: str, namespace: str, watchdog_stop: Event, region: str | None = None) -> Thread:
    """Start the liveness-heartbeat thread and return it.

    Shared by both induction workers (structured + unstructured) so the daemon
    thread that keeps ``updated_at`` fresh across the long/OOM-prone run is
    created identically. Daemon so a hung heartbeat can't block interpreter exit.
    ``region`` is passed through to the per-tick metric emit so the heartbeat
    publishes to the run's own region (not the resolve_region us-east-1 default).
    """
    watchdog = Thread(
        target=_watchdog_loop,
        args=(job_id, namespace, watchdog_stop, _HEARTBEAT_INTERVAL_SECONDS, region),
        daemon=True,
    )
    watchdog.start()
    return watchdog


def _join_watchdog(watchdog: Thread | None, watchdog_stop: Event, job_id: str, logger: logging.Logger) -> None:
    """Stop the heartbeat and join it, warning (via ``logger``) if it won't die.

    Shared teardown for both induction workers. ``touch_job`` is status-blind so
    a stray late tick is harmless — the join is belt-and-suspenders. A ``None``
    watchdog (run threw before it started) is a no-op.
    """
    watchdog_stop.set()
    if watchdog is not None:
        watchdog.join(timeout=_HEARTBEAT_INTERVAL_SECONDS + 5)
        if watchdog.is_alive():
            logger.warning("watchdog thread did not terminate within join timeout for job %s", job_id)


def _run_induction(
    job_id: str,
    body: WorkbenchInductionRequest,
    namespace: str,
    config: dict,
    build_pipeline_fn,
    schemas_mod,
    catalog_table_cls,
):
    job = _jobs[job_id]
    ns = namespace
    _t_start = time.perf_counter()
    # One tracker per job, shared across every Bedrock seam reached below
    # (grounding rerank + LLM generate). Metrics are emitted exactly once on
    # every exit path in the finally block — including the stage-duration and
    # volume datums recorded below, which flow through the SAME PutMetricData
    # call as cost (never EMF-stdout, which is unextracted on this Fargate task).
    cost_tracker = CostTracker()
    # Liveness watchdog — started just before the OOM-prone induce() hot path,
    # stopped in ``finally``. Declared here so ``finally`` can always stop it,
    # even if the run raises before the watchdog is started.
    watchdog_stop = Event()
    watchdog: Thread | None = None
    try:
        # Step 1: Fetch catalogs. FetchMetadata wraps ONLY the per-datasource
        # catalog/metadata fetch (through the dedup check + datasource-detail
        # update). The stage timer records FetchMetadataDurationMs on exit
        # however the block is left (normal fall-through, the dedup short-circuit
        # ``return``, or an exception propagating out).
        with _stage_timer("FetchMetadata", cost_tracker):
            job.status = schemas_mod.JobStatus.FETCHING_METADATA
            dynamo_store.update_job_status(job_id, "fetching_metadata", namespace=ns, source_type="STRUCTURED")
            all_tables = []
            datasource_details = []
            for ds_id in body.datasource_ids:
                if ds_id.endswith("-mock"):
                    # Mock datasources read from bundled JSON fixture (no SMUS / no Glue)
                    catalog = _fetch_catalog_from_fixture(ds_id)
                elif config.get("catalog_source") == "smus":
                    catalog = _fetch_catalog_from_smus(config, ds_id, namespace=ns)
                else:
                    catalog = _fetch_catalog(config["data_catalog_url"], ds_id)
                tbl_dicts = _catalog_to_tables(catalog)
                for tbl_dict in tbl_dicts:
                    tbl_dict["datasourceId"] = ds_id
                    # sourceSchema = database name from catalog (maps to PostgreSQL schema / Athena database)
                    fqn = tbl_dict.get("fullyQualifiedName", "")
                    tbl_dict["sourceSchema"] = fqn.split(".")[0] if "." in fqn else None
                    all_tables.append(catalog_table_cls(**tbl_dict))
                datasource_details.append(
                    {
                        "datasourceId": ds_id,
                        "sourceType": catalog.get("sourceType", ""),
                        "databases": [db.get("name", "") for db in catalog.get("databases", [])],
                        "tables": [t["name"] for t in tbl_dicts],
                        "table_count": len(tbl_dicts),
                        "column_count": sum(len(t.get("columns", [])) for t in tbl_dicts),
                    }
                )

            if not all_tables:
                raise ValueError(f"No tables found in datasources: {body.datasource_ids}")

            # Duplicate detection: check if an accepted proposal with identical
            # structure already exists. Compares table names + column names/types + FKs.
            fingerprint = _compute_structural_fingerprint(all_tables)
            existing_accepted = dynamo_store.list_proposals(namespace=ns, status="accepted")
            for existing in existing_accepted:
                e_meta = existing.get("metadata") or {}
                if e_meta.get("structural_fingerprint") == fingerprint:
                    job.status = schemas_mod.JobStatus.COMPLETED
                    # Surface duplicate_of on the in-memory job too, not just the DDB
                    # row: the /jobs/{id} poll returns this object while the worker
                    # thread is alive (the common case), so a client-side redirect to
                    # the pre-existing proposal depends on it being here. Without it
                    # the client falls back to the job id — which has no proposal row
                    # (no new proposal is created on the duplicate path) → 404.
                    job.duplicate_of = existing["proposal_id"]
                    dynamo_store.update_job_status(
                        job_id,
                        "completed",
                        namespace=ns,
                        source_type="STRUCTURED",
                        duplicate_of=existing["proposal_id"],
                    )
                    job.report = schemas_mod.InductionReport(
                        job_id=job_id,
                        ontology_uri_prefix=body.ontology_uri_prefix,
                        tables_processed=len(all_tables),
                        columns_processed=sum(len(t.columns) for t in all_tables),
                        matches=[],
                        novel_classes_created=0,
                        grounding_ontologies_used=[],
                        induced_ontology_id=existing.get("proposal_id"),
                        induced_ontology_uri=existing.get("ontology_id"),
                    )
                    # start_induction wrote an "inducing" stub at proposal_id==job_id
                    # before dispatching this worker. The duplicate path creates NO
                    # real proposal, so that stub would otherwise linger forever as a
                    # phantom "inducing" row in the proposals list. Remove it (best-
                    # effort — a delete hiccup must not wedge the run).
                    with contextlib.suppress(Exception):
                        dynamo_store.delete_proposal(ns, job_id)
                    # Dedup short-circuit still did the fetch work — record the
                    # input-volume trio (Tables + Columns) so throughput contracts
                    # stay paired on every completing path. NovelClassesCreated /
                    # GroundedClassesMatched are intentionally omitted (no induction
                    # ran — they are genuinely 0/not-applicable, not missing). Job
                    # wall-clock is recorded once in the finally for EVERY exit path.
                    cost_tracker.record_volume("TablesProcessed", len(all_tables))
                    cost_tracker.record_volume("ColumnsProcessed", sum(len(t.columns) for t in all_tables))
                    return

            # Update DynamoDB with datasource details now that we have them.
            # Offload the manifest to S3 first — at 20k tables it is ~575KB and
            # writing it inline blows DynamoDB's 400KB item cap. Only the
            # S3 key lands on the item; the manifest is a write-only audit blob
            # (get_job does NOT read it back), torn down on namespace delete.
            _ds_key = dynamo_store.put_job_datasources_s3(ns, job_id, datasource_details)
            dynamo_store.update_job_status(
                job_id,
                "fetching_metadata",
                namespace=ns,
                datasources_s3_key=_ds_key,
                tables_total=len(all_tables),
                columns_total=sum(len(t.columns) for t in all_tables),
                source_type="STRUCTURED",
            )

        # Step 1.5: Build the pipeline/adapters and assemble the grounding pool.
        # The GroundingLoad stage timer emits GroundingLoadDurationMs on exit. This
        # is a distinct span from FetchMetadata because _load_grounding_ontologies
        # network-fetches foundational ontologies (schema.org / FIBO) as TTL — a
        # one-time cost that must NOT be conflated with the per-datasource catalog
        # fetch it used to sit under (esp. on the 50k rung where seeding schema.org
        # adds a real network round-trip).
        #
        # The grounding pool is EXACTLY what the caller provided (resolved below)
        # — nothing is auto-appended server-side. The namespace's accepted induced
        # ontology is no longer force-included: the UI pre-checks it as a default
        # selection (so it arrives in ``grounding_ontology_ids`` and is resolved
        # like any other pick), but the user can deselect it. With no selections,
        # the pool is empty and grounding runs against nothing (all-novel) rather
        # than a namespace-wide search — see the empty-list contract on
        # ``grounding_ontology_ids`` below.
        #
        # Nearest-sensible-span note: the embedder cost-tracker wiring +
        # namespace-bound store override live in this block too (pipeline setup).
        with _stage_timer("GroundingLoad", cost_tracker):
            pipeline = build_pipeline_fn(config)

            # Route the embedder's token usage onto THIS job's tracker so embedding
            # cost (the bulk of Bedrock work in table_to_ontology) lands in the same
            # accumulator the finally block emits. The default engine build uses the
            # BedrockEmbeddingClient (has set_cost_tracker); the legacy HTTP
            # EmbeddingGeneratorClient does not, so this is a no-op there.
            eg = getattr(pipeline, "eg", None)
            if eg is not None and hasattr(eg, "set_cost_tracker"):
                eg.set_cost_tracker(cost_tracker)

            # Override the pipeline's ontology-catalog adapter with a namespace-bound one
            # so every vector search and embedding write stays within the namespace.
            from coa_ontology.stores import build_stores
            from coa_ontology.stores.adapters import StoreOntologyCatalogAdapter

            ns_graph, ns_vec = build_stores(namespace=ns)
            pipeline.oc = StoreOntologyCatalogAdapter(ns_graph, ns_vec, namespace=ns)

            # Lazy-load any foundational ontologies the user picked
            # in the induction modal that aren't yet loaded + embedded into
            # this namespace. The modal sends a list of curated catalog keys
            # (e.g. "fibo-agreements") — we resolve each to its URI, check
            # the namespace registry, and fire the foundational loader for
            # any that are missing OR have empty embedding indexes.
            # ``resolved_grounding_ids`` is the list of ontology URIs
            # (registry IDs) we'll pass to the strategy for filter scoping.
            resolved_grounding_ids: list[str] = []
            if body.grounding_ontology_ids:
                resolved_grounding_ids = _load_grounding_ontologies(
                    grounding_keys=body.grounding_ontology_ids,
                    namespace=ns,
                    job_id=job_id,
                    schemas_mod=schemas_mod,
                    job=job,
                )

        # Step 2-5: Run induction strategy. The single strategy_instance.induce()
        # call is where 50k-table runs actually saturate — the hot path.
        with _stage_timer("Induce", cost_tracker):
            job.status = schemas_mod.JobStatus.GENERATING_EMBEDDINGS
            dynamo_store.update_job_status(job_id, "generating_embeddings", namespace=ns, source_type="STRUCTURED")

            strategy_cls = get_strategy(body.strategy)
            strategy_instance = strategy_cls()

            job.status = schemas_mod.JobStatus.MATCHING
            dynamo_store.update_job_status(job_id, "matching", namespace=ns, source_type="STRUCTURED")

            job.status = schemas_mod.JobStatus.BUILDING_ONTOLOGY
            dynamo_store.update_job_status(job_id, "building_ontology", namespace=ns, source_type="STRUCTURED")

            # Start the liveness heartbeat before the OOM-prone hot path. It runs
            # through induce() + build_r2rml + the Store stage, keeping updated_at
            # fresh so a slow-but-alive run is never age-swept to failed. Stopped
            # in ``finally``.
            watchdog = _start_watchdog(job_id, ns, watchdog_stop, region=config.get("llm_region"))

            proposal_graph, novel_tables, matches = strategy_instance.induce(
                tables=all_tables,
                ontology_uri_prefix=body.ontology_uri_prefix,
                config=config,
                pipeline=pipeline,
                confidence_threshold=body.confidence_threshold,
                embedding_backend=body.embedding_backend,
                scoring_strategy=body.scoring_strategy,
                structural_weight=body.structural_weight,
                # Pass the resolved pool AS-IS, including an empty list. An empty
                # pool ([] or None) is an explicit "no grounding scope" signal (user
                # selected no ontologies and the namespace has no accepted induced
                # ontologies) — the grounding recall returns no candidates rather
                # than falling back to a namespace-wide search, which would ground
                # against a loaded-but-unselected ontology. Grounding is always
                # scoped to exactly this pool; there is no unscoped/global recall.
                grounding_ontology_ids=resolved_grounding_ids,
                grounding_mode=body.grounding_mode,
                cost_tracker=cost_tracker,
            )
            r2rml_graph = strategy_instance.build_r2rml(
                ontology_uri_prefix=body.ontology_uri_prefix,
                tables=all_tables,
                novel_tables=novel_tables,
                proposal_graph=proposal_graph,
            )

        # Step 6: Save to disk + create proposal in DynamoDB
        with _stage_timer("Store", cost_tracker):
            job.status = schemas_mod.JobStatus.STORING
            dynamo_store.update_job_status(job_id, "storing", namespace=ns, source_type="STRUCTURED")
            out_dir = os.path.join(_output_dir(), job_id)
            os.makedirs(out_dir, exist_ok=True)

            ontology_path = os.path.join(out_dir, "proposal-ontology.ttl")
            r2rml_path = os.path.join(out_dir, "r2rml-mappings.ttl")

            ontology_ttl = proposal_graph.serialize(format="turtle")
            r2rml_ttl = r2rml_graph.serialize(format="turtle")

            # Generate constraint config from DB constraints and store in S3
            from coa_ontology.validation.shapes.config import generate_config_from_db

            constraint_config = generate_config_from_db(all_tables, body.ontology_uri_prefix)
            constraint_config_json = json.dumps(constraint_config.model_dump())

            with open(ontology_path, "w") as f:
                f.write(ontology_ttl)
            with open(r2rml_path, "w") as f:
                f.write(r2rml_ttl)

            # Create proposal record
            proposal_id = job_id  # 1:1 with job for now
            # Class-level matches have ``source_column == ""`` (the row maps to a
            # whole table, not a column). Without this filter the counter inflates
            # by the column-level (datatype-property) matches: e.g. a 13-table
            # induction with 65 columns would report ``novel_classes_created: 78``
            # instead of 13. ``inducer/services/pipeline.py`` and
            # ``inducer/routers/induce.py`` already use the same filter; this
            # brings ``induce_catalog.py`` in line.
            novel_count = sum(1 for m in matches if m.match_type == "novel" and m.source_column == "")
            matched_count = sum(
                1 for m in matches if m.match_type in ("exact", "high_confidence") and m.source_column == ""
            )
            grounding_report_uris = sorted(
                {
                    m.matched_ontology_id
                    for m in matches
                    if m.matched_ontology_id and m.match_type in ("exact", "high_confidence")
                }
            )

            dynamo_store.create_proposal(
                proposal_id=proposal_id,
                job_id=job_id,
                namespace=ns,
                ontology_id=body.ontology_uri_prefix,
                proposal_type="induction",
                ontology_turtle=ontology_ttl,
                r2rml_turtle=r2rml_ttl,
                source_type="STRUCTURED",
                metadata={
                    "label": body.label or "Induced Structured Ontology",
                    "tables_processed": len(all_tables),
                    "columns_processed": sum(len(t.columns) for t in all_tables),
                    "novel_classes_created": novel_count,
                    "matched_count": matched_count,
                    # Only the COUNT is persisted, never the full novel-table
                    # list: at 20k tables ``sorted(novel_tables)`` is ~365KB and
                    # blows DynamoDB's 400KB item cap, and nothing reads the
                    # list back (all consumers use novel_classes_created).
                    "novel_tables_count": len(novel_tables),
                    "datasource_ids": body.datasource_ids,
                    # Matches are offloaded to S3 by create_proposal (popped from
                    # this dict before the DynamoDB write) to stay under the 400 KB
                    # item limit. The truncation here is a defensive backstop for
                    # any path that doesn't offload. Full match data lives in the
                    # InductionReport (in-memory) and the proposal's S3 artifacts.
                    "matches": _floats_to_decimal([m.model_dump() for m in matches if not m.source_column][:50]),
                    # NOTE: column_matches (matches with source_column set) are intentionally
                    # NOT persisted here. They lived inline in this dict for months but had
                    # zero readers anywhere in the codebase. Full match data,
                    # including column-level, is available via the in-memory InductionReport
                    # (schemas.py:64, field `matches`) and the S3 matches.json artifact
                    # written by put_proposal_matches_s3. Adding them back requires a real
                    # consumer AND the S3-offload plumbing — never inline in this dict, the
                    # per-candidate lists blow the 400 KB DynamoDB item limit on wide schemas.
                    "grounding_ontologies_used": grounding_report_uris,
                    "structural_fingerprint": fingerprint,
                    "has_constraint_config": True,
                },
            )

            # Store constraint config in S3 (too large for DDB 400KB limit)
            dynamo_store.put_proposal_constraints(ns, proposal_id, constraint_config_json)

            job.report = schemas_mod.InductionReport(
                job_id=job_id,
                ontology_uri_prefix=body.ontology_uri_prefix,
                tables_processed=len(all_tables),
                columns_processed=sum(len(t.columns) for t in all_tables),
                matches=matches,
                novel_classes_created=novel_count,
                grounding_ontologies_used=list(
                    {
                        m.matched_class_uri.rsplit("#", 1)[0]
                        for m in matches
                        if m.matched_class_uri and m.match_type in ("exact", "high_confidence")
                    }
                ),
                induced_ontology_id=proposal_id,
                induced_ontology_uri=body.ontology_uri_prefix,
            )

            job.status = schemas_mod.JobStatus.COMPLETED

            dynamo_store.update_job_status(
                job_id,
                "completed",
                namespace=ns,
                proposal_id=proposal_id,
                novel_classes_created=novel_count,
                matched_count=matched_count,
                tables_processed=len(all_tables),
                columns_processed=sum(len(t.columns) for t in all_tables),
                # Count only — the full novel-table list is ~365KB at 20k and
                # blows the 400KB item cap; nothing reads it back.
                novel_tables_count=len(novel_tables),
                source_type="STRUCTURED",
            )

        # Volume metrics on the success path. TablesProcessed + the job
        # wall-clock (recorded in the finally) give tables/sec; grounded/novel
        # come from the induction result. Recorded on the shared tracker, emitted
        # once by the finally.
        cost_tracker.record_volume("TablesProcessed", len(all_tables))
        cost_tracker.record_volume("ColumnsProcessed", sum(len(t.columns) for t in all_tables))
        cost_tracker.record_volume("NovelClassesCreated", novel_count)
        cost_tracker.record_volume("GroundedClassesMatched", matched_count)

    except Exception as e:
        # NB: use the module-level ``logging`` (line 10). A local ``import logging``
        # here would bind ``logging`` as a function-local for ALL of _run_induction,
        # leaving it UNBOUND in the finally below on the success path (where this
        # except never runs) → UnboundLocalError that masks a real error and wedges
        # the induction lock. Use the module import everywhere in this function.
        logging.getLogger(__name__).exception("induction job %s failed", job_id)
        cost_tracker.record_volume("InductionJobErrors", 1)
        job.status = schemas_mod.JobStatus.FAILED
        job.error = f"{type(e).__name__}: {e}"
        with contextlib.suppress(Exception):
            dynamo_store.update_job_status(job_id, "failed", namespace=ns, error=str(e), source_type="STRUCTURED")
        # Flip the in-progress proposal stub to failed so the list stops showing
        # it as inducing (proposal_id == job_id). Best-effort.
        with contextlib.suppress(Exception):
            dynamo_store.update_proposal(job_id, namespace=ns, status="failed", accept_error=str(e)[:1024])
    finally:
        # Stop the liveness heartbeat first (before the terminal status is read
        # by any poll) and join it so no tick can land after the run is done.
        _join_watchdog(watchdog, watchdog_stop, job_id, _log)
        # Record the whole-job wall-clock once here so EVERY exit path (success,
        # duplicate short-circuit, failure) contributes exactly one
        # InductionJobDurationMs sample — CloudWatch computes p50/p90 across jobs
        # from these single-value datums.
        cost_tracker.record_job_duration((time.perf_counter() - _t_start) * 1000)
        # Emit per-job Bedrock cost/token/duration/volume metrics exactly once on
        # every exit path so a job that died mid-way still reports what it did.
        # ponytail: best-effort, emit_induction_job_metrics already swallows errors
        emit_induction_job_metrics(cost_tracker, namespace_id=ns, region=config.get("llm_region"))
        # Release the in-flight lock whether the run succeeded or failed — it
        # only guards the ACTIVE run. Block-until-reviewed is enforced
        # separately by start_induction's pending/updated proposal check, which
        # covers the (disjoint) completed-but-unreviewed window. Best-effort so
        # a release hiccup can't crash the worker; the lock's stale-takeover is
        # the backstop if this ever no-ops — but LOG a release failure so a
        # wedged namespace has operational visibility instead of leaking silently.
        try:
            dynamo_store.release_induction_lock(ns)
        except Exception:
            logging.getLogger(__name__).error(
                "failed to release induction lock for namespace %s (job %s); "
                "namespace stays locked until stale-takeover",
                ns,
                job_id,
                exc_info=True,
            )


# These get set by main.py after import
_build_pipeline = None
_schemas_mod: Any = None  # injected by main.py at startup; import-time cycle avoidance
_catalog_table_cls = None


@router.post("/", status_code=202)
def start_induction(body: WorkbenchInductionRequest, request: Request, namespace: str):
    """Start an ontology-induction job for a namespace.

    Dispatches to the unstructured pipeline when the strategy is
    ``unstructured_lexical_graph``; otherwise enforces one in-flight structured
    induction per namespace (a pending/updated proposal or the acquired lock
    blocks a second run), then writes a proposal stub and job record and spawns
    the worker thread.

    Args:
        body: Induction request parameters (strategy, URI prefix, datasources,
            grounding config, etc.).
        request: FastAPI request, used to read app config (Neptune endpoint,
            pipeline builder).
        namespace: Namespace to induce into.

    Returns:
        The pending :class:`JobResponse` for the newly started job.

    Raises:
        HTTPException: 422 if the Neptune endpoint is unconfigured for an
            unstructured run, or 409 if another induction/proposal is in flight.
    """
    # Dispatch to unstructured pipeline if strategy is unstructured_lexical_graph
    if getattr(body, "strategy", "") == "unstructured_lexical_graph":
        from coa_ontology.inducer.routers.induce_unstructured import (
            UnstructuredInductionRequest,
        )
        from coa_ontology.inducer.routers.induce_unstructured import (
            start_induction as start_unstructured,
        )

        # graph_arn must satisfy UnstructuredInductionRequest's SSRF guard:
        # EITHER a canonical Neptune Analytics ARN OR the literal "neptune-db"
        # sentinel (which tells the unstructured worker to resolve the NDB
        # endpoint from server-side config — see _build_lexical_store; the
        # caller-supplied value is never used as an endpoint URL). If the caller
        # passed an explicit NA ARN, forward it; otherwise use the "neptune-db"
        # sentinel. Passing the raw NDB cluster URL here fails the regex → 500
        # (regression once the SSRF pattern landed on the request schema).
        explicit_arn = getattr(body, "graph_arn", None)
        graph_arn = explicit_arn if explicit_arn and explicit_arn.startswith("arn:aws:neptune-graph:") else "neptune-db"
        if graph_arn == "neptune-db" and not request.app.state.config.get("neptune_endpoint", ""):
            raise HTTPException(422, "Neptune endpoint not configured for unstructured induction")

        unstructured_body = UnstructuredInductionRequest(
            graph_arn=graph_arn,
            name="unstructured",
            ontology_uri_prefix=body.ontology_uri_prefix,
            label="Unstructured Induction",
            datasource_ids=body.datasource_ids,
            confidence_threshold=getattr(body, "confidence_threshold", 0.8),
            # Forward foundational-grounding params so the unstructured path
            # grounds its induced classes against the selected loaded
            # ontologies. Without this the fields are dropped here and the
            # unstructured router sees no ontology ids → grounding is skipped
            # (the class of bug where the UI selects an ontology but
            # foundational_grounded stays 0).
            grounding_ontology_ids=getattr(body, "grounding_ontology_ids", None),
            grounding_mode=getattr(body, "grounding_mode", "ENHANCED"),
        )
        return start_unstructured(body=unstructured_body, request=request, namespace=namespace)

    # Enforce one in-flight induction at a time per namespace, in two layers:
    #
    #  1. block-until-reviewed: a completed-but-unreviewed proposal
    #     (pending/updated) still blocks a new run — preserves prior behavior.
    #  2. in-flight lock: a race-free conditional lock (acquire_induction_lock)
    #     blocks a SECOND trigger while the first run is still executing. The
    #     old code did only layer 1, but the proposal row does not exist during
    #     the in-flight window (it's written at worker end), so a concurrent
    #     trigger slipped straight through.
    #
    # The check is scoped to STRUCTURED proposals so a pending unstructured
    # proposal does not block a new structured run (and vice versa).
    #
    # The blocking set lives in proposals.py (PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION),
    # derived from the Smithy-generated ProposalStatus enum, so this guard and the
    # accept guard cannot drift. It covers unreviewed work (pending / updated /
    # accept_failed) AND an accept that is actively merging (accepting /
    # embeddings_sync) — the induction lock is only held for the duration of an
    # induction, so without the in-flight accept states a new induction can start
    # mid-merge and leave two competing structured proposals in the namespace.
    from coa_ontology.proposals import PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

    pending: list[dict] = []
    for _status in PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION:
        pending = dynamo_store.list_proposals(namespace=namespace, status=_status, source_type="STRUCTURED")
        if pending:
            break
    if pending:
        raise HTTPException(
            409,
            {
                "message": (
                    "An in-flight proposal already exists. Accept or reject it before starting a new induction job."
                ),
                "proposal_id": pending[0]["proposal_id"],
            },
        )

    job_id = str(uuid.uuid4())

    # Acquire the in-flight lock BEFORE any writes. On conflict the holder's
    # job_id comes back in the 409 so the UI can point at the running job.
    try:
        dynamo_store.acquire_induction_lock(namespace, job_id)
    except dynamo_store.InductionLockHeldError as e:
        raise HTTPException(
            409,
            {
                "message": (
                    "An induction is already in progress for this namespace. "
                    "Wait for it to finish before starting a new one."
                ),
                "job_id": e.job_id,
            },
        ) from e

    # Everything after the lock is acquired must release it on failure — the
    # worker's finally only runs if the worker THREAD actually starts, so a
    # failure in put_job / create_proposal_stub / Thread.start() here would
    # otherwise leak the lock and wedge the namespace until the stale timeout.
    try:
        _jobs[job_id] = _schemas_mod.JobResponse(
            job_id=job_id,
            status=_schemas_mod.JobStatus.PENDING,
            created_at=datetime.utcnow(),
        )
        _job_namespaces[job_id] = namespace
        config = request.app.state.config

        # Write an in-progress PROPOSAL stub FIRST so the run is visible in the
        # proposals list immediately and survives a browser refresh (the worker
        # upserts it into the real proposal at the end, preserving created_at).
        # Ordered before put_job so a job row never exists without its stub.
        dynamo_store.create_proposal_stub(
            proposal_id=job_id,
            job_id=job_id,
            namespace=namespace,
            ontology_id=body.ontology_uri_prefix,
            source_type="STRUCTURED",
            label=body.label or "Induced Structured Ontology",
        )

        # Write initial job record to DynamoDB.
        dynamo_store.put_job(
            job_id,
            "pending",
            body.model_dump(),
            datasource_details=[],
            namespace=namespace,
            source_type="STRUCTURED",
        )

        Thread(
            target=_run_induction,
            args=(job_id, body, namespace, config, _build_pipeline, _schemas_mod, _catalog_table_cls),
            daemon=True,
        ).start()
    except Exception:
        # Release the just-acquired lock so a transient failure here doesn't
        # wedge the namespace for the full stale-takeout window.
        with contextlib.suppress(Exception):
            dynamo_store.release_induction_lock(namespace)
        raise

    return _jobs[job_id]


# Single terminal-status UNION across all job families — canonical set lives in
# dynamo_store (both modules must agree, and induce_catalog imports dynamo_store,
# so the constant can only live one way round without a cycle). Covers induction
# ``ingested`` + async ``cancelled`` — collapsing to either family's subset would
# regress the other. See dynamo_store.TERMINAL_JOB_STATUSES for the rationale.
_TERMINAL_JOB_STATUSES = dynamo_store.TERMINAL_JOB_STATUSES
# A DDB induction job non-terminal for longer than this with no live thread on
# this task is treated as orphaned (its worker died on an ECS restart) and
# reported as failed, so the poll can terminate instead of spinning forever.
# Kept generous: A2's liveness heartbeat (touch_job) keeps a slow-but-alive run
# fresh, so shortening this only risks false-failing a live long run.
_STALE_INDUCTION_SECONDS = int(os.getenv("INDUCTION_JOB_STALE_SECONDS", "3600"))  # 1h (induction is long)
_STALE_JOB_ERROR = "Induction worker did not complete (task restarted or timed out)"


def _sweep_job_if_stale(item: dict, namespace: str) -> dict:
    """Mark a non-terminal induction job ``failed`` if it has gone stale.

    Shared age-gate for both on-demand recovery paths (lazy ``get_job`` poll and
    the ``count_active_jobs`` self-heal) so the "orphaned worker → failed" rule
    lives in exactly one place. Terminal or fresh (age <
    ``_STALE_INDUCTION_SECONDS``) rows pass through unchanged. Returns the
    (possibly mutated) item; persists the flip to DynamoDB.

    Deliberately does NOT release the induction lock — an unconditional release
    could free a live run's lock during a rolling deploy (two tasks briefly
    run). The lock's own stale-takeover in ``acquire_induction_lock`` reclaims a
    dead holder's lock on the next trigger.
    """
    job_id = item.get("job_id")
    if not job_id:
        _log.warning("Sweep skipped: job row missing job_id: %r", item)
        return item
    status = str(item.get("status", ""))
    if status in _TERMINAL_JOB_STATUSES:
        return item
    updated = item.get("updated_at") or item.get("created_at")
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(updated)).total_seconds() if updated else 0
    except (ValueError, TypeError) as e:
        _log.warning("Invalid updated_at for job %s: %r (%s); treating as fresh", job_id, updated, e)
        age = 0
    if age >= _STALE_INDUCTION_SECONDS:
        _log.warning("Induction job %s stale after %ss in status %s; marking failed", job_id, age, status)
        try:
            dynamo_store.update_job_status(job_id, "failed", namespace=namespace, error=_STALE_JOB_ERROR)
            item["status"] = "failed"
            item["error"] = _STALE_JOB_ERROR
        except Exception:
            _log.error(
                "Failed to persist stale-sweep for job %s in namespace %s; will retry on next sweep",
                job_id,
                namespace,
                exc_info=True,
            )
    return item


@router.get("/jobs/{job_id}")
def get_job(job_id: str, namespace: str):
    """Return an induction job's status, sweeping stale orphans to failed.

    Prefers the in-memory job for this container; otherwise reads from
    DynamoDB (covers unstructured jobs and post-restart). A non-terminal
    DynamoDB row with no live worker that has exceeded the stale threshold is
    marked ``failed`` so the client poll can terminate.

    Args:
        job_id: ID of the job to fetch.
        namespace: Namespace the job must belong to.

    Returns:
        The job (in-memory JobResponse or the DynamoDB item).

    Raises:
        HTTPException: 404 if the job is unknown or belongs to another namespace.
    """
    job = _jobs.get(job_id)
    if job:
        job_ns = _job_namespaces.get(job_id)
        if job_ns != namespace:
            raise HTTPException(404, "Job not found")
        return job
    # Fall back to DynamoDB (covers unstructured jobs and post-restart).
    item = dynamo_store.get_job(job_id, namespace=namespace)
    if not item:
        raise HTTPException(404, "Job not found")
    # If the row is non-terminal but there is no live worker thread on this task
    # (item not in _jobs) and it has gone stale, the worker died on a restart —
    # sweep it to ``failed`` so the client poll terminates.
    return _sweep_job_if_stale(item, namespace)


@router.get("/jobs")
def list_jobs(namespace: str):
    """List in-memory induction jobs for a namespace.

    Args:
        namespace: Namespace whose jobs to list.

    Returns:
        The jobs tracked by this container for the namespace (excludes jobs
        started before a restart; see ``count_active_jobs`` for durable reads).
    """
    return [job for job_id, job in _jobs.items() if _job_namespaces.get(job_id) == namespace]


# Terminal IngestJobState values (see ontology-graph.smithy) — an ingest job
# created by "upload ontology file" (ingest-from-S3) is in-progress at
# pending/running/embeddings_sync.
_TERMINAL_INGEST_STATUSES = {"completed", "failed"}

# ProposalStatus values with no background work in flight (see
# ontology-induction.smithy). "pending" is intentionally included here: a
# proposal awaiting steward review has no active writer and is not a race
# risk for namespace deletion. Only "accepting" represents a background
# thread actively writing to the namespace's ontology data (catalog
# projection, Turtle bulk load, embedding writes, S3 persist — see
# proposals.py) — that is the one proposal state that must block deletion.
_ACCEPTING_PROPOSAL_STATUS = "accepting"


@router.get("/active-jobs")
def count_active_jobs(namespace: str) -> dict[str, int]:
    """Count non-terminal induction jobs, ingest jobs, and accepting proposals.

    Backed by durable DynamoDB reads (dynamo_store.list_jobs /
    list_ingest_jobs / list_proposals), not the in-memory ``_jobs`` dict used
    by ``GET /jobs`` above — that dict only reflects jobs tracked by the
    current running container and would miss jobs started before a restart.
    Used by the namespace-deletion precondition check (control-plane
    delete_handler) to block deletion while any of these could still be
    writing to the namespace's ontology data that the deletion pipeline is
    about to tear down:
      - an induction run (structured or unstructured)
      - an async ontology-ingest job (uploading a custom ontology file)
      - a proposal accept in progress ("accepting" — catalog projection,
        Turtle bulk load, embeddings, S3 persist)

    A pending/accepted/rejected/cancelled/updated proposal has no
    background writer and does not count — only "accepting" does.
    """
    jobs = dynamo_store.list_jobs(namespace=namespace, source_type=None, limit=1000)
    # Self-heal: an orphaned induction row (hard worker death) that went stale
    # AFTER the last boot would otherwise block namespace deletion forever — no
    # restart runs to trigger the lazy get_job sweep, and this counter alone
    # never swept. Age-gate-sweep stale non-terminal rows to ``failed`` here so
    # the next delete attempt self-heals with no restart. Fresh (alive) rows and
    # already-terminal rows are untouched.
    active_jobs = sum(
        1 for job in jobs if _sweep_job_if_stale(job, namespace).get("status") not in _TERMINAL_JOB_STATUSES
    )

    ingest_jobs = dynamo_store.list_ingest_jobs(namespace=namespace, limit=1000)
    active_ingest_jobs = sum(1 for job in ingest_jobs if job.get("status") not in _TERMINAL_INGEST_STATUSES)

    accepting_proposals = dynamo_store.list_proposals(
        namespace=namespace, status=_ACCEPTING_PROPOSAL_STATUS, limit=1000
    )

    return {"activeJobs": active_jobs + active_ingest_jobs + len(accepting_proposals)}


@router.get("/datasources")
def list_datasources(request: Request, namespace: str):
    """List available DATABASE sources from the unified sources table for the namespace.

    Only DATABASE sources (GLUE_DATABASE, JDBC_DATABASE) are relevant for
    ontology induction — document sources don't have structured table metadata.
    Uses the new sources table key schema: PK=NS#{namespaceId}, SK=SRC#{sourceId}.
    """
    import boto3

    config = request.app.state.config
    table_name = config.get("smus_datasources_table") or os.environ.get("DATASOURCES_TABLE", "")
    if not table_name:
        return []
    region = config.get("smus_region") or os.environ.get("AWS_REGION", "us-east-1")
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    # Filter to the caller's namespace — never return another tenant's rows.
    resp = table.scan(
        ProjectionExpression="PK,SK,#n,#s,sourceType,sourceSubType,tablesDiscovered,tablesApproved,namespaceId",
        FilterExpression="namespaceId = :ns",
        ExpressionAttributeNames={"#n": "name", "#s": "status"},
        ExpressionAttributeValues={":ns": namespace},
    )
    items = resp.get("Items", [])
    result = []
    for it in items:
        sk = it.get("SK", "")
        # New sources table: SK=SRC#{sourceId}
        if sk.startswith("SRC#"):
            ds_id = sk.removeprefix("SRC#")
        else:
            # Fallback for old key schema: PK=DS#{id}
            pk = it.get("PK", "")
            ds_id = pk.removeprefix("DS#") if pk.startswith("DS#") else pk

        source_type = it.get("sourceType", "")
        source_sub_type = it.get("sourceSubType", "")

        # Only include DATABASE sources — documents don't have structured table metadata
        if source_type != "DATABASE" or source_sub_type not in ("GLUE_DATABASE", "JDBC_DATABASE"):
            continue

        result.append(
            {
                "datasourceId": ds_id,
                "name": it.get("name", ds_id),
                "status": it.get("status", "UNKNOWN"),
                "sourceType": source_type or source_sub_type,
                "tablesDiscovered": it.get("tablesDiscovered", 0),
                "tablesApproved": it.get("tablesApproved", 0),
                "namespaceId": it.get("namespaceId", ""),
            }
        )
    return result


@router.get("/datasources/induced")
def list_induced_datasources(namespace: str):
    """Return distinct datasource IDs from all accepted proposals in a namespace."""
    import boto3

    proposals = dynamo_store.list_proposals(namespace=namespace, status="accepted")
    ds_set: dict[str, dict] = {}
    for p in proposals:
        meta = p.get("metadata") or {}
        ids = meta.get("datasource_ids") or []
        label = meta.get("label", "")
        for ds_id in ids:
            if ds_id not in ds_set:
                ds_set[ds_id] = {
                    "datasourceId": ds_id,
                    "proposal_id": p.get("proposal_id"),
                    "ontology_id": p.get("ontology_id"),
                    "label": label,
                    "source_type": meta.get("source_type", p.get("source_type", "STRUCTURED")),
                    "tables_processed": meta.get("tables_processed", 0),
                    "columns_processed": meta.get("columns_processed", 0),
                    "novel_classes_created": meta.get("novel_classes_created", meta.get("class_count", 0)),
                }

    # Resolve source names from the sources table
    sources_table = os.environ.get("SOURCES_TABLE", "")
    if sources_table and ds_set:
        table = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")).Table(sources_table)
        for ds_id, entry in ds_set.items():
            try:
                resp = table.get_item(Key={"PK": f"NS#{namespace}", "SK": f"SRC#{ds_id}"})
                item = resp.get("Item")
                if item:
                    entry["name"] = item.get("name", ds_id)
            except Exception:
                pass  # fall back to showing UUID

    return list(ds_set.values())
