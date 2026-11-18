# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The induction tools against a scripted client: the StartInduction body on the wire, the
poll to a terminal state, COA's 409s, the bridge, and the read-only listings."""

from __future__ import annotations

import time
from enum import Enum
from typing import get_args

import pytest
from coa_control_plane_server.models.grounding_mode import GroundingMode
from coa_control_plane_server.models.induction_job_status import InductionJobStatus
from coa_control_plane_server.models.induction_strategy import InductionStrategy
from coa_workflow_agents.clients.coa_api_client import CoaApiError
from coa_workflow_agents.run_context import RunContext, reset_run, set_run
from coa_workflow_agents.tools import induction
from coa_workflow_agents.tools.induction import MUTATING_TOOL_NAMES, build_induction_tools
from coa_workflow_agents.tools.polling import PollFailureError, PollTimeoutError, RunDeadlineReachedError
from structlog.testing import capture_logs

from tests.unit.conftest import FakeCoaClient

pytestmark = pytest.mark.unit

_DEFAULT_PREFIX = "http://coa.amazon.com/namespace/ns1/ontology/induced#"
_PREFIX = "https://example.com/onto#"


@pytest.fixture(autouse=True)
def _instant_polls(monkeypatch):
    monkeypatch.setattr("coa_workflow_agents.tools.polling.time.sleep", lambda _s: None)


def _tool(client, name="start_induction"):
    return next(t for t in build_induction_tools(client, "ns1") if t.tool_name == name)


# --- the StartInduction body ---------------------------------------------------


def test_start_induction_sends_only_the_ids_and_the_derived_defaults():
    client = FakeCoaClient()
    out = _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    assert client.bodies == [
        {
            "datasourceIds": ["src1"],
            "ontologyUriPrefix": _DEFAULT_PREFIX,
            "strategy": "table_to_ontology",
            "groundingMode": "ENHANCED",
        }
    ]
    assert out["status"] == "completed"


def test_start_induction_sends_every_optional_field_given():
    client = FakeCoaClient()
    _tool(client)(
        datasource_ids=["src1"],
        ontology_uri_prefix=_PREFIX,
        strategy="rigor_ontology",
        label="my run",
        grounding_mode="STANDARD",
        grounding_ontology_ids=["onto-a", "onto-b"],
        confidence_threshold=0.75,
        rerank_max_tokens=4000,
        timeout_seconds=5,
    )
    assert client.bodies == [
        {
            "datasourceIds": ["src1"],
            "ontologyUriPrefix": _PREFIX,
            "strategy": "rigor_ontology",
            "label": "my run",
            "groundingMode": "STANDARD",
            "groundingOntologyIds": ["onto-a", "onto-b"],
            "confidenceThreshold": 0.75,
            "rerankMaxTokens": 4000,
        }
    ]


def test_start_induction_sends_a_fresh_client_token_per_call_outside_the_body():
    client = FakeCoaClient()
    _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    first, second = client.tokens
    assert first and second and first != second and len(first) <= 64
    assert all("clientToken" not in body for body in client.bodies)


def test_unstructured_strategy_accepts_no_datasources():
    client = FakeCoaClient()
    _tool(client)(strategy="unstructured_lexical_graph")
    assert client.bodies[0]["datasourceIds"] == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"strategy": "bridge_to_tables", "datasource_ids": ["ds1"]}, "omit datasource_ids"),
        ({"datasource_ids": ["ds1"], "bridge_dry_run": True}, "apply only to bridge_to_tables"),
        ({"strategy": "bridge_to_tables", "bridge_max_tables": 0}, "bridge_max_tables"),
        ({"strategy": "bridge_to_tables", "bridge_max_tables": 201}, "bridge_max_tables"),
        ({"strategy": "bridge_to_tables", "bridge_concurrency": 17}, "bridge_concurrency"),
        ({"datasource_ids": ["ds1"], "rerank_max_tokens": 0}, "rerank_max_tokens"),
        ({"datasource_ids": ["ds1"], "rerank_max_tokens": 8193}, "rerank_max_tokens"),
        ({"datasource_ids": ["ds1"], "confidence_threshold": -0.1}, "confidence_threshold"),
        ({"datasource_ids": ["ds1"], "confidence_threshold": 1.1}, "confidence_threshold"),
        ({}, "at least one approved datasource"),
        ({"strategy": "rigor_ontology", "datasource_ids": []}, "at least one approved datasource"),
        # An explicit prefix is never replaced by the default; the model refuses this one.
        ({"datasource_ids": ["ds1"], "ontology_uri_prefix": ""}, "ontologyUriPrefix|ontology_uri_prefix"),
    ],
)
def test_out_of_contract_inputs_are_refused_before_any_call(kwargs, match):
    client = FakeCoaClient()
    with pytest.raises(ValueError, match=match):
        _tool(client)(**kwargs)
    assert client.bodies == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"confidence_threshold": 0.0},
        {"confidence_threshold": 1.0},
        {"rerank_max_tokens": 1},
        {"rerank_max_tokens": 8192},
        {"strategy": "bridge_to_tables", "datasource_ids": None, "bridge_max_tables": 1},
        {"strategy": "bridge_to_tables", "datasource_ids": None, "bridge_max_tables": 200},
        {"strategy": "bridge_to_tables", "datasource_ids": None, "bridge_concurrency": 1},
        {"strategy": "bridge_to_tables", "datasource_ids": None, "bridge_concurrency": 16},
    ],
)
def test_the_inclusive_ends_of_each_range_are_accepted(kwargs):
    client = FakeCoaClient()
    _tool(client)(**{"datasource_ids": ["ds1"], **kwargs, "timeout_seconds": 5})
    assert len(client.bodies) == 1


# --- the poll ------------------------------------------------------------------


@pytest.mark.parametrize("started", [{"job_id": "job-x"}, {"jobId": "job-x"}], ids=["snake", "camel"])
def test_start_induction_polls_the_job_id_the_start_returned(started):
    client = FakeCoaClient(started=started)
    _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    assert client.polled == [("job-x", None)]


def test_start_induction_with_no_job_id_raises_without_polling():
    client = FakeCoaClient(started={"status": "accepted"})
    with pytest.raises(RuntimeError, match="no job id"):
        _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    assert client.polled == []


def test_start_induction_waits_past_running_and_returns_an_ingested_job():
    client = FakeCoaClient(jobs=[{"status": "running"}, {"status": "ingested", "proposalId": "p1"}])
    out = _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    assert out == {"status": "ingested", "proposalId": "p1"}
    assert len(client.polled) == 2


def test_start_induction_names_the_job_id_as_the_proposal_to_review():
    client = FakeCoaClient(jobs=[{"jobId": "j1", "status": "completed"}])
    assert _tool(client)(datasource_ids=["src1"], timeout_seconds=5)["proposalId"] == "j1"


def test_start_induction_names_the_accepted_proposal_a_duplicate_run_matched():
    client = FakeCoaClient(
        jobs=[{"job_id": "j1", "status": "completed", "proposal_id": "j1", "duplicate_of": "p-accepted"}]
    )
    out = _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    assert out["proposalId"] == "p-accepted"
    assert out["duplicate_of"] == "p-accepted"


def test_get_induction_job_reads_the_proposal_id_a_stored_job_row_carries():
    client = FakeCoaClient(jobs=[{"job_id": "j1", "status": "ingested", "proposal_id": "p-row"}])
    assert _tool(client, "get_induction_job")(job_id="j1")["proposalId"] == "p-row"


@pytest.mark.parametrize("status", ["running", "pending", "failed", "cancelled"])
def test_get_induction_job_names_no_proposal_unless_the_job_finished(status):
    client = FakeCoaClient(jobs=[{"job_id": "j1", "status": status}])
    assert "proposalId" not in _tool(client, "get_induction_job")(job_id="j1")


def test_start_induction_says_a_job_past_its_own_timeout_is_still_running_not_failed():
    client = FakeCoaClient(jobs=[{"jobId": "j1", "status": "running"}])
    with pytest.raises(RuntimeError, match="induction job j1 is still running on COA") as excinfo:
        _tool(client)(datasource_ids=["src1"], timeout_seconds=0)
    assert not isinstance(excinfo.value, PollTimeoutError)
    assert "do not start another" in str(excinfo.value)


def test_start_induction_leaves_a_run_deadline_cut_to_the_still_running_hook():
    client = FakeCoaClient(jobs=[{"jobId": "j1", "status": "running"}])
    token = set_run(RunContext(deadline=time.monotonic() + 1.0, progress=lambda _m: None))
    try:
        with pytest.raises(RunDeadlineReachedError):
            _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    finally:
        reset_run(token)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_start_induction_logs_a_failed_or_cancelled_job_as_poll_job_failed(status):
    client = FakeCoaClient(jobs=[{"status": status}])
    with capture_logs() as logs, pytest.raises(PollFailureError):
        _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    assert [e for e in logs if e["event"] == "poll_job_failed"] == [
        {"event": "poll_job_failed", "operation": "induction_job", "status": status, "log_level": "warning"}
    ]


def test_start_induction_raises_on_a_failed_job():
    client = FakeCoaClient(jobs=[{"status": "failed", "error": "no tables found"}])
    with pytest.raises(PollFailureError, match="no tables found"):
        _tool(client)(datasource_ids=["src1"], timeout_seconds=5)


def test_start_induction_says_cancelled_on_a_cancelled_job():
    client = FakeCoaClient(jobs=[{"status": "running"}, {"status": "cancelled"}])
    with pytest.raises(PollFailureError, match="was cancelled") as excinfo:
        _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    assert excinfo.value.last_seen["status"] == "cancelled"


# --- COA's refusals ------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            '{"detail": {"message": "An in-flight proposal exists.", "proposal_id": "p-1"}}',
            r"proposal p-1 from an earlier induction",
        ),
        ('{"proposalId": "p-camel"}', r"proposal p-camel from an earlier induction"),
        ('{"detail": {"message": "An induction is already in progress", "job_id": "j-9"}}', r"job j-9 is already"),
        ('{"detail": {"jobId": "j-camel"}}', r"induction job j-camel is already running"),
        ('{"detail": {"message": "An induction is already in progress", "job_id": null}}', r"another induction job"),
        ('{"message": "something else"}', r"HTTP 409\): something else"),
        ('{"detail": "Conflict"}', r"HTTP 409\): Conflict"),
        ('{"detail": null}', r'HTTP 409\): \{"detail": null\}'),
        ("[1, 2]", r"HTTP 409\): \[1, 2\]"),
        ("not json", r"HTTP 409\): not json"),
    ],
)
def test_start_induction_explains_coas_409(body, expected):
    client = FakeCoaClient(start_error=CoaApiError("POST", "/induce", 409, body))
    with pytest.raises(RuntimeError, match=expected):
        _tool(client)(datasource_ids=["src1"], timeout_seconds=5)
    assert client.polled == []


def test_the_running_job_409_says_get_induction_job_does_not_wait():
    body = '{"detail": {"message": "An induction is already in progress", "job_id": "j-9"}}'
    client = FakeCoaClient(start_error=CoaApiError("POST", "/induce", 409, body))
    with pytest.raises(RuntimeError, match=r"get_induction_job \(it does not wait\)"):
        _tool(client)(datasource_ids=["src1"], timeout_seconds=5)


@pytest.mark.parametrize(
    "message",
    [
        "a request with this clientToken is still in progress",
        "clientToken was already used with different request parameters",
    ],
)
def test_start_induction_reraises_client_token_409s_unchanged(message):
    client = FakeCoaClient(start_error=CoaApiError("POST", "/induce", 409, f'{{"message": "{message}"}}'))
    with pytest.raises(CoaApiError):
        _tool(client)(datasource_ids=["src1"], timeout_seconds=5)


def test_a_non_bridge_404_propagates_unchanged():
    client = FakeCoaClient(start_error=CoaApiError("POST", "/induce", 404, "no namespace"))
    with pytest.raises(CoaApiError):
        _tool(client)(datasource_ids=["src1"])


# --- the document-to-table bridge ------------------------------------------------


def test_bridge_sends_no_datasources_the_default_ontology_and_its_options():
    client = FakeCoaClient(
        jobs=[
            {"jobId": "j1", "status": "running", "progress": "writing definitions"},
            {"jobId": "j1", "status": "completed", "source_type": "BRIDGE", "bridge_report": {"proposal_id": "p9"}},
        ]
    )
    out = _tool(client)(strategy="bridge_to_tables", bridge_max_tables=10, bridge_concurrency=2, bridge_dry_run=False)

    (body,) = client.bodies
    assert body["datasourceIds"] == []
    assert body["ontologyUriPrefix"] == _DEFAULT_PREFIX
    assert body["strategy"] == "bridge_to_tables"
    assert body["bridgeOptions"] == {"maxTables": 10, "concurrency": 2, "dryRun": False}
    assert out["sourceType"] == "BRIDGE"
    assert out["bridgeProposalId"] == "p9"
    assert "proposalId" not in out
    assert "bridge_report" not in out


def test_a_bridge_without_options_sends_none_and_a_dry_run_has_no_proposal_id():
    client = FakeCoaClient(jobs=[{"jobId": "j1", "status": "completed", "bridgeReport": {"dryRun": True}}])
    out = _tool(client)(strategy="bridge_to_tables", ontology_uri_prefix=_PREFIX)
    assert "bridgeOptions" not in client.bodies[0]
    assert client.bodies[0]["ontologyUriPrefix"] == _PREFIX
    assert "bridgeProposalId" not in out
    assert "proposalId" not in out


def test_a_bridge_404_names_the_missing_ontology():
    client = FakeCoaClient(start_error=CoaApiError("POST", "/induce", 404, "not found"))
    with pytest.raises(RuntimeError, match="no accepted ontology 'https://example.com/onto#'"):
        _tool(client)(strategy="bridge_to_tables", ontology_uri_prefix=_PREFIX)


# --- read-only tools -------------------------------------------------------------


@pytest.mark.parametrize(("include_report", "sent"), [(True, "report"), (False, None)])
def test_get_induction_job_requests_the_report_only_when_asked(include_report, sent):
    client = FakeCoaClient(
        jobs=[{"jobId": "j1", "status": "running", "progress": "embedding", "source_type": "BRIDGE"}]
    )
    out = _tool(client, "get_induction_job")(job_id="j1", include_report=include_report)
    assert out == {"jobId": "j1", "status": "running", "progress": "embedding", "sourceType": "BRIDGE"}
    assert client.polled == [("j1", sent)]


def test_list_induction_jobs_follows_a_token_past_an_empty_page_and_keeps_the_filter():
    client = FakeCoaClient(
        pages=[
            {"items": [{"job_id": "j1"}], "nextToken": "t2"},
            {"items": [], "nextToken": "t3"},
            {"items": [{"job_id": "j3"}]},
        ]
    )
    out = _tool(client, "list_induction_jobs")(status="completed")
    assert out == {"jobs": [{"job_id": "j1"}, {"job_id": "j3"}], "count": 2}
    assert [(c["next_token"], c["status"]) for c in client.list_calls] == [
        (None, "completed"),
        ("t2", "completed"),
        ("t3", "completed"),
    ]


def test_list_induction_jobs_tolerates_a_bare_array():
    client = FakeCoaClient(pages=[[{"job_id": "j1"}, "not-an-object"]])
    assert _tool(client, "list_induction_jobs")() == {"jobs": [{"job_id": "j1"}], "count": 1}


@pytest.mark.parametrize(("requested", "sent"), [(0, 1), (1000, 200), (75, 75)])
def test_list_induction_jobs_clamps_the_page_size_to_coas_range(requested, sent):
    client = FakeCoaClient()
    _tool(client, "list_induction_jobs")(max_results=requested)
    assert [c["max_results"] for c in client.list_calls] == [sent]


@pytest.mark.parametrize(
    ("tool_name", "envelope", "key", "id_key", "entries"),
    [
        ("list_induction_jobs", "items", "jobs", "job_id", ("j1", "j2", "j3")),
        ("list_induced_datasources", "datasources", "datasources", "datasourceId", ("d1", "d2", "d3")),
    ],
)
def test_a_list_tool_stops_at_the_item_cap_and_says_more_may_exist(
    monkeypatch, tool_name, envelope, key, id_key, entries
):
    monkeypatch.setattr(induction, "_LIST_MAX_ITEMS", 2)
    first, second, third = ({id_key: e} for e in entries)
    client = FakeCoaClient(
        pages=[
            {envelope: [first], "nextToken": "t2"},
            {envelope: [second], "nextToken": "t3"},
            {envelope: [third]},
        ]
    )
    out = _tool(client, tool_name)()
    assert out == {key: [first, second], "count": 2, "truncated": True}
    assert len(client.list_calls) == 2


@pytest.mark.parametrize("tool_name", ["list_induction_jobs", "list_induced_datasources"])
def test_a_list_tool_describes_the_cap_it_enforces(tool_name):
    """The description is what the model reads; it must name the cap the code applies."""
    description = _tool(FakeCoaClient(), tool_name).tool_spec["description"]
    assert str(induction._LIST_MAX_ITEMS) in description


def test_a_list_tool_cuts_a_page_that_overshoots_the_cap(monkeypatch):
    monkeypatch.setattr(induction, "_LIST_MAX_ITEMS", 2)
    client = FakeCoaClient(pages=[{"items": [{"job_id": "j1"}, {"job_id": "j2"}, {"job_id": "j3"}]}])
    out = _tool(client, "list_induction_jobs")()
    assert out == {"jobs": [{"job_id": "j1"}, {"job_id": "j2"}], "count": 2, "truncated": True}


def test_a_list_that_ends_exactly_at_the_cap_is_not_truncated(monkeypatch):
    monkeypatch.setattr(induction, "_LIST_MAX_ITEMS", 2)
    client = FakeCoaClient(pages=[{"items": [{"job_id": "j1"}, {"job_id": "j2"}]}])
    assert _tool(client, "list_induction_jobs")() == {"jobs": [{"job_id": "j1"}, {"job_id": "j2"}], "count": 2}


def test_list_induced_datasources_collects_every_page():
    client = FakeCoaClient(pages=[{"datasources": [{"datasourceId": "d1"}], "nextToken": "t2"}, {"datasources": []}])
    out = _tool(client, "list_induced_datasources")()
    assert out == {"datasources": [{"datasourceId": "d1"}], "count": 1}
    assert [c["next_token"] for c in client.list_calls] == [None, "t2"]


# --- contract pins ---------------------------------------------------------------


def _values(enum: type[Enum]) -> set[str]:
    return {member.value for member in enum}


@pytest.mark.parametrize(
    ("literal", "enum"),
    [(induction.InductionStrategyName, InductionStrategy), (induction.GroundingModeName, GroundingMode)],
)
def test_each_tool_literal_equals_its_generated_enum(literal, enum):
    assert set(get_args(literal)) == _values(enum)


def test_job_end_states_are_coa_statuses_or_the_two_documented_extras():
    ends = induction._JOB_SUCCESS_STATES | induction._JOB_FAILURE_STATES
    assert ends <= _values(InductionJobStatus) | {"ingested", "cancelled"}
    assert InductionJobStatus.RUNNING.value not in ends


def test_start_induction_is_the_only_mutating_tool_and_stays_gated():
    names = {t.tool_name for t in build_induction_tools(FakeCoaClient(), "ns1")}  # type: ignore[arg-type]
    assert names == {"start_induction", "get_induction_job", "list_induction_jobs", "list_induced_datasources"}
    assert {"start_induction"} == MUTATING_TOOL_NAMES
