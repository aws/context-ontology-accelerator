# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB metadata store for ontology workbench.

Table: ontology-engine (PK/SK single-table design, override via DYNAMODB_TABLE env)

Item patterns (namespace-prefixed for multi-tenant isolation):
  PK={namespace}#JOB#{job_id}         SK=STATUS              — induction job status + details
  PK={namespace}#JOB#{job_id}         SK=PROPOSAL#ontology   — proposal artifact under job
  PK={namespace}#PROPOSAL#{id}        SK=META                — standalone proposal record

Namespace registry patterns (one partition per namespace, sort key patterns):
  PK=NAMESPACE#{namespace}            SK=META                — namespace descriptor
  PK=NAMESPACE#{namespace}            SK=ONTOLOGY#{ontology_id}
                                                             — one per ontology in the namespace
"""

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from coa_common.constants import RESOURCE_PREFIX
from coa_control_plane_server.models.proposal_status import ProposalStatus

TABLE_NAME = os.getenv("DYNAMODB_TABLE", "ontology-engine")
REGION = os.getenv("DYNAMODB_REGION", os.getenv("NA_REGION", "us-east-1"))
DEFAULT_NAMESPACE = os.getenv("DYNAMODB_DEFAULT_NAMESPACE", "default")
_S3_PROPOSALS_PREFIX = "proposals"

_log = logging.getLogger(__name__)


def resolve_ontology_bucket() -> str:
    """Resolve the ontology-artifacts S3 bucket name.

    Deployed environments always set ``ONTOLOGY_ARTIFACTS_BUCKET`` (wired in
    ``ontology-stack.ts``). The fallback used to hardcode a fixed account id,
    so a missing env var silently pointed reads/writes at *another account's*
    bucket — a cross-account data footgun. Instead we derive the name from the
    **current** account via STS, matching the CDK naming pattern
    ``{prefix}-{env}-ontology-artifacts-{account}``. If even that can't be
    resolved we return "" so the first S3 call fails loudly rather than
    touching the wrong account.

    The prefix comes from ``RESOURCE_PREFIX``, NOT from a hardcoded brand: the
    bucket is a physical resource, so its name tracks the deployment's
    ``resource_prefix``. Hardcoding ``coa-`` here meant a deployment using any
    other prefix (CI/dev deploy with ``scl``) derived a bucket that does not
    exist — surfacing as NoSuchBucket for any caller outside the deployed
    compute, where the env var is unset (e.g. integ tests).
    """
    env_val = os.getenv("ONTOLOGY_ARTIFACTS_BUCKET")
    if env_val:
        return env_val
    try:
        account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
        scl_env = os.getenv("SCL_ENVIRONMENT", "dev")
        # CDK injects RESOURCE_PREFIX into deployed compute as "{prefix}-{env}-",
        # so it already carries the env there; outside it (integ tests, CLI) it
        # carries the bare prefix. Only append the env when it isn't present.
        stem = RESOURCE_PREFIX if RESOURCE_PREFIX.endswith(f"-{scl_env}") else f"{RESOURCE_PREFIX}-{scl_env}"
        derived = f"{stem}-ontology-artifacts-{account}"
        _log.warning(
            "ONTOLOGY_ARTIFACTS_BUCKET unset; derived %s from current account (not a hardcoded default)",
            derived,
        )
        return derived
    except Exception as e:  # noqa: BLE001 — degrade to empty so S3 fails loudly, never cross-account
        _log.error("ONTOLOGY_ARTIFACTS_BUCKET unset and account lookup failed: %s", e)
        return ""


_S3_BUCKET = resolve_ontology_bucket()

_table = None
_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        # Force SigV4. Under the default SigV2 signing, ``Content-Type`` is part
        # of the string-to-sign, so a presigned PUT (which signs an empty
        # Content-Type) is rejected the moment the browser sends
        # ``Content-Type: text/turtle`` (SignatureDoesNotMatch / 403). SigV4 only
        # signs declared headers, so the browser may send any Content-Type — which
        # is what the presign_proposal_artifact_put contract relies on.
        _s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
    return _s3


def _proposal_s3_key(namespace: str, proposal_id: str, artifact: str, version: str = "latest") -> str:
    return f"{_S3_PROPOSALS_PREFIX}/{namespace}/{proposal_id}/{version}/{artifact}.ttl"


def _put_artifact_s3(namespace: str, proposal_id: str, artifact: str, content: str, version: str = "latest") -> str:
    """Store artifact content in S3, return the S3 key."""
    key = _proposal_s3_key(namespace, proposal_id, artifact, version)
    _get_s3().put_object(
        Bucket=_S3_BUCKET,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/turtle",
    )
    return key


def _get_artifact_s3(namespace: str, proposal_id: str, artifact: str, version: str = "latest") -> str | None:
    """Retrieve artifact content from S3."""
    key = _proposal_s3_key(namespace, proposal_id, artifact, version)
    try:
        resp = _get_s3().get_object(Bucket=_S3_BUCKET, Key=key)
        return resp["Body"].read().decode("utf-8")
    except _get_s3().exceptions.NoSuchKey:
        return None
    except Exception as e:
        _log.error("S3 read failed for %s: %s", key, e)
        raise


def presign_proposal_artifact(
    namespace: str,
    proposal_id: str,
    artifact: str = "ontology",
    version: str = "latest",
    expires_in: int = 3600,
) -> str | None:
    """Presigned S3 GET URL for a proposal Turtle artifact (``None`` if absent).

    Lets the browser fetch large Turtle directly from S3, bypassing the 6 MB
    API Gateway / Lambda response cap.
    """
    key = _proposal_s3_key(namespace, proposal_id, artifact, version)
    s3 = _get_s3()
    try:
        s3.head_object(Bucket=_S3_BUCKET, Key=key)
    except ClientError as e:
        # A genuine "object not found" (404 / NoSuchKey) means there is simply
        # nothing to offer -> return None. Any other error (permissions,
        # throttling, network) is a real failure and must propagate rather than
        # be silently swallowed.
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": _S3_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )


def presign_proposal_artifact_put(
    namespace: str,
    proposal_id: str,
    artifact: str = "ontology",
    version: str = "latest",
    expires_in: int = 600,
) -> str:
    """Presigned S3 PUT URL for uploading a proposal Turtle artifact.

    The mirror of :func:`presign_proposal_artifact`: lets the browser upload a
    large edited ontology / R2RML directly to S3, bypassing the 6 MB API
    Gateway / Lambda *request* cap (the inverse of the read-path response cap).

    The target key is the same deterministic ``latest`` key the read path
    serves, so a subsequent ``get_proposal`` presigns exactly the bytes just
    uploaded. ``Content-Type`` is intentionally left out of the signature so
    the browser may send any value (the bucket uses SSE-S3, which needs no
    extra signed headers).
    """
    key = _proposal_s3_key(namespace, proposal_id, artifact, version)
    return _get_s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": _S3_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    return _table


_dao = None


def _get_dao():
    """Shared DynamoDBDAO (lib/common) over the same table.

    Used by the async ingest-job helpers so they get the DAO's reserved-word
    aliasing + condition handling for free instead of hand-rolling it.
    """
    global _dao
    if _dao is None:
        from coa_common import DynamoDBDAO

        _dao = DynamoDBDAO(TABLE_NAME, region=REGION)
    return _dao


def _job_pk(namespace: str, job_id: str) -> str:
    return f"{namespace}#JOB#{job_id}"


def _proposal_pk(namespace: str, proposal_id: str) -> str:
    return f"{namespace}#PROPOSAL#{proposal_id}"


def _ingest_job_pk(namespace: str, job_id: str) -> str:
    # Distinct prefix from induction jobs (``#JOB#``) so async ontology-ingest
    # status rows are never picked up by ``list_jobs``' induction scan.
    return f"{namespace}#INGESTJOB#{job_id}"


def _namespace_pk(namespace: str) -> str:
    return f"NAMESPACE#{namespace}"


def _ontology_sk(ontology_id: str) -> str:
    return f"ONTOLOGY#{ontology_id}"


# ── Per-namespace induction lock (PK={namespace}#INDUCTIONLOCK) ──────────
#
# Enforces "one in-flight induction per namespace" WITHOUT the old race: the
# previous guard listed for a pending/updated PROPOSAL row, which does not exist
# during the in-flight window (the proposal row is written only at worker end),
# so a second trigger slipped through. This lock is a single sentinel item
# acquired with a conditional put at trigger time — it exists ONLY for the
# active-run window and is released when the worker exits (success OR failure).
# The complementary "block until reviewed" limit (a completed-but-unreviewed
# proposal also blocks a new run) is enforced separately, and disjointly, by
# start_induction's pending/updated PROPOSAL check — so the lock does not need
# to survive past worker completion.
#
# Stale-takeover: if the worker's task is recycled mid-run the lock would
# otherwise wedge the namespace forever, so acquire also succeeds when the
# existing lock is older than ``_STALE_INDUCTION_LOCK_SECONDS``.


class InductionLockHeldError(Exception):
    """Raised by ``acquire_induction_lock`` when a live lock already exists.

    Carries the ``job_id`` of the in-flight induction so the caller can surface
    it in the 409 response body (mirrors the old guard's ``proposal_id``).
    """

    def __init__(self, job_id: str) -> None:
        """Store the in-flight job id and build the error message.

        Args:
            job_id: ID of the induction job currently holding the lock.
        """
        super().__init__(f"induction already in flight (job {job_id})")
        self.job_id = job_id


# A lock older than this with no live worker is treated as orphaned (its worker
# died with a recycled task) and can be taken over by a new induction. Kept
# generous because induction is long-running.
_STALE_INDUCTION_LOCK_SECONDS = int(os.getenv("INDUCTION_LOCK_STALE_SECONDS", "3600"))  # 1h


def _induction_lock_pk(namespace: str) -> str:
    return f"{namespace}#INDUCTIONLOCK"


def acquire_induction_lock(namespace: str, job_id: str) -> None:
    """Acquire the per-namespace induction lock, or raise ``InductionLockHeldError``.

    Conditional ``put_item`` on the sentinel ``{ns}#INDUCTIONLOCK`` / ``STATUS``
    item. Succeeds when no lock exists OR the existing lock is stale (older than
    ``_STALE_INDUCTION_LOCK_SECONDS`` — orphaned worker takeover). On a live
    lock the ConditionExpression fails and we raise with the holder's job_id.
    """
    now = datetime.now(UTC)
    stale_before = now.timestamp() - _STALE_INDUCTION_LOCK_SECONDS
    try:
        _get_table().put_item(
            Item={
                "PK": _induction_lock_pk(namespace),
                "SK": "STATUS",
                "job_id": job_id,
                "namespace": namespace,
                "acquired_at": now.isoformat(),
                # Epoch seconds for the stale comparison (avoids parsing ISO in
                # a ConditionExpression, which DynamoDB can't do).
                "acquired_ts": Decimal(str(now.timestamp())),
            },
            # Acquire if: no lock item, OR the item lacks a timestamp (defensive),
            # OR the existing lock is older than the stale cutoff.
            ConditionExpression="attribute_not_exists(PK) OR attribute_not_exists(acquired_ts) OR acquired_ts < :stale",
            ExpressionAttributeValues={":stale": Decimal(str(stale_before))},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            existing = _get_table().get_item(Key={"PK": _induction_lock_pk(namespace), "SK": "STATUS"}).get("Item", {})
            raise InductionLockHeldError(str(existing.get("job_id", ""))) from e
        raise


def release_induction_lock(namespace: str) -> None:
    """Release the per-namespace induction lock (unconditional delete, idempotent)."""
    _get_table().delete_item(Key={"PK": _induction_lock_pk(namespace), "SK": "STATUS"})


def put_job(
    job_id: str,
    status: str,
    request_body: dict,
    datasource_details: list[dict],
    namespace: str = DEFAULT_NAMESPACE,
    source_type: str = "STRUCTURED",
):
    """Write initial induction job record when job starts.

    ``source_type`` is the structured/unstructured discriminator added in
    the unstructured-induction-api wiring. It defaults to ``"STRUCTURED"``
    so existing structured callers keep working at the call-site level
    even before they are explicitly updated to pass the value through.
    """
    _get_table().put_item(
        Item={
            "PK": _job_pk(namespace, job_id),
            "SK": "STATUS",
            "job_id": job_id,
            "namespace": namespace,
            "status": status,
            "source_type": source_type,
            "ontology_uri_prefix": request_body.get("ontology_uri_prefix", ""),
            "label": request_body.get("label", ""),
            "datasource_ids": request_body.get("datasource_ids", []),
            "datasources": datasource_details,
            "confidence_threshold": str(request_body.get("confidence_threshold", 0.80)),
            "scoring_strategy": request_body.get("scoring_strategy", "lexical"),
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )


def update_job_status(job_id: str, status: str, namespace: str = DEFAULT_NAMESPACE, **extra):
    """Update job status and optional extra fields.

    Every ``**extra`` attribute name is aliased through
    ``ExpressionAttributeNames`` (an aliased ``#name`` placeholder in the
    ``UpdateExpression``) so DynamoDB reserved keywords are legal in the SET
    clause. ``error`` in particular is a reserved keyword: the worker failure
    path calls ``update_job_status(..., error=...)``, and embedding the bare
    name (``error = :error``) makes DynamoDB reject the write with
    ``ValidationException: Attribute name is a reserved keyword`` — silently
    losing the job's ``failed`` status and error message. Aliasing uniformly
    keeps any future reserved-keyword attribute safe too.
    """
    expr_parts = ["#s = :s", "updated_at = :ts"]
    names = {"#s": "status"}
    values = {":s": status, ":ts": datetime.now(UTC).isoformat()}
    for k, v in extra.items():
        placeholder = k.replace("#", "_")
        names[f"#{placeholder}"] = k
        expr_parts.append(f"#{placeholder} = :{placeholder}")
        values[f":{placeholder}"] = v
    _get_table().update_item(
        Key={"PK": _job_pk(namespace, job_id), "SK": "STATUS"},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def touch_job(job_id: str, namespace: str = DEFAULT_NAMESPACE) -> None:
    """Bump an induction job's ``updated_at`` liveness timestamp — nothing else.

    Called by the induction worker's watchdog thread (see
    ``induce_catalog._run_induction``) so a long-running-but-alive job keeps
    its ``updated_at`` fresh and is never mistaken for an orphan by the
    age-gated stale sweep. It deliberately SETs **only** ``updated_at`` and
    never touches ``status``: a late watchdog tick that lands after the worker
    has already written a terminal status must not clobber it back to a
    non-terminal value (which would make the age-gate re-sweep a finished job).
    Bumping ``updated_at`` on an already-terminal row is harmless — terminal
    rows are never swept regardless of age.

    Raises:
        botocore.exceptions.ClientError: On DynamoDB failures (throttling,
            network, permission). The watchdog caller catches this per-tick.
    """
    _get_table().update_item(
        Key={"PK": _job_pk(namespace, job_id), "SK": "STATUS"},
        UpdateExpression="SET updated_at = :ts",
        ExpressionAttributeValues={":ts": datetime.now(UTC).isoformat()},
    )


def put_job_datasources_s3(namespace: str, job_id: str, datasources: list[dict]) -> str:
    """Offload a job's datasources manifest to S3, return the S3 key.

    The manifest carries per-datasource table-name lists that routinely exceed
    the DynamoDB 400KB item limit at scale (20k tables -> ~575KB), so it is
    stored in S3 and only the key lands on the job item. Mirrors
    put_proposal_matches_s3.
    """
    key = f"jobs/{namespace}/{job_id}/datasources.json"
    _get_s3().put_object(
        Bucket=_S3_BUCKET,
        Key=key,
        # Built fresh in-process and never round-tripped through a DDB read, so it carries no
        # Decimals (unlike put_proposal_matches_s3); default=str is just a defensive fallback.
        Body=json.dumps(datasources, default=str).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def get_job(job_id: str, namespace: str = DEFAULT_NAMESPACE) -> dict | None:
    """Fetch a job's STATUS item from DynamoDB.

    Args:
        job_id: ID of the job to fetch.
        namespace: Namespace the job belongs to.

    Returns:
        The job item, or None if no such job exists.
    """
    return _get_table().get_item(Key={"PK": _job_pk(namespace, job_id), "SK": "STATUS"}).get("Item")


def _status_str(status) -> str:
    """Normalize a status to its stored string value.

    ``IngestJobState`` is a plain ``(str, Enum)`` (not StrEnum), so ``str(member)``
    yields ``'IngestJobState.RUNNING'`` — not the wire value ``'running'``. Use
    ``.value`` for enum members; pass plain strings through unchanged. Storing
    the bare value keeps the DDB item re-validatable as an ``IngestJob``.
    """
    return status.value if hasattr(status, "value") else str(status)


def put_ingest_job(
    job_id: str,
    ontology_id: str,
    status: str,
    namespace: str = DEFAULT_NAMESPACE,
) -> None:
    """Write the initial async ontology-ingest job record.

    Mirrors ``put_job`` but on a separate ``#INGESTJOB#`` partition. Persisting
    to DynamoDB (rather than an in-process dict) means the status survives ECS
    task restarts/redeploys and is readable regardless of which task instance
    serves the poll — the in-memory dict 404'd forever if the worker task and
    the polling task differed, or if the task recycled mid-ingest.

    Uses the shared ``DynamoDBDAO`` (lib/common) for read/write so the reserved-
    word aliasing + condition handling live in one place.
    """
    now = datetime.now(UTC).isoformat()
    _get_dao().put(
        {
            "PK": _ingest_job_pk(namespace, job_id),
            "SK": "STATUS",
            "job_id": job_id,
            "namespace": namespace,
            "ontology_id": ontology_id,
            "status": _status_str(status),
            "error": None,
            "result": None,
            "created_at": now,
            "updated_at": now,
        },
        auto_timestamp=False,
    )


def update_ingest_job(job_id: str, status: str, namespace: str = DEFAULT_NAMESPACE, **extra) -> None:
    """Update an async ingest job's status (+ optional ``error``/``result``).

    Goes through ``DynamoDBDAO.update``, which aliases every attribute name —
    so DynamoDB reserved words like ``status`` and ``error`` are handled
    automatically without a hand-rolled ``ExpressionAttributeNames`` map.
    """
    update_fields: dict[str, Any] = {
        "status": _status_str(status),
        "updated_at": datetime.now(UTC).isoformat(),
        **extra,
    }
    _get_dao().update(
        {"PK": _ingest_job_pk(namespace, job_id), "SK": "STATUS"},
        update_fields,
        auto_timestamp=False,
    )


def get_ingest_job(job_id: str, namespace: str = DEFAULT_NAMESPACE) -> dict | None:
    """Fetch an async ontology-ingest job's STATUS item.

    Args:
        job_id: ID of the ingest job to fetch.
        namespace: Namespace the ingest job belongs to.

    Returns:
        The ingest job item, or None if no such job exists.
    """
    return _get_dao().get({"PK": _ingest_job_pk(namespace, job_id), "SK": "STATUS"})


def list_ingest_jobs(namespace: str, status: str | None = None, limit: int = 1000) -> list[dict]:
    """Scan async ontology-ingest job items in a namespace with an optional status filter.

    Mirrors ``list_jobs``/``list_proposals`` for the ``#INGESTJOB#`` partition
    (created by ``upload ontology file`` / ingest-from-S3, distinct from both
    induction jobs and proposals). The PK pattern is
    ``{namespace}#INGESTJOB#{job_id}`` with SK ``"STATUS"``.
    """
    filter_parts = ["begins_with(PK, :pk_pfx)", "SK = :sk"]
    values: dict[str, Any] = {
        ":pk_pfx": f"{namespace}#INGESTJOB#",
        ":sk": "STATUS",
    }
    names: dict[str, str] = {}
    if status:
        names["#s"] = "status"
        filter_parts.append("#s = :st")
        values[":st"] = status

    kwargs: dict[str, Any] = {
        "FilterExpression": " AND ".join(filter_parts),
        "ExpressionAttributeValues": values,
        "Limit": limit,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names

    items: list[dict] = []
    last_key = None
    while len(items) < limit:
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = _get_table().scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return items[:limit]


# ── Generic async-job persistence (validate / infer-constraints / …) ───────
#
# Proposal-scoped async jobs (SHACL validation, constraint inference) used to
# live ONLY in per-process dicts. That is lost on any ECS task restart/redeploy:
# the poll then 404s forever (work that may still be running or already done),
# and a burst past the in-memory cap could evict a still-running job. These
# helpers persist the job to DynamoDB (same table, distinct ``#ASYNCJOB#``
# partition) so status survives restarts and cross-instance polls — mirroring
# the ingest-job pattern.

# Statuses past which a job never changes. A job stuck in a non-terminal state
# with no live worker (task recycled mid-run) is swept to ``failed`` on read.
#
# Single source of truth for the terminal-status UNION across every job family.
# ``induce_catalog`` imports this (it depends on dynamo_store, not vice versa,
# so the constant lives here to avoid a circular import). The union covers:
# induction jobs' ``ingested`` (set on proposal-accept, proposals.py) plus the
# async-job set ``completed``/``failed``/``cancelled``. Unioning is safe — an
# async job never reaches ``ingested``, so a wider terminal set only prevents a
# false-positive sweep, never causes a missed one. Collapsing to any single
# family's set would regress another (get_job would re-sweep ``ingested``;
# count_active_jobs would count ``cancelled`` as active forever).
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled", "ingested"})

# A non-terminal job whose ``updated_at`` is older than this is considered
# orphaned (its worker thread died with the task) and reported as failed.
_STALE_JOB_SECONDS = int(os.getenv("ASYNC_JOB_STALE_SECONDS", "1800"))  # 30 min

# Proposal accept runs on a fire-and-forget daemon thread (see proposals.py
# accept_proposal). If that worker dies mid-run — the ECS task recycled, an
# unhandled crash, Neptune/Bedrock hanging past the task's lifetime — the
# proposal is left in a non-terminal ``accepting``/``embeddings_sync`` state
# forever: the Accept button stays disabled and there is no path to recover it.
# Mirror the async-job sweep: on read, a proposal stuck in an in-progress accept
# state past ``_ACCEPT_STALE_SECONDS`` (with no ``updated_at`` heartbeat — the
# accept pipeline bumps updated_at at every step checkpoint, so a live-but-slow
# accept keeps resetting the clock and is NOT swept) is moved to the terminal
# ``accept_failed`` state with an ``accept_error`` so the user can retry.
#
# Taken from the Smithy-generated ProposalStatus enum, NOT hardcoded and not
# imported from proposals.py (which would be circular — proposals depends on this
# module). The generated enum is a leaf dependency both modules can import, so
# there is exactly one definition of these statuses and no "keep in sync" risk.
_PROPOSAL_IN_PROGRESS_STATUSES: frozenset[str] = frozenset(
    {ProposalStatus.ACCEPTING.value, ProposalStatus.EMBEDDINGS_SYNC.value}
)
_PROPOSAL_ACCEPT_FAILED_STATUS = ProposalStatus.ACCEPT_FAILED.value
_ACCEPT_STALE_SECONDS = int(os.getenv("PROPOSAL_ACCEPT_STALE_SECONDS", "1800"))  # 30 min


def _async_job_pk(namespace: str, kind: str, job_id: str) -> str:
    # ``kind`` (e.g. "VALIDATE", "INFER") keeps the partitions from colliding
    # and never overlaps induction (#JOB#) or ingest (#INGESTJOB#) scans.
    return f"{namespace}#ASYNCJOB#{kind}#{job_id}"


def put_async_job(kind: str, job_id: str, item: dict, namespace: str = DEFAULT_NAMESPACE) -> None:
    """Persist the initial async-job record. ``item`` is the wire-shape job dict."""
    now = datetime.now(UTC).isoformat()
    # Spread ``item`` FIRST, then stamp the server-controlled fields, so a
    # caller-supplied created_at/updated_at can never override them — the
    # staleness sweep relies on updated_at being a server timestamp.
    _get_dao().put(
        {
            **item,
            "PK": _async_job_pk(namespace, kind, job_id),
            "SK": "STATUS",
            "namespace": namespace,
            "kind": kind,
            "created_at": now,
            "updated_at": now,
        },
        auto_timestamp=False,
    )


def update_async_job(kind: str, job_id: str, fields: dict, namespace: str = DEFAULT_NAMESPACE) -> None:
    """Update an async-job record (status/result/error). Reserved-word safe via the DAO."""
    _get_dao().update(
        {"PK": _async_job_pk(namespace, kind, job_id), "SK": "STATUS"},
        {**fields, "updated_at": datetime.now(UTC).isoformat()},
        auto_timestamp=False,
    )


def sweep_stale_record(
    item: dict,
    *,
    is_in_progress: Callable[[str], bool],
    max_age_seconds: int,
    terminal_status: str,
    error_field: str,
    error_message: str,
    write: Callable[[dict], None],
    label: str,
    extra_fields: dict | None = None,
) -> dict:
    """Move a stale in-progress record to a terminal status, in place and in DDB.

    A worker that died with its ECS task (or hung past the task's lifetime)
    leaves its record non-terminal forever, so the client poll spins with no
    recovery path. On read, a record still reported as in-progress whose
    ``updated_at`` (falling back to ``created_at``) is older than
    ``max_age_seconds`` is written to ``terminal_status`` with ``error_message``
    on ``error_field``.

    Single implementation for what used to be two byte-identical copies of this
    algorithm — async jobs (:func:`_sweep_if_stale`) and proposal accepts
    (:func:`_sweep_proposal_if_stale`). They differ only in the parameters above,
    so a fix to the timestamp parsing or the staleness rule lands in one place.

    Induction jobs run the same rule through ``induce_catalog._sweep_job_if_stale``
    rather than this helper, because that path additionally serves the
    ``count_active_jobs`` self-heal and carries induction-specific behaviour (it
    deliberately does not release the induction lock). Worth folding together if a
    third caller appears; not worth forcing today.

    ``write`` performs the store update and is called with the field dict; it is
    best-effort — a transient DDB failure is logged and swallowed so it cannot
    500 the very poll the user relies on to observe the stuck state (the next
    read retries the sweep). The older copies let that failure escape.

    Returns the same ``item``, mutated when swept, so callers can return it
    directly.
    """
    status = str(item.get("status", ""))
    if not is_in_progress(status):
        return item
    updated = item.get("updated_at") or item.get("created_at")
    if not updated:
        return item
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(updated)).total_seconds()
    except (ValueError, TypeError):
        return item
    if age < max_age_seconds:
        return item
    _log.warning("%s stale after %.0fs in status %s; marking %s", label, age, status, terminal_status)
    fields = {"status": terminal_status, error_field: error_message, **(extra_fields or {})}
    try:
        write(fields)
    except Exception:  # noqa: BLE001 — transient write failure; next read retries the sweep
        _log.exception("%s: failed to mark stale record as %s", label, terminal_status)
        return item
    # Mirror EVERY written field onto the returned item, ``extra_fields`` included.
    # ``fields`` is exactly what went to DDB, so reusing it keeps the two from
    # diverging: the caller is usually the very poll that triggered this sweep, and
    # updating only status/error left it reporting the pre-sweep ``accept_progress``
    # (e.g. the last completed step) even though DDB already said
    # ``{step: unknown, state: stale}``.
    item.update(fields)
    return item


def _sweep_if_stale(kind: str, item: dict, namespace: str) -> dict:
    """Mark a non-terminal async job failed if it has gone stale (dead worker)."""
    return sweep_stale_record(
        item,
        is_in_progress=lambda s: s not in TERMINAL_JOB_STATUSES,
        max_age_seconds=_STALE_JOB_SECONDS,
        terminal_status="failed",
        error_field="error",
        error_message="Job worker did not complete (task restarted or timed out)",
        write=lambda fields: update_async_job(kind, item["job_id"], fields, namespace=namespace),
        label=f"Async job {item.get('job_id')} ({kind})",
    )


def get_async_job(kind: str, job_id: str, namespace: str = DEFAULT_NAMESPACE) -> dict | None:
    """Read an async job, sweeping it to ``failed`` if non-terminal but stale."""
    item = _get_dao().get({"PK": _async_job_pk(namespace, kind, job_id), "SK": "STATUS"})
    if item is None:
        return None
    return _sweep_if_stale(kind, item, namespace)


def list_jobs(
    namespace: str = DEFAULT_NAMESPACE,
    status: str | None = None,
    source_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Scan induction job items in a namespace with optional filters.

    Mirrors ``list_proposals`` for the JOB partition. The PK pattern is
    ``{namespace}#JOB#{job_id}`` with SK ``"STATUS"``; the scan begins-with
    on the PK prefix and matches on the SK so we only return job-status
    rows (not proposal-artifact rows that share the JOB partition).

    ``source_type`` filter semantics match ``list_proposals``:
      - ``None``: no filter.
      - ``"STRUCTURED"``: matches items with the attribute set to
        ``"STRUCTURED"`` AND items that lack the attribute entirely
        (pre-schema-delta data).
      - ``"UNSTRUCTURED"``: matches only items with the attribute set
        to ``"UNSTRUCTURED"`` exactly.
    """
    filter_parts: list[str] = ["begins_with(PK, :pk_pfx)", "SK = :sk"]
    values: dict[str, Any] = {
        ":pk_pfx": f"{namespace}#JOB#",
        ":sk": "STATUS",
    }
    names: dict[str, str] = {}
    if status:
        # `status` is a DynamoDB reserved word — must be aliased.
        names["#s"] = "status"
        filter_parts.append("#s = :st")
        values[":st"] = status
    if source_type is not None:
        # Mirror list_proposals' asymmetric backfill rule.
        if source_type == "STRUCTURED":
            filter_parts.append("(source_type = :src OR attribute_not_exists(source_type))")
        else:
            filter_parts.append("source_type = :src")
        values[":src"] = source_type

    kwargs: dict[str, Any] = {
        "FilterExpression": " AND ".join(filter_parts),
        "ExpressionAttributeValues": values,
        "Limit": limit,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    items: list[dict] = []
    while len(items) < limit:
        resp = _get_table().scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items[:limit]


def put_proposal(job_id: str, ontology_turtle: str, r2rml_turtle: str, namespace: str = DEFAULT_NAMESPACE):
    """Persist proposal ontology and R2RML to S3, store keys in DynamoDB."""
    ts = datetime.now(UTC).isoformat()
    pk = _job_pk(namespace, job_id)

    onto_key = _put_artifact_s3(namespace, job_id, "ontology", ontology_turtle)
    r2rml_key = _put_artifact_s3(namespace, job_id, "r2rml", r2rml_turtle) if r2rml_turtle else ""

    _get_table().put_item(
        Item={
            "PK": pk,
            "SK": "PROPOSAL#ontology",
            "s3_key": onto_key,
            "format": "text/turtle",
            "created_at": ts,
        }
    )
    _get_table().put_item(
        Item={
            "PK": pk,
            "SK": "PROPOSAL#r2rml",
            "s3_key": r2rml_key,
            "format": "text/turtle",
            "created_at": ts,
        }
    )


def get_proposal(job_id: str, kind: str = "ontology", namespace: str = DEFAULT_NAMESPACE) -> dict | None:
    """Retrieve a proposal artifact. kind: 'ontology' or 'r2rml'."""
    resp = _get_table().get_item(Key={"PK": _job_pk(namespace, job_id), "SK": f"PROPOSAL#{kind}"})
    item = resp.get("Item")
    if not item:
        return None
    # Hydrate from S3 if content was offloaded
    if "s3_key" in item and "content" not in item:
        item["content"] = _get_artifact_s3(namespace, job_id, kind) or ""
    return item


def put_reviewed_proposal(
    job_id: str,
    ontology_turtle: str | None = None,
    r2rml_turtle: str | None = None,
    namespace: str = DEFAULT_NAMESPACE,
):
    """Store/overwrite user-reviewed proposal artifacts — overwrites 'latest' in S3."""
    if ontology_turtle is not None:
        _put_artifact_s3(namespace, job_id, "ontology", ontology_turtle, version="latest")
    if r2rml_turtle is not None:
        _put_artifact_s3(namespace, job_id, "r2rml", r2rml_turtle, version="latest")


def get_reviewed_proposal(job_id: str, kind: str = "ontology", namespace: str = DEFAULT_NAMESPACE) -> dict | None:
    """Retrieve a user-reviewed proposal artifact record.

    Args:
        job_id: ID of the job the proposal belongs to.
        kind: Artifact kind, ``"ontology"`` or ``"r2rml"``.
        namespace: Namespace the proposal belongs to.

    Returns:
        The reviewed-artifact item, or None if none was stored.
    """
    sk = f"PROPOSAL#user_reviewed_{kind}"
    resp = _get_table().get_item(Key={"PK": _job_pk(namespace, job_id), "SK": sk})
    return resp.get("Item")


# ── Proposal storage (PK={namespace}#PROPOSAL#{id}) ─────────────────────


def create_proposal(
    proposal_id: str,
    job_id: str,
    namespace: str,
    ontology_id: str,
    proposal_type: str,
    ontology_turtle: str,
    r2rml_turtle: str,
    source_type: str = "STRUCTURED",
    metadata: dict | None = None,
):
    """Create a new proposal record. Turtle content is stored in S3.

    ``source_type`` is the structured/unstructured discriminator added in
    the unstructured-induction-api wiring. It is persisted as a top-level
    item attribute (not nested in ``metadata``) so ``list_proposals`` can
    apply a server-side ``FilterExpression`` against it.

    Default is ``"STRUCTURED"`` so existing call sites keep working
    unchanged. Pre-delta items written before the schema delta lack the
    attribute entirely; ``list_proposals(source_type="STRUCTURED")``
    treats those as structured for backward compatibility.
    """
    ts = datetime.now(UTC).isoformat()

    # Store large content in S3 — both original (immutable) and latest (overwritten on edits)
    _put_artifact_s3(namespace, proposal_id, "ontology", ontology_turtle, version="original")
    onto_key = _proposal_s3_key(namespace, proposal_id, "ontology", "latest")
    _get_s3().copy_object(
        Bucket=_S3_BUCKET,
        Key=onto_key,
        CopySource={"Bucket": _S3_BUCKET, "Key": _proposal_s3_key(namespace, proposal_id, "ontology", "original")},
    )
    if r2rml_turtle:
        _put_artifact_s3(namespace, proposal_id, "r2rml", r2rml_turtle, version="original")
        r2rml_key = _proposal_s3_key(namespace, proposal_id, "r2rml", "latest")
        _get_s3().copy_object(
            Bucket=_S3_BUCKET,
            Key=r2rml_key,
            CopySource={"Bucket": _S3_BUCKET, "Key": _proposal_s3_key(namespace, proposal_id, "r2rml", "original")},
        )
    else:
        r2rml_key = ""

    # Store matches in S3 if present (can be large with candidates + definitions).
    # Shares put_proposal_matches_s3 with the grounding-override/edit paths so
    # key construction + Decimal-safe serialization stay in one place.
    matches_s3_key = ""
    if metadata and "matches" in metadata:
        matches_s3_key = put_proposal_matches_s3(proposal_id, metadata.pop("matches"), namespace=namespace)

    # Preserve the trigger-time created_at if an in-progress stub row already
    # exists (create_proposal_stub wrote one at induction start). This is a
    # full put_item overwrite, so without this the row's age would reset to the
    # worker-end time, making the proposal look freshly created instead of
    # created when the user triggered it. Best-effort — a missing/failed read
    # just falls back to ``ts``.
    created_at = ts
    try:
        existing = _get_table().get_item(Key={"PK": _proposal_pk(namespace, proposal_id), "SK": "META"}).get("Item")
        if existing and existing.get("created_at"):
            created_at = existing["created_at"]
    except ClientError:
        pass

    item: dict[str, Any] = {
        "PK": _proposal_pk(namespace, proposal_id),
        "SK": "META",
        "proposal_id": proposal_id,
        "job_id": job_id,
        "namespace": namespace,
        "ontology_id": ontology_id,
        "proposal_type": proposal_type,
        "source_type": source_type,
        "status": "pending",
        "ontology_s3_key": onto_key,
        "r2rml_s3_key": r2rml_key,
        "matches_s3_key": matches_s3_key,
        "created_at": created_at,
        "updated_at": ts,
    }
    if metadata:
        item["metadata"] = metadata
    _get_table().put_item(Item=item)


def create_proposal_stub(
    proposal_id: str,
    job_id: str,
    namespace: str,
    ontology_id: str,
    source_type: str = "STRUCTURED",
    label: str = "Induced Structured Ontology",
) -> None:
    """Write an in-progress PROPOSAL stub row at induction TRIGGER time.

    This is what makes a running induction visible in the proposals list
    immediately and survive a browser refresh: the list query scans
    ``{ns}#PROPOSAL#`` / ``META`` rows, and previously no such row existed
    until the worker finished (``create_proposal``). The stub carries
    ``status="inducing"`` and empty S3 keys — the worker upserts it into the
    real proposal (``status="pending"``) via ``create_proposal`` on success,
    preserving this row's ``created_at``; on failure it is flipped to
    ``status="failed"`` via ``update_proposal``.

    ``proposal_id`` is the job_id (1:1, matching both worker paths). No S3
    artifacts exist yet, so ``get_proposal`` presigns to ``None`` cleanly and
    the list view has nothing large to strip.
    """
    ts = datetime.now(UTC).isoformat()
    _get_table().put_item(
        Item={
            "PK": _proposal_pk(namespace, proposal_id),
            "SK": "META",
            "proposal_id": proposal_id,
            "job_id": job_id,
            "namespace": namespace,
            "ontology_id": ontology_id,
            "proposal_type": "induction",
            "source_type": source_type,
            "status": "inducing",
            "ontology_s3_key": "",
            "r2rml_s3_key": "",
            "matches_s3_key": "",
            "created_at": ts,
            "updated_at": ts,
            "metadata": {"label": label},
        }
    )


def _matches_json_default(o):
    """json.dumps default for DynamoDB-sourced payloads.

    Coerce ``Decimal`` values (matches round-tripped through a DDB read arrive
    as Decimal) into JSON-native numbers, else json.dumps raises
    ``Object of type Decimal is not JSON serializable``.
    """
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def put_proposal_matches_s3(proposal_id: str, matches: list, namespace: str = DEFAULT_NAMESPACE) -> str:
    """Offload a proposal's grounding ``matches`` to S3 and return the key.

    ``matches`` (with candidates + definitions) routinely exceed the DynamoDB
    400 KB item limit, so they live in S3 — ``create_proposal`` does this on
    first write and ``get_proposal_by_id`` hydrates them back. Any path that
    rebuilds matches (e.g. grounding overrides) MUST use this instead of
    writing them inline into ``metadata``, or the UpdateItem fails with
    ``Item size has exceeded the maximum allowed size``.
    """
    import json as _json

    key = f"{_S3_PROPOSALS_PREFIX}/{namespace}/{proposal_id}/latest/matches.json"
    _get_s3().put_object(
        Bucket=_S3_BUCKET,
        Key=key,
        Body=_json.dumps({"matches": matches}, default=_matches_json_default).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def put_proposal_constraints(namespace: str, proposal_id: str, constraint_config_json: str) -> str:
    """Offload a proposal's constraint config to S3 and return the key.

    Public counterpart to ``put_proposal_matches_s3`` — keeps the ``constraints``
    artifact-name contract in one place (it's the same name ``get_proposal_by_id``
    hydrates from) instead of callers reaching into the private ``_put_artifact_s3``.
    """
    return _put_artifact_s3(namespace, proposal_id, "constraints", constraint_config_json)


def _sweep_proposal_if_stale(proposal_id: str, namespace: str, item: dict) -> dict:
    """Move a proposal stuck in an in-progress accept state to ``accept_failed``.

    The accept worker is a fire-and-forget daemon thread; if it dies mid-run
    (task recycled, crash, or a Neptune/Bedrock call hanging past the task's
    lifetime) the proposal sits in ``accepting``/``embeddings_sync`` forever with
    no recovery path. On read, if such a proposal has had no ``updated_at``
    heartbeat for ``_ACCEPT_STALE_SECONDS``, move it to the terminal
    ``accept_failed`` state with an ``accept_error`` so the UI surfaces it and the
    user can re-accept (the pipeline is idempotent on re-run). Mirrors
    :func:`_sweep_if_stale` for async jobs.

    The accept pipeline checkpoints every step, refreshing ``updated_at`` each
    time, so a genuinely-live-but-slow accept keeps resetting the staleness clock
    and is never swept — only a worker that has made no progress for the whole
    window is treated as dead.

    Returns the (possibly updated) item. Best-effort: a write error is swallowed
    (the next read retries) so a transient DDB hiccup can't 500 the poll the user
    relies on to observe the stuck state.
    """
    return sweep_stale_record(
        item,
        is_in_progress=lambda s: s in _PROPOSAL_IN_PROGRESS_STATUSES,
        max_age_seconds=_ACCEPT_STALE_SECONDS,
        terminal_status=_PROPOSAL_ACCEPT_FAILED_STATUS,
        error_field="accept_error",
        error_message="Accept worker did not complete (task restarted or timed out); re-accept to retry",
        write=lambda fields: update_proposal(proposal_id, namespace=namespace, **fields),
        label=f"Proposal {proposal_id} (ns={namespace})",
        extra_fields={"accept_progress": {"step": "unknown", "state": "stale"}},
    )


def get_proposal_by_id(
    proposal_id: str, namespace: str = DEFAULT_NAMESPACE, hydrate_turtle: bool = True
) -> dict | None:
    """Fetch a proposal's META item, hydrating S3-offloaded content.

    Args:
        proposal_id: ID of the proposal to fetch.
        namespace: Namespace the proposal belongs to.
        hydrate_turtle: When True, re-read the authoritative ontology/R2RML
            Turtle from S3; skip this (possibly multi-MB) read when only
            metadata is needed.

    Returns:
        The proposal item (with matches and, when requested, Turtle hydrated
        from S3), or None if no such proposal exists.
    """
    resp = _get_table().get_item(Key={"PK": _proposal_pk(namespace, proposal_id), "SK": "META"})
    item = resp.get("Item")
    if not item:
        return None
    # Recover a proposal orphaned by a dead accept worker before anything else
    # reads its status (the GET poll, the accept in-progress guard, etc.).
    item = _sweep_proposal_if_stale(proposal_id, namespace, item)
    # Hydrate turtle content from S3 when an S3 key is present. The S3 copy is
    # authoritative — always re-read it (rather than trusting any inline
    # ``ontology_turtle``), so an edit that rewrote the S3 object is reflected
    # even if a prior read left a stale inline copy on the item.
    #
    # ``hydrate_turtle=False`` skips this potentially multi-MB read for callers
    # that only need metadata (e.g. the detail endpoint, which serves the
    # Turtle out-of-band via a presigned S3 URL rather than inlining it).
    if hydrate_turtle and item.get("ontology_s3_key"):
        item["ontology_turtle"] = _get_artifact_s3(namespace, proposal_id, "ontology") or ""
    if hydrate_turtle and item.get("r2rml_s3_key"):
        item["r2rml_turtle"] = _get_artifact_s3(namespace, proposal_id, "r2rml") or ""
    # Hydrate matches from S3
    if item.get("matches_s3_key") and not (item.get("metadata", {}) or {}).get("matches"):
        try:
            import json as _json

            key = item["matches_s3_key"]
            raw = _get_s3().get_object(Bucket=_S3_BUCKET, Key=key)["Body"].read().decode("utf-8")
            matches_data = _json.loads(raw)
            if "metadata" not in item:
                item["metadata"] = {}
            item["metadata"]["matches"] = matches_data.get("matches", [])
        except Exception as e:  # degrade gracefully, but make a misconfigured bucket / corrupt JSON observable
            _log.warning("matches hydration failed for %s in %s: %s", proposal_id, namespace, e)
    # Hydrate constraint_config from S3
    if (item.get("metadata") or {}).get("has_constraint_config"):
        try:
            import json as _json

            raw = _get_artifact_s3(namespace, proposal_id, "constraints")
            if raw:
                if "metadata" not in item:
                    item["metadata"] = {}
                item["metadata"]["constraint_config"] = _json.loads(raw)
        except Exception as e:  # degrade gracefully, but log so the failure isn't silent
            _log.warning("constraint_config hydration failed for %s in %s: %s", proposal_id, namespace, e)
    return item


def list_proposals(
    namespace: str | None = None,
    ontology_id: str | None = None,
    status: str | None = None,
    proposal_type: str | None = None,
    source_type: str | None = None,
    limit: int | None = 50,
) -> list[dict]:
    """Scan proposals with optional filters.

    ``limit`` caps the number of matching items returned. Pass ``limit=None``
    to drain ALL matches (paginating the scan to exhaustion) — used by cleanup
    paths that must not silently truncate (e.g. deleting every proposal
    grounded against an ontology). ``source_type`` filter semantics (added in
    the unstructured-induction-api wiring):
      - ``None`` (default): no filter on source_type — every proposal
        regardless of source type is returned.
      - ``"STRUCTURED"``: items whose ``source_type`` attribute equals
        ``"STRUCTURED"`` AND items that lack the attribute entirely
        (pre-schema-delta data is treated as structured for backward
        compatibility).
      - ``"UNSTRUCTURED"``: items whose ``source_type`` attribute equals
        ``"UNSTRUCTURED"`` exactly. Pre-delta items that lack the
        attribute are NEVER returned for this filter.
    """
    filter_parts, values, names = [], {}, {}
    if namespace:
        filter_parts.append("begins_with(PK, :ns_pfx)")
        values[":ns_pfx"] = f"{namespace}#PROPOSAL#"
    else:
        filter_parts.append("contains(PK, :proposal_marker)")
        values[":proposal_marker"] = "#PROPOSAL#"
    if ontology_id:
        filter_parts.append("ontology_id = :oid")
        values[":oid"] = ontology_id
    if status:
        # `status` is a DynamoDB reserved word — must be aliased.
        names["#s"] = "status"
        filter_parts.append("#s = :st")
        values[":st"] = status
    if proposal_type:
        filter_parts.append("proposal_type = :pt")
        values[":pt"] = proposal_type
    if source_type is not None:
        # Pre-delta items lack the attribute entirely. The asymmetric
        # backfill rule treats those as STRUCTURED only; an explicit
        # filter for UNSTRUCTURED never matches them.
        if source_type == "STRUCTURED":
            filter_parts.append("(source_type = :src OR attribute_not_exists(source_type))")
        else:
            filter_parts.append("source_type = :src")
        values[":src"] = source_type

    filter_parts.append("SK = :meta")
    values[":meta"] = "META"

    kwargs: dict = {
        "FilterExpression": " AND ".join(filter_parts),
        "ExpressionAttributeValues": values,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names

    # Paginate the scan to collect matching items. DynamoDB Limit caps items
    # evaluated (not returned), so a single scan page may return fewer matches
    # than requested. ``limit=None`` drains every matching item (no cap).
    items: list[dict] = []
    while limit is None or len(items) < limit:
        resp = _get_table().scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items if limit is None else items[:limit]


def update_proposal(
    proposal_id: str,
    namespace: str = DEFAULT_NAMESPACE,
    *,
    expected_status: str | None = None,
    expected_status_not: str | None = None,
    **fields,
) -> None:
    """Update proposal fields.

    When ``expected_status`` is supplied, the update runs as a
    DynamoDB conditional write (``ConditionExpression: #s = :exp``).
    The kwarg is keyword-only so ``status=...`` continues to flow
    through ``**fields`` unchanged. Used by the accept-proposal
    handler to atomically transition ``pending|updated → accepting``
    so concurrent accepts can't both spawn workers — the second
    request gets ``ConditionalCheckFailedException`` which the route
    converts to 409.

    ``expected_status_not`` is the inverse guard: the write only applies while
    the stored status is NOT that value. Used by ``_accept_fail`` so a late
    pipeline-step failure can never downgrade an already-``accepted`` proposal
    (which would drop it out of the ``status="accepted"`` queries that rebuild
    the ontology's R2RML). The caller catches
    ``ConditionalCheckFailedException`` and records diagnostics only.

    The two are mutually exclusive — passing both is a programming error.

    All other call sites (cancel, reject, the worker's own status
    flip after ingest) remain unconditional.
    """
    if expected_status is not None and expected_status_not is not None:
        raise ValueError("expected_status and expected_status_not are mutually exclusive")
    fields["updated_at"] = datetime.now(UTC).isoformat()
    expr_parts, values, names = [], {}, {}
    for k, v in fields.items():
        safe = k.replace("#", "_")
        if k == "status":
            names["#s"] = "status"
            expr_parts.append(f"#s = :{safe}")
        else:
            expr_parts.append(f"{k} = :{safe}")
        values[f":{safe}"] = v
    kwargs: dict[str, Any] = {
        "Key": {"PK": _proposal_pk(namespace, proposal_id), "SK": "META"},
        "UpdateExpression": "SET " + ", ".join(expr_parts),
        "ExpressionAttributeValues": values,
    }
    if expected_status is not None:
        names["#s"] = "status"
        values[":expected_status"] = expected_status
        kwargs["ConditionExpression"] = "#s = :expected_status"
    elif expected_status_not is not None:
        # ``attribute_not_exists`` keeps the guard correct for a legacy row that
        # predates the status field — absent status is not the forbidden value.
        names["#s"] = "status"
        values[":forbidden_status"] = expected_status_not
        kwargs["ConditionExpression"] = "attribute_not_exists(#s) OR #s <> :forbidden_status"
    if names:
        kwargs["ExpressionAttributeNames"] = names
    _get_table().update_item(**kwargs)


# ── Namespace registry (PK=NAMESPACE#{ns}) ──────────────────────────────
#
# Two sort-key patterns live under ``PK=NAMESPACE#{namespace}``:
#
#   SK=META                          namespace descriptor (backend, embedding
#                                    model / dims / index, graph URI prefix,
#                                    counters, timestamps).
#   SK=ONTOLOGY#{ontology_id}        one row per ontology imported into the
#                                    namespace, carrying the catalog-level
#                                    metadata (uri, title, counts, graph_uri,
#                                    embedding_index, etc.).
#
# This registry is the authoritative list of "what ontologies exist in
# namespace X" — it supports a single-partition ``Query`` for the list,
# independent of whether the graph store is reachable.


def put_namespace_meta(namespace: str, data: dict | None = None) -> dict:
    """Upsert the namespace META row. Creates it on first call.

    Only the fields supplied in ``data`` are written; ``created_at`` is
    preserved across updates. Returns the written item.
    """
    ts = datetime.now(UTC).isoformat()
    table = _get_table()
    existing = table.get_item(Key={"PK": _namespace_pk(namespace), "SK": "META"}).get("Item")
    item: dict[str, Any] = {
        "PK": _namespace_pk(namespace),
        "SK": "META",
        "namespace": namespace,
        "created_at": (existing or {}).get("created_at", ts),
        "updated_at": ts,
    }
    if existing:
        item = {**existing, **item}
    if data:
        item.update({k: v for k, v in data.items() if v is not None})
    table.put_item(Item=item)
    return item


def get_namespace_meta(namespace: str) -> dict | None:
    """Fetch a namespace's META item.

    Args:
        namespace: Namespace whose metadata row to fetch.

    Returns:
        The namespace META item, or None if the namespace has no row.
    """
    resp = _get_table().get_item(Key={"PK": _namespace_pk(namespace), "SK": "META"})
    return resp.get("Item")


def list_namespaces(limit: int = 100) -> list[dict]:
    """Return every namespace META row.

    Scans with a filter on ``SK = "META"`` and ``begins_with(PK, "NAMESPACE#")``,
    following ``LastEvaluatedKey`` until ``limit`` matches are collected or the
    table is exhausted.

    The pagination is NOT about the namespace count. DynamoDB applies ``Limit`` to
    the number of items **evaluated before filtering**, not to matches returned,
    and this single-table design stores job, proposal, and ingest-job rows
    alongside the ``NAMESPACE#`` ones. With more than ``limit`` such rows — normal
    after modest usage — the first scanned page can contain few or zero namespace
    rows, and a single-page read simply omitted the rest with no error. ``list_jobs``
    / ``list_proposals`` / ``list_ingest_jobs`` in this module already paginate for
    exactly this reason; this path was the one left behind.
    """
    kwargs: dict = {
        "FilterExpression": "begins_with(PK, :p) AND SK = :sk",
        "ExpressionAttributeValues": {":p": "NAMESPACE#", ":sk": "META"},
    }
    items: list[dict] = []
    while len(items) < limit:
        resp = _get_table().scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items[:limit]


def put_ontology_registry(namespace: str, ontology_id: str, data: dict) -> dict:
    """Create or replace the ONTOLOGY#{id} registry row.

    ``ontology_id`` is the ontology's identifier within the namespace (the
    ontology IRI, matching the value stored on the proposal and used as
    the primary key of the graph-store ontology node).
    """
    ts = datetime.now(UTC).isoformat()
    existing = _get_table().get_item(Key={"PK": _namespace_pk(namespace), "SK": _ontology_sk(ontology_id)}).get("Item")
    item: dict[str, Any] = {
        "PK": _namespace_pk(namespace),
        "SK": _ontology_sk(ontology_id),
        "namespace": namespace,
        "ontology_id": ontology_id,
        "created_at": (existing or {}).get("created_at", ts),
        "updated_at": ts,
    }
    if existing:
        item = {**existing, **item}
    item.update({k: v for k, v in data.items() if v is not None})
    # A full (re)write completes any transient lifecycle state. If the row being
    # replaced carried a transient ``status`` (``ingesting`` stub written at
    # async-upload trigger time, or ``deleting``) and the caller didn't supply a
    # fresh ``status``, drop it — a fully-ingested ontology has no status (that's
    # the shape every normally-loaded row has). Without this the "ingesting" stub
    # status would survive the merge and the row would look perpetually in-flight.
    if "status" not in data and item.get("status") in ("ingesting", "deleting"):
        item.pop("status", None)
    _get_table().put_item(Item=item)
    return item


def get_ontology_registry(namespace: str, ontology_id: str) -> dict | None:
    """Fetch a single ontology's registry row from a namespace.

    Args:
        namespace: Namespace the ontology belongs to.
        ontology_id: Ontology identifier (its IRI within the namespace).

    Returns:
        The ontology registry item, or None if it does not exist.
    """
    resp = _get_table().get_item(Key={"PK": _namespace_pk(namespace), "SK": _ontology_sk(ontology_id)})
    return resp.get("Item")


def get_ontology_registry_by_uri(namespace: str, uri: str) -> dict | None:
    """Look up an ontology row by its ``uri`` attribute.

    Implemented as a single-partition Query + client-side filter. For
    namespaces with many ontologies a ``uri``-keyed GSI is the right next
    step; for now the namespaces we operate against carry at most a few
    dozen ontologies so this is fine.
    """
    rows = list_ontologies_registry(namespace=namespace)
    for r in rows:
        if r.get("uri") == uri:
            return r
    return None


def list_ontologies_registry(
    namespace: str,
    ontology_type: str | None = None,
    domain_tag: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """List ontology rows for a namespace.

    Single-partition ``Query`` on ``PK=NAMESPACE#{ns}`` with SK prefix
    ``ONTOLOGY#``. Additional filters are applied server-side.

    Pagination: a ``FilterExpression`` (e.g. ``ontology_type``) is applied by
    DynamoDB *after* reading up to 1 MB per page, so a single ``query`` page can
    return zero matches even when matching rows exist on a later page. When
    ``limit`` is ``None`` we drain every page (``LastEvaluatedKey`` loop) so
    callers that must not miss a match — notably the delete guard's
    induced-ontology presence check — see the complete set. A positive ``limit``
    keeps the single-page fast path used by the paginated list endpoint.
    """
    expr = "PK = :pk AND begins_with(SK, :skp)"
    values: dict[str, Any] = {":pk": _namespace_pk(namespace), ":skp": "ONTOLOGY#"}
    filter_parts: list[str] = []
    if ontology_type:
        filter_parts.append("ontology_type = :otype")
        values[":otype"] = ontology_type
    if domain_tag:
        filter_parts.append("contains(domain_tags, :dt)")
        values[":dt"] = domain_tag

    kwargs: dict[str, Any] = {
        "KeyConditionExpression": expr,
        "ExpressionAttributeValues": values,
    }
    if filter_parts:
        kwargs["FilterExpression"] = " AND ".join(filter_parts)
    if limit:
        kwargs["Limit"] = limit

    items: list[dict] = []
    seen_keys: set[str] = set()
    while limit is None or len(items) < limit:
        resp = _get_table().query(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        # Defensive: real DynamoDB always advances the pagination cursor, but a
        # malformed/mocked response returning the same (or a non-serializable)
        # key would otherwise spin forever. Break if the cursor repeats.
        key_id = repr(sorted(last_key.items())) if isinstance(last_key, dict) else repr(last_key)
        if key_id in seen_keys:
            break
        seen_keys.add(key_id)
        kwargs["ExclusiveStartKey"] = last_key
    return items if limit is None else items[:limit]


def update_ontology_registry(namespace: str, ontology_id: str, **fields) -> dict | None:
    """Patch a subset of attributes on an ontology registry row."""
    if not fields:
        return get_ontology_registry(namespace, ontology_id)
    fields["updated_at"] = datetime.now(UTC).isoformat()

    # Alias EVERY attribute name via #placeholder. Ontology registry rows use
    # several DynamoDB reserved words (status, name, format, uri, version,
    # license, source, ...); a hand-curated reserved set is fragile and silently
    # broke on `format`. Aliasing everything is always valid and future-proof.
    expr_parts: list[str] = []
    values: dict[str, Any] = {}
    names: dict[str, str] = {}
    for k, v in fields.items():
        placeholder = k.replace("#", "_")
        names[f"#{placeholder}"] = k
        expr_parts.append(f"#{placeholder} = :{placeholder}")
        values[f":{placeholder}"] = v

    kwargs: dict[str, Any] = {
        "Key": {"PK": _namespace_pk(namespace), "SK": _ontology_sk(ontology_id)},
        "UpdateExpression": "SET " + ", ".join(expr_parts),
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names

    resp = _get_table().update_item(**kwargs)
    return resp.get("Attributes")


def extend_ontology_registry(
    namespace: str,
    ontology_id: str,
    *,
    add_class_count: int = 0,
    add_property_count: int = 0,
    add_axiom_count: int = 0,
    add_embedding_count: int = 0,
    source_proposal: str | None = None,
    **set_fields,
) -> dict | None:
    """Atomic append / accumulate on an existing ontology registry row.

    Used by the proposal-accept "append mode" so multiple proposals can
    be merged under a single target ontology:

      - ``add_*_count`` values are atomically added (DynamoDB ``ADD``)
        to the corresponding count attributes. Counts become cumulative
        across accepted proposals; they do NOT reflect the deduplicated
        triple cardinality of the merged graph.
      - ``source_proposal``, if given, is appended to the
        ``source_proposals`` list via DynamoDB ``list_append``. Calling
        with the same proposal_id twice will simply append it twice —
        the list is not a set.
      - ``set_fields`` are overwritten via ``SET`` (e.g. ``last_fetched``).
      - ``updated_at`` is refreshed automatically.
    """
    now_iso = datetime.now(UTC).isoformat()

    set_parts: list[str] = []
    add_parts: list[str] = []
    values: dict[str, Any] = {":ts": now_iso}
    names: dict[str, str] = {}
    reserved = {"status", "namespace", "name"}

    # SET updated_at
    set_parts.append("updated_at = :ts")

    # SET arbitrary fields
    for k, v in set_fields.items():
        placeholder = k.replace("#", "_")
        if k in reserved:
            names[f"#{placeholder}"] = k
            set_parts.append(f"#{placeholder} = :{placeholder}")
        else:
            set_parts.append(f"{k} = :{placeholder}")
        values[f":{placeholder}"] = v

    # SET source_proposals = list_append(if_not_exists(source_proposals, :empty), :new)
    if source_proposal is not None:
        values[":sp_new"] = [source_proposal]
        values[":sp_empty"] = []
        set_parts.append("source_proposals = list_append(if_not_exists(source_proposals, :sp_empty), :sp_new)")

    # ADD counts (atomic accumulate)
    count_fields = [
        ("class_count", add_class_count),
        ("property_count", add_property_count),
        ("axiom_count", add_axiom_count),
        ("embedding_count", add_embedding_count),
    ]
    for attr, delta in count_fields:
        if delta:
            values[f":add_{attr}"] = delta
            add_parts.append(f"{attr} :add_{attr}")

    expr = "SET " + ", ".join(set_parts)
    if add_parts:
        expr += " ADD " + ", ".join(add_parts)

    kwargs: dict[str, Any] = {
        "Key": {"PK": _namespace_pk(namespace), "SK": _ontology_sk(ontology_id)},
        "UpdateExpression": expr,
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names

    resp = _get_table().update_item(**kwargs)
    return resp.get("Attributes")


def delete_ontology_registry(namespace: str, ontology_id: str) -> bool:
    """Drop the ontology registry row. Returns True if it existed."""
    existing = get_ontology_registry(namespace, ontology_id)
    if not existing:
        return False
    _get_table().delete_item(Key={"PK": _namespace_pk(namespace), "SK": _ontology_sk(ontology_id)})
    return True


def _best_effort_delete_s3_prefix(prefix: str, context: str) -> None:
    """Delete every S3 object under ``prefix``; best-effort, never fatal.

    Paginates ``list_objects_v2`` and batch-deletes (``delete_objects`` caps at
    1000 keys/request). Only AWS-side failures (throttling, transient S3/network)
    are swallowed and logged — an orphaned S3 object is harmless and must not
    block the caller's DynamoDB delete. Logic bugs (AttributeError, KeyError)
    propagate so they surface. ``context`` labels the caller in the warning log.
    """
    try:
        s3 = _get_s3()
        paginator = s3.get_paginator("list_objects_v2")
        to_delete: list[dict[str, str]] = []
        for page in paginator.paginate(Bucket=_S3_BUCKET, Prefix=prefix):
            to_delete.extend({"Key": obj["Key"]} for obj in page.get("Contents", []))
        for i in range(0, len(to_delete), 1000):
            s3.delete_objects(Bucket=_S3_BUCKET, Delete={"Objects": to_delete[i : i + 1000]})
    except (ClientError, BotoCoreError) as e:
        _log.warning("S3 teardown for %s failed: %s", context, e)


def delete_proposal(namespace: str, proposal_id: str) -> bool:
    """Delete a proposal's DynamoDB row and its S3 artifacts.

    Returns True if the DynamoDB row existed.
    Removes the proposal META item (``{ns}#PROPOSAL#{id}`` / ``META``) and,
    best-effort, every S3 object under ``proposals/{ns}/{id}/`` (ontology +
    r2rml turtle for both ``original`` and ``latest`` versions, plus matches /
    constraints JSON). S3 cleanup failures are logged but do not fail the
    delete — an orphaned S3 object is harmless, whereas a lingering DynamoDB
    row would keep the proposal visible in the catalog.
    """
    existing = _get_table().get_item(Key={"PK": _proposal_pk(namespace, proposal_id), "SK": "META"}).get("Item")

    # Best-effort S3 teardown: delete every object under the proposal's prefix.
    _best_effort_delete_s3_prefix(
        f"{_S3_PROPOSALS_PREFIX}/{namespace}/{proposal_id}/", f"proposal {proposal_id} in {namespace}"
    )

    if not existing:
        return False
    _get_table().delete_item(Key={"PK": _proposal_pk(namespace, proposal_id), "SK": "META"})
    return True


def delete_all_namespace_items(namespace: str) -> int:
    """Delete ALL DynamoDB items belonging to a namespace.

    Covers ontology registry rows, induction jobs, ingest jobs, and proposals.
    Returns the total number of items deleted.
    """
    table = _get_table()
    deleted = 0

    prefixes = [
        _namespace_pk(namespace),  # NAMESPACE#{ns} — ontology registry
        f"{namespace}#JOB#",  # induction jobs + proposal artifacts
        f"{namespace}#PROPOSAL#",  # standalone proposals
        f"{namespace}#INGESTJOB#",  # async ingest jobs
        f"{namespace}#ASYNCJOB#",  # async validate / infer-constraints jobs
    ]

    for prefix in prefixes:
        last_key = None
        while True:
            scan_kwargs: dict[str, Any] = {
                "FilterExpression": "begins_with(PK, :pfx)",
                "ExpressionAttributeValues": {":pfx": prefix},
                "ProjectionExpression": "PK, SK",
            }
            if last_key:
                scan_kwargs["ExclusiveStartKey"] = last_key
            resp = table.scan(**scan_kwargs)
            items = resp.get("Items", [])
            with table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                    deleted += 1
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break

    # Best-effort S3 teardown: sweep every offloaded job manifest under jobs/{ns}/
    # (put_job_datasources_s3) — deleting the DDB job rows above orphans them otherwise.
    _best_effort_delete_s3_prefix(f"jobs/{namespace}/", f"namespace {namespace} job manifests")

    return deleted
