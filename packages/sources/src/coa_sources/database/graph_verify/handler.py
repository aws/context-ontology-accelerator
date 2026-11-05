# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Graph-verify Lambda: reachability check for RDF_SPARQL sources.

Consumes the graph-verify queue (batchSize 1). For each message it reads the source
record, parses its ``configuration`` (:mod:`coa_common.sparql_config`), runs the
reachability probe registered for ``configuration.store`` and flips the record:

- reachable and authorized → ``status=APPROVED``, ``queryable=true``
- anything else            → ``status=SCAN_FAILED``, ``queryable=false``, ``errorMessage``

Before probing, the endpoint is refused if it is the platform's own catalog cluster
(:func:`_platform_cluster_hosts`). That cluster holds every namespace's ontology in its own
named graph, and Neptune's default graph is their union, so a source on it would let one
namespace read every other namespace's ontology. The refusal fails closed: a deployment that
names no platform cluster, or a cluster that cannot be read, approves no source.

The probe proves the endpoint answers and that this identity may call it — not that the
namespace can answer graph questions, which also needs the ontology import. Serve's
fail-loud guard covers that gap.

A new SPARQL engine is one more entry in :data:`PROBES` (the registration-time twin of
serve's client factory), keyed by the same ``store`` value; no new queue, Lambda or
sub-type.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError
from coa_common.aws_config import THROTTLE_ERROR_CODES, sync_boto_config
from coa_common.constants import GRAPH_BACKED_SUB_TYPES
from coa_common.dao import DynamoDBDAO
from coa_common.dao.base import QueryParams
from coa_common.logging import setup_logging
from coa_common.neptune_endpoints import (
    GRAPH_CLUSTER_APPROVAL_TOKEN_ATTR,
    GRAPH_CLUSTER_CLAIM_ATTR,
    GRAPH_CLUSTER_CLAIM_SK,
    graph_cluster_claim_key,
    graph_cluster_claim_release,
)
from coa_common.sparql_config import (
    SparqlConfigurationError,
    SparqlSourceConfig,
    SparqlStore,
    graph_sources_enabled,
    parse_sparql_configuration,
)

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

SOURCES_TABLE = os.environ.get("SOURCES_TABLE", "")
SOURCE_SCAN_JOBS_TABLE = os.environ.get("SOURCE_SCAN_JOBS_TABLE", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Short on purpose: a status call returns in milliseconds, and an unreachable endpoint
# should fail the verify quickly rather than hold the invocation to its timeout.
PROBE_TIMEOUT_S = int(os.environ.get("GRAPH_VERIFY_TIMEOUT_S", "8"))
# The queue's maxReceiveCount (sources-stack.ts): the receive SQS will not redeliver records, rather than raises.
MAX_RECEIVES = int(os.environ.get("GRAPH_VERIFY_MAX_RECEIVES", "3"))
# The platform catalog cluster (sources-stack.ts), which no graph source may point at.
PLATFORM_NEPTUNE_CLUSTER_ID = os.environ.get("PLATFORM_NEPTUNE_CLUSTER_ID", "")

_ERROR_MESSAGE_MAX = 1000
_LOG_MESSAGE_MAX = 200

_dao: DynamoDBDAO | None = None
_scan_dao: DynamoDBDAO | None = None


def _get_dao() -> DynamoDBDAO:
    global _dao
    if _dao is None:
        _dao = DynamoDBDAO(SOURCES_TABLE, region=AWS_REGION)
    return _dao


def _get_scan_dao() -> DynamoDBDAO | None:
    global _scan_dao
    if _scan_dao is None and SOURCE_SCAN_JOBS_TABLE:
        _scan_dao = DynamoDBDAO(SOURCE_SCAN_JOBS_TABLE, region=AWS_REGION)
    return _scan_dao


class ProbeFailedError(Exception):
    """The endpoint did not prove reachable and authorized; ``str()`` is the reason."""


def probe_neptune(config: SparqlSourceConfig) -> None:
    """Call Neptune's ``GetEngineStatus`` over SigV4 with this Lambda's role.

    ``neptunedata`` signs as service ``neptune-db``, so a well-formed response proves
    both reachability and that the role holds ``neptune-db:GetEngineStatus`` — with no
    SPARQL and no data read.
    """
    client = boto3.client(
        "neptunedata",
        endpoint_url=config.base_url,
        region_name=config.signing_region(AWS_REGION),
        # One attempt: botocore's max_attempts counts retries, total_max_attempts counts every attempt.
        config=sync_boto_config(read_timeout=PROBE_TIMEOUT_S, connect_timeout=PROBE_TIMEOUT_S).merge(
            BotoConfig(retries={"mode": "standard", "total_max_attempts": 1})
        ),
    )
    try:
        client.get_engine_status()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AccessDeniedException", "AccessDenied", "UnauthorizedException"}:
            raise ProbeFailedError(
                f"Access denied calling {config.base_url}; the verify role needs neptune-db:GetEngineStatus "
                "and the cluster must have IAM authentication enabled"
            ) from exc
        raise ProbeFailedError(f"Endpoint {config.base_url} returned {code or 'an error'}") from exc
    except (BotoCoreError, OSError) as exc:
        raise ProbeFailedError(
            f"Endpoint {config.base_url} is unreachable from the service VPC ({type(exc).__name__})"
        ) from exc


class AccountClusterHosts(dict[str, str]):
    """Endpoint host → cluster identifier, plus the clusters whose IAM database authentication is off."""

    def __init__(self, hosts: dict[str, str] | None = None, *, iam_auth_disabled: Iterable[str] = ()) -> None:
        """``hosts`` as the mapping; ``iam_auth_disabled`` names the clusters serve could not sign for."""
        super().__init__(hosts or {})
        self.iam_auth_disabled = frozenset(iam_auth_disabled)


def _account_cluster_endpoints(region: str) -> AccountClusterHosts:
    """Every cluster, reader and custom endpoint of this account's Neptune clusters in ``region``, to its cluster.

    A cluster is named by its identifier, which its host names encode: a cluster deleted and re-created under the
    same identifier keeps its hosts, so it must keep its claim.
    """
    client = boto3.client("neptune", region_name=region, config=sync_boto_config(max_attempts=3))
    hosts: dict[str, str] = {}
    cluster_ids: set[str] = set()
    iam_auth_disabled: set[str] = set()
    for page in client.get_paginator("describe_db_clusters").paginate():
        for cluster in page.get("DBClusters", []):
            if cluster.get("Engine") != "neptune":
                continue
            cluster_id = cluster.get("DBClusterIdentifier", "")
            cluster_ids.add(cluster_id)
            if cluster.get("IAMDatabaseAuthenticationEnabled") is False:
                iam_auth_disabled.add(cluster_id)
            endpoints = (cluster.get("Endpoint"), cluster.get("ReaderEndpoint"), *cluster.get("CustomEndpoints", []))
            hosts.update(dict.fromkeys((h for h in endpoints if h), cluster_id))
    hosts.update({h: cid for cid, h in _cluster_endpoint_hosts(client) if cid in cluster_ids})
    return AccountClusterHosts(hosts, iam_auth_disabled=iam_auth_disabled)


def _cluster_endpoint_hosts(client: Any, **filters: str) -> list[tuple[str, str]]:
    """``(cluster id, host)`` of every listed cluster endpoint; Neptune's DescribeDBClusters omits custom ones."""
    return [
        (endpoint.get("DBClusterIdentifier", ""), endpoint["Endpoint"])
        for page in client.get_paginator("describe_db_cluster_endpoints").paginate(**filters)
        for endpoint in page.get("DBClusterEndpoints", [])
        if endpoint.get("Endpoint")
    ]


class PlatformClusterUnknownError(LookupError):
    """The named platform cluster is not in this account and region."""


def _platform_cluster_hosts(region: str) -> frozenset[str]:
    """The platform cluster's writer, reader and custom endpoints, lower-cased.

    Instance endpoints and addresses need no entry: the account allowlist holds only cluster endpoints.
    """
    client = boto3.client("neptune", region_name=region, config=sync_boto_config(max_attempts=3))
    clusters = client.describe_db_clusters(DBClusterIdentifier=PLATFORM_NEPTUNE_CLUSTER_ID).get("DBClusters", [])
    hosts = {
        h for c in clusters for h in (c.get("Endpoint"), c.get("ReaderEndpoint"), *c.get("CustomEndpoints", [])) if h
    }
    if not hosts:
        raise PlatformClusterUnknownError(PLATFORM_NEPTUNE_CLUSTER_ID)
    hosts.update(h for _, h in _cluster_endpoint_hosts(client, DBClusterIdentifier=PLATFORM_NEPTUNE_CLUSTER_ID))
    return frozenset(h.lower() for h in hosts)


def _is_transient(exc: Exception) -> bool:
    """A throttle, a 5xx or a connection failure: worth an SQS retry rather than a SCAN_FAILED."""
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return True
    if not isinstance(exc, ClientError):
        return False
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
    return _error_code(exc) in THROTTLE_ERROR_CODES or status >= 500


def _error_code(exc: Exception) -> str:
    code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc, ClientError) else ""
    return code or type(exc).__name__


def _lookup_failed(event: str, exc: Exception) -> str:
    """Log a non-transient cluster lookup failure and return its error code for the recorded reason."""
    code = _error_code(exc)
    logger.error(event, error=type(exc).__name__, error_code=code, message=str(exc)[:_LOG_MESSAGE_MAX])
    return code


def _platform_refusal(host: str, last_receive: bool = False) -> str | None:
    """Why ``host`` may not become a graph source as the platform's catalog cluster, or None; fails closed."""
    if not PLATFORM_NEPTUNE_CLUSTER_ID:
        return "This deployment does not name its platform catalog cluster, so no graph source is approved"
    try:
        platform_hosts = _platform_cluster_hosts(AWS_REGION)
    except (ClientError, BotoCoreError, PlatformClusterUnknownError) as exc:
        if _is_transient(exc) and not last_receive:
            raise
        code = _lookup_failed("graph_verify_platform_lookup_failed", exc)
        return f"Could not read the platform's catalog cluster ({code}); nothing was probed"
    if host.lower() in platform_hosts:
        return (
            f"Endpoint {host} is the platform's ontology cluster, which holds every namespace's ontology; "
            "register a Neptune cluster that holds your data instead"
        )
    return None


# store → reachability probe. Must cover every SparqlStore the API accepts; a store with
# no probe fails its verify with a reason rather than being silently approved.
PROBES: dict[SparqlStore, Callable[[SparqlSourceConfig], None]] = {
    SparqlStore.NEPTUNE: probe_neptune,
}


def handler(event: dict[str, Any], context: object) -> None:
    """Process one graph-verify SQS record per invocation."""
    if not graph_sources_enabled():
        # A verify left on the queue when the deployment turned graph sources off: the source stays as it is.
        logger.info("graph_verify_skipped_graph_sources_disabled", records=len(event.get("Records", [])))
        return
    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        try:
            body = json.loads(record["body"])
            source_id = body["sourceId"]
            namespace_id = body["namespaceId"]
        except (json.JSONDecodeError, KeyError, TypeError):
            # No retry can fix a malformed body, so it is dropped rather than DLQ'd.
            logger.error("graph_verify_malformed_message", message_id=message_id)
            continue
        # ClientError from the DynamoDB reads/writes, and a transient cluster-lookup failure before the last
        # receive, propagate: SQS retries them, and a message that keeps failing lands in the DLQ, which is alarmed.
        verify_source(
            namespace_id, source_id, body.get("scanJobSK"), last_receive=_receive_count(record) >= MAX_RECEIVES
        )


def _receive_count(record: dict[str, Any]) -> int:
    try:
        return int(record.get("attributes", {}).get("ApproximateReceiveCount", 1))
    except (TypeError, ValueError):
        return 1


def verify_source(namespace_id: str, source_id: str, scan_job_sk: str | None, *, last_receive: bool = False) -> None:
    """Probe one source and record the outcome on the source and its scan-job row.

    On the ``last_receive`` any failure of the probe step, transient or not, is recorded as SCAN_FAILED with its
    error code rather than raised: SQS would dead-letter the message and leave the source REGISTERED.
    """
    key = {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"}
    item = _get_dao().get(key)
    if not item:
        # Deleted between enqueue and delivery; nothing to record.
        logger.info("graph_verify_source_gone", source_id=source_id, namespace_id=namespace_id)
        return
    sub_type = item.get("sourceSubType", "")
    if sub_type not in GRAPH_BACKED_SUB_TYPES:
        logger.warning("graph_verify_not_a_graph_source", source_id=source_id, sub_type=sub_type)
        return

    try:
        error = _probe(item, namespace_id, scan_job_sk, last_receive)
    except Exception as exc:
        if not last_receive:
            raise
        code = _lookup_failed("graph_verify_last_receive_failed", exc)
        error = f"Verify could not complete ({code}); verify the source again"
    if error is None:
        # _probe recorded APPROVED together with the claim.
        logger.info("graph_verify_reachable", source_id=source_id, namespace_id=namespace_id)
        _finish_scan_job(source_id, scan_job_sk, status="COMPLETED", error=None)
    elif error == _SUPERSEDED:
        # Nothing to record on the source. Close this verify's own job only if it is still open: a
        # duplicate delivery of the approving verify must not turn its COMPLETED job into FAILED.
        logger.info("graph_verify_superseded", source_id=source_id, namespace_id=namespace_id)
        _finish_scan_job(source_id, scan_job_sk, status="FAILED", error=error, only_if_open=True)
    else:
        logger.warning("graph_verify_failed", source_id=source_id, namespace_id=namespace_id, reason=error)
        _record(key, status="SCAN_FAILED", queryable=False, error=error, scan_job_sk=scan_job_sk)
        # Only while the job is open: a duplicate delivery whose probe fails must not undo a COMPLETED job.
        _finish_scan_job(source_id, scan_job_sk, status="FAILED", error=error, only_if_open=True)


def _probe(item: dict[str, Any], namespace_id: str, scan_job_sk: str | None, last_receive: bool = False) -> str | None:
    """Run the store's probe; ``None`` once the source is recorded APPROVED, else the failure reason."""
    try:
        config = parse_sparql_configuration(item.get("configuration"))
    except SparqlConfigurationError as exc:
        return f"Invalid source configuration: {exc}"
    # Nothing is sent to an endpoint that is not a cluster of this account in this region.
    region = config.signing_region(AWS_REGION)
    if region != AWS_REGION:
        return f"The source signs for {region}; only clusters in this deployment's region {AWS_REGION} are verified"
    refusal = _platform_refusal(config.host, last_receive)
    if refusal:
        return refusal
    cluster_id, refusal = _account_cluster(config.host, last_receive)
    if refusal:
        return refusal
    claim_key = graph_cluster_claim_key(region, cluster_id)
    if not _claim_cluster(namespace_id, claim_key):
        return _CLAIMED_ELSEWHERE
    try:
        error = _probe_holding_claim(item, namespace_id, config, claim_key, scan_job_sk)
    except Exception:
        _release_unused_claim(item, namespace_id, claim_key["PK"])
        raise
    if error:
        _release_unused_claim(item, namespace_id, claim_key["PK"], source_gone=error == _SOURCE_DELETED)
    return error


def _probe_holding_claim(
    item: dict[str, Any],
    namespace_id: str,
    config: SparqlSourceConfig,
    claim_key: dict[str, str],
    scan_job_sk: str | None,
) -> str | None:
    """Note the claim, probe, and record APPROVED in one write with the claim, which a concurrent verify may drop."""
    if not _note_claim(item, claim_key["PK"]):
        return _SOURCE_DELETED
    return _run_probe(config) or _approve_holding_claim(item, namespace_id, claim_key, scan_job_sk)


def _approve_holding_claim(
    item: dict[str, Any], namespace_id: str, claim_key: dict[str, str], scan_job_sk: str | None
) -> str | None:
    """Record APPROVED only while the namespace holds the claim, taking it again if a concurrent verify dropped it."""
    now = _now_iso()
    condition, expected = _expected_state(scan_job_sk)
    values: dict[str, Any] = {":approved": "APPROVED", ":true": True, ":now": now, **expected}
    update = (
        f"SET #st = :approved, queryable = :true, {_EVER_APPROVED_ATTR} = :true, updatedAt = :now, lastScanAt = :now"
    )
    if scan_job_sk:
        update += ", lastScanJobId = :job"
        values[":job"] = scan_job_sk
    try:
        _get_dao().transact_write(
            [
                {
                    "Put": {
                        "TableName": SOURCES_TABLE,
                        "Item": {
                            **claim_key,
                            "namespaceId": namespace_id,
                            GRAPH_CLUSTER_APPROVAL_TOKEN_ATTR: uuid.uuid4().hex,
                        },
                        "ConditionExpression": "attribute_not_exists(PK) OR namespaceId = :ns",
                        "ExpressionAttributeValues": {":ns": namespace_id},
                    }
                },
                {
                    "Update": {
                        "TableName": SOURCES_TABLE,
                        "Key": {"PK": item["PK"], "SK": item["SK"]},
                        "UpdateExpression": update + " REMOVE errorMessage",
                        "ConditionExpression": condition,
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": values,
                    }
                },
            ]
        )
    except ClientError as exc:
        claim_lost, source_gone = _failed_conditions(exc, 2)
        if claim_lost:
            return _CLAIMED_ELSEWHERE
        if source_gone:
            if _get_dao().get({"PK": item["PK"], "SK": item["SK"]}, consistent_read=True):
                logger.info("graph_verify_result_superseded", key=item["SK"])
                return _SUPERSEDED
            logger.info("graph_verify_source_deleted_during_probe", key=item["SK"])
            return _SOURCE_DELETED
        raise
    return None


def _expected_state(scan_job_sk: str | None) -> tuple[str, dict[str, Any]]:
    """The condition under which this verify may record its result, and its values.

    Create and re-verify both leave the row REGISTERED for the verify they enqueue, so a row in
    any other status has moved on (deleted, or recorded by another verify); and a verify older
    than the last one that recorded (``lastScanJobId``, a scan-job timestamp) is a stale
    delivery. A message without a scan job is checked on the status alone.
    """
    values: dict[str, Any] = {":registered": "REGISTERED"}
    if not scan_job_sk:
        return "#st = :registered", values
    values[":job"] = scan_job_sk
    return "#st = :registered AND (attribute_not_exists(lastScanJobId) OR lastScanJobId < :job)", values


def _failed_conditions(exc: ClientError, count: int) -> list[bool]:
    """Which items of a cancelled transaction failed their condition; all False for any other error."""
    if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return [False] * count
    reasons = exc.response.get("CancellationReasons") or []
    failed = [r.get("Code") == "ConditionalCheckFailed" for r in reasons]
    return (failed + [False] * count)[:count]


def _account_cluster(host: str, last_receive: bool) -> tuple[str, str | None]:
    """The identifier of the account cluster ``host`` names, and the reason it may not be verified, if any."""
    try:
        account_hosts = _account_cluster_endpoints(AWS_REGION)
    except (ClientError, BotoCoreError) as exc:
        if _is_transient(exc) and not last_receive:
            raise
        code = _lookup_failed("graph_verify_account_lookup_failed", exc)
        return "", f"Could not list this account's Neptune clusters ({code}); nothing was probed"
    if host not in account_hosts:
        return "", f"Endpoint {host} is not a Neptune cluster endpoint of this account in {AWS_REGION}"
    cluster_id = account_hosts[host]
    if cluster_id in getattr(account_hosts, "iam_auth_disabled", ()):
        return cluster_id, (
            f"The Neptune cluster {cluster_id} does not have IAM database authentication enabled, "
            "which serve needs to sign its queries; enable it, then verify the source again"
        )
    return cluster_id, None


def _run_probe(config: SparqlSourceConfig) -> str | None:
    probe = PROBES.get(config.store)
    if probe is None:
        return f"No reachability check exists for store '{config.store.value}'"
    try:
        probe(config)
    except ProbeFailedError as exc:
        return str(exc)
    return None


_CLAIMED_ELSEWHERE = "The Neptune cluster is registered to another namespace; a cluster serves one namespace"
_SOURCE_DELETED = "The source was deleted during its verify"
_SUPERSEDED = "A newer verify or a delete superseded this verify; nothing was recorded"
# A source in one of these statuses uses its noted claim: it serves, or its verify is still running.
_CLAIM_USING_STATUSES = frozenset({"APPROVED", "REGISTERED"})
# Written with every approval. A source that was ever APPROVED keeps its cluster's claim until it is deleted.
_EVER_APPROVED_ATTR = "graphClusterEverApproved"


def _claim_cluster(namespace_id: str, claim_key: dict[str, str]) -> bool:
    """Claim the cluster for the namespace, or keep its claim; False if another namespace holds it.

    ``auto_timestamp=False`` keeps the claim out of the ``ByNamespace`` GSI (``namespaceId``, ``createdAt``), so it
    never appears in a namespace's source listing.
    """
    try:
        # An update, not a put: a put would drop the approval token a concurrent release checks.
        _get_dao().update(
            claim_key,
            {"namespaceId": namespace_id},
            condition="attribute_not_exists(PK) OR namespaceId = :ns",
            condition_values={":ns": namespace_id},
            auto_timestamp=False,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return False
    return True


def _note_claim(item: dict[str, Any], claim_pk: str) -> bool:
    """Note the claim on the source; False if the source was deleted since."""
    try:
        _get_dao().update(
            {"PK": item["PK"], "SK": item["SK"]},
            {GRAPH_CLUSTER_CLAIM_ATTR: claim_pk},
            condition="attribute_exists(PK)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return False
    return True


_MAX_SOURCE_PAGES = 100  # the DAO's query_all bound


def _release_unused_claim(item: dict[str, Any], namespace_id: str, claim_pk: str, *, source_gone: bool = False) -> None:
    """Release the namespace's claim after a failed verify unless a source of the namespace uses it. Best-effort.

    Kept while the source was ever APPROVED and still exists, or another source of the namespace that notes the
    claim was ever APPROVED or is still being verified, so a claim goes only with its last such source's delete.
    The delete is conditional on the namespace (a claim another namespace took since is kept), on no approval since
    the claim was read (a sibling approved meanwhile keeps it), and on this source not being APPROVED (a duplicate
    delivery).
    """
    if _uses_claim_while_failed(item) and not source_gone:
        return
    dao = _get_dao()
    try:
        claim = dao.get({"PK": claim_pk, "SK": GRAPH_CLUSTER_CLAIM_SK}, consistent_read=True)
        if _other_source_uses_claim(dao, namespace_id, item.get("SK", ""), claim_pk):
            return
        dao.transact_write(
            [
                graph_cluster_claim_release(SOURCES_TABLE, claim_pk, namespace_id, claim),
                {
                    "ConditionCheck": {
                        "TableName": SOURCES_TABLE,
                        "Key": {"PK": item["PK"], "SK": item["SK"]},
                        "ConditionExpression": "attribute_not_exists(PK) OR #st <> :approved",
                        "ExpressionAttributeNames": {"#st": "status"},
                        "ExpressionAttributeValues": {":approved": "APPROVED"},
                    }
                },
            ]
        )
    except ClientError as exc:
        if any(_failed_conditions(exc, 2)):
            logger.info("graph_cluster_claim_kept", namespace_id=namespace_id, claim=claim_pk)
        else:
            logger.warning("graph_cluster_claim_release_failed", namespace_id=namespace_id, claim=claim_pk)
    except BotoCoreError:
        logger.warning("graph_cluster_claim_release_failed", namespace_id=namespace_id, claim=claim_pk)


def _uses_claim_while_failed(source: dict[str, Any]) -> bool:
    """A source keeps its claim through failed verifies once it was ever APPROVED, or while it still serves."""
    return source.get(_EVER_APPROVED_ATTR) is True or source.get("queryable") is True


def _other_source_uses_claim(dao: DynamoDBDAO, namespace_id: str, source_sk: str, claim_pk: str) -> bool:
    """Whether another source of the namespace notes the claim and uses it; a listing cut short keeps it."""
    start: dict[str, Any] | None = None
    for _ in range(_MAX_SOURCE_PAGES):
        page = dao.query(
            QueryParams(
                key_condition="PK = :pk AND begins_with(SK, :prefix)",
                expression_values={":pk": f"NS#{namespace_id}", ":prefix": "SRC#"},
                exclusive_start_key=start,
                # A lagging replica could miss a source's claim note and let the release delete a claim in use.
                consistent_read=True,
            )
        )
        if any(
            i.get(GRAPH_CLUSTER_CLAIM_ATTR) == claim_pk
            and (i.get("status") in _CLAIM_USING_STATUSES or _uses_claim_while_failed(i))
            and i.get("SK") != source_sk
            for i in page.items
        ):
            return True
        start = page.last_evaluated_key
        if not start:
            return False
    return True


def _record(key: dict[str, str], *, status: str, queryable: bool, error: str | None, scan_job_sk: str | None) -> None:
    """Write the verify outcome onto the source record.

    Also points ``lastScanJobId`` / ``lastScanAt`` at this verify, as discovery does
    for a SQL scan, so the console's last-scan field and its failure banner (which
    reads the scan-job row ``lastScanJobId`` names) show the verify's result.

    Conditional on the row still waiting for this verify (:func:`_expected_state`), so a
    source deleted while being probed is not resurrected as a partial item, and a stale or
    duplicate verify never overwrites a newer state.
    """
    now = _now_iso()
    fields: dict[str, Any] = {"status": status, "queryable": queryable, "updatedAt": now, "lastScanAt": now}
    if scan_job_sk:
        fields["lastScanJobId"] = scan_job_sk
    if error:
        fields["errorMessage"] = error[:_ERROR_MESSAGE_MAX]
    condition, values = _expected_state(scan_job_sk)
    try:
        _get_dao().update(
            key,
            fields,
            remove_fields=None if error else ["errorMessage"],
            condition=condition,
            condition_names={"#st": "status"},
            condition_values=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        logger.info("graph_verify_result_superseded_or_deleted", key=key)


def _finish_scan_job(
    source_id: str, scan_job_sk: str | None, *, status: str, error: str | None, only_if_open: bool = False
) -> None:
    """Close the Scan History row the API opened. Best-effort: the source row is authoritative.

    ``only_if_open`` leaves a row another delivery of the same verify already closed.
    """
    scan_dao = _get_scan_dao()
    if scan_dao is None or not scan_job_sk:
        return
    fields: dict[str, Any] = {"status": status, "completedAt": _now_iso()}
    if error:
        fields["errorMessage"] = error[:_ERROR_MESSAGE_MAX]
    condition: dict[str, Any] = (
        {
            "condition": "attribute_exists(PK) AND #st = :open",
            "condition_names": {"#st": "status"},
            "condition_values": {":open": "IN_PROGRESS"},
        }
        if only_if_open
        else {"condition": "attribute_exists(PK)"}
    )
    if not scan_dao.update(
        {"PK": f"SRC#{source_id}", "SK": scan_job_sk},
        fields,
        raise_on_error=False,
        **condition,
    ):
        logger.warning("graph_verify_scan_job_update_failed", source_id=source_id, scan_job_sk=scan_job_sk)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
