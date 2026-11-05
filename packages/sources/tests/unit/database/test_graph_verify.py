# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the RDF_SPARQL graph-verify Lambda."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import coa_sources.database.graph_verify.handler as gv
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from coa_common.dao.base import PaginatedResult
from coa_common.dao.dynamodb import DynamoDBDAO
from coa_common.sparql_config import GRAPH_SOURCES_ENV, SparqlStore, parse_sparql_configuration
from moto import mock_aws

from tests.unit.conftest import dao_double

pytestmark = pytest.mark.unit

MODULE = "coa_sources.database.graph_verify.handler"
_NS = "ns-1"
_SRC = "src-graph-1"
_SK = "2026-10-08T10:00:00.000000Z"
_HOST = "db.cluster-abc.us-east-1.neptune.amazonaws.com"
_READER = "db.cluster-ro-abc.us-east-1.neptune.amazonaws.com"
_CLUSTER = "cluster-RES1"
# The real lister, before the autouse fixture below replaces it for the rest of the module.
_ACCOUNT_CLUSTER_ENDPOINTS = gv._account_cluster_endpoints


def _graph_item(**config):
    return {
        "PK": f"NS#{_NS}",
        "SK": f"SRC#{_SRC}",
        "sourceSubType": "RDF_SPARQL",
        "status": "REGISTERED",
        "configuration": json.dumps({"store": "neptune", "dataEndpoint": _HOST, "region": "us-east-1", **config}),
    }


def _event(body):
    return {"Records": [{"messageId": "m-1", "body": json.dumps(body) if isinstance(body, dict) else body}]}


_MESSAGE = {"sourceId": _SRC, "namespaceId": _NS, "scanJobSK": _SK}


_PLATFORM_ID = "coa-platform"
_PLATFORM_HOSTS = frozenset(
    {
        "coa-platform.cluster-abc.us-east-1.neptune.amazonaws.com",
        "coa-platform.cluster-ro-abc.us-east-1.neptune.amazonaws.com",
        "pr.cluster-custom-abc.us-east-1.neptune.amazonaws.com",
    }
)
# The real platform lookup, before the autouse fixture below replaces it for the rest of the module.
_PLATFORM_CLUSTER_HOSTS = gv._platform_cluster_hosts


@pytest.fixture(autouse=True)
def _graph_sources_on(monkeypatch):
    monkeypatch.setenv(GRAPH_SOURCES_ENV, "true")


@pytest.fixture(autouse=True)
def _account_clusters():
    """The verify lists this account's clusters before it probes; the test host is one of them."""
    hosts = dict.fromkeys(_PLATFORM_HOSTS, "cluster-PLATFORM") | {_HOST: _CLUSTER}
    with patch(f"{MODULE}._account_cluster_endpoints", return_value=hosts) as described:
        yield described


@pytest.fixture(autouse=True)
def _platform_cluster():
    """The deployment names its platform cluster, a different cluster from the test host's."""
    with (
        patch(f"{MODULE}.PLATFORM_NEPTUNE_CLUSTER_ID", _PLATFORM_ID),
        patch(f"{MODULE}._platform_cluster_hosts", return_value=_PLATFORM_HOSTS) as described,
    ):
        yield described


@pytest.fixture
def daos():
    dao = dao_double()
    scan_dao = dao_double()
    scan_dao.update.return_value = True
    with patch(f"{MODULE}._get_dao", return_value=dao), patch(f"{MODULE}._get_scan_dao", return_value=scan_dao):
        yield dao, scan_dao


def _is_claim(key):
    return key["PK"].startswith("GRAPHCLUSTER#")


def _claim_writes(dao):
    """The conditional updates that took the cluster claim, as (row, call)."""
    return [({**c.args[0], **c.args[1]}, c) for c in dao.update.call_args_list if _is_claim(c.args[0])]


def _failing_claim(error):
    """A dao.update side effect that fails the claim write with ``error`` and lets every other update through."""

    def update(key, fields, **kwargs):
        if _is_claim(key):
            raise error
        return True

    return update


def _approval(dao):
    """The Update item of the transaction that recorded APPROVED with the claim, if any."""
    for call in reversed(dao.transact_write.call_args_list):
        for item in call.args[0]:
            if "Update" in item:
                return item["Update"]
    return None


def _source_fields(dao):
    """The verify outcome written onto the source: a failure by update, an approval in the claim's transaction."""
    for call in reversed(dao.update.call_args_list):
        if "status" in call.args[1]:
            return call.args[1]
    approval = _approval(dao)
    assert approval is not None, "no outcome was written"
    values = approval["ExpressionAttributeValues"]
    fields = {"status": values[":approved"], "queryable": values[":true"], "lastScanAt": values[":now"]}
    return fields | ({"lastScanJobId": values[":job"]} if ":job" in values else {})


class TestVerifyOutcome:
    def test_reachable_source_is_approved_and_queryable(self, daos):
        dao, scan_dao = daos
        dao.get.return_value = _graph_item()
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.handler(_event(_MESSAGE), None)
        fields = _source_fields(dao)
        assert fields["status"] == "APPROVED"
        assert fields["queryable"] is True
        approval = _approval(dao)
        assert approval["UpdateExpression"].endswith("REMOVE errorMessage")
        # Only the verify the source is waiting for may record its result (a stale or duplicate one may not).
        assert approval["ConditionExpression"] == (
            "#st = :registered AND (attribute_not_exists(lastScanJobId) OR lastScanJobId < :job)"
        )
        # The console's last-scan field and failure banner follow lastScanJobId.
        assert fields["lastScanJobId"] == _SK
        assert fields["lastScanAt"]
        job = scan_dao.update.call_args
        assert job.args[0] == {"PK": f"SRC#{_SRC}", "SK": _SK}
        assert job.args[1]["status"] == "COMPLETED"

    def test_unreachable_source_is_scan_failed_with_reason(self, daos):
        dao, scan_dao = daos
        dao.get.return_value = _graph_item()
        probe = MagicMock(side_effect=gv.ProbeFailedError("Endpoint unreachable"))
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}):
            gv.handler(_event(_MESSAGE), None)
        fields = _source_fields(dao)
        assert fields["status"] == "SCAN_FAILED"
        assert fields["queryable"] is False
        assert fields["errorMessage"] == "Endpoint unreachable"
        assert scan_dao.update.call_args.args[1]["status"] == "FAILED"
        assert scan_dao.update.call_args.args[1]["errorMessage"] == "Endpoint unreachable"

    def test_probe_is_selected_by_configured_store(self, daos):
        dao, _ = daos
        dao.get.return_value = _graph_item()
        neptune_probe = MagicMock()
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: neptune_probe}):
            gv.handler(_event(_MESSAGE), None)
        (config,) = neptune_probe.call_args.args
        assert config.store is SparqlStore.NEPTUNE
        assert config.host == _HOST

    def test_store_without_a_probe_fails_with_reason(self, daos):
        dao, _ = daos
        dao.get.return_value = _graph_item()
        with patch.dict(gv.PROBES, {}, clear=True):
            gv.handler(_event(_MESSAGE), None)
        fields = _source_fields(dao)
        assert fields["status"] == "SCAN_FAILED"
        assert "No reachability check exists for store 'neptune'" in fields["errorMessage"]

    def test_invalid_stored_configuration_fails_with_reason(self, daos):
        dao, _ = daos
        item = _graph_item()
        item["configuration"] = json.dumps({"store": "neptune"})
        dao.get.return_value = item
        gv.handler(_event(_MESSAGE), None)
        fields = _source_fields(dao)
        assert fields["status"] == "SCAN_FAILED"
        assert fields["errorMessage"].startswith("Invalid source configuration")

    def test_error_message_is_truncated(self, daos):
        dao, _ = daos
        dao.get.return_value = _graph_item()
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock(side_effect=gv.ProbeFailedError("x" * 5000))}):
            gv.handler(_event(_MESSAGE), None)
        assert len(_source_fields(dao)["errorMessage"]) == 1000


class TestSkippedMessages:
    def test_malformed_message_is_dropped(self, daos):
        dao, _ = daos
        gv.handler(_event("not json"), None)
        gv.handler(_event({"sourceId": _SRC}), None)
        dao.get.assert_not_called()

    def test_deleted_source_is_a_no_op(self, daos):
        dao, scan_dao = daos
        dao.get.return_value = None
        gv.handler(_event(_MESSAGE), None)
        dao.update.assert_not_called()
        scan_dao.update.assert_not_called()

    def test_non_graph_source_is_not_touched(self, daos):
        dao, _ = daos
        item = _graph_item()
        item["sourceSubType"] = "JDBC_DATABASE"
        dao.get.return_value = item
        gv.handler(_event(_MESSAGE), None)
        dao.update.assert_not_called()

    def test_source_deleted_during_probe_is_not_resurrected(self, daos):
        dao, _ = daos
        dao.get.return_value = _graph_item()
        dao.update.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.handler(_event(_MESSAGE), None)  # no raise

    def test_transient_ddb_error_is_raised_for_sqs_retry(self, daos):
        dao, _ = daos
        dao.get.side_effect = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")
        with pytest.raises(ClientError):
            gv.handler(_event(_MESSAGE), None)

    def test_missing_scan_job_sk_skips_history_update(self, daos):
        dao, scan_dao = daos
        dao.get.return_value = _graph_item()
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.handler(_event({"sourceId": _SRC, "namespaceId": _NS}), None)
        assert _source_fields(dao)["status"] == "APPROVED"
        assert "lastScanJobId" not in _source_fields(dao)
        scan_dao.update.assert_not_called()


class TestGraphSourcesOff:
    """A verify still queued when the deployment turned graph sources off changes nothing."""

    @pytest.fixture(autouse=True)
    def _graph_sources_off(self, monkeypatch):
        monkeypatch.delenv(GRAPH_SOURCES_ENV)

    def test_a_queued_verify_probes_claims_and_approves_nothing(self, daos):
        dao, scan_dao = daos
        dao.get.return_value = _graph_item()
        probe = MagicMock()
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}), patch(f"{MODULE}.logger") as logger:
            gv.handler(_event(_MESSAGE), None)
        probe.assert_not_called()
        dao.update.assert_not_called()
        dao.put.assert_not_called()
        dao.transact_write.assert_not_called()
        scan_dao.update.assert_not_called()
        assert logger.info.call_args.args[0] == "graph_verify_skipped_graph_sources_disabled"

    def test_with_graph_sources_on_the_same_verify_approves(self, daos, monkeypatch):
        monkeypatch.setenv(GRAPH_SOURCES_ENV, "on")
        dao, _ = daos
        dao.get.return_value = _graph_item()
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.handler(_event(_MESSAGE), None)
        assert _source_fields(dao)["status"] == "APPROVED"


class TestProbeNeptune:
    def _config(self, **overrides):
        return parse_sparql_configuration({"dataEndpoint": _HOST, **overrides})

    def test_calls_get_engine_status_signed_for_the_source_region(self):
        client = MagicMock()
        eu = "db.cluster-abc.eu-west-1.neptune.amazonaws.com"
        with patch(f"{MODULE}.boto3.client", return_value=client) as factory:
            gv.probe_neptune(self._config(dataEndpoint=eu, region="eu-west-1", port=8182))
        assert factory.call_args.args == ("neptunedata",)
        assert factory.call_args.kwargs["endpoint_url"] == f"https://{eu}:8182"
        assert factory.call_args.kwargs["region_name"] == "eu-west-1"
        client.get_engine_status.assert_called_once_with()

    def test_the_probe_is_one_attempt_within_its_timeout(self):
        # botocore's max_attempts counts retries, so max_attempts=1 was two attempts and twice the probe timeout.
        with patch(f"{MODULE}.boto3.client", return_value=MagicMock()) as factory:
            gv.probe_neptune(self._config())
        config = factory.call_args.kwargs["config"]
        assert config.retries == {"mode": "standard", "total_max_attempts": 1}
        assert (config.connect_timeout, config.read_timeout) == (gv.PROBE_TIMEOUT_S, gv.PROBE_TIMEOUT_S)

    def test_falls_back_to_the_lambda_region(self):
        with patch(f"{MODULE}.boto3.client", return_value=MagicMock()) as factory:
            gv.probe_neptune(self._config())
        assert factory.call_args.kwargs["region_name"] == gv.AWS_REGION

    def test_access_denied_names_the_missing_permission(self):
        client = MagicMock()
        client.get_engine_status.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "GetEngineStatus"
        )
        with patch(f"{MODULE}.boto3.client", return_value=client), pytest.raises(gv.ProbeFailedError) as exc:
            gv.probe_neptune(self._config())
        assert "neptune-db:GetEngineStatus" in str(exc.value)

    def test_connection_error_is_reported_as_unreachable(self):
        client = MagicMock()
        client.get_engine_status.side_effect = EndpointConnectionError(endpoint_url=f"https://{_HOST}:8182")
        with patch(f"{MODULE}.boto3.client", return_value=client), pytest.raises(gv.ProbeFailedError) as exc:
            gv.probe_neptune(self._config())
        assert "unreachable" in str(exc.value)


def test_every_store_has_a_probe():
    """A store the API accepts but this Lambda cannot probe would fail every verify."""
    assert set(gv.PROBES) == set(SparqlStore)


class TestAccountConfinement:
    """The probe is sent only to a cluster endpoint of this account, in this Lambda's region."""

    def _run(self, daos, item, account_hosts):
        dao, _ = daos
        dao.get.return_value = item
        probe = MagicMock()
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}),
            patch(
                f"{MODULE}._account_cluster_endpoints", return_value=dict.fromkeys(account_hosts, _CLUSTER)
            ) as described,
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
        ):
            gv.handler(_event(_MESSAGE), None)
        return _source_fields(dao), probe, described

    def test_a_cluster_endpoint_of_this_account_is_probed(self, daos):
        fields, probe, described = self._run(daos, _graph_item(), {_HOST})
        assert fields["status"] == "APPROVED"
        probe.assert_called_once()
        described.assert_called_once_with("us-east-1")

    def test_a_cluster_endpoint_of_another_account_is_refused_before_any_request(self, daos):
        other = "db.cluster-zzz999.us-east-1.neptune.amazonaws.com"
        fields, probe, _ = self._run(daos, _graph_item(dataEndpoint=other), {_HOST})
        assert fields["status"] == "SCAN_FAILED"
        assert "not a Neptune cluster endpoint of this account" in fields["errorMessage"]
        probe.assert_not_called()

    def test_a_stored_row_with_an_outside_host_is_refused_before_any_request(self, daos):
        fields, probe, described = self._run(daos, _graph_item(dataEndpoint="169.254.169.254"), {_HOST})
        assert fields["status"] == "SCAN_FAILED"
        assert fields["errorMessage"].startswith("Invalid source configuration")
        probe.assert_not_called()
        described.assert_not_called()

    def test_a_source_in_another_region_is_refused_before_any_request(self, daos):
        west = "db.cluster-abc.us-west-2.neptune.amazonaws.com"
        fields, probe, described = self._run(daos, _graph_item(dataEndpoint=west, region="us-west-2"), {west})
        assert fields["status"] == "SCAN_FAILED"
        assert "us-east-1" in fields["errorMessage"]
        probe.assert_not_called()
        described.assert_not_called()

    def test_endpoints_are_read_from_describe_db_clusters(self):
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {
                "DBClusters": [
                    {
                        "Engine": "neptune",
                        "Endpoint": _HOST,
                        "ReaderEndpoint": "db.cluster-ro-abc.us-east-1.neptune.amazonaws.com",
                        "CustomEndpoints": ["r.cluster-custom-abc.us-east-1.neptune.amazonaws.com"],
                    },
                    {"Engine": "aurora-postgresql", "Endpoint": "pg.cluster-x.us-east-1.rds.amazonaws.com"},
                ]
            }
        ]
        with patch(f"{MODULE}.boto3.client", return_value=client) as make:
            hosts = _ACCOUNT_CLUSTER_ENDPOINTS("us-east-1")
        assert make.call_args.args[0] == "neptune"
        client.get_paginator.assert_any_call("describe_db_clusters")
        assert set(hosts) == {
            _HOST,
            "db.cluster-ro-abc.us-east-1.neptune.amazonaws.com",
            "r.cluster-custom-abc.us-east-1.neptune.amazonaws.com",
        }


class TestIamDatabaseAuthentication:
    """Serve signs every query with IAM, so verify refuses a cluster without IAM database authentication."""

    def test_a_cluster_without_iam_authentication_is_refused_before_any_request(self, daos):
        dao, _ = daos
        dao.get.return_value = _graph_item()
        probe = MagicMock()
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}),
            patch(
                f"{MODULE}._account_cluster_endpoints",
                return_value=gv.AccountClusterHosts({_HOST: _CLUSTER}, iam_auth_disabled={_CLUSTER}),
            ),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
        ):
            gv.handler(_event(_MESSAGE), None)
        fields = _source_fields(dao)
        assert fields["status"] == "SCAN_FAILED"
        assert "IAM database authentication" in fields["errorMessage"]
        probe.assert_not_called()
        assert _claim_writes(dao) == []

    def test_the_lister_reads_iam_authentication_from_describe_db_clusters(self):
        client = _paging_client(
            {
                "describe_db_clusters": [
                    {
                        "DBClusters": [
                            {
                                "DBClusterIdentifier": "iam",
                                "Engine": "neptune",
                                "Endpoint": _HOST,
                                "IAMDatabaseAuthenticationEnabled": True,
                            },
                            {
                                "DBClusterIdentifier": "open",
                                "Engine": "neptune",
                                "Endpoint": "open.cluster-abc.us-east-1.neptune.amazonaws.com",
                                "IAMDatabaseAuthenticationEnabled": False,
                            },
                        ]
                    }
                ]
            }
        )
        with patch(f"{MODULE}.boto3.client", return_value=client):
            hosts = _ACCOUNT_CLUSTER_ENDPOINTS("us-east-1")
        assert hosts == {_HOST: "iam", "open.cluster-abc.us-east-1.neptune.amazonaws.com": "open"}
        assert hosts.iam_auth_disabled == frozenset({"open"})


def _describe_error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "DescribeDBClusters")


_TRANSIENT = [_describe_error("Throttling"), EndpointConnectionError(endpoint_url="https://rds.us-east-1")]


class TestClusterLookupErrors:
    """a transient DescribeDBClusters failure is retried by SQS; any other is recorded with its error code."""

    def _run(self, daos, lister, error):
        dao, scan_dao = daos
        dao.get.return_value = _graph_item()
        probe = MagicMock()
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}),
            patch(f"{MODULE}.{lister}", side_effect=error),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
            patch.object(gv, "logger") as log,
        ):
            gv.handler(_event(_MESSAGE), None)
        return _source_fields(dao), probe, log

    @pytest.mark.parametrize("lister", ["_account_cluster_endpoints", "_platform_cluster_hosts"])
    @pytest.mark.parametrize("error", _TRANSIENT, ids=["throttled", "unreachable"])
    def test_a_transient_lookup_error_is_raised_for_sqs_retry(self, daos, lister, error):
        dao, scan_dao = daos
        with pytest.raises(type(error)):
            self._run(daos, lister, error)
        dao.update.assert_not_called()
        scan_dao.update.assert_not_called()

    @pytest.mark.parametrize("lister", ["_account_cluster_endpoints", "_platform_cluster_hosts"])
    def test_on_the_last_receive_a_transient_lookup_error_is_recorded_not_raised(self, daos, lister):
        """The DLQ would leave the source REGISTERED, which delete and re-scan both refuse."""
        dao, _ = daos
        dao.get.return_value = _graph_item()
        event = _event(_MESSAGE)
        event["Records"][0]["attributes"] = {"ApproximateReceiveCount": "3"}
        with (
            patch(f"{MODULE}.{lister}", side_effect=_describe_error("Throttling")),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
        ):
            gv.handler(event, None)
        fields = _source_fields(dao)
        assert fields["status"] == "SCAN_FAILED"
        assert "Throttling" in fields["errorMessage"]

    @pytest.mark.parametrize("lister", ["_account_cluster_endpoints", "_platform_cluster_hosts"])
    def test_a_refused_lookup_records_its_error_code(self, daos, lister):
        fields, probe, log = self._run(daos, lister, _describe_error("AccessDenied"))
        assert fields["status"] == "SCAN_FAILED"
        assert "AccessDenied" in fields["errorMessage"]
        assert log.error.call_args.kwargs["error_code"] == "AccessDenied"
        probe.assert_not_called()


class TestLastReceive:
    """On the last receive SQS will not redeliver, so any failure is recorded rather than left REGISTERED."""

    def _run(self, daos, receive_count):
        dao, scan_dao = daos
        dao.get.return_value = _graph_item()
        dao.update.side_effect = _failing_claim(
            ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "m"}}, "UpdateItem")
        )
        event = _event(_MESSAGE)
        event["Records"][0]["attributes"] = {"ApproximateReceiveCount": receive_count}
        probe = MagicMock()
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
        ):
            gv.handler(event, None)
        return dao, scan_dao, probe

    def test_any_error_on_the_last_receive_is_recorded_with_its_code(self, daos):
        dao, scan_dao, probe = self._run(daos, "3")
        fields = _source_fields(dao)
        assert fields["status"] == "SCAN_FAILED"
        assert fields["queryable"] is False
        assert "ProvisionedThroughputExceededException" in fields["errorMessage"]
        assert scan_dao.update.call_args.args[1]["status"] == "FAILED"
        probe.assert_not_called()

    def test_the_same_error_before_the_last_receive_is_raised_for_sqs_retry(self, daos):
        with pytest.raises(ClientError):
            self._run(daos, "2")
        assert not [c for c in daos[0].update.call_args_list if "status" in c.args[1]]


_CUSTOM = "r.cluster-custom-abc.us-east-1.neptune.amazonaws.com"


def _paging_client(pages):
    """A neptune client whose paginator serves ``pages[operation]``."""
    client = MagicMock()
    client.get_paginator.side_effect = lambda op: MagicMock(paginate=MagicMock(return_value=pages.get(op, [])))
    return client


class TestCustomEndpoints:
    """Neptune's DescribeDBClusters carries no custom endpoints; DescribeDBClusterEndpoints lists them."""

    _CLUSTERS = [
        {
            "DBClusters": [
                {"DBClusterIdentifier": "db", "Engine": "neptune", "Endpoint": _HOST},
                {"DBClusterIdentifier": "pg", "Engine": "aurora-postgresql", "Endpoint": "pg.cluster-x.rds"},
            ]
        }
    ]

    def test_a_listed_custom_endpoint_of_a_neptune_cluster_is_approved(self, daos):
        client = _paging_client(
            {
                "describe_db_clusters": self._CLUSTERS,
                "describe_db_cluster_endpoints": [
                    {
                        "DBClusterEndpoints": [
                            {"DBClusterIdentifier": "db", "EndpointType": "CUSTOM", "Endpoint": _CUSTOM}
                        ]
                    }
                ],
            }
        )
        dao, _ = daos
        dao.get.return_value = _graph_item(dataEndpoint=_CUSTOM)
        probe = MagicMock()
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}),
            patch(f"{MODULE}._account_cluster_endpoints", _ACCOUNT_CLUSTER_ENDPOINTS),
            patch(f"{MODULE}.boto3.client", return_value=client),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
        ):
            gv.handler(_event(_MESSAGE), None)
        assert _source_fields(dao)["status"] == "APPROVED"
        probe.assert_called_once()

    def test_a_custom_endpoint_of_another_engines_cluster_is_not_listed(self):
        other = "o.cluster-custom-x.us-east-1.rds.amazonaws.com"
        client = _paging_client(
            {
                "describe_db_clusters": self._CLUSTERS,
                "describe_db_cluster_endpoints": [
                    {"DBClusterEndpoints": [{"DBClusterIdentifier": "pg", "EndpointType": "CUSTOM", "Endpoint": other}]}
                ],
            }
        )
        with patch(f"{MODULE}.boto3.client", return_value=client):
            assert other not in _ACCOUNT_CLUSTER_ENDPOINTS("us-east-1")

    def test_the_platform_clusters_custom_endpoints_are_refused(self):
        client = _paging_client(
            {
                "describe_db_cluster_endpoints": [
                    {"DBClusterEndpoints": [{"DBClusterIdentifier": _PLATFORM_ID, "Endpoint": "PR.cluster-custom-p.x"}]}
                ]
            }
        )
        client.describe_db_clusters.return_value = {
            "DBClusters": [{"DBClusterIdentifier": _PLATFORM_ID, "Endpoint": "coa-platform.cluster-p.x"}]
        }
        with (
            patch(f"{MODULE}.boto3.client", return_value=client),
            patch(f"{MODULE}.PLATFORM_NEPTUNE_CLUSTER_ID", _PLATFORM_ID),
        ):
            hosts = _PLATFORM_CLUSTER_HOSTS("us-east-1")
        assert "pr.cluster-custom-p.x" in hosts
        client.get_paginator.assert_called_once_with("describe_db_cluster_endpoints")


class TestClusterClaim:
    """A Neptune cluster serves one namespace: verify claims it, keyed on the cluster rather than the host."""

    def _run(self, daos, host):
        dao, _ = daos
        dao.get.return_value = _graph_item(dataEndpoint=host)
        probe = MagicMock()
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}),
            patch(f"{MODULE}._account_cluster_endpoints", return_value={_HOST: _CLUSTER, _READER: _CLUSTER}),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
        ):
            gv.handler(_event(_MESSAGE), None)
        return dao, probe

    def test_the_writer_and_reader_endpoints_claim_the_same_cluster(self, daos):
        claims = []
        for host in (_HOST, _READER):
            dao, probe = self._run(daos, host)
            # The claim is taken before the probe, and again in the write that records APPROVED.
            taken = [row for row, _ in _claim_writes(dao)]
            retaken = [i["Put"]["Item"] for c in dao.transact_write.call_args_list for i in c.args[0] if "Put" in i]
            assert len(taken) == 1 and [{**c, "approvalToken": None} for c in retaken] == [
                {**taken[0], "approvalToken": None}
            ]
            assert retaken[0]["approvalToken"]
            claims.append(taken[0])
            assert _source_fields(dao)["status"] == "APPROVED"
            noted = [c.args[1] for c in dao.update.call_args_list if "graphClusterClaim" in c.args[1]]
            assert noted and noted[-1]["graphClusterClaim"] == taken[0]["PK"]
            dao.update.reset_mock()
            dao.transact_write.reset_mock()
        assert claims[0]["PK"] == claims[1]["PK"] == f"GRAPHCLUSTER#us-east-1#{_CLUSTER}"
        assert claims[0]["namespaceId"] == _NS

    def test_the_claim_is_conditional_and_stays_out_of_the_namespace_listing(self, daos):
        dao, _ = self._run(daos, _HOST)
        ((_, call),) = _claim_writes(dao)
        assert call.kwargs["auto_timestamp"] is False
        assert call.kwargs["condition"] == "attribute_not_exists(PK) OR namespaceId = :ns"
        assert call.kwargs["condition_values"] == {":ns": _NS}

    def test_a_re_created_cluster_keeps_the_claim_of_its_identifier(self):
        """Hosts derive from the cluster identifier, so a cluster deleted and re-created under it is the same claim."""
        client = _paging_client(
            {
                "describe_db_clusters": [
                    {
                        "DBClusters": [
                            {
                                "DBClusterIdentifier": "db",
                                "DbClusterResourceId": "cluster-R2",
                                "Engine": "neptune",
                                "Endpoint": _HOST,
                                "ReaderEndpoint": _READER,
                            }
                        ]
                    }
                ]
            }
        )
        with patch(f"{MODULE}.boto3.client", return_value=client):
            assert _ACCOUNT_CLUSTER_ENDPOINTS("us-east-1") == {_HOST: "db", _READER: "db"}

    def test_a_cluster_claimed_by_another_namespace_is_refused_before_any_request(self, daos):
        dao, _ = daos
        dao.update.side_effect = _failing_claim(
            ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        )
        dao, probe = self._run(daos, _READER)
        fields = _source_fields(dao)
        assert fields["status"] == "SCAN_FAILED"
        assert "registered to another namespace" in fields["errorMessage"]
        probe.assert_not_called()


class TestClaimReleaseOnFailedVerify:
    """A namespace whose own probe fails after claiming does not keep the cluster from every other namespace."""

    _CLAIM_PK = f"GRAPHCLUSTER#us-east-1#{_CLUSTER}"

    def _run(self, daos, others, pages=None):
        dao, _ = daos
        dao.get.return_value = _graph_item()
        dao.query.return_value = PaginatedResult(items=others)
        if pages is not None:
            dao.query.side_effect = pages
        probe = MagicMock(side_effect=gv.ProbeFailedError("Endpoint unreachable"))
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
            patch(f"{MODULE}.SOURCES_TABLE", "sources"),
            patch.object(gv, "logger") as log,
        ):
            gv.handler(_event(_MESSAGE), None)
        return dao, log

    def _released(self, dao):
        return [
            item["Delete"]
            for call in dao.transact_write.call_args_list
            for item in call.args[0]
            if "Delete" in item and item["Delete"]["Key"] == {"PK": self._CLAIM_PK, "SK": "CLAIM"}
        ]

    def test_the_claim_is_released_conditional_on_the_namespace(self, daos):
        dao, _ = self._run(daos, [])
        assert _source_fields(dao)["status"] == "SCAN_FAILED"
        (delete,) = self._released(dao)
        assert delete["TableName"] == "sources"
        # No approval has written a token on the claim since it was read.
        assert delete["ConditionExpression"] == "namespaceId = :ns AND attribute_not_exists(approvalToken)"
        assert delete["ExpressionAttributeValues"] == {":ns": _NS}

    def test_another_approved_source_on_the_cluster_keeps_the_claim(self, daos):
        other = {"sourceId": "src-2", "status": "APPROVED", "graphClusterClaim": self._CLAIM_PK}
        dao, _ = self._run(daos, [other])
        assert self._released(dao) == []

    def test_a_source_on_a_later_page_that_uses_the_claim_keeps_it(self, daos):
        other = {"sourceId": "src-2", "status": "APPROVED", "graphClusterClaim": self._CLAIM_PK}
        first = PaginatedResult(items=[], last_evaluated_key={"PK": f"NS#{_NS}", "SK": "SRC#a"})
        dao, _ = self._run(daos, [], pages=[first, PaginatedResult(items=[other])])
        assert self._released(dao) == []

    def test_a_listing_cut_short_at_the_page_bound_keeps_the_claim(self, daos):
        endless = PaginatedResult(items=[], last_evaluated_key={"PK": f"NS#{_NS}", "SK": "SRC#a"})
        dao, _ = self._run(daos, [], pages=lambda *_args, **_kwargs: endless)
        assert self._released(dao) == []
        assert dao.query.call_count == gv._MAX_SOURCE_PAGES

    def test_a_failed_source_noting_the_claim_does_not_keep_it(self, daos):
        other = {"sourceId": "src-2", "status": "SCAN_FAILED", "graphClusterClaim": self._CLAIM_PK}
        dao, _ = self._run(daos, [other])
        assert len(self._released(dao)) == 1

    def test_the_sources_are_read_consistently_before_a_release(self, daos):
        """A lagging replica could miss a sibling's claim note, and the release would delete a claim it relies on."""
        dao, _ = self._run(daos, [])
        assert dao.query.call_args_list
        assert all(c.args[0].consistent_read for c in dao.query.call_args_list)

    def test_the_claim_is_read_consistently_before_a_release(self, daos):
        """A stale read of the approval token would cancel a release that should happen, with nothing logged."""
        dao, _ = self._run(daos, [])
        claim_reads = [c for c in dao.get.call_args_list if c.args[0]["PK"] == self._CLAIM_PK]
        assert claim_reads and all(c.kwargs.get("consistent_read") is True for c in claim_reads)

    def test_a_release_cancelled_by_its_conditions_is_logged(self, daos):
        dao, _ = daos
        dao.transact_write.side_effect = ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
            },
            "TransactWriteItems",
        )
        _, log = self._run(daos, [])
        assert "graph_cluster_claim_kept" in [c.args[0] for c in log.info.call_args_list]
        assert "graph_cluster_claim_release_failed" not in [c.args[0] for c in log.warning.call_args_list]

    def test_a_failed_release_is_logged_and_the_failure_still_recorded(self, daos):
        dao, _ = daos
        dao.transact_write.side_effect = ClientError({"Error": {"Code": "TransactionCanceledException"}}, "Tx")
        dao, log = self._run(daos, [])
        assert _source_fields(dao)["status"] == "SCAN_FAILED"
        assert "graph_cluster_claim_release_failed" in [c.args[0] for c in log.warning.call_args_list]

    def test_an_approved_source_keeps_its_claim(self, daos):
        dao, _ = daos
        dao.get.return_value = _graph_item()
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
        ):
            gv.handler(_event(_MESSAGE), None)
        assert _source_fields(dao)["status"] == "APPROVED"
        writes = [i for c in dao.transact_write.call_args_list for i in c.args[0]]
        assert [k for i in writes for k in i] == ["Put", "Update"]


_CONFLICT = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
_THROTTLED = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "m"}}, "UpdateItem")


class _ClaimRow:
    """One cluster-claim row whose conditional update behaves as DynamoDB's does; other updates succeed.

    ``note_error`` fails the first claim-note update on the source, as a throttle would.
    """

    def __init__(self, namespace_id, note_error=None):
        self.namespace_id = namespace_id
        self.note_error = note_error

    def update(self, key, fields, *, condition=None, condition_values=None, **_):
        if _is_claim(key):
            assert condition == "attribute_not_exists(PK) OR namespaceId = :ns"
            if self.namespace_id not in (None, condition_values[":ns"]):
                raise _CONFLICT
            self.namespace_id = fields["namespaceId"]
            return True
        if self.note_error is not None and gv.GRAPH_CLUSTER_CLAIM_ATTR in fields:
            error, self.note_error = self.note_error, None
            raise error
        return True


class TestAFailedVerifyKeepsAServingSourcesClaim:
    """A failed verify of a source that was serving keeps the claim; any other failed verify releases it.

    A re-verify keeps ``queryable``, so a namespace's only APPROVED source that times out once (a failover) does
    not free the cluster for another namespace to register. Every failure after the claim releases it otherwise.
    """

    _CLAIM_PK = f"GRAPHCLUSTER#us-east-1#{_CLUSTER}"

    def _run(self, daos, *, probe, receive_count="1", serving=False, note_error=None):
        dao, _ = daos
        dao.get.return_value = {**_graph_item(), "queryable": serving, "graphClusterClaim": self._CLAIM_PK}
        dao.query.return_value = PaginatedResult(items=[])
        dao.update.side_effect = _ClaimRow(_NS if serving else None, note_error).update
        event = _event(_MESSAGE)
        event["Records"][0]["attributes"] = {"ApproximateReceiveCount": receive_count}
        with (
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}),
            patch(f"{MODULE}.AWS_REGION", "us-east-1"),
            patch(f"{MODULE}.SOURCES_TABLE", "sources"),
        ):
            gv.handler(event, None)
        return dao

    def _released(self, dao):
        return [
            item["Delete"]
            for call in dao.transact_write.call_args_list
            for item in call.args[0]
            if "Delete" in item and item["Delete"]["Key"] == {"PK": self._CLAIM_PK, "SK": "CLAIM"}
        ]

    def test_a_re_verify_whose_probe_fails_keeps_the_serving_source_s_claim(self, daos):
        probe = MagicMock(side_effect=gv.ProbeFailedError("Endpoint unreachable"))
        dao = self._run(daos, probe=probe, serving=True)
        assert _source_fields(dao)["status"] == "SCAN_FAILED"
        assert self._released(dao) == []

    def test_an_unexpected_probe_error_on_the_last_receive_releases_the_claim(self, daos):
        dao = self._run(daos, probe=MagicMock(side_effect=RuntimeError("boom")), receive_count="3")
        assert _source_fields(dao)["status"] == "SCAN_FAILED"
        assert len(self._released(dao)) == 1

    def test_an_unexpected_probe_error_on_the_last_receive_keeps_a_serving_source_s_claim(self, daos):
        probe = MagicMock(side_effect=RuntimeError("boom"))
        dao = self._run(daos, probe=probe, receive_count="3", serving=True)
        assert _source_fields(dao)["status"] == "SCAN_FAILED"
        assert self._released(dao) == []

    def test_a_throttled_claim_note_releases_the_claim_before_sqs_retries(self, daos):
        probe = MagicMock()
        with pytest.raises(ClientError):
            self._run(daos, probe=probe, note_error=_THROTTLED)
        assert len(self._released(daos[0])) == 1
        probe.assert_not_called()

    def test_a_throttled_claim_note_on_the_last_receive_releases_the_claim_and_records_the_failure(self, daos):
        dao = self._run(daos, probe=MagicMock(), receive_count="3", note_error=_THROTTLED)
        assert len(self._released(dao)) == 1
        assert "ProvisionedThroughputExceededException" in _source_fields(dao)["errorMessage"]


class TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace:
    """On a real conditional table: one failed re-verify does not hand the namespace's cluster to another namespace.

    B's only source is APPROVED and holds the claim. Its re-verify fails once (a timeout), then A registers B's
    cluster. A must be refused, and B's next verify approved.
    """

    _CLAIM = {"PK": f"GRAPHCLUSTER#us-east-1#{_CLUSTER}", "SK": "CLAIM"}

    @pytest.fixture
    def table(self):
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            keys = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]
            attributes = [{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")]
            ddb.create_table(
                TableName="sources", KeySchema=keys, AttributeDefinitions=attributes, BillingMode="PAY_PER_REQUEST"
            )
            dao = DynamoDBDAO("sources", region="us-east-1")
            with (
                patch(f"{MODULE}._get_dao", return_value=dao),
                patch(f"{MODULE}._get_scan_dao", return_value=None),
                patch(f"{MODULE}.SOURCES_TABLE", "sources"),
                patch(f"{MODULE}.AWS_REGION", "us-east-1"),
            ):
                yield ddb.Table("sources")

    def _register(self, table, namespace_id, source_id):
        item = {**_graph_item(), "PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}", "sourceId": source_id}
        table.put_item(Item={**item, "namespaceId": namespace_id})

    def _verify(self, table, namespace_id, source_id, probe):
        # Create and re-verify both hand the verify a REGISTERED row (the API's re-verify lock).
        table.update_item(
            Key={"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
            UpdateExpression="SET #st = :r",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":r": "REGISTERED"},
        )
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}):
            gv.verify_source(namespace_id, source_id, None)
        source = table.get_item(Key={"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"})["Item"]
        claim = table.get_item(Key=self._CLAIM).get("Item")
        return source["status"], claim and claim["namespaceId"]

    def test_one_failed_re_verify_does_not_let_another_namespace_take_the_cluster(self, table):
        self._register(table, "ns-B", "b1")
        assert self._verify(table, "ns-B", "b1", MagicMock()) == ("APPROVED", "ns-B")
        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        assert self._verify(table, "ns-B", "b1", timeout) == ("SCAN_FAILED", "ns-B")
        self._register(table, "ns-A", "a1")
        assert self._verify(table, "ns-A", "a1", MagicMock()) == ("SCAN_FAILED", "ns-B")
        assert self._verify(table, "ns-B", "b1", MagicMock()) == ("APPROVED", "ns-B")

    def test_a_first_verify_whose_probe_fails_frees_the_cluster(self, table):
        self._register(table, "ns-B", "b1")
        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        assert self._verify(table, "ns-B", "b1", timeout) == ("SCAN_FAILED", None)
        self._register(table, "ns-A", "a1")
        assert self._verify(table, "ns-A", "a1", MagicMock()) == ("APPROVED", "ns-A")

    def test_two_consecutive_failed_re_verifies_keep_the_cluster(self, table):
        """A source that was ever APPROVED keeps its cluster's claim until it is deleted, however often it fails."""
        self._register(table, "ns-B", "b1")
        assert self._verify(table, "ns-B", "b1", MagicMock()) == ("APPROVED", "ns-B")
        for _ in range(2):
            timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
            assert self._verify(table, "ns-B", "b1", timeout) == ("SCAN_FAILED", "ns-B")
        self._register(table, "ns-A", "a1")
        assert self._verify(table, "ns-A", "a1", MagicMock()) == ("SCAN_FAILED", "ns-B")

    def test_a_failed_sibling_that_was_approved_keeps_the_claim(self, table):
        self._register(table, "ns-B", "b2")
        assert self._verify(table, "ns-B", "b2", MagicMock()) == ("APPROVED", "ns-B")
        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        assert self._verify(table, "ns-B", "b2", timeout) == ("SCAN_FAILED", "ns-B")
        self._register(table, "ns-B", "b1")
        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        assert self._verify(table, "ns-B", "b1", timeout) == ("SCAN_FAILED", "ns-B")


class TestTheClaimFollowsTheSourcesThatUseIt:
    """Whether a failed verify frees the cluster depends on the namespace's sources, not on the attempt that claimed.

    The claim is kept while a source that was serving is re-verified, or another source of the namespace on the
    cluster is serving or still being verified. A retry after an attempt that took the claim and then stopped
    (a Lambda timeout) frees it as the first attempt would have.
    """

    _CLAIM = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace._CLAIM
    table = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace.table
    _register = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace._register
    _verify = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace._verify
    _timeout = staticmethod(lambda: MagicMock(side_effect=gv.ProbeFailedError("timeout")))

    def _claimed_by(self, table, namespace_id, source_id):
        table.put_item(Item={**self._CLAIM, "namespaceId": namespace_id})
        table.update_item(
            Key={"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
            UpdateExpression="SET graphClusterClaim = :c",
            ExpressionAttributeValues={":c": self._CLAIM["PK"]},
        )

    def test_a_retry_after_an_attempt_that_took_the_claim_and_stopped_frees_it(self, table):
        self._register(table, "ns-B", "b1")
        self._claimed_by(table, "ns-B", "b1")
        assert self._verify(table, "ns-B", "b1", self._timeout()) == ("SCAN_FAILED", None)

    def test_another_source_still_being_verified_keeps_the_claim(self, table):
        self._register(table, "ns-B", "b1")
        self._register(table, "ns-B", "b2")
        self._claimed_by(table, "ns-B", "b2")
        assert self._verify(table, "ns-B", "b1", self._timeout()) == ("SCAN_FAILED", "ns-B")

    def test_a_claim_released_during_the_probe_is_taken_again_before_the_source_is_approved(self, table):
        self._register(table, "ns-B", "b1")

        def duplicate_delivery_releases(_config):
            table.delete_item(Key=self._CLAIM)

        assert self._verify(table, "ns-B", "b1", MagicMock(side_effect=duplicate_delivery_releases)) == (
            "APPROVED",
            "ns-B",
        )

    def test_a_cluster_another_namespace_took_during_the_probe_is_not_approved(self, table):
        self._register(table, "ns-B", "b1")

        def taken(_config):
            table.put_item(Item={**self._CLAIM, "namespaceId": "ns-A"})

        assert self._verify(table, "ns-B", "b1", MagicMock(side_effect=taken)) == ("SCAN_FAILED", "ns-A")

    def test_a_source_deleted_before_its_claim_is_noted_frees_the_cluster(self, table):
        self._register(table, "ns-B", "b1")
        dao = gv._get_dao()
        real_update = dao.update

        def deleted_first(key, fields, **kwargs):
            if gv.GRAPH_CLUSTER_CLAIM_ATTR in fields:
                table.delete_item(Key=key)
            return real_update(key, fields, **kwargs)

        with (
            patch.object(dao, "update", side_effect=deleted_first),
            patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}),
        ):
            gv.verify_source("ns-B", "b1", None)
        assert table.get_item(Key=self._CLAIM).get("Item") is None


class TestNoSourceIsApprovedWithoutTheClaimAndNoClaimOutlivesItsSources:
    """Interleavings with a duplicate delivery and a delete, on a moto table."""

    _CLAIM = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace._CLAIM
    table = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace.table
    _register = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace._register
    _verify = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace._verify
    _claimed_by = TestTheClaimFollowsTheSourcesThatUseIt._claimed_by

    def _claim(self, table):
        item = table.get_item(Key=self._CLAIM).get("Item")
        return item and item["namespaceId"]

    def test_a_duplicate_delivery_that_fails_after_the_other_approved_keeps_the_claim(self, table):
        self._register(table, "ns-B", "b1")

        def other_delivery_approves_first(_config):
            with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
                gv.verify_source("ns-B", "b1", None)
            raise gv.ProbeFailedError("timeout")

        self._verify(table, "ns-B", "b1", MagicMock(side_effect=other_delivery_approves_first))
        assert self._claim(table) == "ns-B"

    def test_a_source_deleted_during_its_probe_leaves_no_claim(self, table):
        self._register(table, "ns-B", "b1")

        def deleted(_config):
            # The delete path removes the row, then releases the claim no other source notes.
            table.delete_item(Key={"PK": "NS#ns-B", "SK": "SRC#b1"})
            table.delete_item(Key=self._CLAIM)

        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock(side_effect=deleted)}):
            gv.verify_source("ns-B", "b1", None)
        assert self._claim(table) is None

    def test_a_serving_source_deleted_before_its_claim_is_noted_leaves_no_claim(self, table):
        self._register(table, "ns-B", "b1")
        table.update_item(
            Key={"PK": "NS#ns-B", "SK": "SRC#b1"},
            UpdateExpression="SET queryable = :t",
            ExpressionAttributeValues={":t": True},
        )
        dao = gv._get_dao()
        real_update = dao.update

        def deleted_first(key, fields, **kwargs):
            if gv.GRAPH_CLUSTER_CLAIM_ATTR in fields:
                table.delete_item(Key=key)
            return real_update(key, fields, **kwargs)

        probe = {SparqlStore.NEPTUNE: MagicMock()}
        with patch.object(dao, "update", side_effect=deleted_first), patch.dict(gv.PROBES, probe):
            gv.verify_source("ns-B", "b1", None)
        assert self._claim(table) is None

    @pytest.mark.parametrize("status", ["DELETING", "DELETE_FAILED"])
    def test_a_sibling_on_its_way_out_does_not_keep_the_claim(self, table, status):
        self._register(table, "ns-B", "b1")
        self._register(table, "ns-B", "b2")
        self._claimed_by(table, "ns-B", "b2")
        table.update_item(
            Key={"PK": "NS#ns-B", "SK": "SRC#b2"},
            UpdateExpression="SET #st = :s",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": status},
        )
        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        assert self._verify(table, "ns-B", "b1", timeout) == ("SCAN_FAILED", None)

    def test_a_sibling_approved_while_the_release_reads_the_sources_keeps_the_claim(self, table):
        self._register(table, "ns-B", "b1")
        self._register(table, "ns-B", "b2")
        dao = gv._get_dao()
        real_query = dao.query

        def sibling_approves_after_the_read(params):
            page = real_query(params)
            with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
                gv.verify_source("ns-B", "b2", None)
            return page

        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        with patch.object(dao, "query", side_effect=sibling_approves_after_the_read):
            self._verify(table, "ns-B", "b1", timeout)
        sibling = table.get_item(Key={"PK": "NS#ns-B", "SK": "SRC#b2"})["Item"]
        assert (sibling["status"], self._claim(table)) == ("APPROVED", "ns-B")

    def test_a_later_claim_does_not_erase_a_sibling_approval_a_release_must_see(self, table):
        """A verify's first claim write must keep the approval token, or a release that read none deletes the claim."""
        self._register(table, "ns-B", "b1")
        self._register(table, "ns-B", "b2")
        dao = gv._get_dao()
        real_query = dao.query

        def sibling_approves_then_another_verify_claims(params):
            page = real_query(params)
            with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
                gv.verify_source("ns-B", "b2", None)
            assert gv._claim_cluster("ns-B", self._CLAIM)
            return page

        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        with patch.object(dao, "query", side_effect=sibling_approves_then_another_verify_claims):
            self._verify(table, "ns-B", "b1", timeout)
        sibling = table.get_item(Key={"PK": "NS#ns-B", "SK": "SRC#b2"})["Item"]
        assert (sibling["status"], self._claim(table)) == ("APPROVED", "ns-B")


class TestAStaleVerifyRecordsNothing:
    """A verify may record its result only on the REGISTERED row it was sent for, never over a newer state."""

    table = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace.table
    _register = TestAFailedReVerifyKeepsTheClusterFromAnotherNamespace._register

    def _row(self, table):
        return table.get_item(Key={"PK": "NS#ns-B", "SK": "SRC#b1"})["Item"]

    def _set(self, table, **fields):
        table.update_item(
            Key={"PK": "NS#ns-B", "SK": "SRC#b1"},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ExpressionAttributeNames={f"#{k}": k for k in fields},
            ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
        )

    @pytest.mark.parametrize("status", ["DELETING", "DELETE_FAILED", "SCAN_FAILED"])
    def test_a_late_delivery_does_not_approve_a_source_that_moved_on(self, table, status):
        self._register(table, "ns-B", "b1")
        self._set(table, status=status, queryable=False)
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.verify_source("ns-B", "b1", "2026-11-02T10:00:00.000000Z")
        row = self._row(table)
        assert (row["status"], row["queryable"]) == (status, False)

    def test_a_late_failure_does_not_overwrite_a_newer_approval(self, table):
        self._register(table, "ns-B", "b1")
        self._set(table, status="APPROVED", queryable=True, lastScanJobId="2026-11-02T11:00:00.000000Z")
        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: timeout}):
            gv.verify_source("ns-B", "b1", "2026-11-02T10:00:00.000000Z")
        row = self._row(table)
        assert (row["status"], row["queryable"]) == ("APPROVED", True)

    def test_an_older_verify_does_not_record_over_a_newer_ones_result(self, table):
        # Verify 2 approved, re-verify 3 took the lock, and a duplicate of verify 1 arrives now.
        self._register(table, "ns-B", "b1")
        self._set(table, status="REGISTERED", queryable=True, lastScanJobId="2026-11-02T11:00:00.000000Z")
        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: timeout}):
            gv.verify_source("ns-B", "b1", "2026-11-02T10:00:00.000000Z")
        row = self._row(table)
        assert (row["status"], row["queryable"], row["lastScanJobId"]) == (
            "REGISTERED",
            True,
            "2026-11-02T11:00:00.000000Z",
        )

    def test_the_verify_the_source_waits_for_still_records_its_result(self, table):
        self._register(table, "ns-B", "b1")
        self._set(table, lastScanJobId="2026-11-02T09:00:00.000000Z")
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.verify_source("ns-B", "b1", "2026-11-02T10:00:00.000000Z")
        row = self._row(table)
        assert (row["status"], row["queryable"]) == ("APPROVED", True)


class TestADuplicateDeliveryLeavesTheScanJobAlone:
    """The same verify delivered twice: the second records nothing, and the job the first closed stays COMPLETED."""

    @pytest.fixture
    def tables(self):
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            keys = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]
            attributes = [{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")]
            for name in ("sources", "scan_jobs"):
                ddb.create_table(
                    TableName=name, KeySchema=keys, AttributeDefinitions=attributes, BillingMode="PAY_PER_REQUEST"
                )
            with (
                patch(f"{MODULE}._get_dao", return_value=DynamoDBDAO("sources", region="us-east-1")),
                patch(f"{MODULE}._get_scan_dao", return_value=DynamoDBDAO("scan_jobs", region="us-east-1")),
                patch(f"{MODULE}.SOURCES_TABLE", "sources"),
                patch(f"{MODULE}.AWS_REGION", "us-east-1"),
            ):
                yield ddb.Table("sources"), ddb.Table("scan_jobs")

    def test_the_second_delivery_does_not_mark_the_completed_job_failed(self, tables):
        sources, jobs = tables
        job = "2026-11-02T10:00:00.000000Z"
        item = {**_graph_item(), "PK": "NS#ns-B", "SK": "SRC#b1", "sourceId": "b1", "namespaceId": "ns-B"}
        sources.put_item(Item=item)
        jobs.put_item(Item={"PK": "SRC#b1", "SK": job, "status": "IN_PROGRESS"})
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.verify_source("ns-B", "b1", job)
            gv.verify_source("ns-B", "b1", job)
        row = sources.get_item(Key={"PK": "NS#ns-B", "SK": "SRC#b1"})["Item"]
        assert (row["status"], row["queryable"]) == ("APPROVED", True)
        assert jobs.get_item(Key={"PK": "SRC#b1", "SK": job})["Item"]["status"] == "COMPLETED"

    def test_a_second_delivery_whose_probe_fails_does_not_mark_the_completed_job_failed(self, tables):
        sources, jobs = tables
        job = "2026-11-02T10:00:00.000000Z"
        item = {**_graph_item(), "PK": "NS#ns-B", "SK": "SRC#b1", "sourceId": "b1", "namespaceId": "ns-B"}
        sources.put_item(Item=item)
        jobs.put_item(Item={"PK": "SRC#b1", "SK": job, "status": "IN_PROGRESS"})
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.verify_source("ns-B", "b1", job)
        timeout = MagicMock(side_effect=gv.ProbeFailedError("timeout"))
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: timeout}):
            gv.verify_source("ns-B", "b1", job)
        row = sources.get_item(Key={"PK": "NS#ns-B", "SK": "SRC#b1"})["Item"]
        assert (row["status"], row["queryable"]) == ("APPROVED", True)
        assert jobs.get_item(Key={"PK": "SRC#b1", "SK": job})["Item"]["status"] == "COMPLETED"

    def test_a_superseded_older_verify_still_closes_its_own_open_job(self, tables):
        sources, jobs = tables
        old, new = "2026-11-02T10:00:00.000000Z", "2026-11-02T11:00:00.000000Z"
        item = {**_graph_item(), "PK": "NS#ns-B", "SK": "SRC#b1", "sourceId": "b1", "namespaceId": "ns-B"}
        sources.put_item(Item={**item, "status": "APPROVED", "queryable": True, "lastScanJobId": new})
        jobs.put_item(Item={"PK": "SRC#b1", "SK": old, "status": "IN_PROGRESS"})
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: MagicMock()}):
            gv.verify_source("ns-B", "b1", old)
        assert jobs.get_item(Key={"PK": "SRC#b1", "SK": old})["Item"]["status"] == "FAILED"


class TestPlatformClusterRefusal:
    """the platform cluster holds every namespace's ontology, and Neptune's default graph is their union, so no
    endpoint of it may become a graph source. The refusal fails closed and comes before any request to the endpoint."""

    def _run(self, daos, item):
        dao, _ = daos
        dao.get.return_value = item
        probe = MagicMock()
        with patch.dict(gv.PROBES, {SparqlStore.NEPTUNE: probe}), patch(f"{MODULE}.AWS_REGION", "us-east-1"):
            gv.handler(_event(_MESSAGE), None)
        return _source_fields(dao), probe

    @pytest.mark.parametrize("host", sorted(_PLATFORM_HOSTS), ids=["writer", "reader", "custom"])
    def test_every_endpoint_of_the_platform_cluster_is_refused(self, daos, host):
        fields, probe = self._run(daos, _graph_item(dataEndpoint=host))
        assert fields["status"] == "SCAN_FAILED"
        assert fields["queryable"] is False
        assert "platform's ontology cluster" in fields["errorMessage"]
        probe.assert_not_called()

    def test_the_refusal_ignores_case(self, daos):
        upper = "COA-PLATFORM.cluster-abc.us-east-1.neptune.amazonaws.com"
        hosts = dict.fromkeys(_PLATFORM_HOSTS, "cluster-PLATFORM") | {upper: _CLUSTER}
        with patch(f"{MODULE}._account_cluster_endpoints", return_value=hosts):
            fields, probe = self._run(daos, _graph_item(dataEndpoint=upper))
        assert fields["status"] == "SCAN_FAILED"
        probe.assert_not_called()

    def test_another_cluster_of_the_account_is_still_approved(self, daos):
        fields, probe = self._run(daos, _graph_item())
        assert fields["status"] == "APPROVED"
        probe.assert_called_once()

    def test_a_deployment_that_names_no_platform_cluster_approves_nothing(self, daos):
        with patch(f"{MODULE}.PLATFORM_NEPTUNE_CLUSTER_ID", ""):
            fields, probe = self._run(daos, _graph_item())
        assert fields["status"] == "SCAN_FAILED"
        assert "platform" in fields["errorMessage"]
        probe.assert_not_called()

    def test_a_failed_platform_lookup_approves_nothing(self, daos):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DescribeDBClusters")
        with patch(f"{MODULE}._platform_cluster_hosts", side_effect=error):
            fields, probe = self._run(daos, _graph_item())
        assert fields["status"] == "SCAN_FAILED"
        assert "platform" in fields["errorMessage"]
        probe.assert_not_called()

    def test_the_platform_hosts_are_its_writer_reader_and_custom_endpoints(self):
        client = MagicMock()
        client.describe_db_clusters.return_value = {
            "DBClusters": [
                {
                    "DBClusterIdentifier": _PLATFORM_ID,
                    "Endpoint": "COA-Platform.cluster-abc.us-east-1.neptune.amazonaws.com",
                    "ReaderEndpoint": "coa-platform.cluster-ro-abc.us-east-1.neptune.amazonaws.com",
                    "CustomEndpoints": ["pr.cluster-custom-abc.us-east-1.neptune.amazonaws.com"],
                }
            ]
        }
        with (
            patch(f"{MODULE}.boto3.client", return_value=client),
            patch(f"{MODULE}.PLATFORM_NEPTUNE_CLUSTER_ID", _PLATFORM_ID),
        ):
            hosts = _PLATFORM_CLUSTER_HOSTS("us-east-1")
        client.describe_db_clusters.assert_called_once_with(DBClusterIdentifier=_PLATFORM_ID)
        assert hosts == _PLATFORM_HOSTS

    def test_a_platform_cluster_that_is_not_found_is_a_failed_lookup(self):
        client = MagicMock()
        client.describe_db_clusters.return_value = {"DBClusters": []}
        with (
            patch(f"{MODULE}.boto3.client", return_value=client),
            patch(f"{MODULE}.PLATFORM_NEPTUNE_CLUSTER_ID", _PLATFORM_ID),
            pytest.raises(gv.PlatformClusterUnknownError),
        ):
            _PLATFORM_CLUSTER_HOSTS("us-east-1")
