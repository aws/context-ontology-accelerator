# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The InductionAgent's tools: run ontology induction over approved sources (ontology-induction.smithy).

``start_induction`` starts the Bedrock-backed job and polls it to a terminal state, so the
agent gets one outcome (a completed job, a named failure or a timeout), never a bare job id.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from coa_control_plane_server.models.bridge_options import BridgeOptions
from coa_control_plane_server.models.include_mode import IncludeMode
from coa_control_plane_server.models.induction_job_status import InductionJobStatus
from coa_control_plane_server.models.induction_source_type import InductionSourceType
from coa_control_plane_server.models.induction_strategy import InductionStrategy
from coa_control_plane_server.models.start_induction_request_content import StartInductionRequestContent
from strands import tool
from strands.types.tools import AgentTool

from coa_workflow_agents.clients.coa_api_client import (
    LIST_INDUCED_DATASOURCES_MAX_RESULTS,
    LIST_INDUCTION_JOBS_MAX_RESULTS,
    CoaApiClient,
    CoaApiError,
    build_request,
    is_client_token_conflict,
)
from coa_workflow_agents.tools._shared import clamp, collect_pages_capped, first_present
from coa_workflow_agents.tools.polling import (
    PollFailureError,
    PollTimeoutError,
    RunDeadlineReachedError,
    poll_to_terminal,
    read_job_id,
)

# The generated InductionStrategy / GroundingMode values. Strands builds the tool's JSON
# schema from its type hints, so these stay Literals; a unit test pins each to its enum.
InductionStrategyName = Literal["table_to_ontology", "rigor_ontology", "unstructured_lexical_graph", "bridge_to_tables"]
GroundingModeName = Literal["NONE", "STANDARD", "ENHANCED"]

# StartInduction bounds (RerankMaxTokens, BridgeOptions), checked here for a clear message.
_RERANK_MAX_TOKENS = (1, 8192)
_BRIDGE_MAX_TABLES = (1, 200)
_BRIDGE_CONCURRENCY = (1, 16)
_BRIDGE_STRATEGY = InductionStrategy.BRIDGE_TO_TABLES.value
# Strategies that read tables from the listed sources: COA answers 400 to an empty list.
_TABLE_STRATEGIES = frozenset({InductionStrategy.TABLE_TO_ONTOLOGY.value, InductionStrategy.RIGOR_ONTOLOGY.value})
# The relational strategy, right for the database sources COA onboards. A signature default
# must be a Literal member, so it is spelled out; mypy checks it against the pinned Literal.
_DEFAULT_STRATEGY: InductionStrategyName = "table_to_ontology"
_DEFAULT_INDUCE_TIMEOUT_S = 3600
# A list tool returns at most this many entries, so one call cannot flood the model's context.
_LIST_MAX_ITEMS = 500

# A job ends completed or failed, and becomes ingested once its proposal is accepted.
# COA's job store also counts cancelled as terminal (never set on an induction job today),
# so it is a failure here rather than a poll that never ends. ingested and cancelled are
# stored by COA but are not InductionJobStatus members.
_JOB_INGESTED = "ingested"
_JOB_CANCELLED = "cancelled"
_JOB_SUCCESS_STATES = frozenset({InductionJobStatus.COMPLETED.value, _JOB_INGESTED})
_JOB_FAILURE_STATES = frozenset({InductionJobStatus.FAILED.value, _JOB_CANCELLED})


def _explain_induction_conflict(exc: CoaApiError) -> str:
    """COA's 409 from StartInduction as an actionable message.

    COA allows one induction at a time: the 409 names either the running job (``job_id``) or
    the namespace's unreviewed proposal (``proposal_id``).
    """
    try:
        body: Any = json.loads(exc.body)
    except ValueError:
        body = None
    detail: Any = body.get("detail", body) if isinstance(body, dict) else None
    if not isinstance(detail, dict):
        detail = {"message": detail} if isinstance(detail, str) and detail else {}
    proposal_id = first_present(detail, "proposal_id", "proposalId")
    job_id = first_present(detail, "job_id", "jobId")
    message = str(detail.get("message") or exc.body[:300])
    if proposal_id:
        return (
            f"COA refused to start induction: proposal {proposal_id} from an earlier induction in this namespace "
            "is still unreviewed. It must be accepted, cancelled or rejected before "
            "induction can start again; if it is being accepted right now, wait for that to finish."
        )
    if job_id or "already in progress" in message:
        running = f"induction job {job_id}" if job_id else "another induction job"
        return (
            f"COA refused to start induction: {running} is already running in this namespace. "
            "Read its status once with get_induction_job (it does not wait) instead of starting another."
        )
    return f"COA refused to start induction (HTTP 409): {message}"


def _check_range(name: str, value: float | None, bounds: tuple[float, float]) -> None:
    """Raise ``ValueError`` when ``value`` is set and outside the inclusive ``bounds``."""
    low, high = bounds
    if value is not None and not (low <= value <= high):
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")


def _job_result(job: dict[str, Any]) -> dict[str, Any]:
    """``job`` as the tools return it, with the proposal to review named.

    COA stores ``bridge_report`` and ``source_type`` in snake_case; the model names them
    ``bridgeReport`` / ``sourceType``. A bridge report's proposal id is lifted to
    ``bridgeProposalId`` (a dry run has none). Any other finished job gets ``proposalId``: COA
    stores its proposal under the job's own id (a stored job row may also name it as
    ``proposal_id``), or creates none when it finds an identical accepted proposal and sets
    ``duplicate_of`` to it, which then wins.
    """
    out = dict(job)
    for camel, snake in (("bridgeReport", "bridge_report"), ("sourceType", "source_type")):
        if camel not in out and snake in out:
            out[camel] = out.pop(snake)
    if out.get("sourceType") == InductionSourceType.BRIDGE.value or "bridgeReport" in out:
        report = out.get("bridgeReport")
        proposal_id = first_present(report, "proposalId", "proposal_id") if isinstance(report, dict) else None
        if proposal_id:
            out["bridgeProposalId"] = proposal_id
    elif out.get("status") in _JOB_SUCCESS_STATES:
        # The stored job row may carry proposal_id; duplicate_of wins, since no new proposal exists then.
        proposal_id = first_present(out, "duplicate_of", "duplicateOf", "proposalId", "proposal_id") or read_job_id(
            out, "jobId"
        )
        if proposal_id:
            out["proposalId"] = str(proposal_id)
    return out


def _listing(key: str, items: list[dict[str, Any]], truncated: bool) -> dict[str, Any]:
    """A list tool's result: the items, their count, and ``truncated`` only when more may exist."""
    return {key: items, "count": len(items), **({"truncated": True} if truncated else {})}


def _default_ontology_uri_prefix(namespace_id: str) -> str:
    """The ``ontologyUriPrefix`` COA's web UI uses for a namespace (``OntologyPage.tsx``).

    The API requires the field but the UI never asks for it, so neither does this tool.
    """
    return f"http://coa.amazon.com/namespace/{namespace_id}/ontology/induced#"


def build_induction_tools(client: CoaApiClient, namespace_id: str) -> list[AgentTool]:
    """The InductionAgent's tools, bound to one namespace and the caller's client."""

    @tool
    def start_induction(
        datasource_ids: list[str] | None = None,
        ontology_uri_prefix: str | None = None,
        strategy: InductionStrategyName = _DEFAULT_STRATEGY,
        label: str | None = None,
        grounding_mode: GroundingModeName = "ENHANCED",
        grounding_ontology_ids: list[str] | None = None,
        confidence_threshold: float | None = None,
        rerank_max_tokens: int | None = None,
        bridge_max_tables: int | None = None,
        bridge_concurrency: int | None = None,
        bridge_dry_run: bool | None = None,
        timeout_seconds: int = _DEFAULT_INDUCE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Start an ontology induction job over approved data sources, and WAIT for it to finish.

        Every ``datasource_id`` must already be ``APPROVED``: COA does not check, and an
        unapproved source just contributes no tables. This is a real, billable LLM pipeline
        that the call waits for until the job is ``completed`` (or ``ingested``), at most about 54
        minutes by default (the run deadline less a minute); a longer job ends as ``still_running``
        and keeps running in COA. It raises when the job fails. Only the datasource ids are needed: the
        strategy defaults to the relational one and the URI prefix to the namespace's own.

        Document-to-table bridge (``strategy="bridge_to_tables"``): links the classes of an
        ACCEPTED table ontology to document classes accepted into the same ontology, and
        proposes document-derived definitions. Pass no ``datasource_ids``. It bridges the
        accepted ontology named by ``ontology_uri_prefix`` (COA registers an induced ontology
        under its URI prefix, so the default is the one the usual flow produced; COA answers
        404 when there is none). It writes nothing itself: the job's ``bridgeReport`` names a
        proposal (``bridgeProposalId``) to review like any other. ``bridge_dry_run=True``
        reports the plan without a proposal but still makes the LLM calls.

        Args:
            datasource_ids: APPROVED source ids. At least one for ``table_to_ontology`` and
                ``rigor_ontology``; none for ``bridge_to_tables``.
            ontology_uri_prefix: URI prefix for the generated classes and properties.
                Omit it to use ``http://coa.amazon.com/namespace/<ns>/ontology/induced#``.
            strategy: ``table_to_ontology`` (default, relational tables), ``rigor_ontology``
                (``en`` namespaces only), ``unstructured_lexical_graph`` (lexical graphs) or
                ``bridge_to_tables`` (above).
            label: Optional label for the run.
            grounding_mode: ``ENHANCED`` (default) verifies every grounding with an LLM
                rerank; ``STANDARD`` grounds exact-name matches only; ``NONE`` grounds nothing.
            grounding_ontology_ids: Ontologies to ground against, besides the namespace's
                accepted induced ones (always used).
            confidence_threshold: Match confidence threshold, 0.0 to 1.0.
            rerank_max_tokens: Output-token cap for the ENHANCED rerank (1 to 8192, COA
                default 1000). Raise it when a job fails saying the rerank was truncated.
            bridge_max_tables: Bridge only: most table classes in one run (1 to 200, COA
                default 60). Caps the run's LLM cost.
            bridge_concurrency: Bridge only: tables processed in parallel (1 to 16, default 6).
            bridge_dry_run: Bridge only: report the plan without creating a proposal.
            timeout_seconds: Stop waiting after this long (default 3600 s, in practice capped by
                the run deadline: about 3240 s after the run started, by default); the job keeps
                running.

        Returns:
            The terminal ``GetInductionJob`` response: ``jobId``, ``status``, a slim
            ``report`` and ``proposalId``, the proposal to review next. When COA found an
            identical accepted proposal it created none: ``duplicate_of`` is set and
            ``proposalId`` names that accepted proposal; say so. Tell the caller about
            ``report.droppedTables`` (tables the run could not model). A bridge job instead
            has ``sourceType: "BRIDGE"``, its ``bridgeReport`` (counts, ``links``,
            ``llmFailures``, ``notes``; keys may be snake_case) and ``bridgeProposalId``
            (absent on a dry run or when nothing would change).

        Raises:
            ValueError: an argument COA would refuse (out of range, or the wrong mix of
                strategy, sources and bridge options).
            RuntimeError: the job failed (the message carries COA's ``error``; report it,
                and do not start another job unless the caller asks) or was cancelled; the job
                was still running when ``timeout_seconds`` ran out (it keeps running: check it
                with ``get_induction_job``, never start another); or COA refused with
                409 because the namespace has an open proposal or a running job (the message
                names it); or the job's status could not be read after repeated failures (it may
                still be running: check ``get_induction_job``, never start another).
            CoaApiError: any other COA error. A 409 about the ``clientToken`` saying it is
                still in progress means the job may be running: check ``list_induction_jobs``
                before calling again, since a new call starts a new billable job.
        """
        _check_range("confidence_threshold", confidence_threshold, (0.0, 1.0))
        _check_range("rerank_max_tokens", rerank_max_tokens, _RERANK_MAX_TOKENS)
        _check_range("bridge_max_tables", bridge_max_tables, _BRIDGE_MAX_TABLES)
        _check_range("bridge_concurrency", bridge_concurrency, _BRIDGE_CONCURRENCY)
        bridge_fields = {
            key: value
            for key, value in (
                ("max_tables", bridge_max_tables),
                ("concurrency", bridge_concurrency),
                ("dry_run", bridge_dry_run),
            )
            if value is not None
        }
        is_bridge = strategy == _BRIDGE_STRATEGY
        if bridge_fields and not is_bridge:
            # COA answers 400 to bridge options on any other strategy; refuse here with a clearer message.
            raise ValueError("bridge_max_tables / bridge_concurrency / bridge_dry_run apply only to bridge_to_tables")
        if is_bridge and datasource_ids:
            raise ValueError(
                "bridge_to_tables runs on an accepted ontology and reads no data sources; omit datasource_ids"
            )
        if strategy in _TABLE_STRATEGIES and not datasource_ids:
            raise ValueError(f"{strategy} needs at least one approved datasource id")
        # Only an omitted prefix (None) gets the default; an explicit one, even "", is the
        # caller's and is validated as given.
        prefix = ontology_uri_prefix if ontology_uri_prefix is not None else _default_ontology_uri_prefix(namespace_id)
        request = build_request(
            StartInductionRequestContent,
            datasource_ids=list(datasource_ids or []),
            ontology_uri_prefix=prefix,
            strategy=strategy,
            grounding_mode=grounding_mode,
            label=label or None,
            grounding_ontology_ids=grounding_ontology_ids or None,
            confidence_threshold=confidence_threshold,
            rerank_max_tokens=rerank_max_tokens,
            bridge_options=build_request(BridgeOptions, **bridge_fields) if bridge_fields else None,
        )

        # A fresh clientToken per tool call: the client's transport retry reuses it (COA
        # de-duplicates, so a retry never starts a second job); a deliberate re-run is a new job.
        try:
            started = client.start_induction(namespace_id, request, client_token=str(uuid.uuid4()))
        except CoaApiError as exc:
            if exc.status_code == 409 and not is_client_token_conflict(exc):
                raise RuntimeError(_explain_induction_conflict(exc)) from exc
            if exc.status_code == 404 and is_bridge:
                raise RuntimeError(
                    f"COA has no accepted ontology {prefix!r} in this namespace to bridge; pass the "
                    "ontologyId of an accepted induced ontology as ontology_uri_prefix"
                ) from exc
            raise
        # The response says job_id, though the model says jobId; read_job_id takes either.
        job_id = read_job_id(started, "jobId")
        if not job_id:
            raise RuntimeError(
                f"StartInduction returned no job id (response keys: {sorted(started.keys())}); "
                "cannot poll for completion"
            )
        description = f"induction job {job_id}"
        try:
            job = poll_to_terminal(
                description,
                lambda: client.get_induction_job(namespace_id, job_id),
                operation="induction_job",
                is_terminal=lambda j: j.get("status") in _JOB_SUCCESS_STATES,
                is_failure=lambda j: j.get("status") in _JOB_FAILURE_STATES,
                timeout_s=float(timeout_seconds),
            )
        except PollFailureError as exc:
            if exc.last_seen.get("status") == _JOB_CANCELLED:
                raise PollFailureError(f"{description} was cancelled", exc.last_seen) from exc
            raise
        except RunDeadlineReachedError:
            raise  # StillRunningHook turns it into a still_running result.
        except PollTimeoutError as exc:
            # The job did not fail; COA keeps running it.
            raise RuntimeError(
                f"{description} is still running on COA after timeout_seconds={timeout_seconds}; it was not "
                "cancelled. Check it with get_induction_job; do not start another induction for it."
            ) from exc
        return _job_result(job)

    @tool
    def get_induction_job(job_id: str, include_report: bool = False) -> dict[str, Any]:
        """Get an induction job's current status without waiting (``GetInductionJob``, read-only).

        For a job started in an earlier run, or one still running.

        Args:
            job_id: The induction job id.
            include_report: Ask for the full match report, which can be large.

        Returns:
            ``{jobId, status, createdAt, report?, error?}``, plus ``proposalId`` once the job
            succeeded (see ``start_induction``); a bridge job adds ``sourceType: "BRIDGE"``,
            ``progress`` while running, and its ``bridgeReport`` and ``bridgeProposalId`` once
            completed.
        """
        include = IncludeMode.REPORT.value if include_report else None
        return _job_result(client.get_induction_job(namespace_id, job_id, include=include))

    @tool
    def list_induction_jobs(status: str | None = None, max_results: int = 50) -> dict[str, Any]:
        """List the induction jobs in this namespace (``ListInductionJobs``, read-only).

        At most 500 jobs are returned. When more may exist the result has ``truncated: true``:
        narrow with ``status``.

        Args:
            status: Optional filter, e.g. ``"completed"``, ``"failed"``, ``"pending"``.
            max_results: Page size (clamped to 1-200); the result is still limited to 500 jobs.

        Returns:
            ``{jobs: [...], count}`` (plus ``truncated: true`` when more may exist);
            each job has ``jobId`` (or ``job_id``) and ``status``.
        """
        page_size = clamp(max_results, 1, LIST_INDUCTION_JOBS_MAX_RESULTS)
        jobs, truncated = collect_pages_capped(
            lambda tok: client.list_induction_jobs(namespace_id, status=status, max_results=page_size, next_token=tok),
            "jobs",
            "inductionJobs",
            max_items=_LIST_MAX_ITEMS,
        )
        return _listing("jobs", jobs, truncated)

    @tool
    def list_induced_datasources() -> dict[str, Any]:
        """List the datasources already in an accepted ontology (``ListInducedDatasources``, read-only).

        Only a source behind an ``accepted`` proposal is listed. Each entry has
        ``datasourceId``, the accepting ``proposal_id`` and ``ontology_id``, ``label``,
        ``tables_processed``, ``columns_processed``, ``novel_classes_created``,
        ``source_type`` and, when COA finds it, the source ``name``.

        Returns:
            ``{datasources: [...], count}`` (plus ``truncated: true`` when more than 500 may exist).
        """
        datasources, truncated = collect_pages_capped(
            lambda tok: client.list_induced_datasources(
                namespace_id, next_token=tok, max_results=LIST_INDUCED_DATASOURCES_MAX_RESULTS
            ),
            "datasources",
            max_items=_LIST_MAX_ITEMS,
        )
        return _listing("datasources", datasources, truncated)

    return [start_induction, get_induction_job, list_induction_jobs, list_induced_datasources]


# start_induction starts a billable job, even as a bridge dry run.
MUTATING_TOOL_NAMES = frozenset({"start_induction"})
