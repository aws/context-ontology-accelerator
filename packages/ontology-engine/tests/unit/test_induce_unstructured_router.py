# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the unstructured induction FastAPI router.

Namespace comes only from the ``?namespace=`` query parameter, and jobs and
proposals are scoped to ``source_type="UNSTRUCTURED"``.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_control_plane_server.models.induction_job_status import (
    InductionJobStatus,
)
from coa_ontology import dynamo_store as dynamo_store_module
from coa_ontology.inducer.routers import induce_unstructured
from coa_ontology.inducer.unstructured.schemas import (
    UnstructuredInductionRequest,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

_NS = "550e8400-e29b-41d4-a716-446655440001"
_NAME = "test-onto"
_GRAPH_ARN = "arn:aws:neptune-graph:us-east-1:000000000000:graph/g-test"
_ONTOLOGY_URI_PREFIX = f"{GRAPH_BASE_URI}/namespace/{_NS}/ontology/{_NAME}"


def _valid_request_body(namespace: str | None = None) -> dict[str, Any]:
    """Minimal valid POST body; its ``namespace`` is deliberately wrong.

    The router must take the namespace from the query param, never the body.
    """
    return {
        "namespace": namespace if namespace is not None else "body-namespace-IGNORED",
        "name": _NAME,
        "graph_arn": _GRAPH_ARN,
        "ontology_uri_prefix": _ONTOLOGY_URI_PREFIX,
    }


@pytest.fixture()
def test_app() -> FastAPI:
    """Fresh FastAPI app with ONLY the unstructured router mounted."""
    app = FastAPI()
    app.state.config = {}
    app.include_router(
        induce_unstructured.router,
        prefix="/induce/unstructured",
        tags=["induction-unstructured"],
    )
    return app


@pytest.fixture()
def client(test_app: FastAPI) -> TestClient:
    return TestClient(test_app)


@pytest.fixture()
def mock_dynamo():
    """Patch :mod:`dynamo_store` at the router's import site with ``autospec=True``.

    Defaults emulate an empty Dynamo (no pending proposals, no jobs).
    """
    with patch.object(induce_unstructured, "dynamo_store", autospec=True) as m:
        m.list_proposals.return_value = []
        m.put_job.return_value = None
        m.list_jobs.return_value = []
        m.get_job.return_value = None
        m.update_job_status.return_value = None
        m.create_proposal.return_value = None
        # In-flight lock + stub visibility. Default: lock is free (acquire
        # succeeds), stub write is a no-op. Autospec exposes the real
        # InductionLockHeldError class so a test can make acquire raise it.
        m.acquire_induction_lock.return_value = None
        m.create_proposal_stub.return_value = None
        m.release_induction_lock.return_value = None
        m.InductionLockHeldError = dynamo_store_module.InductionLockHeldError
        yield m


class TestStartInduction:
    """``POST /induce/unstructured/?namespace=<ns>`` — 202 + 409 cases."""

    def test_returns_202_and_job_id(self, client, mock_dynamo):
        """A valid POST with an explicit ``?namespace=`` returns 202 + job_id."""
        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["job_id"]  # present and non-empty
        assert body["status"] == InductionJobStatus.PENDING.value

        worker.assert_called_once()
        called_job_id, called_body, called_ns, called_config, called_language = worker.call_args.args
        assert called_language == "en"
        assert called_job_id == body["job_id"]
        assert isinstance(called_body, UnstructuredInductionRequest)
        assert called_body.graph_arn == _GRAPH_ARN
        assert called_ns == _NS
        # request.app.state.config (empty dict in the test app) is forwarded.
        assert called_config == {}

    def test_persists_job_with_explicit_namespace_and_source_type(self, client, mock_dynamo):
        """``put_job`` is called with the explicit ``namespace`` + UNSTRUCTURED."""
        with patch.object(induce_unstructured, "_run_unstructured_induction"):
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        assert response.status_code == 202, response.text
        mock_dynamo.put_job.assert_called_once()
        kwargs = mock_dynamo.put_job.call_args.kwargs
        assert kwargs["source_type"] == "UNSTRUCTURED"
        assert kwargs["status"] == InductionJobStatus.PENDING.value
        assert kwargs["namespace"] == _NS
        assert kwargs["job_id"] == response.json()["job_id"]
        # Request body persisted verbatim so an operator can replay the run.
        assert kwargs["request_body"]["graph_arn"] == _GRAPH_ARN
        assert kwargs["request_body"]["name"] == _NAME

    def test_pending_unstructured_proposal_returns_409(self, client, mock_dynamo):
        """A pending UNSTRUCTURED proposal blocks a new unstructured run.

        The 409 body surfaces the offending ``proposal_id`` so the client
        can route the user to the existing proposal's review page, and the
        in-flight probe is scoped to ``source_type="UNSTRUCTURED"``.
        """
        mock_dynamo.list_proposals.return_value = [{"proposal_id": "existing-unstr-prop-123", "namespace": _NS}]

        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        assert response.status_code == 409
        # FastAPI wraps HTTPException dict bodies under "detail".
        detail = response.json()["detail"]
        assert detail["proposal_id"] == "existing-unstr-prop-123"
        assert "unstructured" in detail["message"].lower()

        first_call_kwargs = mock_dynamo.list_proposals.call_args_list[0].kwargs
        assert first_call_kwargs["namespace"] == _NS
        assert first_call_kwargs["status"] == "pending"
        assert first_call_kwargs["source_type"] == "UNSTRUCTURED"

        # No job persisted, no worker spawned.
        mock_dynamo.put_job.assert_not_called()
        worker.assert_not_called()

    def test_updated_unstructured_proposal_returns_409(self, client, mock_dynamo):
        """A proposal in ``status="updated"`` also blocks a new run.

        The router probes ``"pending"`` first then falls back to
        ``"updated"`` — both are in-flight states, both scoped to
        UNSTRUCTURED.
        """
        mock_dynamo.list_proposals.side_effect = [
            [],  # pending probe — empty
            [{"proposal_id": "updated-unstr-prop-456", "namespace": _NS}],  # updated
        ]

        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        assert response.status_code == 409
        assert response.json()["detail"]["proposal_id"] == "updated-unstr-prop-456"

        assert mock_dynamo.list_proposals.call_count == 2
        statuses = [c.kwargs["status"] for c in mock_dynamo.list_proposals.call_args_list]
        assert statuses == ["pending", "updated"]
        for c in mock_dynamo.list_proposals.call_args_list:
            assert c.kwargs["source_type"] == "UNSTRUCTURED"

        worker.assert_not_called()

    @pytest.mark.parametrize("blocking_status", ["accept_failed", "accepting", "embeddings_sync"])
    def test_accept_state_unstructured_proposal_returns_409(self, client, mock_dynamo, blocking_status):
        """An unstructured proposal whose accept failed or is still merging
        blocks a new run, same as on the structured side. Unstructured
        proposals share the accept pipeline, so without these states a new
        induction could start mid-merge or right after a failed accept."""
        from coa_ontology.proposals import PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

        assert blocking_status in PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

        def _list(namespace, status, source_type):
            return [{"proposal_id": f"{status}-prop", "namespace": namespace}] if status == blocking_status else []

        mock_dynamo.list_proposals.side_effect = _list

        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        assert response.status_code == 409
        assert response.json()["detail"]["proposal_id"] == f"{blocking_status}-prop"
        for c in mock_dynamo.list_proposals.call_args_list:
            assert c.kwargs["source_type"] == "UNSTRUCTURED"
        mock_dynamo.release_induction_lock.assert_called_once_with(_NS)
        mock_dynamo.put_job.assert_not_called()
        worker.assert_not_called()

    def test_pending_structured_proposal_does_not_block_unstructured_run(self, client, mock_dynamo):
        """Source-type isolation: the in-flight probe is UNSTRUCTURED-scoped."""
        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        # No 409 — the run proceeds.
        assert response.status_code == 202, response.text
        assert response.json()["job_id"]

        assert mock_dynamo.list_proposals.call_count >= 1
        for c in mock_dynamo.list_proposals.call_args_list:
            assert c.kwargs["source_type"] == "UNSTRUCTURED", (
                "Source-type isolation broken: in-flight probe was not scoped "
                "to UNSTRUCTURED, so a pending STRUCTURED proposal could "
                "erroneously block a new unstructured run."
            )

        mock_dynamo.put_job.assert_called_once()
        assert mock_dynamo.put_job.call_args.kwargs["source_type"] == "UNSTRUCTURED"
        worker.assert_called_once()

    def test_inflight_lock_conflict_returns_409(self, client, mock_dynamo):
        """A held induction lock yields a 409 carrying the running job_id.

        No proposal row exists yet, so ``list_proposals`` is empty.
        """
        mock_dynamo.list_proposals.return_value = []  # no proposal row during the in-flight window
        mock_dynamo.acquire_induction_lock.side_effect = dynamo_store_module.InductionLockHeldError("running-job-1")

        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "in progress" in detail["message"].lower()
        assert detail["job_id"] == "running-job-1"
        # Lock conflict short-circuits BEFORE any writes or the worker spawn.
        mock_dynamo.put_job.assert_not_called()
        mock_dynamo.create_proposal_stub.assert_not_called()
        worker.assert_not_called()

    def test_trigger_writes_stub_and_acquires_lock_before_worker(self, client, mock_dynamo):
        """On a clean trigger the router acquires the lock and writes an
        in-progress PROPOSAL stub (visible immediately + refresh-safe) with the
        SAME id as the job, scoped UNSTRUCTURED — before spawning the worker."""
        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        mock_dynamo.acquire_induction_lock.assert_called_once()
        lock_args = mock_dynamo.acquire_induction_lock.call_args.args
        assert lock_args == (_NS, job_id)

        mock_dynamo.create_proposal_stub.assert_called_once()
        stub_kwargs = mock_dynamo.create_proposal_stub.call_args.kwargs
        assert stub_kwargs["proposal_id"] == job_id
        assert stub_kwargs["job_id"] == job_id
        assert stub_kwargs["namespace"] == _NS
        assert stub_kwargs["source_type"] == "UNSTRUCTURED"
        worker.assert_called_once()

    def test_lock_released_when_post_acquire_step_fails(self, client, mock_dynamo):
        """If a write AFTER acquiring the lock (put_job here) fails, the router
        must release the lock — otherwise the namespace stays wedged until the
        stale-takeover window. The worker's finally can't cover this because the
        worker thread never starts."""
        mock_dynamo.put_job.side_effect = RuntimeError("dynamo down")

        # Starlette's TestClient re-raises unhandled server exceptions, so the
        # RuntimeError propagates rather than becoming a 500 response.
        with (
            patch.object(induce_unstructured, "_run_unstructured_induction") as worker,
            pytest.raises(RuntimeError, match="dynamo down"),
        ):
            client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        # The worker never spawns, and crucially the lock is released so a
        # retry isn't blocked for an hour.
        mock_dynamo.acquire_induction_lock.assert_called_once()
        mock_dynamo.release_induction_lock.assert_called_once_with(_NS)
        worker.assert_not_called()

    def test_proposal_written_just_before_lock_acquire_still_blocks(self, client, mock_dynamo):
        """Check-then-act race: the running worker
        writes its pending proposal and releases the lock in the gap between a
        new trigger's proposal check and its lock acquire. The proposal check
        must run UNDER the lock so this trigger 409s instead of starting a
        duplicate run, and the lock it took must be released again."""

        def _acquire_after_worker_finished(namespace, job_id):
            mock_dynamo.list_proposals.return_value = [{"proposal_id": "prev-run", "namespace": namespace}]

        mock_dynamo.acquire_induction_lock.side_effect = _acquire_after_worker_finished

        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post(
                f"/induce/unstructured/?namespace={_NS}",
                json=_valid_request_body(),
            )

        assert response.status_code == 409
        assert response.json()["detail"]["proposal_id"] == "prev-run"
        mock_dynamo.release_induction_lock.assert_called_once_with(_NS)
        mock_dynamo.create_proposal_stub.assert_not_called()
        mock_dynamo.put_job.assert_not_called()
        worker.assert_not_called()

    def test_missing_namespace_query_param_returns_422(self, client, mock_dynamo):
        """Omitting ``?namespace=`` is a 422 — namespace is a required query param.

        Proves the router does NOT fall back to the body's ``namespace``
        field or to ``"default"``: with no query param the
        request is rejected outright.
        """
        with patch.object(induce_unstructured, "_run_unstructured_induction") as worker:
            response = client.post("/induce/unstructured/", json=_valid_request_body())

        assert response.status_code == 422
        mock_dynamo.put_job.assert_not_called()
        worker.assert_not_called()


class TestGetJob:
    """``GET /induce/unstructured/jobs/{job_id}?namespace=<ns>``."""

    def test_returns_job_when_source_type_is_unstructured(self, client, mock_dynamo):
        """An UNSTRUCTURED job is returned, and the explicit namespace is threaded."""
        mock_dynamo.get_job.return_value = {
            "job_id": "abc",
            "namespace": _NS,
            "status": "pending",
            "source_type": "UNSTRUCTURED",
            "ontology_uri_prefix": _ONTOLOGY_URI_PREFIX,
        }

        response = client.get(f"/induce/unstructured/jobs/abc?namespace={_NS}")

        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == "abc"
        assert body["source_type"] == "UNSTRUCTURED"
        mock_dynamo.get_job.assert_called_once_with(job_id="abc", namespace=_NS)

    def test_returns_404_when_job_missing(self, client, mock_dynamo):
        """A missing job returns 404."""
        mock_dynamo.get_job.return_value = None

        response = client.get(f"/induce/unstructured/jobs/missing?namespace={_NS}")

        assert response.status_code == 404
        assert response.json()["detail"] == "Job not found"

    def test_returns_404_when_source_type_is_structured(self, client, mock_dynamo):
        """Cross-source-type isolation: a STRUCTURED job leaks NO information.

        The router returns 404 (not 403) so the response does not reveal
        that a job with the same ID exists under a different source type.
        """
        mock_dynamo.get_job.return_value = {
            "job_id": "shared-id",
            "namespace": _NS,
            "status": "completed",
            "source_type": "STRUCTURED",
            "ontology_uri_prefix": _ONTOLOGY_URI_PREFIX,
        }

        response = client.get(f"/induce/unstructured/jobs/shared-id?namespace={_NS}")

        assert response.status_code == 404, (
            "Cross-source-type isolation broken: a STRUCTURED job was returned via the unstructured router."
        )
        assert response.json()["detail"] == "Job not found"

    def test_returns_404_when_source_type_attribute_missing(self, client, mock_dynamo):
        """Pre-delta items lacking ``source_type`` are hidden too.

        The router's check ``job.get("source_type") != "UNSTRUCTURED"``
        is True when the attribute is absent, so a pre-delta (structured)
        job never leaks through the unstructured endpoint.
        """
        mock_dynamo.get_job.return_value = {
            "job_id": "predelta",
            "namespace": _NS,
            "status": "completed",
            # NO source_type attribute — emulates pre-delta data.
            "ontology_uri_prefix": _ONTOLOGY_URI_PREFIX,
        }

        response = client.get(f"/induce/unstructured/jobs/predelta?namespace={_NS}")

        assert response.status_code == 404
        assert response.json()["detail"] == "Job not found"


class TestNamespaceThreading:
    """The ``?namespace=`` query value is the ONLY source of namespace.

    These tests use a namespace (``"foo"``) that differs from the body's
    ``namespace`` field so a regression where the router consulted the
    body (or defaulted to ``"default"``) would change the observed Dynamo
    call args and fail.
    """

    def test_list_jobs_threads_explicit_namespace_and_source_type(self, client, mock_dynamo):
        """``GET /jobs?namespace=foo`` calls ``list_jobs`` scoped to foo + UNSTRUCTURED."""
        mock_dynamo.list_jobs.return_value = [{"job_id": "j1", "namespace": "foo", "source_type": "UNSTRUCTURED"}]

        response = client.get("/induce/unstructured/jobs?namespace=foo")

        assert response.status_code == 200
        mock_dynamo.list_jobs.assert_called_once()
        kwargs = mock_dynamo.list_jobs.call_args.kwargs
        assert kwargs["namespace"] == "foo"
        assert kwargs["source_type"] == "UNSTRUCTURED"


class TestRunUnstructuredInductionWorker:
    """Direct, in-thread invocation of ``_run_unstructured_induction``."""

    def _make_request_body(self) -> UnstructuredInductionRequest:
        return UnstructuredInductionRequest(**_valid_request_body())

    def _patch_external_dependencies(self, stack: ExitStack) -> None:
        """Patch every AWS-touching client constructor used by the worker.

        ``spec=`` is required so the config's ``isinstance`` checks accept the mocks.
        """
        from coa_ontology.bedrock_embeddings import (
            BedrockEmbeddingClient,
        )
        from coa_ontology.inducer.unstructured.services.description_generator import (
            LLMDescriptionClient,
        )
        from coa_ontology.inducer.unstructured.services.iri_resolver import (
            IRIResolver,
        )
        from coa_ontology.inducer.unstructured.stores.protocol import (
            LexicalGraphStore,
        )

        spec_map = {
            "NeptuneAnalyticsLexicalStore": LexicalGraphStore,
            "BedrockEmbeddingClient": BedrockEmbeddingClient,
            "LLMClient": LLMDescriptionClient,
            "InMemoryIRIResolver": IRIResolver,
        }
        for name, cls in spec_map.items():
            stack.enter_context(
                patch.object(
                    induce_unstructured,
                    name,
                    return_value=MagicMock(spec=cls),
                )
            )

    def test_pipeline_exception_marks_job_failed_and_creates_no_proposal(self):
        """When the pipeline raises, the worker persists ``status="failed"``
        with the error message and creates NO proposal.
        """
        body = self._make_request_body()
        app_config: dict = {}

        boom = RuntimeError("synthetic pipeline failure")
        pipeline_mock = MagicMock()
        pipeline_mock.run.side_effect = boom

        with ExitStack() as stack:
            ds = stack.enter_context(patch.object(induce_unstructured, "dynamo_store", autospec=True))
            stack.enter_context(
                patch.object(
                    induce_unstructured,
                    "UnstructuredInductionPipeline",
                    return_value=pipeline_mock,
                )
            )
            self._patch_external_dependencies(stack)

            # The worker swallows the exception and persists failure.
            induce_unstructured._run_unstructured_induction("job-fail", body, _NS, app_config)

            pipeline_mock.run.assert_called_once()

            failed_calls = [
                c
                for c in ds.update_job_status.call_args_list
                if c.kwargs.get("status") == InductionJobStatus.FAILED.value
            ]
            assert len(failed_calls) == 1, (
                "Expected exactly one update_job_status(status='failed') call; "
                f"got {ds.update_job_status.call_args_list!r}"
            )
            failed_kwargs = failed_calls[0].kwargs
            assert failed_kwargs["job_id"] == "job-fail"
            assert failed_kwargs["namespace"] == _NS
            assert failed_kwargs["source_type"] == "UNSTRUCTURED"
            assert "RuntimeError" in failed_kwargs["error"]
            assert "synthetic pipeline failure" in failed_kwargs["error"]

            # No proposal created on the failure path.
            ds.create_proposal.assert_not_called()

    def test_pipeline_success_creates_proposal(self):
        """On success the worker creates a proposal record (UNSTRUCTURED, empty R2RML).

        This counterpart to the failure test makes the
        ``create_proposal.assert_not_called()`` invariant meaningful: we
        know the worker DOES create a proposal on success, so its absence
        on the failure path is a real guarantee rather than a vacuous one.
        """
        from datetime import UTC, datetime

        from coa_ontology.inducer.unstructured.schemas import (
            UnstructuredInductionReport,
        )

        body = self._make_request_body()
        app_config: dict = {}

        report = UnstructuredInductionReport(
            pipeline_run_id="run-ok",
            namespace=_NS,
            ontology_id=_NAME,
            tboxClassCount=12,
            objectPropertyCount=120,
            datatypePropertyCount=0,
            conceptCount=2,
            individualCount=0,
            closeMatchAxiomCount=1,
            description_mode="schema",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            turtle="@prefix ex: <http://example.com/> . ex:Foo a <http://www.w3.org/2002/07/owl#Class> .",
            warnings=["v0 limitation"],
        )

        pipeline_mock = MagicMock()
        pipeline_mock.run.return_value = report

        with ExitStack() as stack:
            ds = stack.enter_context(patch.object(induce_unstructured, "dynamo_store", autospec=True))
            stack.enter_context(
                patch.object(
                    induce_unstructured,
                    "UnstructuredInductionPipeline",
                    return_value=pipeline_mock,
                )
            )
            self._patch_external_dependencies(stack)

            induce_unstructured._run_unstructured_induction("job-ok", body, _NS, app_config)

            ds.create_proposal.assert_called_once()
            create_kwargs = ds.create_proposal.call_args.kwargs
            assert create_kwargs["source_type"] == "UNSTRUCTURED"
            assert create_kwargs["proposal_id"] == "job-ok"
            assert create_kwargs["job_id"] == "job-ok"
            assert create_kwargs["namespace"] == _NS
            assert create_kwargs["ontology_turtle"] == report.turtle
            assert create_kwargs["r2rml_turtle"] == ""  # empty for unstructured

    def test_watchdog_heartbeat_ticks_during_slow_run(self, monkeypatch):
        """The heartbeat calls ``touch_job`` while a slow ``pipeline.run`` blocks.

        The heartbeat reuses the structured ``induce_catalog._watchdog_loop``,
        which calls ``induce_catalog.dynamo_store.touch_job`` — a DIFFERENT
        binding from the ``induce_unstructured.dynamo_store`` mock — so
        ``touch_job`` is patched on the shared real module object.
        """
        import threading
        from datetime import UTC, datetime

        from coa_ontology import induce_catalog
        from coa_ontology.inducer.unstructured.schemas import (
            UnstructuredInductionReport,
        )

        # Tight heartbeat so a tick lands immediately. The worker reads this via
        # a lazy ``from induce_catalog import _HEARTBEAT_INTERVAL_SECONDS`` on
        # each call, so patching the module attr before invocation takes effect.
        monkeypatch.setattr(induce_catalog, "_HEARTBEAT_INTERVAL_SECONDS", 0.001)

        body = self._make_request_body()
        app_config: dict = {}

        report = UnstructuredInductionReport(
            pipeline_run_id="run-hb",
            namespace=_NS,
            ontology_id=_NAME,
            tboxClassCount=1,
            objectPropertyCount=0,
            datatypePropertyCount=0,
            conceptCount=0,
            individualCount=0,
            closeMatchAxiomCount=0,
            description_mode="schema",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            turtle="@prefix ex: <http://example.com/> . ex:Foo a <http://www.w3.org/2002/07/owl#Class> .",
            warnings=[],
        )

        ticked = threading.Event()

        def _blocking_run(config, on_status):
            # Block until the watchdog has bumped updated_at at least once, so
            # the assertion below can't pass just because the run outlived a tick.
            assert ticked.wait(timeout=5), "watchdog never ticked during the blocking run"
            return report

        pipeline_mock = MagicMock()
        pipeline_mock.run.side_effect = _blocking_run

        with ExitStack() as stack:
            ds = stack.enter_context(patch.object(induce_unstructured, "dynamo_store", autospec=True))
            # Watchdog uses induce_catalog's binding of the shared dynamo_store
            # module — patch touch_job on the real module so the tick is observed.
            stack.enter_context(
                patch.object(dynamo_store_module, "touch_job", side_effect=lambda *a, **k: ticked.set())
            )
            # Stub the per-tick OOM-observability emit (shared watchdog loop) so
            # the heartbeat doesn't make a live PutMetricData call. Emission is
            # covered by test_watchdog_metric_emission.
            stack.enter_context(patch.object(induce_catalog, "emit_induction_heartbeat_metrics"))
            stack.enter_context(
                patch.object(induce_unstructured, "UnstructuredInductionPipeline", return_value=pipeline_mock)
            )
            self._patch_external_dependencies(stack)

            induce_unstructured._run_unstructured_induction("job-hb", body, _NS, app_config)

            pipeline_mock.run.assert_called_once()
            # The heartbeat ran during the blocking run and touched THIS job —
            # keeping updated_at fresh so it stays counted / is never swept.
            dynamo_store_module.touch_job.assert_any_call("job-hb", _NS)
            # And the worker still completed the success path (proposal created).
            ds.create_proposal.assert_called_once()

            statuses = [c.kwargs.get("status") for c in ds.update_job_status.call_args_list]
            assert InductionJobStatus.COMPLETED.value in statuses
            assert InductionJobStatus.FAILED.value not in statuses

    def _run_and_capture_config(self, *, body, app_config, resolved_ids=None):
        """Run the worker inline with all external clients patched and return
        the ``UnstructuredInductionConfig`` the worker passed to the pipeline.

        ``resolved_ids`` is what the shared grounding-ID resolver (patched
        here) returns — this decouples the worker's build-service-iff-resolved
        wiring from the resolver's own logic (covered in
        test_resolve_grounding_ids.py). Also patches the grounding-service
        constructors so no real AWS/HTTP client is built.
        """
        from datetime import UTC, datetime

        from coa_ontology.inducer.services.grounding import GroundingService
        from coa_ontology.inducer.unstructured.schemas import (
            UnstructuredInductionReport,
        )

        report = UnstructuredInductionReport(
            pipeline_run_id="run",
            namespace=_NS,
            ontology_id=_NAME,
            tboxClassCount=1,
            objectPropertyCount=0,
            datatypePropertyCount=0,
            conceptCount=0,
            individualCount=0,
            closeMatchAxiomCount=0,
            foundationalGroundedCount=0,
            description_mode="schema",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            turtle="@prefix ex: <http://example.com/> .",
            warnings=[],
        )
        pipeline_mock = MagicMock()
        pipeline_mock.run.return_value = report

        with ExitStack() as stack:
            stack.enter_context(patch.object(induce_unstructured, "dynamo_store", autospec=True))
            stack.enter_context(
                patch.object(induce_unstructured, "UnstructuredInductionPipeline", return_value=pipeline_mock)
            )
            self._patch_external_dependencies(stack)
            # The worker resolves grounding ids via the shared resolver (imported
            # inside the worker from induce_catalog). Patch it at the source
            # module so the lazy import picks up the mock.
            stack.enter_context(
                patch(
                    "coa_ontology.induce_catalog.resolve_grounding_ontology_ids",
                    return_value=list(resolved_ids or []),
                )
            )
            # The grounding-service dependencies are only constructed when
            # grounding resolves; patch them so no real client/store is built.
            # The worker builds a namespace-bound StoreOntologyCatalogAdapter
            # via build_stores(ns) (imported lazily from
            # coa_ontology.stores / .stores.adapters) — patch at
            # those source modules so the lazy imports pick up the mocks.
            stack.enter_context(patch("coa_ontology.stores.build_stores", return_value=(MagicMock(), MagicMock())))
            stack.enter_context(
                patch(
                    "coa_ontology.stores.adapters.StoreOntologyCatalogAdapter",
                    return_value=MagicMock(),
                )
            )
            stack.enter_context(patch.object(induce_unstructured, "EmbeddingGeneratorClient", return_value=MagicMock()))
            grounding_service_cls = stack.enter_context(
                patch.object(
                    induce_unstructured,
                    "GroundingService",
                    return_value=MagicMock(spec=GroundingService),
                )
            )

            induce_unstructured._run_unstructured_induction("job", body, _NS, app_config)

            # Expose the GroundingService constructor mock so a test can assert
            # the model/region it was built with.
            self._grounding_service_cls = grounding_service_cls

            pipeline_mock.run.assert_called_once()
            # First positional arg to pipeline.run is the config.
            return pipeline_mock.run.call_args.args[0]

    def test_grounding_wired_when_ids_resolve(self):
        """When grounding selections RESOLVE to loaded URIs, the worker builds a
        GroundingService and threads the resolved URIs onto the config."""
        body = UnstructuredInductionRequest(
            **_valid_request_body(),
            grounding_ontology_ids=["fibo-agreements"],  # a curated key
            grounding_mode="ENHANCED",
        )
        app_config = {
            "ontology_catalog_url": "http://catalog",
            "embedding_generator_url": "http://embed",
        }
        config = self._run_and_capture_config(body=body, app_config=app_config, resolved_ids=["https://fibo/FBC/"])
        # Config carries the RESOLVED URI, not the raw curated key.
        assert config.grounding_ontology_ids == ["https://fibo/FBC/"]
        assert config.grounding_mode == "ENHANCED"
        assert config.grounding_service is not None

    def test_grounding_service_uses_configured_model_and_region(self):
        """GROUNDING_LLM_MODEL_ID (resolved into
        ``grounding_llm_model_id``) overrides the induction default and pairs
        with ``llm_region``, so a non-US Region can name a profile it can
        invoke instead of the us. default."""
        body = UnstructuredInductionRequest(
            **_valid_request_body(),
            grounding_ontology_ids=["fibo-agreements"],
            grounding_mode="ENHANCED",
        )
        app_config = {
            "ontology_catalog_url": "http://catalog",
            "embedding_generator_url": "http://embed",
            "llm_region": "ap-northeast-1",
            "llm_model_id": "global.anthropic.claude-sonnet-5",
            # Resolved by main.py from GROUNDING_LLM_MODEL_ID (the override).
            "grounding_llm_model_id": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
        }
        config = self._run_and_capture_config(body=body, app_config=app_config, resolved_ids=["https://fibo/FBC/"])
        assert config.grounding_service is not None
        self._grounding_service_cls.assert_called_once()
        kwargs = self._grounding_service_cls.call_args.kwargs
        assert kwargs["llm_model_id"] == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert kwargs["llm_region"] == "ap-northeast-1"

    def test_grounding_service_falls_back_when_grounding_model_id_is_empty(self):
        """An empty grounding model id must not reach Bedrock as an empty
        modelId; it falls back to the Haiku rerank default."""
        body = UnstructuredInductionRequest(
            **_valid_request_body(),
            grounding_ontology_ids=["fibo-agreements"],
            grounding_mode="ENHANCED",
        )
        app_config = {
            "ontology_catalog_url": "http://catalog",
            "embedding_generator_url": "http://embed",
            "llm_region": "us-east-1",
            "llm_model_id": "us.anthropic.claude-sonnet-4-6",
            "grounding_llm_model_id": "",
        }
        self._run_and_capture_config(body=body, app_config=app_config, resolved_ids=["https://fibo/FBC/"])
        kwargs = self._grounding_service_cls.call_args.kwargs
        assert kwargs["llm_model_id"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_grounding_skipped_when_no_selection(self):
        """No grounding_ontology_ids on the request → resolver isn't consulted,
        grounding_service is None, pipeline skips grounding."""
        body = self._make_request_body()  # no grounding fields
        app_config = {
            "ontology_catalog_url": "http://catalog",
            "embedding_generator_url": "http://embed",
        }
        config = self._run_and_capture_config(body=body, app_config=app_config)
        # Empty resolved pool is passed AS an empty list. Both [] and None mean
        # "no grounding scope" (there is no unscoped/global recall), and
        # grounding_service is None so the pipeline skips grounding.
        assert config.grounding_ontology_ids == []
        assert config.grounding_service is None

    def test_grounding_skipped_when_selection_resolves_to_nothing(self):
        """A selection that resolves to an EMPTY list (e.g. unloadable ontology)
        must NOT build a GroundingService — the pipeline stays in the
        no-grounding path rather than grounding against zero ontologies."""
        body = UnstructuredInductionRequest(
            **_valid_request_body(),
            grounding_ontology_ids=["https://unresolvable/"],
        )
        app_config = {
            "ontology_catalog_url": "http://catalog",
            "embedding_generator_url": "http://embed",
        }
        config = self._run_and_capture_config(body=body, app_config=app_config, resolved_ids=[])
        assert config.grounding_service is None
        # Empty pool → empty list (explicit "no scope"). Both [] and None mean
        # "no grounding scope"; there is no unscoped/global recall.
        assert config.grounding_ontology_ids == []
