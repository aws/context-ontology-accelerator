# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``CoaApiClient``: HTTP behavior, paths and retries, against a mocked ``requests.Session``."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from coa_workflow_agents.clients import coa_api_client
from coa_workflow_agents.clients.coa_api_client import (
    CoaApiClient,
    CoaApiError,
    _no_auth,
    _query_string,
    _s3_error_code,
    is_client_token_conflict,
    sanitized_route,
)

pytestmark = pytest.mark.unit


class _FakeTokenProvider:
    def __init__(self, token: str = "fake-id-token"):
        self.token = token
        self.calls = 0

    def get_id_token(self) -> str:
        self.calls += 1
        return self.token


def _fake_response(status_code: int = 200, json_body: dict | None = None, content: bytes = b"{}"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.text = str(json_body) if json_body is not None else content.decode()
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


def _client() -> CoaApiClient:
    return CoaApiClient(base_url="https://coa.example.com/prod/", token_provider=_FakeTokenProvider())


# ── Bearer token attachment ─────────────────────────────────────────────


def test_get_attaches_bearer_token_header():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {"ok": True})
        client.get("/namespaces/ns1/sources")

    _, kwargs = mock_request.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer fake-id-token"
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_token_is_fetched_fresh_on_every_request_not_cached_by_the_client():
    """The client itself holds no token cache — that's the provider's job
    (CallerTokenProvider). Every request calls get_id_token(), so a provider
    that refuses a token near expiry is consulted on the very next call."""
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {"ok": True})
        client.get("/a")
        client.get("/b")

    assert client.token_provider.calls == 2


# ── URL construction ─────────────────────────────────────────────────────


def test_base_url_trailing_slash_is_normalized():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {"ok": True})
        client.get("/namespaces/ns1/sources")

    args, _ = mock_request.call_args
    url = args[1]
    assert url == "https://coa.example.com/prod/namespaces/ns1/sources"
    assert "//namespaces" not in url.replace("https://", "")


# ── Error handling ───────────────────────────────────────────────────────


@pytest.mark.parametrize("status_code", [400, 403, 404, 409, 500, 503])
def test_non_2xx_response_raises_coa_api_error_with_status_and_body(status_code):
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        resp = _fake_response(status_code, content=b'{"message": "boom"}')
        resp.text = '{"message": "boom"}'
        mock_request.return_value = resp
        with pytest.raises(CoaApiError) as exc_info:
            client.get("/namespaces/ns1/sources/bad")

    assert exc_info.value.status_code == status_code
    assert exc_info.value.method == "GET"
    assert "boom" in exc_info.value.body
    # The gate (polling.py's _is_retryable_fetch_error) depends on this
    # attribute existing and being the real HTTP status.
    assert exc_info.value.status_code == status_code


def test_coa_api_error_message_truncates_a_very_long_body():
    client = _client()
    huge_body = "x" * 5000
    with patch.object(client._session, "request") as mock_request:
        resp = _fake_response(500)
        resp.text = huge_body
        mock_request.return_value = resp
        with pytest.raises(CoaApiError) as exc_info:
            client.get("/x")

    assert len(str(exc_info.value)) < 2000


def test_empty_response_body_returns_empty_dict_not_a_json_decode_error():
    """A 202/204-style response with no body must not raise trying to .json() it."""
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        resp = _fake_response(202, content=b"")
        resp.json.side_effect = ValueError("no JSON object could be decoded")
        mock_request.return_value = resp
        result = client.post("/namespaces/ns1/sources/src1/approve")

    assert result == {}


def test_non_empty_response_body_is_parsed_as_json():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {"sourceId": "abc123", "status": "APPROVED"})
        result = client.get("/namespaces/ns1/sources/abc123")

    assert result == {"sourceId": "abc123", "status": "APPROVED"}


def test_a_non_json_2xx_body_raises_coa_api_error_not_a_raw_json_decode_error():
    """A gateway error page served with a 200 is a ``CoaApiError``, not a raw JSON decode error."""
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        resp = _fake_response(200, content=b"<html>not json</html>")
        resp.text = "<html>not json</html>"
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        mock_request.return_value = resp
        with pytest.raises(CoaApiError) as exc_info:
            client.get("/namespaces/ns1/sources")

    assert exc_info.value.status_code == 200
    assert "not json" in exc_info.value.body


def test_a_connection_error_from_requests_propagates_unmodified():
    """The poll's retry relies on this: a ``requests`` ConnectionError escapes as-is, with no status."""
    client = _client()
    with (
        patch.object(client._session, "request") as mock_request,
        pytest.raises(requests.exceptions.ConnectionError, match="simulated connection failure"),
    ):
        mock_request.side_effect = requests.exceptions.ConnectionError("simulated connection failure")
        client.get("/namespaces/ns1/sources")


def test_a_timeout_from_requests_propagates_unmodified():

    client = _client()
    with (
        patch.object(client._session, "request") as mock_request,
        pytest.raises(requests.exceptions.Timeout, match="simulated timeout"),
    ):
        mock_request.side_effect = requests.exceptions.Timeout("simulated timeout")
        client.get("/namespaces/ns1/sources")


# ── HTTP verb wrappers ────────────────────────────────────────────────────


def test_post_sends_empty_dict_body_when_none_given():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(202, {})
        client.post("/namespaces/ns1/sources/src1/approve")

    _, kwargs = mock_request.call_args
    assert kwargs["json"] == {}


def test_put_and_patch_send_the_given_body():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {})
        client.put("/namespaces/ns1/sources/s/tables/t/review", {"decision": "APPROVED"})

    args, kwargs = mock_request.call_args
    assert args[0] == "PUT"
    assert kwargs["json"] == {"decision": "APPROVED"}


def test_patch_sends_the_given_body():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {})
        client.patch("/namespaces/ns1/sources/s", {"metadataEnrichmentEnabled": False})

    args, kwargs = mock_request.call_args
    assert args[0] == "PATCH"
    assert kwargs["json"] == {"metadataEnrichmentEnabled": False}


def test_delete_sends_no_body():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {})
        client.delete("/namespaces/ns1/sources/s1")

    args, kwargs = mock_request.call_args
    assert args[0] == "DELETE"
    assert kwargs["json"] is None


# ── Session reuse ────────────────────────────────────────────────────────


def test_every_request_reuses_the_same_session_instance():
    """One ``requests.Session`` serves every call, so connections are reused."""
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {"ok": True})
        client.get("/a")
        client.get("/b")
        client.post("/c")

    assert mock_request.call_count == 3  # all three calls went through the one session


def test_close_releases_the_session_and_is_idempotent():
    client = _client()
    with patch.object(client._session, "close") as mock_close:
        client.close()
        client.close()  # must not raise on a second call

    assert mock_close.call_count == 2


def test_context_manager_closes_the_session_on_exit():
    with (
        patch("coa_workflow_agents.clients.coa_api_client.requests.Session.close") as mock_close,
        _client() as client,
    ):
        assert client is not None
    mock_close.assert_called_once()


# ── A sample of the COA-specific convenience methods, as integration glue ──


def test_approve_source_posts_to_the_right_path():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(202, {})
        client.approve_source("ns1", "src1")

    args, _ = mock_request.call_args
    assert args[0] == "POST"
    assert args[1] == "https://coa.example.com/prod/namespaces/ns1/sources/src1/approve"


def test_accept_proposal_omits_ontology_id_when_not_given():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(202, {})
        client.accept_proposal("ns1", "prop1")

    _, kwargs = mock_request.call_args
    assert kwargs["json"] == {}


def test_accept_proposal_includes_ontology_id_when_given():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(202, {})
        client.accept_proposal("ns1", "prop1", ontology_id="onto-1")

    _, kwargs = mock_request.call_args
    # AcceptProposalRequest.ontology_id has no alias: camelCase would be dropped.
    assert kwargs["json"] == {"ontology_id": "onto-1"}


def test_reject_proposals_sends_snake_case_and_returns_the_per_proposal_results():
    client = _client()
    results = [{"proposal_id": "p1", "status": "rejected"}, {"proposal_id": "p2", "error": "not found"}]
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, results)
        out = client.reject_proposals("ns1", ["p1", "p2"], "bad data")

    _, kwargs = mock_request.call_args
    # RejectRequest fields have no alias: "proposalIds" is a 422.
    assert kwargs["json"] == {"proposal_ids": ["p1", "p2"], "reason": "bad data"}
    assert out == {"results": results}


# COA's handler request models, read from source so a rename there fails here.
_PROPOSALS_PY = Path(__file__).resolve().parents[4] / "packages/ontology-engine/src/coa_ontology/proposals.py"


def _handler_model_fields(class_name: str) -> set[str]:
    """Field names of a pydantic model in ``proposals.py``; fails on an alias (the client sends field names)."""
    tree = ast.parse(_PROPOSALS_PY.read_text())
    model = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    assert model is not None, f"{class_name} not found in {_PROPOSALS_PY}: the request-body contract moved"
    fields = {n.target.id for n in model.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    assert "alias" not in ast.unparse(model), f"{class_name} now has an alias: send the alias instead"
    return fields


def test_proposal_request_bodies_use_the_handler_model_field_names():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, [])
        client.reject_proposals("ns1", ["p1"], "r")
        reject_body = mock_request.call_args.kwargs["json"]
        client.accept_proposal("ns1", "p1", ontology_id="onto-1")
        accept_body = mock_request.call_args.kwargs["json"]

    # Every RejectRequest field is required; AcceptProposalRequest has optional ones the client may leave out.
    assert set(reject_body) == _handler_model_fields("RejectRequest")
    assert accept_body and set(accept_body) <= _handler_model_fields("AcceptProposalRequest")


# ── Query string encoding ────────────────────────────────────────────────


def test_query_string_url_encodes_special_characters():

    q = _query_string({"nextToken": "tok&with=special?chars#here"})
    assert q == "?nextToken=tok%26with%3Dspecial%3Fchars%23here"


def test_query_string_omits_none_and_empty_values():

    assert _query_string({"a": None, "b": ""}) == ""
    assert _query_string({"a": None, "b": "x"}) == "?b=x"


def test_query_string_returns_empty_string_for_no_params():

    assert _query_string({}) == ""


def test_list_induction_jobs_with_a_malicious_looking_status_value_is_safely_encoded():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {"jobs": []})
        client.list_induction_jobs("ns1", status="completed&foo=bar")

    args, _ = mock_request.call_args
    url = args[1]
    # The injected '&foo=bar' must be percent-encoded as part of the status
    # value, not interpreted as a second query parameter.
    assert "foo=bar" not in url or "status=completed%26foo%3Dbar" in url
    assert "status=completed%26foo%3Dbar" in url


# ── Each method's (method, path) ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("call", "expected_method", "expected_path"),
    [
        (lambda c: c.create_source("ns1", {"sourceType": "DATABASE"}), "POST", "/namespaces/ns1/sources"),
        (lambda c: c.get_source("ns1", "src1"), "GET", "/namespaces/ns1/sources/src1"),
        (lambda c: c.list_sources("ns1"), "GET", "/namespaces/ns1/sources?maxResults=50"),
        (
            lambda c: c.list_source_tables("ns1", "src1"),
            "GET",
            "/namespaces/ns1/sources/src1/tables?maxResults=50",
        ),
        (
            lambda c: c.get_source_table("ns1", "src1", "tbl1"),
            "GET",
            "/namespaces/ns1/sources/src1/tables/tbl1",
        ),
        (
            lambda c: c.review_source_table("ns1", "src1", "tbl1", "APPROVED"),
            "PUT",
            "/namespaces/ns1/sources/src1/tables/tbl1/review",
        ),
        (
            lambda c: c.review_source_column("ns1", "src1", "tbl1", "col1", "APPROVED"),
            "PUT",
            "/namespaces/ns1/sources/src1/tables/tbl1/columns/col1/review",
        ),
        (
            lambda c: c.update_source_table_keys(
                "ns1", "src1", "tbl1", {"foreignKeys": [{"column": "a", "targetTable": "t"}]}
            ),
            "PATCH",
            "/namespaces/ns1/sources/src1/tables/tbl1/keys",
        ),
        (
            lambda c: c.update_source_table_metadata("ns1", "src1", "tbl1", {"description": "d"}),
            "PATCH",
            "/namespaces/ns1/sources/src1/tables/tbl1/metadata",
        ),
        (
            lambda c: c.update_source_column_metadata("ns1", "src1", "tbl1", "col1", {"description": "d"}),
            "PATCH",
            "/namespaces/ns1/sources/src1/tables/tbl1/columns/col1/metadata",
        ),
        (lambda c: c.reject_source("ns1", "src1"), "POST", "/namespaces/ns1/sources/src1/reject"),
        (lambda c: c.start_induction("ns1", {}), "POST", "/namespaces/ns1/induce"),
        (lambda c: c.get_induction_job("ns1", "job1"), "GET", "/namespaces/ns1/induce/jobs/job1"),
        (lambda c: c.list_induction_jobs("ns1"), "GET", "/namespaces/ns1/induce/jobs?maxResults=50"),
        (lambda c: c.get_proposal("ns1", "prop1"), "GET", "/namespaces/ns1/proposals/prop1"),
        (lambda c: c.list_proposals("ns1"), "GET", "/namespaces/ns1/proposals?maxResults=50"),
        (
            lambda c: c.validate_proposal("ns1", "prop1"),
            "POST",
            "/namespaces/ns1/proposals/prop1/validate",
        ),
        (
            lambda c: c.get_proposal_validation_job("ns1", "prop1", "job1"),
            "GET",
            "/namespaces/ns1/proposals/prop1/validate/jobs/job1",
        ),
        (
            lambda c: c.reject_proposals("ns1", ["prop1", "prop2"], "bad data"),
            "POST",
            "/namespaces/ns1/proposals/reject",
        ),
        (
            lambda c: c.get_source_scan_job("ns1", "src1", "job1"),
            "GET",
            "/namespaces/ns1/sources/src1/scan/job1",
        ),
        (lambda c: c.rescan_source("ns1", "src1"), "POST", "/namespaces/ns1/sources/src1/rescan"),
        (lambda c: c.get_ontology("ns1", "urn:o"), "GET", "/namespaces/ns1/ontologies/urn%3Ao"),
        (lambda c: c.get_ontology_download_url("ns1", "o1"), "GET", "/namespaces/ns1/ontologies/o1/download"),
        (lambda c: c.fetch_ontology("ns1", "o1"), "POST", "/namespaces/ns1/ontologies/o1/fetch"),
        (lambda c: c.list_foundational_ontologies("ns1"), "GET", "/namespaces/ns1/ontology/foundational"),
        (lambda c: c.describe_schema("ns1"), "GET", "/namespaces/ns1/schema?includeProperties=false"),
        (lambda c: c.get_namespace("ns1"), "GET", "/namespaces/ns1"),
        (lambda c: c.list_namespaces(), "GET", "/namespaces?maxResults=100"),
        (lambda c: c.update_namespace("ns1", {"displayName": "d"}), "PUT", "/namespaces/ns1"),
        (lambda c: c.update_namespace_status("ns1", "ARCHIVED"), "PATCH", "/namespaces/ns1/status"),
        (lambda c: c.delete_namespace("ns1"), "DELETE", "/namespaces/ns1"),
        (lambda c: c.health_check(), "GET", "/health"),
        (lambda c: c.get_system_health(), "GET", "/system-health"),
        (lambda c: c.list_platform_roles(), "GET", "/roles"),
        (lambda c: c.list_namespace_roles("ns1"), "GET", "/namespaces/ns1/roles"),
        (lambda c: c.get_namespace_role("ns1", "r1"), "GET", "/namespaces/ns1/roles/r1"),
        (lambda c: c.list_namespace_grants("ns1"), "GET", "/namespaces/ns1/grants?maxResults=100"),
        (lambda c: c.delete_grant("ns1", "g1"), "DELETE", "/namespaces/ns1/grants/g1"),
        (lambda c: c.list_my_grants(), "GET", "/principals/me/grants?maxResults=1000"),
        (lambda c: c.list_platform_grants(), "GET", "/grants?maxResults=100"),
        (lambda c: c.delete_platform_grant("g1"), "DELETE", "/grants/g1"),
        (lambda c: c.get_metric("ns1", "churn"), "GET", "/namespaces/ns1/metrics/churn"),
        (lambda c: c.list_metrics("ns1"), "GET", "/namespaces/ns1/metrics?maxResults=200"),
        (lambda c: c.update_metric("ns1", "churn", {}), "PUT", "/namespaces/ns1/metrics/churn"),
        (lambda c: c.delete_metric("ns1", "churn"), "DELETE", "/namespaces/ns1/metrics/churn"),
        (lambda c: c.validate_metric("ns1", {}), "POST", "/namespaces/ns1/metrics/validate"),
        (lambda c: c.get_import_job("ns1", "j1"), "GET", "/namespaces/ns1/import-jobs/j1"),
        (lambda c: c.export_osi("ns1", names=["a"]), "GET", "/namespaces/ns1/export-osi?format=inline&names=a"),
    ],
)
def test_client_method_builds_the_expected_method_and_path(call, expected_method, expected_path):
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200 if expected_method == "GET" else 202, {})
        call(client)

    args, _ = mock_request.call_args
    assert args[0] == expected_method
    assert args[1] == f"https://coa.example.com/prod/{expected_path.lstrip('/')}"


def test_path_segments_are_url_encoded_not_interpolated_raw():
    """A column name containing '/' (or other reserved chars) must be
    percent-encoded into ONE path segment, not split into extra segments that
    route the PATCH to a different COA resource (CWE-116)."""
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(202, {})
        client.update_source_column_metadata("ns1", "src1", "tbl1", "weird/col#x", {"description": "d"})

    args, _ = mock_request.call_args
    url = args[1]
    # the slash and hash in the column name are encoded, not left as path/fragment
    assert "columns/weird%2Fcol%23x/metadata" in url
    assert "weird/col" not in url


# ── rescan_source / get_source_scan_job ──────────────────────────────────


def test_rescan_source_omits_confirm_discard_open_review_when_not_given():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(202, {})
        client.rescan_source("ns1", "src1")

    _, kwargs = mock_request.call_args
    assert kwargs["json"] == {}


def test_rescan_source_includes_confirm_discard_open_review_when_given():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(202, {})
        client.rescan_source("ns1", "src1", confirm_discard_open_review=True)

    _, kwargs = mock_request.call_args
    assert kwargs["json"] == {"confirmDiscardOpenReview": True}


def test_get_source_scan_job_returns_the_error_message_on_a_failed_scan():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(
            200,
            {
                "jobId": "job1",
                "sourceId": "src1",
                "status": "FAILED",
                "errorMessage": "Network: cannot reach db.example.com:1433 — timed out",
            },
        )
        result = client.get_source_scan_job("ns1", "src1", "job1")

    assert result["errorMessage"] == "Network: cannot reach db.example.com:1433 — timed out"


# ── clientToken retry (post_idempotent) ─────────────────────────────────


@pytest.fixture
def no_backoff(monkeypatch):
    """Record the retry backoff sleeps instead of actually sleeping."""
    sleeps: list[float] = []
    monkeypatch.setattr("coa_workflow_agents.clients.coa_api_client.time.sleep", sleeps.append)
    return sleeps


def _error_response(status_code: int):
    resp = _fake_response(status_code, content=b'{"message": "unavailable"}')
    resp.text = '{"message": "unavailable"}'
    return resp


@pytest.mark.parametrize(
    ("call", "path", "expected_body"),
    [
        (
            lambda c: c.create_source("ns1", {"sourceType": "DATABASE"}, client_token="tok"),
            "/namespaces/ns1/sources",
            {"sourceType": "DATABASE", "clientToken": "tok"},
        ),
        (
            lambda c: c.rescan_source("ns1", "src1", client_token="tok"),
            "/namespaces/ns1/sources/src1/rescan",
            {"clientToken": "tok"},
        ),
        (
            lambda c: c.rescan_source("ns1", "src1", confirm_discard_open_review=True, client_token="tok"),
            "/namespaces/ns1/sources/src1/rescan",
            {"confirmDiscardOpenReview": True, "clientToken": "tok"},
        ),
        (
            lambda c: c.start_induction("ns1", {"strategy": "x"}, client_token="tok"),
            "/namespaces/ns1/induce",
            {"strategy": "x", "clientToken": "tok"},
        ),
        (
            lambda c: c.validate_proposal("ns1", "p1", client_token="tok"),
            "/namespaces/ns1/proposals/p1/validate",
            {"clientToken": "tok"},
        ),
        (
            lambda c: c.infer_constraints("ns1", "p1", client_token="tok"),
            "/namespaces/ns1/proposals/p1/infer-constraints",
            {"clientToken": "tok"},
        ),
        (
            lambda c: c.ingest_ontology_from_s3("ns1", "o1", {"s3Key": "k"}, client_token="tok"),
            "/namespaces/ns1/ontologies/o1/ingest-from-s3",
            {"s3Key": "k", "clientToken": "tok"},
        ),
        (
            lambda c: c.load_foundational_ontology("ns1", "fibo", client_token="tok"),
            "/namespaces/ns1/ontology/foundational/fibo/load",
            {"clientToken": "tok"},
        ),
        (
            lambda c: c.create_namespace({"name": "n"}, client_token="tok"),
            "/namespaces",
            {"name": "n", "clientToken": "tok"},
        ),
        (
            lambda c: c.create_grant("ns1", {"principalId": "a@example.com"}, client_token="tok"),
            "/namespaces/ns1/grants",
            {"principalId": "a@example.com", "clientToken": "tok"},
        ),
        (
            lambda c: c.create_platform_grant({"role": "platform-viewer"}, client_token="tok"),
            "/grants",
            {"role": "platform-viewer", "clientToken": "tok"},
        ),
        (
            lambda c: c.create_metric("ns1", {"name": "churn"}, client_token="tok"),
            "/namespaces/ns1/metrics",
            {"name": "churn", "clientToken": "tok"},
        ),
        (
            lambda c: c.import_osi("ns1", {"s3Key": "k"}, client_token="tok"),
            "/namespaces/ns1/import-osi",
            {"s3Key": "k", "clientToken": "tok"},
        ),
    ],
)
def test_client_token_is_added_to_the_full_body_of_each_retry_safe_post(call, path, expected_body):
    """The exact body: the token is ADDED, the request's own fields are kept."""
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(202, {"ok": True})
        call(client)

    args, kwargs = mock_request.call_args
    assert args[1].endswith(path)
    assert kwargs["json"] == expected_body


def test_no_client_token_sends_one_plain_post_without_the_field(no_backoff):

    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.side_effect = requests.exceptions.Timeout("simulated timeout")
        with pytest.raises(requests.exceptions.Timeout):
            client.start_induction("ns1", {"strategy": "x"})

    assert mock_request.call_count == 1  # no retry without a token
    assert "clientToken" not in mock_request.call_args.kwargs["json"]
    assert no_backoff == []


@pytest.mark.parametrize("transient", ["timeout", "connection", "503"])
def test_post_idempotent_retries_a_transient_failure_with_the_same_token(transient, no_backoff):

    first: object
    if transient == "timeout":
        first = requests.exceptions.Timeout("simulated timeout")
    elif transient == "connection":
        first = requests.exceptions.ConnectionError("simulated connection failure")
    else:
        first = _error_response(503)

    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.side_effect = [first, _fake_response(202, {"job_id": "j1"})]
        result = client.start_induction("ns1", {"strategy": "x"}, client_token="tok")

    assert result == {"job_id": "j1"}
    assert mock_request.call_count == 2
    assert [c.kwargs["json"]["clientToken"] for c in mock_request.call_args_list] == ["tok", "tok"]
    assert no_backoff == [1.0]


@pytest.mark.parametrize("failure", ["503", "timeout", "connection"])
def test_post_idempotent_gives_up_after_three_attempts_and_raises_the_last_error(failure, no_backoff):

    def make():
        if failure == "timeout":
            return requests.exceptions.Timeout("simulated timeout")
        if failure == "connection":
            return requests.exceptions.ConnectionError("simulated connection failure")
        return _error_response(503)

    expected = {"503": CoaApiError, "timeout": requests.exceptions.Timeout}.get(
        failure, requests.exceptions.ConnectionError
    )
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.side_effect = [make(), make(), make()]
        with pytest.raises(expected):
            client.rescan_source("ns1", "src1", client_token="tok")

    assert mock_request.call_count == 3
    assert no_backoff == [1.0, 2.0]  # linear backoff between the three attempts


def _in_progress_409():
    # COA's idempotency wrapper text; the sources handler puts it under "error".
    resp = _fake_response(409, content=b"x")
    resp.text = '{"error": "a request with this clientToken is still in progress"}'
    return resp


def test_post_idempotent_waits_out_an_in_progress_conflict_after_a_lost_response(no_backoff):
    """The case the retry exists for: the first send reached COA but the
    response was lost. The retry lands while COA still runs it (409 in
    progress); waiting then gets the replayed response instead of failing a
    scan or job that did start."""

    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.side_effect = [
            requests.exceptions.ConnectionError("reset"),
            _in_progress_409(),
            _fake_response(202, {"job_id": "j1"}),
        ]
        result = client.start_induction("ns1", {"strategy": "x"}, client_token="tok")

    assert result == {"job_id": "j1"}
    assert [c.kwargs["json"]["clientToken"] for c in mock_request.call_args_list] == ["tok"] * 3
    assert no_backoff == [1.0, 5.0]


def test_post_idempotent_gives_up_on_an_in_progress_conflict_after_six_waits(no_backoff):
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.side_effect = [_in_progress_409() for _ in range(7)]
        with pytest.raises(CoaApiError) as exc_info:
            client.create_source("ns1", {"sourceType": "DATABASE"}, client_token="tok")

    assert exc_info.value.status_code == 409
    assert mock_request.call_count == 7
    assert no_backoff == [5.0] * 6


def test_post_idempotent_does_not_retry_an_ssl_error(no_backoff):
    """SSLError is a ConnectionError subclass but permanent."""

    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.side_effect = requests.exceptions.SSLError("bad certificate")
        with pytest.raises(requests.exceptions.SSLError):
            client.create_source("ns1", {"sourceType": "DATABASE"}, client_token="tok")

    assert mock_request.call_count == 1
    assert no_backoff == []


@pytest.mark.parametrize("status_code", [400, 409, 500, 504])
def test_post_idempotent_does_not_retry_other_errors(status_code, no_backoff):
    """504 especially: COA releases the token's claim on any non-2xx and the
    backend may already have started the job, so a retry could duplicate it."""
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _error_response(status_code)
        with pytest.raises(CoaApiError):
            client.create_source("ns1", {"sourceType": "DATABASE"}, client_token="tok")

    assert mock_request.call_count == 1
    assert no_backoff == []


def test_list_proposals_sends_paging_and_filter_params():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {"items": []})
        client.list_proposals("ns1", status="pending", next_token="t 2", max_results=200)

    url = mock_request.call_args.args[1]
    assert url.endswith("/namespaces/ns1/proposals?maxResults=200&status=pending&nextToken=t+2")


# ── delete / cancel / ontology routes ───────────────────────────────────


@pytest.mark.parametrize(
    ("call", "method", "path"),
    [
        (lambda c: c.delete_source("ns1", "s1"), "DELETE", "/namespaces/ns1/sources/s1"),
        (lambda c: c.cancel_proposal("ns1", "p1"), "POST", "/namespaces/ns1/proposals/p1/cancel"),
        (
            lambda c: c.delete_ontology("ns1", "http://x.org/o#"),
            "DELETE",
            "/namespaces/ns1/ontologies?ontology_id=http%3A%2F%2Fx.org%2Fo%23",
        ),
        (
            lambda c: c.get_ontology("ns1", "http://x.org/o#"),
            "GET",
            "/namespaces/ns1/ontologies/http%3A%2F%2Fx.org%2Fo%23",
        ),
        (
            lambda c: c.list_ontologies("ns1", ontology_type="induced", next_token="t"),
            "GET",
            "/namespaces/ns1/ontologies?maxResults=100&ontology_type=induced&nextToken=t",
        ),
        (
            lambda c: c.get_graph_vertex("ns1", "object-property", "http://x.org/o#p"),
            "GET",
            "/namespaces/ns1/graph/object-property?uri=http%3A%2F%2Fx.org%2Fo%23p",
        ),
        (
            lambda c: c.get_ontology_overview("ns1", "urn:o", limit=10, sample=True),
            "GET",
            "/namespaces/ns1/graph/ontology-overview?ontology_id=urn%3Ao&limit=10&sample=true",
        ),
        (
            lambda c: c.describe_schema("ns1", include_properties=True),
            "GET",
            "/namespaces/ns1/schema?includeProperties=true",
        ),
        (
            lambda c: c.load_foundational_ontology("ns1", "schema-org"),
            "POST",
            "/namespaces/ns1/ontology/foundational/schema-org/load",
        ),
    ],
)
def test_ontology_and_delete_routes(call, method, path):
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {})
        call(client)
    args, _ = mock_request.call_args
    assert args[0] == method
    assert args[1] == f"https://coa.example.com/prod{path}"


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_delete_ontology_never_sends_a_request_without_an_id(blank):
    """COA's DELETE /ontologies WITHOUT ontology_id wipes the whole namespace."""
    client = _client()
    with patch.object(client._session, "request") as mock_request, pytest.raises(ValueError, match="non-blank"):
        client.delete_ontology("ns1", blank)  # type: ignore[arg-type]
    mock_request.assert_not_called()


def test_get_graph_vertex_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="unknown graph vertex kind"):
        _client().get_graph_vertex("ns1", "ontology", "http://x.org/o")


def test_put_presigned_sends_no_bearer_token_and_redacts_the_url_on_failure():
    client = _client()
    with patch.object(client._session, "put") as mock_put:
        mock_put.return_value = _fake_response(403, content=b"denied")
        with pytest.raises(CoaApiError) as exc_info:
            client.put_presigned("https://b.s3.amazonaws.com/k?X-Amz-Signature=secret", b"x", "text/turtle")
    kwargs = mock_put.call_args.kwargs
    assert kwargs["headers"] == {"Content-Type": "text/turtle"}
    assert "secret" not in str(exc_info.value)
    assert client.token_provider.calls == 0


def test_presigned_transfers_refuse_a_non_https_url():
    client = _client()
    with pytest.raises(ValueError, match="https"):
        client.put_presigned("http://b/k", b"x", "text/turtle")
    with pytest.raises(ValueError, match="https"):
        client.download_presigned("http://b/k", max_bytes=10)


def test_download_presigned_enforces_the_size_limit():
    client = _client()
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_content.return_value = [b"12345", b"67890"]
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: None
    with patch.object(client._session, "get", return_value=resp):
        with pytest.raises(ValueError, match="exceeds"):
            client.download_presigned("https://b/k", max_bytes=8)
        assert client.download_presigned("https://b/k", max_bytes=10) == b"1234567890"


def test_presigned_transfers_suppress_netrc_and_send_no_bearer():
    """requests falls back to ~/.netrc credentials when no auth is given; the
    no-op auth hook stops a Basic header going to the S3 host."""

    client = _client()
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_content.return_value = [b"ok"]
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: None
    with patch.object(client._session, "get", return_value=resp) as mock_get:
        client.download_presigned("https://b.s3.amazonaws.com/k?sig", max_bytes=10)
    kwargs = mock_get.call_args.kwargs
    assert kwargs["auth"] is _no_auth and kwargs["stream"] is True
    assert "headers" not in kwargs or "Authorization" not in (kwargs["headers"] or {})
    with patch.object(client._session, "put") as mock_put:
        mock_put.return_value = _fake_response(200, {})
        client.put_presigned("https://b.s3.amazonaws.com/k?sig", b"x", "text/turtle")
    assert mock_put.call_args.kwargs["auth"] is _no_auth
    assert client.token_provider.calls == 0


def test_presigned_failures_report_only_the_s3_error_code():
    """An S3 error body can echo the signed request (StringToSign, signature)."""
    client = _client()
    body = (
        "<Error><Code>SignatureDoesNotMatch</Code><StringToSign>AWS4-HMAC-SHA256 secret-scope</StringToSign>"
        "<SignatureProvided>deadbeef</SignatureProvided></Error>"
    )
    resp = MagicMock()
    resp.status_code = 403
    resp.text = body
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: None
    with patch.object(client._session, "get", return_value=resp), pytest.raises(CoaApiError) as exc_info:
        client.download_presigned("https://b.s3.amazonaws.com/k?X-Amz-Signature=deadbeef", max_bytes=10)
    message = str(exc_info.value)
    assert "SignatureDoesNotMatch" in message
    assert "deadbeef" not in message and "secret-scope" not in message


@pytest.mark.parametrize(
    ("call", "path"),
    [
        (
            lambda c: c.ingest_ontology_from_s3("ns1", "urn:o", {"s3Key": "k"}, client_token="tok"),
            "/namespaces/ns1/ontologies/urn%3Ao/ingest-from-s3",
        ),
        (
            lambda c: c.load_foundational_ontology("ns1", "schema-org", client_token="tok"),
            "/namespaces/ns1/ontology/foundational/schema-org/load",
        ),
    ],
)
def test_new_token_operations_retry_a_503_with_the_same_token(call, path, no_backoff):
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.side_effect = [_error_response(503), _fake_response(202, {"result": {}})]
        call(client)
    assert mock_request.call_count == 2
    assert all(c.args[1].endswith(path) for c in mock_request.call_args_list)
    assert [c.kwargs["json"]["clientToken"] for c in mock_request.call_args_list] == ["tok", "tok"]


@pytest.mark.parametrize(
    ("call", "method", "path", "body"),
    [
        (
            lambda c: c.get_ontology_upload_url("ns1", filename="o.ttl", content_type="text/turtle", title="T"),
            "POST",
            "/namespaces/ns1/ontologies/upload-url",
            {"filename": "o.ttl", "contentType": "text/turtle", "title": "T"},
        ),
        (
            lambda c: c.fetch_ontology("ns1", "urn:o", source_url="https://x.org/o.ttl"),
            "POST",
            "/namespaces/ns1/ontologies/urn%3Ao/fetch",
            {"sourceUrl": "https://x.org/o.ttl"},
        ),
        (
            lambda c: c.get_ingest_status("ns1", "urn:o", "j1"),
            "GET",
            "/namespaces/ns1/ontologies/urn%3Ao/ingest-status/j1",
            None,
        ),
        (
            lambda c: c.get_ontology_download_url("ns1", "urn:o"),
            "GET",
            "/namespaces/ns1/ontologies/urn%3Ao/download",
            None,
        ),
        (lambda c: c.list_foundational_ontologies("ns1"), "GET", "/namespaces/ns1/ontology/foundational", None),
        (
            lambda c: c.graph_search("ns1", "order", kind="class", limit=5, offset=0),
            "GET",
            "/namespaces/ns1/graph/search?q=order&kind=class&limit=5&offset=0",
            None,
        ),
    ],
)
def test_remaining_ontology_routes_and_bodies(call, method, path, body):
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {})
        call(client)
    args, kwargs = mock_request.call_args
    assert args[0] == method
    assert args[1] == f"https://coa.example.com/prod{path}"
    if body is not None:
        assert kwargs["json"] == body


@pytest.mark.parametrize("method", ["put", "get"])
def test_presigned_transport_errors_never_echo_the_signed_url(method):
    """requests' ConnectionError message embeds the URL, signature included."""

    client = _client()
    url = "https://b.s3.amazonaws.com/k?X-Amz-Signature=SECRETSIG"
    with patch.object(client._session, method) as mock_call:
        mock_call.side_effect = requests.exceptions.ConnectionError(f"Max retries exceeded with url: {url}")
        with pytest.raises(CoaApiError) as exc_info:
            if method == "put":
                client.put_presigned(url, b"x", "text/turtle")
            else:
                client.download_presigned(url, max_bytes=10)
    assert "SECRETSIG" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__
    assert mock_call.call_args.kwargs["allow_redirects"] is False


def test_s3_error_code_falls_back_for_a_non_xml_body():

    assert _s3_error_code("<html>oops ?X-Amz-Signature=abc</html>") == "S3 error"
    assert _s3_error_code("") == "S3 error"


def test_create_source_reports_the_final_responses_headers_lower_cased(no_backoff):
    """headers_out carries the last attempt's headers only, keys lower-cased, so
    the tool can read COA's X-Idempotent-Replay after a transient retry."""
    first = _fake_response(503, content=b"x")
    first.headers = {"X-First": "1"}
    replay = _fake_response(201, {"sourceId": "src1"})
    replay.headers = {"X-Idempotent-Replay": "true", "Content-Type": "application/json"}
    client = _client()
    headers: dict[str, str] = {}
    with patch.object(client._session, "request") as mock_request:
        mock_request.side_effect = [first, replay]
        result = client.create_source("ns1", {"sourceType": "DATABASE"}, client_token="tok", headers_out=headers)

    assert result == {"sourceId": "src1"}
    assert headers == {"x-idempotent-replay": "true", "content-type": "application/json"}


def test_create_source_reports_headers_without_a_client_token_too():
    resp = _fake_response(201, {"sourceId": "src1"})
    resp.headers = {"X-Idempotent-Replay": "false"}
    client = _client()
    headers: dict[str, str] = {}
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = resp
        client.create_source("ns1", {"sourceType": "DATABASE"}, headers_out=headers)

    assert headers == {"x-idempotent-replay": "false"}
    assert "clientToken" not in mock_request.call_args.kwargs["json"]


@pytest.mark.parametrize(
    ("chunks", "limit", "expected"),
    [
        ([b"abc", b"def"], 4, (b"abcd", True)),  # cut inside the second chunk
        ([b"abc", b"def"], 6, (b"abcdef", False)),  # exactly the limit is not truncated
        ([b"abc"], 3, (b"abc", False)),
        ([b"abc", b"d"], 3, (b"abc", True)),  # more data after a chunk that fills it exactly
    ],
)
def test_download_presigned_prefix_returns_at_most_the_limit_and_says_if_more_remained(chunks, limit, expected):
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_content.return_value = iter(chunks)
    resp.__enter__.return_value = resp
    client = _client()
    with patch.object(client._session, "get", return_value=resp) as mock_get:
        result = client.download_presigned_prefix("https://b.s3.amazonaws.com/k?X-Amz-Signature=s", max_bytes=limit)

    assert result == expected
    _, kwargs = mock_get.call_args
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    # Never pull more than needed off the wire: a chunk is at most limit + 1 bytes.
    assert resp.iter_content.call_args.kwargs["chunk_size"] == limit + 1


@pytest.mark.parametrize(
    ("call", "method", "path", "body"),
    [
        (
            lambda c: c.update_source_metadata("ns1", "src1", {"name": "n"}),
            "PUT",
            "/namespaces/ns1/sources/src1/metadata",
            {"name": "n"},
        ),
        (
            lambda c: c.keep_rescan_removal("ns1", "src1", "db.t"),
            "PUT",
            "/namespaces/ns1/sources/src1/tables/db.t/keep",
            {},
        ),
        (
            lambda c: c.keep_rescan_removal("ns1", "src1", "db.t", column_name="c1"),
            "PUT",
            "/namespaces/ns1/sources/src1/tables/db.t/keep",
            {"columnName": "c1"},
        ),
        (
            lambda c: c.put_source_rescan_schedule("ns1", "src1", {"enabled": False}),
            "PUT",
            "/namespaces/ns1/sources/src1/rescan-schedule",
            {"enabled": False},
        ),
        (
            lambda c: c.put_source_event_rescan("ns1", "src1", enabled=False),
            "PUT",
            "/namespaces/ns1/sources/src1/event-rescan",
            {"enabled": False},  # an explicit false, never omitted
        ),
    ],
)
def test_source_lifecycle_routes_and_bodies(call, method, path, body):
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {})
        call(client)

    args, kwargs = mock_request.call_args
    assert args[0] == method
    assert args[1] == "https://coa.example.com/prod" + path
    assert kwargs["json"] == body


def test_list_source_scan_jobs_sends_paging_query():
    client = _client()
    with patch.object(client._session, "request") as mock_request:
        mock_request.return_value = _fake_response(200, {"items": []})
        client.list_source_scan_jobs("ns1", "src1", next_token="t+1", max_results=5)

    args, _ = mock_request.call_args
    assert args[1] == "https://coa.example.com/prod/namespaces/ns1/sources/src1/scan?maxResults=5&nextToken=t%2B1"


@pytest.mark.parametrize("column", ["", "  "])
def test_keep_rescan_removal_refuses_a_blank_column_which_coa_would_widen_to_the_whole_table(column):
    client = _client()
    with patch.object(client._session, "request") as mock_request, pytest.raises(ValueError, match="blank"):
        client.keep_rescan_removal("ns1", "src1", "db.t", column_name=column)
    mock_request.assert_not_called()


def test_client_session_is_not_a_constructor_argument_and_fields_are_frozen():
    with pytest.raises(TypeError):
        CoaApiClient(base_url="https://x", token_provider=MagicMock(), _session=requests.Session())  # type: ignore[call-arg]
    client = _client()
    assert "_session" not in repr(client)
    with pytest.raises(dataclasses.FrozenInstanceError):
        client.base_url = "https://elsewhere"  # type: ignore[misc]


# ── Request bodies the handlers read by their own field names ────────────


def _sent_body(call) -> dict:
    client = _client()
    with patch.object(client._session, "request", return_value=_fake_response(200, {})) as mock_request:
        call(client)
    return mock_request.call_args.kwargs["json"]


def test_repair_proposal_datatypes_sends_dry_run_snake_case():
    assert _sent_body(lambda c: c.repair_proposal_datatypes("ns1", "p1", dry_run=True)) == {"dry_run": True}


def test_validate_proposal_sends_its_optional_fields_snake_case():
    body = _sent_body(
        lambda c: c.validate_proposal(
            "ns1", "p1", shacl_shapes_turtle="@prefix", tiers=["T1"], competency_questions=["q"]
        )
    )
    assert body == {"shacl_shapes_turtle": "@prefix", "tiers": ["T1"], "competency_questions": ["q"]}


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda c: c.bulk_delete_metrics_by_name("ns1", ["a", "b"]), {"names": ["a", "b"]}),
        (lambda c: c.bulk_delete_metrics("ns1", criteria={"sourceTable": "t"}), {"filter": {"sourceTable": "t"}}),
        (lambda c: c.bulk_delete_metrics("ns1", delete_all=True), {"deleteAll": True}),
    ],
)
def test_bulk_delete_metrics_sends_exactly_one_selector(call, expected):
    assert _sent_body(call) == expected


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.bulk_delete_metrics_by_name("ns1", []),
        lambda c: c.bulk_delete_metrics_by_name("ns1", ["a", ""]),
        lambda c: c.bulk_delete_metrics("ns1"),
        lambda c: c.bulk_delete_metrics("ns1", criteria={"sourceTable": "t"}, delete_all=True),
        lambda c: c.bulk_delete_metrics("ns1", criteria={"sourceTable": ""}),
    ],
)
def test_bulk_delete_metrics_refuses_an_ambiguous_selection_before_sending(call):
    client = _client()
    with patch.object(client._session, "request") as mock_request, pytest.raises(ValueError):
        call(client)
    mock_request.assert_not_called()


def test_is_client_token_conflict_matches_only_the_token_409():
    assert is_client_token_conflict(CoaApiError("POST", "/x", 409, '{"message": "clientToken is still in progress"}'))
    assert not is_client_token_conflict(CoaApiError("POST", "/x", 409, '{"message": "source is approving"}'))
    assert not is_client_token_conflict(CoaApiError("POST", "/x", 400, "clientToken"))
    assert not is_client_token_conflict(ValueError("clientToken"))


# ── Logged routes ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/namespaces/2e6136a3-1111-4111-8111-111111111111/sources/abc123/approve",
            "/namespaces/{id}/sources/{id}/approve",
        ),
        ("/namespaces", "/namespaces"),
        ("/namespaces/ns1/induce/jobs?maxResults=10&nextToken=SECRET", "/namespaces/{id}/induce/jobs"),
        # Not decoded: an encoded "/" stays inside its one id segment.
        ("/namespaces/ns1/sources/s%2F1/tables/Orders", "/namespaces/{id}/sources/{id}/tables/{id}"),
        ("/grants/user%40example.com", "/grants/{id}"),
        # Lower-case names from the call are ids too, not route words.
        ("/namespaces/n1/metrics/churn-rate", "/namespaces/{id}/metrics/{id}"),
        (
            "/namespaces/n1/sources/s1/tables/orders/columns/email/review",
            "/namespaces/{id}/sources/{id}/tables/{id}/columns/{id}/review",
        ),
        ("/namespaces/n1/graph/object-property?uri=x", "/namespaces/{id}/graph/object-property"),
        ("", ""),
    ],
)
def test_sanitized_route_replaces_id_segments(path, expected):
    assert sanitized_route(path) == expected


def _client_path_words() -> set[str]:
    """Every literal word in the client's request paths (string constants starting with ``/``)."""
    tree = ast.parse(Path(coa_api_client.__file__).read_text())
    words: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/"):
            words.update(seg for seg in node.value.split("?", 1)[0].split("/") if seg)
    return words


def test_route_words_match_client_path_literals_exactly():
    """Both ways: a missing word would log as ``{id}``, a word no path uses could keep an id."""
    assert _client_path_words() | coa_api_client._GRAPH_VERTEX_ROUTES == coa_api_client._ROUTE_WORDS


# ── Ranged presigned reads ───────────────────────────────────────────────

_SIGNED_URL = "https://bucket.s3.amazonaws.com/key?X-Amz-Signature=abc"


def _range_response(status: int, chunks: list[bytes]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.iter_content.return_value = iter(chunks)
    resp.__enter__.return_value = resp
    return resp


def test_download_presigned_prefix_sends_a_range_and_reads_a_206():
    client = _client()
    with patch.object(client._session, "get", return_value=_range_response(206, [b"abcd"])) as mock_get:
        assert client.download_presigned_prefix(_SIGNED_URL, 3, offset=10) == (b"abc", True)
    assert mock_get.call_args.kwargs["headers"] == {"Range": "bytes=10-13"}


def test_download_presigned_prefix_skips_forward_when_the_server_ignores_the_range():
    client = _client()
    with patch.object(client._session, "get", return_value=_range_response(200, [b"0123", b"4567", b"89"])):
        assert client.download_presigned_prefix(_SIGNED_URL, 3, offset=5) == (b"567", True)


def test_download_presigned_prefix_past_the_end_is_empty_and_a_negative_offset_is_refused():
    client = _client()
    with patch.object(client._session, "get", return_value=_range_response(416, [])):
        assert client.download_presigned_prefix(_SIGNED_URL, 3, offset=99) == (b"", False)
    with pytest.raises(ValueError, match="offset"):
        client.download_presigned_prefix(_SIGNED_URL, 3, offset=-1)
