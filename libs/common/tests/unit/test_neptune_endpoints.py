# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Serve's check that a graph source is not on COA's own catalog cluster.

Graph-verify approves only this account's cluster endpoints, by exact name; this name check is what serve applies
to a source record, so it must refuse every host form of the platform cluster that a record could carry.
"""

from __future__ import annotations

import pytest
from coa_common.neptune_endpoints import endpoint_host, is_platform_endpoint, platform_refusal

pytestmark = pytest.mark.unit

PLATFORM = "coa-dev-neptune.cluster-abc123xyz.us-west-2.neptune.amazonaws.com"


@pytest.mark.parametrize(
    "host",
    [
        PLATFORM,
        "coa-dev-neptune.cluster-ro-abc123xyz.us-west-2.neptune.amazonaws.com",
        PLATFORM.upper(),
        f"https://{PLATFORM}:8182/sparql",
        f"{PLATFORM}.",  # the trailing-dot FQDN resolves to the same cluster
        "coa-dev-neptune.cluster-ro-abc123xyz.us-west-2.neptune.amazonaws.com.",
    ],
)
def test_every_named_host_form_of_the_platform_cluster_is_refused(host: str) -> None:
    assert platform_refusal(host, f"https://{PLATFORM}:8182", OTHER_CLAIM) is not None


# A custom or instance endpoint names no cluster, and Neptune's endpoint hash is shared by every cluster of an
# account and region, so the cluster such a source is on comes from its claim, which verify keyed on the cluster
# it resolved the endpoint to.
CUSTOM_HOSTS = [
    "any-name.cluster-custom-abc123xyz.us-west-2.neptune.amazonaws.com",
    "coa-dev-neptune-instance-1.abc123xyz.us-west-2.neptune.amazonaws.com",  # an instance endpoint
    "any-instance.abc123xyz.us-west-2.neptune.amazonaws.com.",
]
PLATFORM_CLAIM = "GRAPHCLUSTER#us-west-2#coa-dev-neptune"
OTHER_CLAIM = "GRAPHCLUSTER#us-west-2#customer-graph"


@pytest.mark.parametrize("host", CUSTOM_HOSTS)
def test_a_custom_or_instance_endpoint_whose_claim_names_the_platform_cluster_is_refused(host: str) -> None:
    assert platform_refusal(host, PLATFORM, PLATFORM_CLAIM) is not None


@pytest.mark.parametrize("host", CUSTOM_HOSTS)
def test_a_custom_or_instance_endpoint_of_another_cluster_in_the_platforms_account_is_not_refused(host: str) -> None:
    assert platform_refusal(host, PLATFORM, OTHER_CLAIM) is None


@pytest.mark.parametrize(
    "host",
    [
        "customer-graph.cluster-def456uvw.us-west-2.neptune.amazonaws.com",
        "customer-graph.cluster-ro-def456uvw.us-west-2.neptune.amazonaws.com",
        "customer-graph.cluster-abc123xyz.us-east-1.neptune.amazonaws.com",  # same hash, another region
        "customer-instance-1.def456uvw.us-west-2.neptune.amazonaws.com",
    ],
)
def test_another_accounts_or_regions_cluster_is_not_refused(host: str) -> None:
    assert platform_refusal(host, PLATFORM, OTHER_CLAIM) is None


def test_an_unnamed_platform_refuses_every_source() -> None:
    host = "customer-graph.cluster-def456uvw.us-west-2.neptune.amazonaws.com"
    assert platform_refusal(host, " ", OTHER_CLAIM) is not None


@pytest.mark.parametrize("host", CUSTOM_HOSTS)
def test_a_caller_that_reads_no_claim_still_refuses_every_custom_or_instance_endpoint(host: str) -> None:
    assert is_platform_endpoint(endpoint_host(host), PLATFORM)


@pytest.mark.parametrize("host", CUSTOM_HOSTS)
def test_a_source_with_no_noted_claim_still_has_its_custom_or_instance_endpoint_refused(host: str) -> None:
    assert platform_refusal(host, PLATFORM, None) is not None
    assert is_platform_endpoint(endpoint_host(host), PLATFORM, None)
