# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The onboarding tools against a scripted client: the CreateSource body on the wire, the
register/approve/reject/rescan/delete polls, documents sources, and the lifecycle writes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from coa_common.sparql_config import GRAPH_SOURCES_DISABLED_MESSAGE
from coa_control_plane_server.models.source_type import SourceType
from coa_workflow_agents.clients.coa_api_client import CoaApiError
from coa_workflow_agents.tools import onboarding
from coa_workflow_agents.tools.onboarding import MUTATING_TOOL_NAMES, build_onboarding_tools
from coa_workflow_agents.tools.polling import PollFailureError, PollTimeoutError
from structlog.testing import capture_logs

from tests.unit.conftest import FakeCoaClient

pytestmark = pytest.mark.unit

_GLUE = {"glueConfiguration": {"catalogId": "123456789012", "region": "us-east-1", "databaseName": "db"}}
_JDBC = {"jdbcConfiguration": {"engine": "SQLSERVER", "host": "db.example.com", "port": 1433, "databaseName": "orders"}}
_ENDPOINT = "db.cluster-x.us-east-1.neptune.amazonaws.com"
_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:x"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("coa_workflow_agents.tools.polling.time.sleep", lambda _s: None)
    monkeypatch.setattr("coa_workflow_agents.tools.onboarding.time.sleep", lambda _s: None)


def _tool(client, name):
    return next(t for t in build_onboarding_tools(client, "ns1") if t.tool_name == name)


def _http(status: int, body: str = "") -> CoaApiError:
    return CoaApiError("GET", "/namespaces/ns1/sources/s", status, body)


def _create(client, name="my source", database_source=_GLUE, **kwargs):
    return _tool(client, "create_source")(name=name, source_type="DATABASE", database_source=database_source, **kwargs)


def _source(status: str, **extra: Any) -> dict[str, Any]:
    return {"sourceId": "src1", "status": status, **extra}


# --- create_source: the body ----------------------------------------------------


def test_create_source_sends_the_name_and_the_config_and_echoes_the_name():
    client = FakeCoaClient()
    out = _create(client, name="my sql server source", database_source=_JDBC)
    assert out == {"sourceId": "src1", "status": "REGISTERED", "name": "my sql server source"}
    assert client.bodies == [
        {"sourceType": "DATABASE", "databaseSource": {"name": "my sql server source", **_JDBC}},
    ]


@pytest.mark.parametrize("bad_name", ["", "   ", "x" * 129, None, 123])
def test_create_source_refuses_a_blank_or_too_long_name_before_calling_coa(bad_name):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match="name must be 1-128 characters"):
        _create(client, name=bad_name)
    assert client.calls == []


@pytest.mark.parametrize(
    ("given", "sent"),
    [("x", "x"), ("y" * 128, "y" * 128), ("  orders db  ", "orders db"), (" " + "z" * 128 + " ", "z" * 128)],
)
def test_create_source_trims_the_name_and_accepts_128_characters(given, sent):
    client = FakeCoaClient()
    out = _create(client, name=given)
    assert client.bodies[0]["databaseSource"]["name"] == sent
    assert out["name"] == sent


def test_create_source_name_argument_wins_over_a_name_inside_database_source():
    client = FakeCoaClient()
    _create(client, name="from-arg", database_source={**_GLUE, "name": "from-config"})
    assert client.bodies[0]["databaseSource"]["name"] == "from-arg"


@pytest.mark.parametrize(
    ("database_source", "message"),
    [
        ({}, "exactly one config key"),
        ({"glueConfiguration": {}, "jdbcConfiguration": {}}, "exactly one config key"),
        ({"glueConfiguration": "nope"}, "must be an object"),
        ("nope", "must be an object"),
        ({"glueConfiguration": {"catalogId": "123456789012"}}, "databaseName"),
        ({"jdbcConfiguration": {**_JDBC["jdbcConfiguration"], "engine": "DB2"}}, "engine"),
        ({"glueConfiguration": {**_GLUE["glueConfiguration"], "tableFilters": "x"}}, "no field"),
        ({"glueConfiguration": {**_GLUE["glueConfiguration"], "executionEngine": "SPARK"}}, "executionEngine"),
    ],
)
def test_create_source_refuses_a_bad_database_source_before_calling_coa(database_source, message):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match=message):
        _create(client, database_source=database_source)
    assert client.calls == []


def test_create_source_refuses_a_source_type_other_than_database():
    client = FakeCoaClient()
    with pytest.raises(ValueError, match="source_type must be 'DATABASE'"):
        _tool(client, "create_source")(name="x", source_type="DOCUMENTS", database_source=_GLUE)
    assert client.calls == []


def test_create_source_sends_the_enrichment_flag_only_when_given():
    client = FakeCoaClient()
    _create(client)
    _create(client, metadata_enrichment_enabled=False)
    assert "metadataEnrichmentEnabled" not in client.bodies[0]["databaseSource"]
    assert client.bodies[1]["databaseSource"]["metadataEnrichmentEnabled"] is False


def test_glue_redshift_needs_a_workgroup_and_sends_it_when_given():
    client = FakeCoaClient()
    redshift = {"glueConfiguration": {**_GLUE["glueConfiguration"], "executionEngine": "REDSHIFT"}}
    with pytest.raises(ValueError, match="requires redshiftWorkgroup"):
        _create(client, database_source=redshift)
    assert client.calls == []
    redshift["glueConfiguration"]["redshiftWorkgroup"] = "wg-prod"
    _create(client, database_source=redshift)
    config = client.bodies[0]["databaseSource"]["glueConfiguration"]
    assert (config["executionEngine"], config["redshiftWorkgroup"]) == ("REDSHIFT", "wg-prod")


def test_the_source_type_argument_is_a_generated_source_type():
    schema = _tool(FakeCoaClient(), "create_source").tool_spec["inputSchema"]["json"]["properties"]["source_type"]
    assert set(json.dumps(schema).split('"')) >= {SourceType.DATABASE.value}


# --- create_source: RDF_SPARQL --------------------------------------------------


def test_create_source_accepts_a_sparql_configuration():
    client = FakeCoaClient()
    cfg = {"dataEndpoint": _ENDPOINT, "dataNamedGraph": "https://example.org/g", "port": 8182}
    _create(client, name="graph", database_source={"sparqlConfiguration": cfg})
    assert client.bodies[0]["databaseSource"]["sparqlConfiguration"] == cfg


@pytest.mark.parametrize(
    ("cfg", "message"),
    [
        ({"store": "neptune"}, "dataEndpoint"),
        ({"dataEndpoint": _ENDPOINT, "store": "blazegraph"}, "store"),
        ({"dataEndpoint": _ENDPOINT, "authMode": "basic"}, "authMode"),
        (
            {
                "dataEndpoint": _ENDPOINT,
                "credentialSecretArn": _SECRET_ARN,
            },
            "not accepted for a neptune store",
        ),
    ],
)
def test_create_source_refuses_an_invalid_sparql_configuration_locally(cfg, message):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match=message):
        _create(client, name="graph", database_source={"sparqlConfiguration": cfg})
    assert client.calls == []


def test_create_source_explains_graph_sources_turned_off():
    error = CoaApiError("POST", "/sources", 400, GRAPH_SOURCES_DISABLED_MESSAGE)
    client = FakeCoaClient(create_error=error)
    with pytest.raises(RuntimeError, match="graph \\(RDF_SPARQL\\) sources turned off"):
        _create(client, database_source={"sparqlConfiguration": {"dataEndpoint": _ENDPOINT}})


def test_create_source_reraises_another_400_unchanged():
    error = CoaApiError("POST", "/sources", 400, "bad request")
    with pytest.raises(CoaApiError):
        _create(FakeCoaClient(create_error=error))


# --- create_source: retry safety and replays of deleted sources -----------------


def test_create_source_sends_the_same_token_for_the_same_request_and_another_for_another():
    client = FakeCoaClient()
    _create(client)
    _create(client)
    _create(client, name="other")
    first, again, other = client.tokens
    assert first == again != other
    assert len(first) == 64


def _replayed(*ids: str, deleted: set[str]):
    """A client whose creates replay ``ids`` in turn (header set), the ``deleted`` ones read 404."""
    gone = [_http(404)] * onboarding._SOURCE_GONE_CHECKS
    return FakeCoaClient(
        created=[{"sourceId": i, "status": "REGISTERED"} for i in ids],
        create_headers=[{"x-idempotent-replay": "true"} if i in deleted else {} for i in ids],
        sources=[*gone, _source("SCANNING")],
    )


def test_create_source_registers_again_past_a_replay_of_a_deleted_source():
    client = _replayed("gone", "new1", deleted={"gone"})
    out = _create(client)
    assert (out["sourceId"], out["replacedDeletedSourceIds"]) == ("new1", ["gone"])
    assert len(client.tokens) == 2 and client.tokens[0] != client.tokens[1]
    assert client.tokens[1] == onboarding._next_create_source_token(client.tokens[0], "gone")


def test_a_replayed_source_is_judged_deleted_no_sooner_than_a_new_source_is_believed_missing():
    """Both read the same eventually consistent GetSource; a shorter replay window would duplicate a source."""
    window = onboarding._SOURCE_GONE_CHECKS * onboarding._SOURCE_GONE_RECHECK_S
    assert window >= onboarding._NEW_SOURCE_NOT_FOUND_GRACE_S


def test_a_replayed_source_that_reads_404_for_a_while_but_exists_is_not_registered_again():
    """The 404s fill all but the last read of the window GetSource's lag is allowed: not a deleted source."""
    client = FakeCoaClient(
        created={"sourceId": "src1", "status": "REGISTERED"},
        create_headers={"x-idempotent-replay": "true"},
        sources=[*[_http(404)] * (onboarding._SOURCE_GONE_CHECKS - 1), _source("SCANNING")],
    )
    out = _create(client)
    assert out["sourceId"] == "src1" and "replacedDeletedSourceIds" not in out
    assert client.create_calls == 1


def test_a_resend_past_a_deleted_replay_logs_create_source_replay_resent():
    client = _replayed("gone", "new1", deleted={"gone"})
    with capture_logs() as logs:
        _create(client)
    assert [e for e in logs if e["event"] == "create_source_replay_resent"] == [
        {"event": "create_source_replay_resent", "attempt": 1, "log_level": "warning"}
    ]


def test_create_source_returns_a_replayed_live_source_unchanged():
    client = FakeCoaClient(
        created={"sourceId": "src1", "status": "SCANNING"},
        create_headers={"x-idempotent-replay": "true"},
        sources=[_source("SCANNING")],
    )
    out = _create(client)
    assert out["sourceId"] == "src1" and "replacedDeletedSourceIds" not in out
    assert len(client.tokens) == 1


@pytest.mark.parametrize("status", ["DELETING", "DELETE_FAILED"])
def test_a_replayed_source_being_deleted_counts_as_deleted(status):
    client = FakeCoaClient(
        created=[{"sourceId": "old"}, {"sourceId": "new"}],
        create_headers=[{"x-idempotent-replay": "true"}, {}],
        sources=[_source(status)],
    )
    assert _create(client)["replacedDeletedSourceIds"] == ["old"]


def test_create_source_gives_up_after_three_resends_past_deleted_replays():
    ids = [f"gone{i}" for i in range(5)]
    client = FakeCoaClient(
        created=[{"sourceId": i} for i in ids],
        create_headers={"x-idempotent-replay": "true"},
        sources=[_http(404)],
    )
    with pytest.raises(RuntimeError, match="keeps replaying deleted sources"):
        _create(client)
    assert client.create_calls == 4


def test_a_replayed_source_read_that_fails_otherwise_propagates():
    client = FakeCoaClient(
        created={"sourceId": "src1"}, create_headers={"x-idempotent-replay": "true"}, sources=[_http(500)]
    )
    with pytest.raises(CoaApiError):
        _create(client)


# --- create_document_source -----------------------------------------------------


@pytest.fixture
def docs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.pdf").write_bytes(b"%PDF-1")
    (tmp_path / "docs" / "b.md").write_bytes(b"# b")
    (tmp_path / "docs" / "c.exe").write_bytes(b"MZ")
    (tmp_path / "docs" / ".hidden.md").write_bytes(b"# h")
    (tmp_path / ".private").mkdir()
    (tmp_path / ".private" / "notes.md").write_bytes(b"# n")
    return tmp_path


def _doc_client(*statuses: str, **kwargs):
    return FakeCoaClient(
        created={"sourceId": "d1", "status": "REGISTERED"},
        sources=[{"sourceId": "d1", "status": s} for s in (statuses or ("COMPLETED",))],
        pages=[{"items": []}],
        **kwargs,
    )


def test_create_document_source_uploads_the_files_then_creates_and_waits(docs):
    client = _doc_client("SCANNING_KG_BUILD", "COMPLETED")
    out = _tool(client, "create_document_source")(
        name=" Manuals ", local_files=["docs/a.pdf", "docs/b.md"], extraction_config={"chunkSize": 1024}
    )
    assert out["status"] == "COMPLETED"
    assert client.names() == [
        "get_source_upload_urls",
        "put_presigned",
        "put_presigned",
        "create_source",
        "get_source",
        "get_source",
    ]
    uploads = [c for c in client.calls if c[0] == "get_source_upload_urls"]
    assert uploads[0][2]["files"] == [
        {"filename": "a.pdf", "contentType": "application/pdf"},
        {"filename": "b.md", "contentType": "text/markdown"},
    ]
    assert [c[1][1:] for c in client.calls if c[0] == "put_presigned"] == [(6, "application/pdf"), (3, "text/markdown")]
    assert client.bodies == [
        {
            "sourceType": "DOCUMENTS",
            "documentSource": {
                "name": "Manuals",
                "uploadId": "11111111-2222-4333-8444-555555555555",
                "extractionConfig": {"chunkSize": 1024},
            },
        }
    ]


def test_create_document_source_from_s3_sends_the_bucket_prefixes_and_role():
    client = _doc_client()
    _tool(client, "create_document_source")(
        name="corpus",
        source_bucket_arn="arn:aws:s3:::my-bucket",
        s3_prefixes=["policies/"],
        role_arn="arn:aws:iam::123456789012:role/r",
    )
    assert client.bodies[0]["documentSource"] == {
        "name": "corpus",
        "sourceBucketArn": "arn:aws:s3:::my-bucket",
        "s3Prefixes": ["policies/"],
        "roleArn": "arn:aws:iam::123456789012:role/r",
    }
    assert "get_source_upload_urls" not in client.names()


def test_create_document_source_raises_when_ingestion_fails():
    client = _doc_client("SCANNING", "SCAN_FAILED")
    with pytest.raises(PollFailureError):
        _tool(client, "create_document_source")(name="corpus", source_bucket_arn="arn:aws:s3:::bucket-b")


def test_create_document_source_refuses_a_name_a_source_already_has_before_uploading(docs):
    client = _doc_client()
    client.pages = [{"items": [{"sourceId": "d0", "name": "Manuals", "status": "COMPLETED"}]}]
    with pytest.raises(RuntimeError, match="already exists \\(sourceId d0"):
        _tool(client, "create_document_source")(name="Manuals", local_files=["docs/a.pdf"])
    assert client.names() == []


def test_create_document_source_waits_out_a_first_read_404():
    client = _doc_client()
    client.sources = [_http(404), {"sourceId": "d1", "status": "COMPLETED"}]
    out = _tool(client, "create_document_source")(name="corpus", source_bucket_arn="arn:aws:s3:::bucket-b")
    assert out["status"] == "COMPLETED"


def test_create_document_source_believes_a_404_after_the_grace(monkeypatch):
    monkeypatch.setattr(onboarding, "_NEW_SOURCE_NOT_FOUND_GRACE_S", -1.0)
    client = _doc_client()
    client.sources = [_http(404)]
    with pytest.raises(CoaApiError) as excinfo:
        _tool(client, "create_document_source")(
            name="corpus", source_bucket_arn="arn:aws:s3:::bucket-b", timeout_seconds=1
        )
    assert excinfo.value.status_code == 404


def test_create_document_source_registers_again_past_a_replay_of_a_deleted_s3_source():
    client = _doc_client()
    client.created = [{"sourceId": "gone"}, {"sourceId": "d1"}]
    client.create_headers = [{"x-idempotent-replay": "true"}, {}]
    client.sources = [*[_http(404)] * onboarding._SOURCE_GONE_CHECKS, {"sourceId": "d1", "status": "COMPLETED"}]
    out = _tool(client, "create_document_source")(name="corpus", source_bucket_arn="arn:aws:s3:::bucket-b")
    assert out["replacedDeletedSourceIds"] == ["gone"]


def _name_409(source_id: str | None, status: str) -> CoaApiError:
    body = {"error": "name taken", "status": status, **({"sourceId": source_id} if source_id else {})}
    return CoaApiError("POST", "/sources", 409, json.dumps(body))


@pytest.mark.parametrize(
    ("replayed", "held", "message"),
    [
        (False, _name_409("other", "DELETING"), "source other that holds this name is still being deleted"),
        (False, _name_409("other", "DELETE_FAILED"), "source other that holds this name is still being deleted"),
        (True, _name_409("gone", "DELETING"), "source gone that holds this name is still being deleted"),
        # The holder is the source this call replayed, whatever its status reads.
        (True, _name_409("gone", "COMPLETED"), "source gone that holds this name is still being deleted"),
        # No id in the body: the replayed source is named, and with no replay there is nothing to name.
        (True, _name_409(None, "DELETING"), "source gone that holds this name is still being deleted"),
        (False, _name_409(None, "DELETING"), None),
        (False, _name_409("other", "COMPLETED"), None),
    ],
    ids=[
        "fresh-deleting",
        "fresh-delete-failed",
        "replayed-deleting",
        "replayed-same-id",
        "replayed-no-id",
        "fresh-no-id",
        "fresh-live-holder",
    ],
)
def test_create_document_source_explains_a_name_held_by_a_source_being_deleted(replayed, held, message):
    client = _doc_client(create_error=held)
    if replayed:
        client.created = [{"sourceId": "gone"}]
        client.create_headers = [{"x-idempotent-replay": "true"}]
        client.create_error = [None, held]
        client.sources = [_http(404)]
    if message is None:
        with pytest.raises(CoaApiError):
            _tool(client, "create_document_source")(name="corpus", source_bucket_arn="arn:aws:s3:::bucket-b")
        return
    with pytest.raises(RuntimeError, match=message):
        _tool(client, "create_document_source")(name="corpus", source_bucket_arn="arn:aws:s3:::bucket-b")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "exactly one of"),
        ({"local_files": ["docs/a.pdf"], "source_bucket_arn": "arn:aws:s3:::bucket-b"}, "exactly one of"),
        ({"local_files": ["docs/a.pdf"], "s3_prefixes": ["p/"]}, "apply only to an S3"),
        ({"local_files": []}, "non-empty list"),
        ({"local_files": ["docs/c.exe"]}, "not a supported document type"),
        ({"local_files": ["docs/a.pdf", "docs/a.pdf"]}, "unique"),
        ({"local_files": ["docs/a.pdf"] * 101}, "at most 100"),
        ({"local_files": ["docs/.hidden.md"]}, "not hidden"),
        ({"local_files": [".private/notes.md"]}, "not hidden"),
        ({"local_files": ["docs/missing.pdf"]}, "not a file"),
        ({"local_files": ["/etc/passwd"]}, "relative"),
        ({"local_files": ["../outside.pdf"]}, "escapes"),
        ({"source_bucket_arn": "not-an-arn"}, "source_bucket_arn"),
        ({"source_bucket_arn": "arn:aws:s3:::bucket-b", "extraction_config": {"chunks": 1}}, "no field"),
        ({"source_bucket_arn": "arn:aws:s3:::bucket-b", "extraction_config": "x"}, "must be an object"),
    ],
)
def test_create_document_source_validates_before_any_call(docs, kwargs, message):
    client = _doc_client()
    with pytest.raises(ValueError, match=message):
        _tool(client, "create_document_source")(name="corpus", **kwargs)
    assert client.names() == [] and client.bodies == []


@pytest.mark.parametrize(
    "response",
    [
        {"uploadUrls": [{"filename": "a.pdf", "uploadUrl": "https://s3.example/a"}, {"filename": "b.md"}]},
        {"uploadId": "u1", "uploadUrls": [{"filename": "a.pdf", "uploadUrl": "https://s3.example/a"}]},
        {"uploadUrls": [{"filename": "a.pdf", "uploadUrl": "https://s3.example/a"}]},
    ],
    ids=["missing-url", "missing-second-file", "missing-upload-id"],
)
def test_create_document_source_refuses_an_incomplete_upload_url_response(docs, response):
    client = _doc_client()
    client.get_source_upload_urls = lambda ns, files: response  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="every file"):
        _tool(client, "create_document_source")(name="corpus", local_files=["docs/a.pdf", "docs/b.md"])
    assert "put_presigned" not in client.names() and "create_source" not in client.names()


def test_create_document_source_refuses_a_blank_name():
    with pytest.raises(ValueError, match="name must be 1-128"):
        _tool(_doc_client(), "create_document_source")(name=" ", source_bucket_arn="arn:aws:s3:::bucket-b")


# --- the read tools -------------------------------------------------------------


def test_get_source_status_passes_the_response_through():
    client = FakeCoaClient(sources=[_source("PENDING_REVIEW")])
    assert _tool(client, "get_source_status")(source_id="src1") == _source("PENDING_REVIEW")


def test_list_sources_follows_every_page_and_forwards_the_type_filter():
    client = FakeCoaClient(
        pages=[
            {"items": [{"sourceId": "a"}], "nextToken": "t2"},
            {"items": [], "nextToken": "t3"},
            {"items": [{"sourceId": "b"}]},
        ]
    )
    out = _tool(client, "list_sources")(source_type="DOCUMENTS")
    assert out == {"sources": [{"sourceId": "a"}, {"sourceId": "b"}], "count": 2}
    assert [(c["next_token"], c["source_type"]) for c in client.list_calls] == [
        (None, "DOCUMENTS"),
        ("t2", "DOCUMENTS"),
        ("t3", "DOCUMENTS"),
    ]


def test_list_sources_without_a_filter_sends_none():
    client = FakeCoaClient()
    _tool(client, "list_sources")()
    assert client.list_calls[0]["source_type"] is None


@pytest.mark.parametrize(("requested", "sent"), [(0, 1), (1000, 100), (30, 30)])
def test_list_sources_clamps_the_page_size_to_coas_range(requested, sent):
    client = FakeCoaClient()
    _tool(client, "list_sources")(max_results=requested)
    assert client.list_calls[0]["max_results"] == sent


def test_the_source_type_filter_matches_the_generated_source_types():
    schema = _tool(FakeCoaClient(), "list_sources").tool_spec["inputSchema"]["json"]["properties"]["source_type"]
    text = json.dumps(schema)
    assert all(t.value in text for t in SourceType)


def test_get_scan_job_status_calls_out_an_incomplete_scan():
    job = {
        "jobId": "j1",
        "status": "COMPLETED",
        "tablesFailed": 2,
        "failedTables": ["a", "b"],
        "filterWarnings": ["tableFilter 'x.*' matched nothing"],
    }
    out = _tool(FakeCoaClient(scan_job=job), "get_scan_job_status")(source_id="src1", scan_job_id="j1")
    assert out["jobId"] == "j1"
    assert len(out["partialResultWarnings"]) == 2
    assert "2 table(s) were listed but could not be read (e.g. a, b)" in out["partialResultWarnings"][0]
    assert out["partialResultWarnings"][1] == "tableFilter 'x.*' matched nothing"


def test_get_scan_job_status_leaves_a_clean_scan_unchanged():
    job = {"jobId": "j1", "status": "COMPLETED", "tablesFailed": 0}
    assert _tool(FakeCoaClient(scan_job=job), "get_scan_job_status")(source_id="s", scan_job_id="j1") == job


def test_list_source_scan_jobs_follows_every_page_at_the_max_page_size():
    client = FakeCoaClient(pages=[{"items": [{"jobId": "j1"}], "nextToken": "t"}, {"items": [{"jobId": "j2"}]}])
    out = _tool(client, "list_source_scan_jobs")(source_id="src1")
    assert out == {"items": [{"jobId": "j1"}, {"jobId": "j2"}], "count": 2}
    assert [c["max_results"] for c in client.list_calls] == [onboarding.LIST_SOURCE_SCAN_JOBS_MAX_RESULTS] * 2


# --- approve_source -------------------------------------------------------------


def test_approve_source_returns_an_approved_source_without_calling_coa_again():
    client = FakeCoaClient(sources=[_source("APPROVED")])
    assert _tool(client, "approve_source")(source_id="src1", timeout_seconds=1)["status"] == "APPROVED"
    assert client.names() == ["get_source"]


@pytest.mark.parametrize("status", ["PENDING_REVIEW", "APPROVAL_FAILED", "RESCAN_REVIEW"])
def test_approve_source_approves_from_a_retryable_state_and_polls(status):
    client = FakeCoaClient(sources=[_source(status), _source("APPROVING"), _source("APPROVED")])
    out = _tool(client, "approve_source")(source_id="src1", timeout_seconds=5)
    assert out["status"] == "APPROVED"
    assert client.names().count("approve_source") == 1


@pytest.mark.parametrize("status", ["REGISTERED", "SCANNING", "REJECTED", "SCAN_FAILED"])
def test_approve_source_refuses_a_source_that_cannot_be_approved_without_calling_coa(status):
    client = FakeCoaClient(sources=[_source(status)])
    with pytest.raises(RuntimeError, match="ApproveSource requires"):
        _tool(client, "approve_source")(source_id="src1", timeout_seconds=1)
    assert "approve_source" not in client.names()


def test_approve_source_rejoins_an_approval_in_flight_without_a_second_call():
    client = FakeCoaClient(sources=[_source("APPROVING"), _source("APPROVED")])
    _tool(client, "approve_source")(source_id="src1", timeout_seconds=5)
    assert "approve_source" not in client.names()


def test_approve_source_raises_when_the_approval_fails():
    client = FakeCoaClient(sources=[_source("PENDING_REVIEW"), _source("APPROVAL_FAILED")])
    with pytest.raises(PollFailureError):
        _tool(client, "approve_source")(source_id="src1", timeout_seconds=5)


def test_approve_source_retrying_from_a_failure_ignores_the_stale_first_read():
    client = FakeCoaClient(sources=[_source("APPROVAL_FAILED"), _source("APPROVAL_FAILED"), _source("APPROVED")])
    out = _tool(client, "approve_source")(source_id="src1", timeout_seconds=5)
    assert out["status"] == "APPROVED"


# --- reject_source --------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [("PENDING_REVIEW", "REJECTED"), ("RESCAN_REVIEW", "APPROVED"), ("REJECTION_FAILED", "REJECTED")],
)
def test_reject_source_rejects_from_a_rejectable_state_and_polls_to_its_end(start, end):
    client = FakeCoaClient(sources=[_source(start), _source("REJECTING"), _source(end)])
    out = _tool(client, "reject_source")(source_id="src1", timeout_seconds=5)
    assert out["status"] == end
    assert client.names().count("reject_source") == 1


def test_reject_source_returns_a_rejected_source_without_calling_coa_again():
    client = FakeCoaClient(sources=[_source("REJECTED")])
    _tool(client, "reject_source")(source_id="src1", timeout_seconds=1)
    assert client.names() == ["get_source"]


def test_reject_source_rejoins_a_reject_in_flight_without_a_second_call():
    client = FakeCoaClient(sources=[_source("REJECTING"), _source("REJECTED")])
    _tool(client, "reject_source")(source_id="src1", timeout_seconds=5)
    assert "reject_source" not in client.names()


@pytest.mark.parametrize("status", ["APPROVED", "SCANNING", "REGISTERED"])
def test_reject_source_refuses_a_source_that_cannot_be_rejected_without_calling_coa(status):
    client = FakeCoaClient(sources=[_source(status)])
    with pytest.raises(RuntimeError, match="RejectSource requires"):
        _tool(client, "reject_source")(source_id="src1", timeout_seconds=1)
    assert "reject_source" not in client.names()


def test_reject_source_raises_when_the_rejection_fails():
    client = FakeCoaClient(sources=[_source("PENDING_REVIEW"), _source("REJECTION_FAILED")])
    with pytest.raises(PollFailureError):
        _tool(client, "reject_source")(source_id="src1", timeout_seconds=5)


# --- rescan_source --------------------------------------------------------------


def _scan(status: str, scan_id: str | None) -> dict[str, Any]:
    return _source(status, **({"databaseDetails": {"lastScanJobId": scan_id}} if scan_id else {}))


@pytest.mark.parametrize(
    ("before", "final"),
    [
        (_scan("SCAN_FAILED", "s1"), _scan("PENDING_REVIEW", "s2")),
        (_scan("APPROVED", "s1"), _scan("RESCAN_REVIEW", "s2")),
        (_scan("COMPLETED", None), _scan("COMPLETED", None)),
        (_scan("REGISTERED", None), _scan("APPROVED", "s2")),
    ],
)
def test_rescan_source_polls_to_the_state_the_rescan_ends_in(before, final):
    client = FakeCoaClient(sources=[before, _scan("SCANNING", "s1"), final])
    out = _tool(client, "rescan_source")(source_id="src1", timeout_seconds=5)
    assert out["status"] == final["status"]
    assert client.names().count("rescan_source") == 1


def test_rescan_source_finishes_when_an_unchanged_rescan_returns_straight_to_approved():
    client = FakeCoaClient(sources=[_scan("APPROVED", "s1"), _scan("APPROVED", "s1"), _scan("APPROVED", "s2")])
    out = _tool(client, "rescan_source")(source_id="src1", timeout_seconds=5)
    assert out["databaseDetails"]["lastScanJobId"] == "s2"


@pytest.mark.parametrize("polled_scan_id", ["s1", None])
def test_rescan_source_does_not_finish_on_approved_without_a_new_scan_id(polled_scan_id):
    client = FakeCoaClient(sources=[_scan("APPROVED", "s1"), _scan("APPROVED", polled_scan_id)])
    with pytest.raises(PollTimeoutError):
        _tool(client, "rescan_source")(source_id="src1", timeout_seconds=0)


def test_rescan_source_raises_when_it_fails_again():
    client = FakeCoaClient(sources=[_scan("SCAN_FAILED", "s1"), _scan("SCANNING", "s1"), _scan("SCAN_FAILED", "s2")])
    with pytest.raises(PollFailureError):
        _tool(client, "rescan_source")(source_id="src1", timeout_seconds=5)


def test_rescan_source_sends_the_discard_flag_only_when_true_and_a_fresh_token_each_time():
    client = FakeCoaClient(sources=[_scan("APPROVED", "s1"), _scan("RESCAN_REVIEW", "s2")])
    _tool(client, "rescan_source")(source_id="src1", timeout_seconds=5)
    client.sources = [_scan("RESCAN_REVIEW", "s1"), _scan("RESCAN_REVIEW", "s2")]
    _tool(client, "rescan_source")(source_id="src1", confirm_discard_open_review=True, timeout_seconds=5)
    sent = [c[2] for c in client.calls if c[0] == "rescan_source"]
    assert [s["confirm_discard_open_review"] for s in sent] == [None, True]
    assert sent[0]["client_token"] != sent[1]["client_token"]


@pytest.mark.parametrize("status", ["PENDING_REVIEW", "RESCAN_REVIEW", "COMPLETED"])
def test_rescan_source_ignores_a_stale_first_read_of_the_pre_rescan_state(status):
    final = "COMPLETED" if status == "COMPLETED" else "RESCAN_REVIEW"
    client = FakeCoaClient(
        sources=[_scan(status, "s1"), _scan(status, "s1"), _scan("SCANNING", "s1"), _scan(final, "s2")]
    )
    out = _tool(client, "rescan_source")(source_id="src1", timeout_seconds=5)
    assert out["databaseDetails"]["lastScanJobId"] == "s2"


# --- delete_source --------------------------------------------------------------


def test_delete_source_deletes_and_polls_until_the_source_is_gone():
    client = FakeCoaClient(sources=[_source("APPROVED"), _source("DELETING"), _http(404)])
    out = _tool(client, "delete_source")(source_id="src1", timeout_seconds=5)
    assert out == {"sourceId": "src1", "deleted": True, "alreadyGone": False}
    assert client.names() == ["get_source", "delete_source", "get_source", "get_source"]


def test_delete_source_on_a_source_that_is_already_gone_does_nothing():
    client = FakeCoaClient(sources=[_http(404)])
    out = _tool(client, "delete_source")(source_id="src1")
    assert out == {"sourceId": "src1", "deleted": True, "alreadyGone": True}
    assert "delete_source" not in client.names()


def test_delete_source_rejoins_a_delete_in_progress_without_resending():
    client = FakeCoaClient(sources=[_source("DELETING"), _http(404)])
    _tool(client, "delete_source")(source_id="src1", timeout_seconds=5)
    assert "delete_source" not in client.names()


@pytest.mark.parametrize(
    "active", ["REGISTERED", "SCANNING", "SCANNING_ENTITY_EXTRACTION", "SCANNING_KG_BUILD", "ENRICHING"]
)
def test_delete_source_refuses_while_a_scan_is_active(active):
    client = FakeCoaClient(sources=[_source(active)])
    with pytest.raises(RuntimeError, match="cannot delete while a scan is active"):
        _tool(client, "delete_source")(source_id="src1", timeout_seconds=1)
    assert "delete_source" not in client.names()


def test_delete_source_sends_the_delete_for_a_possibly_stranded_registered_graph_source():
    client = FakeCoaClient(sources=[_source("REGISTERED", sourceSubType="RDF_SPARQL"), _http(404)])
    out = _tool(client, "delete_source")(source_id="src1", timeout_seconds=5)
    assert out["deleted"] is True and "delete_source" in client.names()


def test_delete_source_maps_coa_refusing_a_registered_graph_source_to_scan_active():
    client = FakeCoaClient(
        sources=[_source("REGISTERED", sourceSubType="RDF_SPARQL")],
        errors={"delete_source": CoaApiError("DELETE", "/s", 409, "scan active")},
    )
    with pytest.raises(RuntimeError, match="cannot delete while a scan is active"):
        _tool(client, "delete_source")(source_id="src1", timeout_seconds=1)


def test_delete_source_retries_from_delete_failed_and_ignores_the_stale_first_read():
    client = FakeCoaClient(
        sources=[_source("DELETE_FAILED"), _source("DELETE_FAILED"), _source("DELETING"), _http(404)]
    )
    out = _tool(client, "delete_source")(source_id="src1", timeout_seconds=5)
    assert out["deleted"] is True and client.names().count("delete_source") == 1


def test_delete_source_raises_when_the_teardown_fails():
    client = FakeCoaClient(sources=[_source("APPROVED"), _source("DELETING"), _source("DELETE_FAILED")])
    with pytest.raises(PollFailureError):
        _tool(client, "delete_source")(source_id="src1", timeout_seconds=5)


@pytest.mark.parametrize("bad_id", ["", "  ", " src1", "src1 ", None])
def test_delete_source_refuses_a_blank_or_padded_id_before_any_call(bad_id):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match="source_id must be a non-blank id"):
        _tool(client, "delete_source")(source_id=bad_id, timeout_seconds=1)
    assert client.calls == []


# --- update_source_metadata -----------------------------------------------------


def test_update_source_metadata_sends_only_the_given_fields_trimmed():
    client = FakeCoaClient()
    _tool(client, "update_source_metadata")(source_id="src1", name="  New name  ", metadata_enrichment_enabled=False)
    assert client.bodies == [{"name": "New name", "metadataEnrichmentEnabled": False}]


def test_update_source_metadata_sends_one_full_config_object():
    client = FakeCoaClient()
    config = {**_GLUE["glueConfiguration"], "tableFilter": "sales_.*"}
    _tool(client, "update_source_metadata")(source_id="src1", database_source={"glueConfiguration": config})
    assert client.bodies == [{"glueConfiguration": config}]


def test_update_source_metadata_sends_only_the_config_object_from_database_source():
    client = FakeCoaClient()
    _tool(client, "update_source_metadata")(
        source_id="src1", database_source={**_GLUE, "name": "sneaky", "metadataEnrichmentEnabled": True}
    )
    assert client.bodies == [_GLUE]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "nothing to update"),
        ({"name": "  "}, "name must be 1-128"),
        ({"database_source": {}}, "exactly one config key"),
        ({"database_source": {"glueConfiguration": {"catalogId": "1"}}}, "invalid database_source.glueConfiguration"),
        ({"database_source": {**_GLUE, **_JDBC}}, "exactly one config key"),
    ],
)
def test_update_source_metadata_refuses_bad_input_before_calling_coa(kwargs, message):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match=message):
        _tool(client, "update_source_metadata")(source_id="src1", **kwargs)
    assert client.calls == []


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"databricksSqlWarehouseConfiguration": {}}, "Databricks"),
        ({"sparqlConfiguration": {"dataEndpoint": _ENDPOINT}}, "RDF_SPARQL"),
    ],
)
def test_update_source_metadata_refuses_a_config_COA_cannot_change(config, message):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match=message):
        _tool(client, "update_source_metadata")(source_id="src1", database_source=config)
    assert client.calls == []


# --- the other lifecycle writes -------------------------------------------------


@pytest.mark.parametrize(("column", "sent"), [(None, None), ("c1", "c1"), ("  c1 ", "c1")])
def test_keep_rescan_removal_passes_the_table_and_the_optional_column(column, sent):
    client = FakeCoaClient()
    _tool(client, "keep_rescan_removal")(source_id="src1", table_id="db.t", column_name=column)
    assert client.calls == [("keep_rescan_removal", ("ns1", "src1", "db.t"), {"column_name": sent})]


@pytest.mark.parametrize("column", ["", "   "])
def test_keep_rescan_removal_refuses_a_blank_column_that_would_keep_the_whole_table(column):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match="must not be blank"):
        _tool(client, "keep_rescan_removal")(source_id="src1", table_id="db.t", column_name=column)
    assert client.calls == []


def test_set_rescan_schedule_enables_with_an_expression_and_a_timezone():
    client = FakeCoaClient()
    _tool(client, "set_rescan_schedule")(
        source_id="src1", enabled=True, schedule_expression=" cron(0 3 * * ? *) ", timezone=" Europe/Paris "
    )
    assert client.bodies == [{"enabled": True, "scheduleExpression": "cron(0 3 * * ? *)", "timezone": "Europe/Paris"}]


def test_set_rescan_schedule_disables_with_an_explicit_false_and_sends_nothing_else():
    client = FakeCoaClient()
    _tool(client, "set_rescan_schedule")(
        source_id="src1", enabled=False, schedule_expression="rate(1 day)", timezone="UTC"
    )
    assert client.bodies == [{"enabled": False}]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"enabled": True}, "schedule_expression is required"),
        ({"enabled": True, "schedule_expression": "  "}, "schedule_expression is required"),
        ({"enabled": "false"}, "invalid rescan schedule"),
        ({"enabled": None}, "invalid rescan schedule"),
    ],
)
def test_set_rescan_schedule_refuses_ambiguous_input_before_calling_coa(kwargs, message):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match=message):
        _tool(client, "set_rescan_schedule")(source_id="src1", **kwargs)
    assert client.calls == []


@pytest.mark.parametrize("enabled", [True, False])
def test_set_event_rescan_sends_the_boolean(enabled):
    client = FakeCoaClient()
    _tool(client, "set_event_rescan")(source_id="src1", enabled=enabled)
    assert client.calls == [("put_source_event_rescan", ("ns1", "src1"), {"enabled": enabled})]


# --- the gate -------------------------------------------------------------------


def test_the_write_tools_are_gated_and_the_reads_are_not():
    names = {t.tool_name for t in build_onboarding_tools(FakeCoaClient(), "ns1")}
    reads = {"get_source_status", "list_sources", "get_scan_job_status", "list_source_scan_jobs"}
    assert names - reads == MUTATING_TOOL_NAMES


@pytest.mark.parametrize(
    ("tool_name", "status", "kwargs"),
    [
        ("rescan_source", "RESCAN_REVIEW", {"confirm_discard_open_review": False}),
        ("approve_source", "PENDING_REVIEW", {}),
        ("reject_source", "PENDING_REVIEW", {}),
    ],
)
def test_a_coa_409_on_a_lifecycle_write_propagates_and_starts_no_poll(tool_name, status, kwargs):
    """COA's own refusal is the contract (rescan does no local state check); it must reach the caller."""
    refusal = CoaApiError("POST", "/s", 409, "not in a state that allows this")
    client = FakeCoaClient(sources=[_source(status)], errors={tool_name: refusal})
    with pytest.raises(CoaApiError) as excinfo:
        _tool(client, tool_name)(source_id="src1", timeout_seconds=1, **kwargs)
    assert excinfo.value.status_code == 409
    assert client.names() == ["get_source", tool_name]
