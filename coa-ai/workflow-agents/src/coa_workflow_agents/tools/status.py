# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The supervisor's own read-only tool, ``get_namespace_status``.

Pages through sources, induction jobs and proposals and correlates them by datasource id,
so one call answers "what is the status of this namespace?". It mutates nothing, so it is
not gated. The reshape (``build_namespace_status``) is pure and tested against both the
envelope and casing variants the list endpoints return.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from strands import tool
from strands.types.tools import AgentTool

from coa_workflow_agents.clients.coa_api_client import (
    LIST_INDUCTION_JOBS_MAX_RESULTS,
    LIST_PROPOSALS_MAX_RESULTS,
    LIST_SOURCES_MAX_RESULTS,
    CoaApiClient,
)
from coa_workflow_agents.tools._shared import collect_pages, datasource_ids, envelope_items, first_str


class JobRef(TypedDict):
    """An induction job as attached to a source row (id + status only)."""

    jobId: str | None
    status: str | None


class ProposalRef(TypedDict):
    """A proposal as attached to a source row (id + status only)."""

    proposalId: str | None
    status: str | None


class JobEntry(TypedDict):
    """A normalized induction job in the top-level list (adds its datasource ids)."""

    jobId: str | None
    status: str | None
    datasourceIds: list[str]


class ProposalEntry(TypedDict):
    """A normalized proposal in the top-level list (adds ontology + datasource ids)."""

    proposalId: str | None
    status: str | None
    ontologyId: str | None
    datasourceIds: list[str]


class SourceRow(TypedDict):
    """One source with the jobs induced from it and the proposals that resulted."""

    sourceId: str | None
    name: str | None
    status: str | None
    inductionJobs: list[JobRef]
    proposals: list[ProposalRef]


class StatusCounts(TypedDict):
    """Entity counts for the namespace overview."""

    sources: int
    inductionJobs: int
    proposals: int


class NamespaceStatus(TypedDict):
    """The full namespace status overview returned by ``build_namespace_status``."""

    sources: list[SourceRow]
    inductionJobs: list[JobEntry]
    proposals: list[ProposalEntry]
    counts: StatusCounts


def _index_by_datasource[E, R](
    entries: list[E], ids: Callable[[E], list[str]], ref: Callable[[E], R]
) -> dict[str, list[R]]:
    """Each entry's ``ref`` under every distinct datasource id it names (a fresh ref per id)."""
    index: dict[str, list[R]] = {}
    for entry in entries:
        for ds in dict.fromkeys(ids(entry)):
            index.setdefault(ds, []).append(ref(entry))
    return index


def build_namespace_status(
    *,
    sources_resp: Any,
    induction_jobs_resp: Any,
    proposals_resp: Any,
) -> NamespaceStatus:
    """Attach to each source the jobs and proposals naming its id; list every job and proposal too.

    Pure. Jobs and proposals that name no listed source stay in the top-level lists.
    """
    sources = envelope_items(sources_resp, "sources")
    jobs = envelope_items(induction_jobs_resp, "jobs", "inductionJobs")
    proposals = envelope_items(proposals_resp, "proposals")

    norm_jobs: list[JobEntry] = [
        {
            "jobId": first_str(j, "jobId", "job_id", "id"),
            "status": first_str(j, "status"),
            "datasourceIds": datasource_ids(j),
        }
        for j in jobs
    ]
    norm_proposals: list[ProposalEntry] = [
        {
            "proposalId": first_str(p, "proposalId", "proposal_id", "id"),
            "status": first_str(p, "status"),
            "ontologyId": first_str(p, "ontologyId", "ontology_id"),
            "datasourceIds": datasource_ids(p),
        }
        for p in proposals
    ]

    jobs_by_ds = _index_by_datasource(
        norm_jobs, lambda j: j["datasourceIds"], lambda j: JobRef(jobId=j["jobId"], status=j["status"])
    )
    proposals_by_ds = _index_by_datasource(
        norm_proposals,
        lambda p: p["datasourceIds"],
        lambda p: ProposalRef(proposalId=p["proposalId"], status=p["status"]),
    )

    source_rows: list[SourceRow] = []
    for s in sources:
        sid = first_str(s, "sourceId", "source_id", "id")
        source_rows.append(
            {
                "sourceId": sid,
                "name": first_str(s, "name"),
                "status": first_str(s, "status"),
                "inductionJobs": jobs_by_ds.get(sid, []) if sid is not None else [],
                "proposals": proposals_by_ds.get(sid, []) if sid is not None else [],
            }
        )

    return {
        "sources": source_rows,
        "inductionJobs": norm_jobs,
        "proposals": norm_proposals,
        "counts": {
            "sources": len(source_rows),
            "inductionJobs": len(norm_jobs),
            "proposals": len(norm_proposals),
        },
    }


def build_status_tools(client: CoaApiClient, namespace_id: str) -> list[AgentTool]:
    """The supervisor's read-only status tool, bound to ``namespace_id``."""

    @tool
    def get_namespace_status() -> NamespaceStatus:
        """End-to-end status of this COA namespace in ONE call (read-only).

        Answers "what is the current status of everything in this namespace?"
        — every data source and its lifecycle status, every ontology-induction
        job and its status, and every induction proposal and its status, all
        correlated by datasource id so each source shows the jobs induced from
        it and the proposals that resulted. One call covers sources, induction
        jobs and proposals together; use it for any status or overview question.

        Sources, induction jobs and proposals are all paged through to
        completion (each COA list endpoint pages via ``nextToken``), so a
        namespace with many of any of them is not truncated.

        Read-only: it never mutates COA state and needs no confirmation.

        Returns:
            ``{sources: [{sourceId, name, status, inductionJobs: [...],
            proposals: [...]}], inductionJobs: [...], proposals: [...],
            counts: {sources, inductionJobs, proposals}}``. A source with no
            induction job or proposal simply has empty lists; jobs/proposals
            that reference no listed source still appear in the top-level
            ``inductionJobs``/``proposals`` arrays, so nothing is dropped.
        """
        return build_namespace_status(
            sources_resp=collect_pages(
                lambda tok: client.list_sources(namespace_id, next_token=tok, max_results=LIST_SOURCES_MAX_RESULTS),
                "sources",
            ),
            induction_jobs_resp=collect_pages(
                lambda tok: client.list_induction_jobs(
                    namespace_id, next_token=tok, max_results=LIST_INDUCTION_JOBS_MAX_RESULTS
                ),
                "jobs",
                "inductionJobs",
            ),
            proposals_resp=collect_pages(
                lambda tok: client.list_proposals(namespace_id, next_token=tok, max_results=LIST_PROPOSALS_MAX_RESULTS),
                "proposals",
            ),
        )

    return [get_namespace_status]
