# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the supervisor's namespace status-overview tool.

Covers the pure correlation logic (``build_namespace_status``) against the
shape variance the live COA list endpoints exhibit (``items`` vs a legacy
plural key vs a bare list; snake_case vs camelCase; datasource ids at top
level vs nested under ``metadata``), plus the read-only ``get_namespace_status``
tool wiring.
"""

from __future__ import annotations

from typing import Any

import pytest
from coa_workflow_agents.clients.coa_api_client import (
    LIST_INDUCTION_JOBS_MAX_RESULTS,
    LIST_PROPOSALS_MAX_RESULTS,
    LIST_SOURCES_MAX_RESULTS,
)
from coa_workflow_agents.tools.status import build_namespace_status, build_status_tools

pytestmark = pytest.mark.unit


# ── build_namespace_status: correlation ──────────────────────────────────


def test_correlates_jobs_and_proposals_to_their_source():
    out = build_namespace_status(
        sources_resp={"items": [{"sourceId": "s1", "name": "sales", "status": "APPROVED"}]},
        induction_jobs_resp={"items": [{"jobId": "j1", "status": "SUCCEEDED", "datasourceIds": ["s1"]}]},
        proposals_resp={"items": [{"proposalId": "p1", "status": "accepted", "metadata": {"datasource_ids": ["s1"]}}]},
    )
    assert out["counts"] == {"sources": 1, "inductionJobs": 1, "proposals": 1}
    src = out["sources"][0]
    assert src["sourceId"] == "s1" and src["status"] == "APPROVED"
    assert src["inductionJobs"] == [{"jobId": "j1", "status": "SUCCEEDED"}]
    assert src["proposals"] == [{"proposalId": "p1", "status": "accepted"}]


def test_source_with_no_jobs_or_proposals_has_empty_lists():
    out = build_namespace_status(
        sources_resp={"items": [{"sourceId": "s1", "name": "a", "status": "SCANNING"}]},
        induction_jobs_resp={"items": []},
        proposals_resp={"items": []},
    )
    src = out["sources"][0]
    assert src["inductionJobs"] == []
    assert src["proposals"] == []


def test_jobs_and_proposals_not_matching_a_source_still_surface_top_level():
    """Nothing is dropped: a proposal/job referencing an unknown (or no)
    datasource still appears in the top-level arrays."""
    out = build_namespace_status(
        sources_resp={"items": [{"sourceId": "s1", "name": "a", "status": "APPROVED"}]},
        induction_jobs_resp={"items": [{"jobId": "j9", "status": "FAILED", "datasourceIds": ["sX"]}]},
        proposals_resp={"items": [{"proposalId": "p9", "status": "pending"}]},  # no datasource ids
    )
    assert out["sources"][0]["inductionJobs"] == []  # j9 doesn't match s1
    assert out["sources"][0]["proposals"] == []
    assert {j["jobId"] for j in out["inductionJobs"]} == {"j9"}  # but still listed
    assert {p["proposalId"] for p in out["proposals"]} == {"p9"}


def test_tolerates_bare_list_legacy_plural_and_camel_snake():
    out = build_namespace_status(
        sources_resp=[{"source_id": "s1", "name": "a", "status": "APPROVED"}],  # bare list + snake id
        induction_jobs_resp={
            "jobs": [{"job_id": "j1", "status": "SUCCEEDED", "datasourceId": "s1"}]
        },  # legacy + single camel id
        proposals_resp={
            "proposals": [{"proposal_id": "p1", "status": "accepted", "metadata": {"datasourceIds": ["s1"]}}]
        },
    )
    src = out["sources"][0]
    assert src["sourceId"] == "s1"
    assert src["inductionJobs"] == [{"jobId": "j1", "status": "SUCCEEDED"}]
    assert src["proposals"] == [{"proposalId": "p1", "status": "accepted"}]


def test_duplicate_datasource_id_within_one_entry_attaches_once():
    """A job/proposal that lists the same datasource id twice must attach to
    that source exactly once (the dict-inversion dedupes ids per entry, matching
    the old list-membership behaviour)."""
    out = build_namespace_status(
        sources_resp={"items": [{"sourceId": "s1", "name": "a", "status": "APPROVED"}]},
        induction_jobs_resp={"items": [{"jobId": "j1", "status": "SUCCEEDED", "datasourceIds": ["s1", "s1"]}]},
        proposals_resp={"items": []},
    )
    assert out["sources"][0]["inductionJobs"] == [{"jobId": "j1", "status": "SUCCEEDED"}]


def test_empty_everything_is_zeroed_not_crashing():
    out = build_namespace_status(sources_resp=None, induction_jobs_resp=None, proposals_resp=None)
    assert out["counts"] == {"sources": 0, "inductionJobs": 0, "proposals": 0}
    assert out["sources"] == [] and out["inductionJobs"] == [] and out["proposals"] == []


def test_unexpected_envelope_shape_is_treated_as_empty():
    """A dict with neither `items` nor a known plural key yields no entities
    (not a crash) — pins the _shared.envelope_items fall-through explicitly."""
    out = build_namespace_status(
        sources_resp={"unexpected": 1}, induction_jobs_resp={"weird": []}, proposals_resp={"nope": "x"}
    )
    assert out["counts"] == {"sources": 0, "inductionJobs": 0, "proposals": 0}


# ── get_namespace_status tool ────────────────────────────────────────────


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_sources(self, namespace_id: str, **_: Any) -> dict[str, Any]:
        self.calls.append("list_sources")
        return {"items": [{"sourceId": "s1", "name": "sales", "status": "APPROVED"}]}

    def list_induction_jobs(self, namespace_id: str, **_: Any) -> dict[str, Any]:
        self.calls.append("list_induction_jobs")
        return {"items": [{"jobId": "j1", "status": "SUCCEEDED", "datasourceIds": ["s1"]}]}

    def list_proposals(self, namespace_id: str, **_: Any) -> dict[str, Any]:
        self.calls.append("list_proposals")
        return {"items": [{"proposalId": "p1", "status": "accepted", "metadata": {"datasource_ids": ["s1"]}}]}


def test_get_namespace_status_tool_reads_all_three_and_correlates():
    client = _FakeClient()
    tools = build_status_tools(client, "ns1")  # type: ignore[arg-type]
    assert len(tools) == 1
    tool = tools[0]
    assert tool.tool_name == "get_namespace_status"

    out = tool()  # the @tool object is directly callable

    assert client.calls == ["list_sources", "list_induction_jobs", "list_proposals"]
    assert out["counts"] == {"sources": 1, "inductionJobs": 1, "proposals": 1}
    assert out["sources"][0]["proposals"] == [{"proposalId": "p1", "status": "accepted"}]


def test_get_namespace_status_makes_only_read_calls():
    """Read-only guarantee: the tool invokes ONLY the three list endpoints and
    nothing that mutates COA state."""
    client = _FakeClient()
    tool = build_status_tools(client, "ns1")[0]  # type: ignore[arg-type]
    tool()
    assert set(client.calls) == {"list_sources", "list_induction_jobs", "list_proposals"}
    assert all(c.startswith("list_") or c.startswith("get_") for c in client.calls)


class _PagedClient:
    """Sources and induction jobs span two pages; proposals is single-response."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._src = [
            {"items": [{"sourceId": "s1", "name": "a", "status": "APPROVED"}], "nextToken": "s-p2"},
            {"items": [{"sourceId": "s2", "name": "b", "status": "SCANNING"}]},
        ]
        self._jobs = [
            {"items": [{"jobId": "j1", "status": "SUCCEEDED", "datasourceIds": ["s1"]}], "nextToken": "j-p2"},
            {"items": [{"jobId": "j2", "status": "RUNNING", "datasourceIds": ["s2"]}]},
        ]
        self._si = self._ji = 0

    def list_sources(self, namespace_id: str, *, next_token: str | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append(("list_sources", next_token))
        page = self._src[self._si]
        self._si += 1
        return page

    def list_induction_jobs(self, namespace_id: str, *, next_token: str | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append(("list_induction_jobs", next_token))
        page = self._jobs[self._ji]
        self._ji += 1
        return page

    def list_proposals(self, namespace_id: str, **_: Any) -> dict[str, Any]:
        self.calls.append(("list_proposals", None))
        return {"items": []}


def test_get_namespace_status_pages_sources_and_jobs_to_completion():
    """The overview must not truncate at page 1 — it follows nextToken for the
    paginated endpoints (sources, induction jobs). Pins the docstring's
    'nothing is dropped' promise against a multi-page namespace."""
    client = _PagedClient()
    tool = build_status_tools(client, "ns1")[0]  # type: ignore[arg-type]

    out = tool()

    assert out["counts"]["sources"] == 2  # both pages
    assert out["counts"]["inductionJobs"] == 2
    # nextToken was actually followed for each paginated endpoint.
    src_tokens = [t for (n, t) in client.calls if n == "list_sources"]
    job_tokens = [t for (n, t) in client.calls if n == "list_induction_jobs"]
    assert src_tokens == [None, "s-p2"]
    assert job_tokens == [None, "j-p2"]
    # Correlation still holds across pages.
    by_id = {s["sourceId"]: s for s in out["sources"]}
    assert by_id["s1"]["inductionJobs"] == [{"jobId": "j1", "status": "SUCCEEDED"}]
    assert by_id["s2"]["inductionJobs"] == [{"jobId": "j2", "status": "RUNNING"}]


class _StuckTokenClient:
    """A broken/malicious server: every page returns the SAME non-empty
    nextToken, which would loop _shared.collect_pages forever without a guard.

    Caps itself so a GUARD REGRESSION fails fast with a clear message instead
    of hanging the test suite until a CI timeout: with the guard in place
    only two source fetches happen, well under the cap."""

    def __init__(self) -> None:
        self.source_calls = 0

    def list_sources(self, namespace_id: str, *, next_token: str | None = None, **_: Any) -> dict[str, Any]:
        self.source_calls += 1
        if self.source_calls > 5:
            raise AssertionError("pagination did not terminate: collect_pages looped on a repeated nextToken")
        return {"items": [{"sourceId": "s1", "name": "a", "status": "APPROVED"}], "nextToken": "STUCK"}

    def list_induction_jobs(self, namespace_id: str, *, next_token: str | None = None, **_: Any) -> dict[str, Any]:
        return {"items": []}

    def list_proposals(self, namespace_id: str, **_: Any) -> dict[str, Any]:
        return {"items": []}


def test_get_namespace_status_terminates_on_a_repeated_pagination_token():
    """Cycle guard: a server echoing the same non-empty
    nextToken must NOT hang the (synchronous, event-loop-blocking) tool. The
    loop stops the moment a token repeats — here after exactly two source
    calls (initial None, then one "STUCK", which is then seen again)."""
    client = _StuckTokenClient()
    tool = build_status_tools(client, "ns1")[0]  # type: ignore[arg-type]

    out = tool()  # must return, not hang

    # Exactly two fetches: next_token=None, then "STUCK"; the second "STUCK" is
    # already in seen_tokens, so the loop exits instead of fetching a third time.
    assert client.source_calls == 2
    # Both fetches ran before the repeat was detected, so the repeated page's
    # item is accumulated twice — acceptable: the guard's job is to TERMINATE,
    # not to dedupe. (A real paginating server never repeats a token.)
    assert out["counts"]["sources"] == 2


class _PagedProposalsClient(_FakeClient):
    """Proposals span two pages (ListProposals is paginated in COA 0.3.7)."""

    def __init__(self) -> None:
        super().__init__()
        self.proposal_tokens: list[str | None] = []

    def list_proposals(self, namespace_id: str, *, next_token: str | None = None, **_: Any) -> dict[str, Any]:
        self.proposal_tokens.append(next_token)
        if len(self.proposal_tokens) > 5:
            raise AssertionError("proposal pagination did not terminate")
        if next_token is None:
            return {"items": [{"proposalId": "p1", "status": "accepted"}], "nextToken": "p-2"}
        return {"items": [{"proposalId": "p2", "status": "pending"}]}


def test_get_namespace_status_pages_proposals_to_completion():
    client = _PagedProposalsClient()
    out = build_status_tools(client, "ns1")[0]()  # type: ignore[arg-type]
    assert client.proposal_tokens == [None, "p-2"]
    assert {p["proposalId"] for p in out["proposals"]} == {"p1", "p2"}


class _PageSizeClient(_FakeClient):
    """Records the page size of each list call."""

    def __init__(self) -> None:
        super().__init__()
        self.sizes: dict[str, int] = {}

    def list_sources(self, namespace_id: str, *, max_results: int = 50, **kw: Any) -> dict[str, Any]:
        self.sizes["sources"] = max_results
        return super().list_sources(namespace_id, **kw)

    def list_induction_jobs(self, namespace_id: str, *, max_results: int = 50, **kw: Any) -> dict[str, Any]:
        self.sizes["jobs"] = max_results
        return super().list_induction_jobs(namespace_id, **kw)

    def list_proposals(self, namespace_id: str, *, max_results: int = 50, **kw: Any) -> dict[str, Any]:
        self.sizes["proposals"] = max_results
        return super().list_proposals(namespace_id, **kw)


def test_get_namespace_status_asks_for_each_endpoints_largest_page():
    """Fewest round-trips, and never a size COA rejects (one shared definition)."""

    client = _PageSizeClient()
    build_status_tools(client, "ns1")[0]()  # type: ignore[arg-type]
    assert client.sizes == {
        "sources": LIST_SOURCES_MAX_RESULTS,
        "jobs": LIST_INDUCTION_JOBS_MAX_RESULTS,
        "proposals": LIST_PROPOSALS_MAX_RESULTS,
    }
    assert (LIST_SOURCES_MAX_RESULTS, LIST_INDUCTION_JOBS_MAX_RESULTS, LIST_PROPOSALS_MAX_RESULTS) == (100, 200, 200)
