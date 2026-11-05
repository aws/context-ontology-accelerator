# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Whether a Neptune endpoint belongs to COA's own catalog cluster.

That cluster holds every namespace's ontology in its own named graph, and Neptune's default graph is their union, so
no graph source may point at any of its endpoints. Serve checks a source's host against the deployment's platform
endpoint by name, and a custom or instance endpoint by the cluster its claim names; graph-verify reads the cluster's
endpoints from Neptune.
"""

from __future__ import annotations

from typing import Any, TypeGuard
from urllib.parse import urlsplit

# The deployment's own catalog cluster (serve-stack.ts).
PLATFORM_ENDPOINT_ENV = "NEPTUNE_ENDPOINT"

# A registered Neptune cluster serves one namespace. Graph-verify writes one claim row per cluster in the sources
# table and notes its PK on the source row under GRAPH_CLUSTER_CLAIM_ATTR; serve re-checks it before every query.
GRAPH_CLUSTER_CLAIM_SK = "CLAIM"
GRAPH_CLUSTER_CLAIM_ATTR = "graphClusterClaim"
# Each approval writes a fresh token on the claim row, so a release that read the row before checking the
# namespace's sources cannot delete a claim a source was approved under in the meantime.
GRAPH_CLUSTER_APPROVAL_TOKEN_ATTR = "approvalToken"
_GRAPH_CLUSTER_CLAIM_PREFIX = "GRAPHCLUSTER#"
_ENDPOINT_KINDS = ("cluster-ro-", "cluster-custom-", "cluster-")


def endpoint_host(url: str) -> str:
    """The lower-cased host of an endpoint URL or bare host name, without a fully-qualified trailing dot."""
    return (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower().removesuffix(".")


def _cluster(host: str) -> str:
    """A Neptune endpoint's cluster: ``<cluster>.cluster[-ro|-custom]-<hash>.<region>…`` has one key for every kind."""
    name, _, rest = host.lower().partition(".")
    kind = next((k for k in _ENDPOINT_KINDS if rest.startswith(k)), None)
    return f"{name}.{rest.removeprefix(kind)}" if kind else host.lower()


def is_platform_endpoint(host: str, platform: str, claim_pk: str | None = None) -> bool:
    """Any endpoint of COA's own cluster: by name, or by the cluster the source's claim names.

    A writer or reader endpoint names its cluster. A custom endpoint and an instance endpoint
    (``<instance>.<hash>.<region>…``) do not, and the hash is shared by every cluster of an account and region, so for
    them the cluster is the one verify resolved the endpoint to and keyed the claim on. Without a claim (none read, or
    none noted on the source) it fails closed: any such endpoint in the platform's account and region counts.
    """
    if _cluster(host) == _cluster(platform):
        return True
    if not isinstance(claim_pk, str):
        _, _, rest = host.lower().partition(".")
        _, _, platform_rest = platform.lower().partition(".")
        suffix = next((platform_rest.removeprefix(k) for k in _ENDPOINT_KINDS if platform_rest.startswith(k)), None)
        return suffix is not None and rest in (f"cluster-custom-{suffix}", suffix)
    platform_id, _, rest = platform.lower().partition(".")
    region = rest.split(".")[1] if rest.count(".") >= 1 else ""
    return claim_pk.lower() == f"{_GRAPH_CLUSTER_CLAIM_PREFIX}{region}#{platform_id}".lower()


def graph_cluster_claim_key(region: str, cluster_id: str) -> dict[str, str]:
    """The claim row of one cluster: every endpoint of it (writer, reader, custom) shares the key."""
    return {"PK": f"{_GRAPH_CLUSTER_CLAIM_PREFIX}{region}#{cluster_id}", "SK": GRAPH_CLUSTER_CLAIM_SK}


def graph_cluster_claim_release(table: str, claim_pk: str, namespace_id: str, claim: object) -> dict[str, Any]:
    """The TransactWriteItems Delete releasing the namespace's claim, unless it changed since ``claim`` was read."""
    token = claim.get(GRAPH_CLUSTER_APPROVAL_TOKEN_ATTR) if isinstance(claim, dict) else None
    if isinstance(token, str):
        condition = f"namespaceId = :ns AND {GRAPH_CLUSTER_APPROVAL_TOKEN_ATTR} = :token"
        values = {":ns": namespace_id, ":token": token}
    else:
        condition = f"namespaceId = :ns AND attribute_not_exists({GRAPH_CLUSTER_APPROVAL_TOKEN_ATTR})"
        values = {":ns": namespace_id}
    return {
        "Delete": {
            "TableName": table,
            "Key": {"PK": claim_pk, "SK": GRAPH_CLUSTER_CLAIM_SK},
            "ConditionExpression": condition,
            "ExpressionAttributeValues": values,
        }
    }


def is_graph_cluster_claim_pk(pk: object) -> TypeGuard[str]:
    """Whether ``pk`` names a cluster claim row, so a source row's note cannot steer a read to another row."""
    return (
        isinstance(pk, str)
        and pk.startswith(_GRAPH_CLUSTER_CLAIM_PREFIX)
        and len(pk) > len(_GRAPH_CLUSTER_CLAIM_PREFIX)
    )


def graph_cluster_refusal(claim: dict[str, Any] | None, namespace_id: str) -> str | None:
    """Why a source of ``namespace_id`` may not read its cluster, given the cluster's claim row; fails closed."""
    if not claim:
        return "the source's cluster is not claimed for its namespace; verify the source again"
    if claim.get("namespaceId") != namespace_id:
        return "the source's cluster is registered to another namespace"
    return None


def platform_refusal(host: str, platform_endpoint: str, claim_pk: str | None) -> str | None:
    """Why a graph source on ``host`` with claim ``claim_pk`` may not be read, or None.

    Fails closed when the platform cluster is not named.
    """
    platform = endpoint_host(platform_endpoint.strip())
    if not platform:
        return "this deployment does not name COA's own catalog cluster, so no graph source can be checked against it"
    if is_platform_endpoint(endpoint_host(host), platform, claim_pk):
        return "the source is on COA's own catalog cluster, which holds every namespace's ontology"
    return None
