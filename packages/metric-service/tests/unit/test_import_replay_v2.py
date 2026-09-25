# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for immutable schema-v2 async import replay."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _permissive_source_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep replay tests independent from deployed source metadata."""
    from coa_metrics.source_status import PERMISSIVE_ENV

    monkeypatch.setenv(PERMISSIVE_ENV, "true")


def _definition(name: str, *, description: str = "description", concepts: list[str] | None = None):
    from coa_metrics.neptune_client import MetricAiContext, MetricDefinition, MetricDialect

    return MetricDefinition(
        name=name,
        description=description,
        expression_dialects=[
            MetricDialect(dialect="trino", expression=f"SELECT '{name}'"),
            MetricDialect(dialect="postgresql", expression=f"SELECT '{name}'"),
        ],
        data_source_id="source-1",
        source_table="orders",
        default_time_grain="DAY",
        unit=None,
        return_type="decimal",
        ai_context=MetricAiContext(
            synonyms=[f"{name} synonym"],
            instructions="Use carefully",
            examples=[f"Show {name}"],
        ),
        ontology_concepts=list(concepts or []),
        defined_by="import-worker",
        effective_from="2026-08-24",
    )


def _payload():
    from coa_metrics.api.import_job_store import MetricDisposition
    from coa_metrics.api.import_plan_payload import OffsetPlanPayload, PlannedMetric

    return OffsetPlanPayload(
        offset=50,
        entries=(
            PlannedMetric(
                source_index=50,
                name="created",
                disposition=MetricDisposition.CREATE,
                definition=_definition("created", concepts=["urn:example:Order"]),
            ),
            PlannedMetric(
                source_index=51,
                name="updated",
                disposition=MetricDisposition.UPDATE,
                definition=_definition("updated"),
                warning="existing metric overwritten",
            ),
            PlannedMetric(
                source_index=52,
                name="invalid",
                disposition=MetricDisposition.ERROR,
                error="invalid: bad definition",
            ),
        ),
    )


def test_plan_payload_round_trips_every_nested_field_canonically() -> None:
    from coa_metrics.api.import_plan_payload import canonical_plan_payload_bytes, parse_plan_payload

    plan = _payload()
    serialized = canonical_plan_payload_bytes(plan)

    assert parse_plan_payload(serialized) == plan
    assert canonical_plan_payload_bytes(parse_plan_payload(serialized)) == serialized
    assert json.loads(serialized)["schemaVersion"] == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value["entries"][0].update({"sourceIndex": 51}),
        lambda value: value["entries"][0]["definition"].update({"name": "different"}),
        lambda value: value["entries"][2].update(
            {"definition": json.loads(json.dumps(value["entries"][0]["definition"]))}
        ),
    ],
    ids=["unknown-field", "noncontiguous-index", "name-mismatch", "definition-on-error"],
)
def test_plan_payload_rejects_malformed_invariants(mutate) -> None:
    from coa_metrics.api.import_plan_payload import canonical_plan_payload_bytes, parse_plan_payload

    raw = json.loads(canonical_plan_payload_bytes(_payload()))
    mutate(raw)

    with pytest.raises(ValueError):
        parse_plan_payload(json.dumps(raw).encode())


def test_plan_payload_rejects_duplicate_json_keys() -> None:
    from coa_metrics.api.import_plan_payload import parse_plan_payload

    with pytest.raises(ValueError, match="duplicate key"):
        parse_plan_payload(b'{"schemaVersion":2,"schemaVersion":2,"offset":0,"entries":[]}')


def test_offset_plan_reference_strict_dynamodb_round_trip() -> None:
    from decimal import Decimal

    from coa_metrics.api.import_job_store import (
        OffsetPlanReference,
        _parse_item_offset_plan,
        _serialize_offset_plan_reference,
    )

    reference = OffsetPlanReference(
        offset=50,
        entry_count=3,
        payload_key="ns-1/imports/checkpoints/job-1/50/token.json",
        payload_version_id="version-1",
        payload_sha256="a" * 64,
        payload_bytes=1234,
    )
    serialized = _serialize_offset_plan_reference(reference)
    dynamodb_value = {
        **serialized,
        "schemaVersion": Decimal(2),
        "offset": Decimal(50),
        "entryCount": Decimal(3),
        "payloadBytes": Decimal(1234),
    }

    assert _parse_item_offset_plan({"offsetPlan": dynamodb_value}, expected_offset=50) == reference

    with pytest.raises(ValueError, match="unsupported"):
        _parse_item_offset_plan(
            {"offsetPlan": {**dynamodb_value, "schemaVersion": Decimal(3)}},
            expected_offset=50,
        )
    with pytest.raises(ValueError, match="exactly"):
        _parse_item_offset_plan(
            {"offsetPlan": {**dynamodb_value, "extra": "field"}},
            expected_offset=50,
        )


@pytest.mark.parametrize(
    "stale_plan",
    [
        {"offset": 50, "entries": [{"name": "new", "disposition": "CREATE"}]},
        {"schemaVersion": 2, "malformed": True},
    ],
    ids=["newer-offset", "malformed"],
)
def test_processed_offset_wins_over_unrelated_or_malformed_plan(stale_plan) -> None:
    from botocore.exceptions import ClientError
    from coa_metrics.api.import_job_store import OffsetClaimState, claim_job_offset

    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "processed"}},
        "UpdateItem",
    )
    table.get_item.return_value = {
        "Item": {
            "status": "IN_PROGRESS",
            "metricsTotal": 1,
            "metricsProcessed": 1,
            "nextOffset": 1,
            "chunkSize": 50,
            "processedOffsets": [0],
            "offsetPlan": stale_plan,
        }
    }

    with patch("coa_metrics.api.import_job_store._get_table", return_value=table):
        claim = claim_job_offset("ns-1", "job-1", offset=0, end_offset=1, chunk_size=50, lease_seconds=60)

    assert claim.state == OffsetClaimState.ALREADY_PROCESSED


def test_inactive_job_wins_over_malformed_plan() -> None:
    from botocore.exceptions import ClientError
    from coa_metrics.api.import_job_store import OffsetClaimState, claim_job_offset

    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "terminal"}},
        "UpdateItem",
    )
    table.get_item.return_value = {"Item": {"status": "FAILED", "offsetPlan": {"malformed": True}}}

    with patch("coa_metrics.api.import_job_store._get_table", return_value=table):
        claim = claim_job_offset("ns-1", "job-1", offset=0, end_offset=1, chunk_size=50, lease_seconds=60)

    assert claim.state == OffsetClaimState.NOT_ACTIVE


def test_staged_async_source_requires_and_returns_version_id() -> None:
    from coa_metrics.api.import_osi import _write_to_s3

    client = MagicMock()
    client.put_object.return_value = {"VersionId": "source-version"}
    with (
        patch("coa_metrics.api.import_osi._get_s3_client", return_value=client),
        patch("coa_metrics.api.import_osi._get_bucket", return_value="bucket"),
        patch("coa_metrics.api.import_osi.uuid.uuid4", return_value="unique"),
    ):
        staged = _write_to_s3("content", "ns-1")

    assert staged.key.endswith("-unique.yaml")
    assert staged.key.startswith("ns-1/imports/jobs/")
    assert staged.version_id == "source-version"


def test_worker_reads_exact_staged_source_version_after_key_is_overwritten() -> None:
    import boto3
    from coa_metrics.api.import_worker import _load_source_document
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        client.put_bucket_versioning(
            Bucket="test-bucket",
            VersioningConfiguration={"Status": "Enabled"},
        )
        key = "ns-1/imports/jobs/source.yaml"
        source_version = client.put_object(Bucket="test-bucket", Key=key, Body=b"version-one")["VersionId"]
        client.put_object(Bucket="test-bucket", Key=key, Body=b"version-two")

        expected_document = MagicMock(metrics=[MagicMock(name="metric-v1")], datasets=[])
        parse_result = MagicMock(success=True, document=expected_document)
        with (
            patch("coa_metrics.api.import_worker._BUCKET", "test-bucket"),
            patch("coa_metrics.api.import_worker._get_s3", return_value=client),
            patch("coa_metrics.api.import_worker.parse_osi_yaml", return_value=parse_result) as parse,
        ):
            document = _load_source_document(
                {"s3VersionId": source_version},
                key,
                metrics_total=1,
            )

    assert document is expected_document
    parse.assert_called_once_with("version-one")


def test_build_plan_turns_invalid_metric_iri_into_error_before_neptune_io() -> None:
    from coa_metrics.api.import_job_store import MetricDisposition
    from coa_metrics.api.import_worker import _build_offset_plan
    from coa_metrics.neptune_client import MetricNeptuneClient

    metric = MagicMock(name="bad name")
    metric.name = "bad name"
    document = MagicMock(metrics=[metric], datasets=[])
    neptune = MetricNeptuneClient()

    with (
        patch("coa_metrics.api.import_worker._osi_metric_to_definition", return_value=_definition("bad name")),
        patch("coa_metrics.neptune_client._sparql_query") as query,
        patch("coa_metrics.neptune_client._sparql_update") as update,
    ):
        plan = _build_offset_plan("ns-1", 0, [metric], document, neptune)

    assert plan.entries[0].disposition == MetricDisposition.ERROR
    assert "forbidden" in (plan.entries[0].error or "")
    query.assert_not_called()
    update.assert_not_called()


def test_build_plan_propagates_real_resolver_transport_failure() -> None:
    from coa_metrics.api.import_worker import _build_offset_plan
    from coa_metrics.neptune_client import MetricNeptuneClient

    metric = MagicMock(name="metric")
    metric.name = "metric"
    document = MagicMock(metrics=[metric], datasets=[])
    neptune = MetricNeptuneClient()
    transport_error = httpx.ReadTimeout("Neptune timed out")

    with (
        patch(
            "coa_metrics.api.import_worker._osi_metric_to_definition",
            return_value=_definition("metric", concepts=["Order"]),
        ),
        patch("coa_metrics.neptune_client._sparql_query", side_effect=transport_error),
        pytest.raises(httpx.ReadTimeout) as raised,
    ):
        _build_offset_plan("ns-1", 0, [metric], document, neptune)

    assert raised.value is transport_error


def test_build_plan_propagates_malformed_resolver_uri_as_retryable_protocol_error() -> None:
    from coa_metrics.api.import_worker import _build_offset_plan
    from coa_metrics.neptune_client import MetricNeptuneClient

    metric = MagicMock(name="metric")
    metric.name = "metric"
    document = MagicMock(metrics=[metric], datasets=[])
    neptune = MetricNeptuneClient()
    malformed_response = {
        "results": {
            "bindings": [
                {
                    "label": {"value": "Order"},
                    "cls": {"type": "uri", "value": "not-an-absolute-iri"},
                }
            ]
        }
    }

    with (
        patch(
            "coa_metrics.api.import_worker._osi_metric_to_definition",
            return_value=_definition("metric", concepts=["Order"]),
        ),
        patch("coa_metrics.neptune_client._sparql_query", return_value=malformed_response),
        pytest.raises(ValueError, match="resolver returned an unsupported class URI") as raised,
    ):
        _build_offset_plan("ns-1", 0, [metric], document, neptune)

    assert raised.value.__class__ is ValueError


def test_duplicate_names_are_planned_create_then_update_without_second_lookup() -> None:
    from coa_metrics.api.import_job_store import MetricDisposition
    from coa_metrics.api.import_worker import _build_offset_plan

    first_metric = MagicMock()
    first_metric.name = "revenue"
    second_metric = MagicMock()
    second_metric.name = "revenue"
    document = MagicMock(metrics=[first_metric, second_metric], datasets=[])
    neptune = MagicMock()
    neptune.get_metric.return_value = None

    with patch(
        "coa_metrics.api.import_worker._osi_metric_to_definition",
        side_effect=[
            _definition("revenue", description="first"),
            _definition("revenue", description="second"),
        ],
    ):
        plan = _build_offset_plan("ns-1", 0, document.metrics, document, neptune)

    assert [entry.disposition for entry in plan.entries] == [
        MetricDisposition.CREATE,
        MetricDisposition.UPDATE,
    ]
    assert plan.entries[1].warning == "Metric 'revenue' overwritten by a later definition in the same import"
    assert plan.entries[1].definition is not None
    assert plan.entries[1].definition.description == "second"
    neptune.get_metric.assert_called_once_with("ns-1", "revenue")


def test_checkpoint_load_verifies_version_hash_and_job_scope() -> None:
    from coa_metrics.api.import_worker import _load_plan_payload, _store_plan_payload

    client = MagicMock()
    client.put_object.return_value = {"VersionId": "plan-version"}
    with patch("coa_metrics.api.import_worker._get_s3", return_value=client):
        reference = _store_plan_payload("ns-1", "job-1", "token-1", _payload())

    payload = client.put_object.call_args.kwargs["Body"]
    client.get_object.return_value = {
        "VersionId": "plan-version",
        "Body": io.BytesIO(payload),
    }
    with patch("coa_metrics.api.import_worker._get_s3", return_value=client):
        assert _load_plan_payload("ns-1", "job-1", reference) == _payload()

    wrong_scope = reference.__class__(
        offset=reference.offset,
        entry_count=reference.entry_count,
        payload_key="other/imports/checkpoints/job-1/50/token.json",
        payload_version_id=reference.payload_version_id,
        payload_sha256=reference.payload_sha256,
        payload_bytes=reference.payload_bytes,
    )
    with (
        patch("coa_metrics.api.import_worker._get_s3", return_value=client),
        pytest.raises(ValueError, match="outside"),
    ):
        _load_plan_payload("ns-1", "job-1", wrong_scope)

    client.get_object.return_value = {
        "VersionId": "plan-version",
        "Body": io.BytesIO(payload + b"tampered"),
    }
    with (
        patch("coa_metrics.api.import_worker._get_s3", return_value=client),
        pytest.raises(ValueError, match="byte length"),
    ):
        _load_plan_payload("ns-1", "job-1", reference)

    same_length_tamper = bytes([payload[0] ^ 1]) + payload[1:]
    client.get_object.return_value = {
        "VersionId": "plan-version",
        "Body": io.BytesIO(same_length_tamper),
    }
    with (
        patch("coa_metrics.api.import_worker._get_s3", return_value=client),
        pytest.raises(ValueError, match="digest"),
    ):
        _load_plan_payload("ns-1", "job-1", reference)


@pytest.mark.parametrize("mutate_before_failure", [False, True], ids=["failure-before-write", "ambiguous-after-write"])
def test_partial_write_takeover_replays_exact_payload_and_accounts_once(mutate_before_failure: bool) -> None:
    import boto3
    from coa_metrics.api.import_worker import _process_chunk
    from moto import mock_aws

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="import-jobs",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_bucket_versioning(
            Bucket="test-bucket",
            VersioningConfiguration={"Status": "Enabled"},
        )
        source_key = "ns-1/imports/jobs/source.yaml"
        source_result = s3.put_object(Bucket="test-bucket", Key=source_key, Body=b"source")
        source_version = source_result["VersionId"]

        key = {"PK": "NS#ns-1", "SK": "IMPORT#job-1"}
        table.put_item(
            Item={
                **key,
                "jobId": "job-1",
                "namespaceId": "ns-1",
                "status": "IN_PROGRESS",
                "s3Key": source_key,
                "s3VersionId": source_version,
                "metricsTotal": 3,
                "metricsProcessed": 0,
                "nextOffset": 0,
                "chunkSize": 50,
                "metricsCreated": 0,
                "metricsUpdated": 0,
                "processedOffsets": [],
                "errors": [],
                "warnings": [],
            }
        )

        metrics = []
        for name in ("metric_a", "metric_b", "bad_metric"):
            metric = MagicMock()
            metric.name = name
            metrics.append(metric)
        document = MagicMock(metrics=metrics, datasets=[])
        parse_result = MagicMock(success=True, document=document)

        definitions = {
            "metric_a": _definition("metric_a", description="original A"),
            "metric_b": _definition("metric_b", description="original B"),
        }

        def convert(metric, _document, _caller):
            if metric.name == "bad_metric":
                raise ValueError("invalid metric")
            return definitions[metric.name]

        physical: dict[str, object] = {}
        neptune = MagicMock()
        neptune.get_metric.side_effect = lambda _namespace, name: physical.get(name)
        failed_once = False

        def create(_namespace, definition):
            nonlocal failed_once
            if definition.name == "metric_b" and not failed_once:
                failed_once = True
                if mutate_before_failure:
                    physical[definition.name] = definition
                raise RuntimeError("ambiguous metric_b write")
            physical[definition.name] = definition

        neptune.create_metric.side_effect = create
        neptune.update_metric.side_effect = lambda _namespace, name, definition: physical.__setitem__(name, definition)

        message = {
            "namespaceId": "ns-1",
            "jobId": "job-1",
            "s3Key": source_key,
            "offset": 0,
            "chunkSize": 50,
        }
        with (
            patch("coa_metrics.api.import_job_store._get_table", return_value=table),
            patch("coa_metrics.api.import_job_store.time.time", side_effect=[1_000, 1_002]),
            patch("coa_metrics.api.import_worker._BUCKET", "test-bucket"),
            patch("coa_metrics.api.import_worker._OFFSET_LEASE_SECONDS", 1),
            patch("coa_metrics.api.import_worker._get_s3", return_value=s3),
            patch("coa_metrics.api.import_worker._get_neptune", return_value=neptune),
            patch("coa_metrics.api.import_worker.parse_osi_yaml", return_value=parse_result) as parse,
            patch("coa_metrics.api.import_worker._osi_metric_to_definition", side_effect=convert) as to_definition,
            patch("coa_metrics.api.import_worker._get_sqs") as sqs,
        ):
            with pytest.raises(RuntimeError, match="ambiguous metric_b write"):
                _process_chunk(message)

            after_failure = table.get_item(Key=key, ConsistentRead=True)["Item"]
            assert after_failure["metricsProcessed"] == 0
            assert after_failure["processedOffsets"] == []
            assert after_failure["offsetPlan"]["schemaVersion"] == 2
            assert "metric_a" in physical
            assert ("metric_b" in physical) is mutate_before_failure

            # A v2 takeover must not need the original source at all.
            s3.delete_object(Bucket="test-bucket", Key=source_key, VersionId=source_version)
            _process_chunk(message)

        completed = table.get_item(Key=key, ConsistentRead=True)["Item"]
        assert completed["status"] == "COMPLETED"
        assert completed["processedOffsets"] == [0]
        assert completed["metricsProcessed"] == 3
        assert completed["nextOffset"] == 3
        assert completed["metricsCreated"] == 2
        assert completed["metricsUpdated"] == 0
        assert completed["errors"] == ["bad_metric: invalid metric"]
        assert completed["warnings"] == []
        assert "offsetPlan" not in completed
        assert physical["metric_a"] == definitions["metric_a"]
        assert physical["metric_b"] == definitions["metric_b"]
        assert parse.call_count == 1
        assert to_definition.call_count == 3
        sqs.assert_not_called()
