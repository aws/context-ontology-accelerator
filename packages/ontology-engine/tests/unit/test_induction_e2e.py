# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""End-to-end induction test.

Mocks external services (Bedrock, DynamoDB, data-catalog HTTP, ontology-catalog)
and runs the full induction pipeline synchronously, then verifies the proposal
ontology and R2RML output.

Run:
    cd ontology-workbench
    pytest tests/test_induction_e2e.py -v
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

# Force HTTP catalog source for tests (must be set before app import)
os.environ.setdefault("CATALOG_SOURCE", "http")

import pytest
from fastapi.testclient import TestClient
from rdflib import OWL, RDF, Graph, URIRef

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_induction_globals():
    """Snapshot/restore the ``induce_catalog`` process globals per test.

    ``_run_induction`` and the router mutate ``_jobs`` / ``_job_namespaces`` as
    module globals (job register + namespace lookup). Without teardown, an entry
    leaked by a mid-test assertion failure changes the behavior of unrelated
    tests under some collection orderings. Snapshot before, restore after —
    pytest teardown runs even when assertions fail. Mirrors the guard in
    ``test_induce_catalog_cost_metrics.py``.

    NB: the in-flight induction LOCK and proposal rows live in the ``client``
    fixture's per-test in-memory DynamoDB, not here; the worker-thread reaping
    that keeps those hermetic is done in ``client`` itself (it must run while
    that fixture's ``_get_table`` patch is still active — see the note there).
    """
    from coa_ontology import induce_catalog

    jobs_snapshot = dict(induce_catalog._jobs)
    namespaces_snapshot = dict(induce_catalog._job_namespaces)
    try:
        yield
    finally:
        induce_catalog._jobs.clear()
        induce_catalog._jobs.update(jobs_snapshot)
        induce_catalog._job_namespaces.clear()
        induce_catalog._job_namespaces.update(namespaces_snapshot)


# Load sample catalog
SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "data-catalog", "sample_data")
with open(os.path.join(SAMPLE_DIR, "catalog1.json")) as f:
    CATALOG = json.load(f)

# Index by datasource ID
CATALOGS = {s["datasourceId"]: s for s in CATALOG["sources"]}


def _fake_embed_text(text: str) -> list[float]:
    """Deterministic fake embedding: hash text into a 16-dim vector."""
    h = hash(text) & 0xFFFFFFFF
    return [((h >> i) & 0xF) / 15.0 for i in range(16)]


@pytest.fixture()
def client():
    """Create test client with all external services mocked."""
    # Force HTTP catalog source so the mock _fetch_catalog is used
    os.environ["CATALOG_SOURCE"] = "http"
    # Mock DynamoDB — store in-memory dict
    dynamo_data = {}

    def fake_put_item(Item, **kwargs):
        # Honor the induction-lock conditional put. The production code uses
        # exactly one ConditionExpression here:
        #   "attribute_not_exists(PK) OR attribute_not_exists(acquired_ts) OR acquired_ts < :stale"
        # Evaluate it against the current stored item so the lock's acquire /
        # stale-takeover semantics are actually exercised (not just tolerated).
        cond = kwargs.get("ConditionExpression")
        if cond:
            existing = dynamo_data.get((Item["PK"], Item["SK"]))
            values = kwargs.get("ExpressionAttributeValues", {})
            acquired = existing.get("acquired_ts") if existing else None
            ok = existing is None or acquired is None or (":stale" in values and acquired < values[":stale"])
            if not ok:
                from botocore.exceptions import ClientError

                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conditional put failed"}},
                    "PutItem",
                )
        dynamo_data[(Item["PK"], Item["SK"])] = Item

    def fake_get_item(Key):
        item = dynamo_data.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def fake_delete_item(Key):
        dynamo_data.pop((Key["PK"], Key["SK"]), None)

    def fake_update_item(Key, UpdateExpression, **kwargs):
        item = dynamo_data.get((Key["PK"], Key["SK"]), {**Key})
        # Crude SET parser for tests
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {})
        for part in UpdateExpression.replace("SET ", "").split(", "):
            field, val_ref = [s.strip() for s in part.split("=")]
            # Resolve attribute name aliases
            for alias, real in (names or {}).items():
                field = field.replace(alias, real)
            item[field] = values.get(val_ref, val_ref)
        dynamo_data[(Key["PK"], Key["SK"])] = item

    def fake_scan(**kwargs):
        items = []
        filter_expr = kwargs.get("FilterExpression", "")
        values = kwargs.get("ExpressionAttributeValues", {})
        limit = kwargs.get("Limit", 50)
        for (pk, sk), item in dynamo_data.items():
            if "#PROPOSAL#" in pk and sk == "META":
                # Respect status filter if present
                if "#s = :st" in filter_expr and ":st" in values and item.get("status") != values[":st"]:
                    continue
                items.append(item)
                if len(items) >= limit:
                    break
        return {"Items": items}

    def fake_query(**kwargs):
        # Namespace-registry Query: PK = NAMESPACE#{ns} AND begins_with(SK,
        # "ONTOLOGY#"), with an optional ontology_type FilterExpression.
        # list_ontologies_registry now paginates on LastEvaluatedKey, so this
        # MUST return a real dict (no LastEvaluatedKey) rather than a bare
        # MagicMock — otherwise resp.get("LastEvaluatedKey") is truthy forever
        # and the drain-all loop spins.
        values = kwargs.get("ExpressionAttributeValues", {})
        pk = values.get(":pk")
        skp = values.get(":skp", "")
        want_type = values.get(":otype")
        items = []
        for (ipk, isk), item in dynamo_data.items():
            if ipk != pk or not isk.startswith(skp):
                continue
            if want_type is not None and item.get("ontology_type") != want_type:
                continue
            items.append(item)
        return {"Items": items}

    mock_table = MagicMock()
    mock_table.put_item = MagicMock(side_effect=fake_put_item)
    mock_table.get_item = MagicMock(side_effect=fake_get_item)
    mock_table.update_item = MagicMock(side_effect=fake_update_item)
    mock_table.delete_item = MagicMock(side_effect=fake_delete_item)
    mock_table.scan = MagicMock(side_effect=fake_scan)
    mock_table.query = MagicMock(side_effect=fake_query)

    # Mock Bedrock embedding client
    mock_bedrock = MagicMock()
    mock_bedrock.generate_lexical = MagicMock(
        side_effect=lambda ontology_id, classes, backend=None, store=True: (
            [{"vector": _fake_embed_text(c.get("labels", [""])[0]), "dimensions": 16} for c in classes],
            "fake-titan-v2",
        )
    )

    # Mock data-catalog HTTP call
    def fake_fetch_catalog(data_catalog_url, datasource_id):
        if datasource_id not in CATALOGS:
            raise ValueError(f"Unknown datasource: {datasource_id}")
        return CATALOGS[datasource_id]

    # ignore_cleanup_errors: induction runs in a FastAPI BackgroundTask thread that
    # may still be writing into tmpdir when the test returns (the endpoint replies
    # 202 immediately). Without this, teardown races the thread and raises
    # "OSError: [Errno 39] Directory not empty". Tolerate the residual files.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        os.environ["INDUCE_OUTPUT_DIR"] = tmpdir
        os.environ["CATALOG_SOURCE"] = "http"

        # Mock S3 — store artifacts in memory
        s3_store = {}
        mock_s3 = MagicMock()

        def fake_put_object(Bucket, Key, Body, **kwargs):
            s3_store[Key] = Body if isinstance(Body, bytes) else Body.encode("utf-8")

        def fake_get_object(Bucket, Key):
            if Key not in s3_store:
                raise mock_s3.exceptions.NoSuchKey({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            body = MagicMock()
            body.read.return_value = s3_store[Key]
            return {"Body": body}

        def fake_copy_object(Bucket, Key, CopySource, **kwargs):
            src_key = CopySource["Key"]
            if src_key in s3_store:
                s3_store[Key] = s3_store[src_key]

        def fake_head_object(Bucket, Key, **kwargs):
            if Key not in s3_store:
                # Real S3 raises botocore ClientError (404) for a missing key —
                # match that so presign_proposal_artifact's `except ClientError`
                # (which returns None for an absent artifact, e.g. an in-progress
                # stub proposal) is exercised the same way it is in production.
                from botocore.exceptions import ClientError

                raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
            return {"ContentLength": len(s3_store[Key])}

        def fake_generate_presigned_url(ClientMethod, Params=None, ExpiresIn=3600, **kwargs):
            params = Params or {}
            return f"https://s3.test.local/{params.get('Bucket', 'bucket')}/{params.get('Key', '')}?sig=test"

        mock_s3.put_object = MagicMock(side_effect=fake_put_object)
        mock_s3.get_object = MagicMock(side_effect=fake_get_object)
        mock_s3.copy_object = MagicMock(side_effect=fake_copy_object)
        mock_s3.head_object = MagicMock(side_effect=fake_head_object)
        mock_s3.generate_presigned_url = MagicMock(side_effect=fake_generate_presigned_url)
        mock_s3.exceptions = MagicMock()
        mock_s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with (
            patch("coa_ontology.dynamo_store._get_table", return_value=mock_table),
            patch("coa_ontology.dynamo_store._get_s3", return_value=mock_s3),
            patch("coa_ontology.induce_catalog._fetch_catalog", side_effect=fake_fetch_catalog),
            # Stub the CloudWatch metrics seam. In production this is best-effort
            # (it swallows its own errors), but the REAL call builds a boto3
            # cloudwatch client and does a PutMetricData that, in the unmocked
            # test env, fails only after a slow STS/GetCallerIdentity retry. That
            # runs in the worker's finally BETWEEN setting the job COMPLETED and
            # release_induction_lock — widening that window enough that a test
            # which polls "completed" and immediately re-triggers hits the
            # still-held lock (409 instead of 202), an ordering/timing flake.
            # Stubbing it keeps lock release prompt and deterministic without
            # touching the lock/visibility behavior the tests assert.
            patch("coa_ontology.induce_catalog.emit_induction_job_metrics"),
        ):
            from coa_ontology.main import _patched_build_pipeline, app

            # Ensure test uses HTTP catalog (mocked), not SMUS
            app.state.config["catalog_source"] = "http"

            # Patch the pipeline builder to use fake embeddings
            def _test_build_pipeline(config):
                pipeline = _patched_build_pipeline(config)
                pipeline.eg = mock_bedrock
                return pipeline

            from coa_ontology import induce_catalog

            # Record every REAL induction worker this test starts so we can join
            # them before the mocks unwind. A worker sets its job to COMPLETED
            # *before* its finally writes the proposal + releases the lock, so a
            # test's poll loop returns while the worker is still executing its
            # tail. Since dynamo_store._get_table is patched at MODULE level
            # (shared across threads), a straggler surviving into the NEXT test
            # calls _get_table() and resolves THAT test's fresh in-memory table —
            # writing a phantom STRUCTURED proposal / lock row into it. The next
            # fresh induction then sees a pending proposal (or held lock) and
            # wrongly returns 409 instead of 202. This flakes only under some
            # PYTHONHASHSEED orderings because test order + thread-finish timing
            # shift with the seed. Recording at start() (instead of matching
            # threads by _target) is robust: CPython clears Thread._target once
            # run() returns, so an enumerate-based match races the worker's exit.
            started_workers: list = []
            _RealThread = induce_catalog.Thread

            class _RecordingThread(_RealThread):
                def start(self):
                    started_workers.append(self)
                    super().start()

            # Suppress-worker tests re-patch induce_catalog.Thread with a
            # MagicMock inside their own `with patch(...)`, which shadows this for
            # their scope — they start no real worker, so started_workers stays
            # empty for them. All other tests get the recording wrapper.
            with (
                patch.object(
                    __import__("coa_ontology.induce_catalog", fromlist=["_build_pipeline"]),
                    "_build_pipeline",
                    _test_build_pipeline,
                ),
                patch.object(induce_catalog, "Thread", _RecordingThread),
            ):
                try:
                    yield TestClient(app), tmpdir, dynamo_data
                finally:
                    # Join inside the patch context so a joined worker's tail
                    # writes resolve THIS test's mock table, not a torn-down one.
                    for _worker in started_workers:
                        _worker.join(timeout=30)


class TestInductionE2E:
    """Full induction flow: submit → poll → check proposal → review → accept."""

    def test_induction_healthcare(self, client):
        http, tmpdir, dynamo = client

        # Step 1: Submit induction
        resp = http.post(
            "/induce/?namespace=default",
            json={
                "datasource_ids": ["ds-def789"],
                "ontology_uri_prefix": "https://test.com/ontology/healthcare#",
                "label": "Healthcare Test",
            },
        )
        assert resp.status_code == 202
        job = resp.json()
        job_id = job["job_id"]
        # The induction runs on a background thread; by the time this response
        # is read it may already have progressed past the initial phase. Accept
        # any non-terminal status — Step 2 polls through to completion, which is
        # the assertion that actually matters.
        assert job["status"] not in ("completed", "failed")

        # Step 2: Poll until done (sync in test — background thread)
        import time

        for _ in range(30):
            resp = http.get(f"/induce/jobs/{job_id}?namespace=default")
            if resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)

        result = resp.json()
        assert result["status"] == "completed", f"Job failed: {result.get('error')}"

        report = result["report"]
        assert report["tables_processed"] == 3
        assert report["columns_processed"] == 11
        assert report["novel_classes_created"] > 0
        assert report["induced_ontology_id"] is not None  # proposal_id

        # Step 3: Verify proposal in DynamoDB
        proposal_id = report["induced_ontology_id"]
        resp = http.get(f"/ontology/proposals/{proposal_id}")
        assert resp.status_code == 200
        proposal = resp.json()
        assert proposal["status"] == "pending"
        assert proposal["ontology_id"] == "https://test.com/ontology/healthcare#"
        # Turtle is served out-of-band via presigned S3 URLs, not inlined.
        assert proposal["ontology_url"]
        import coa_ontology.dynamo_store as ds

        stored = ds.get_proposal_by_id(proposal_id, namespace="default")
        ontology_turtle = stored.get("ontology_turtle", "")
        r2rml_turtle = stored.get("r2rml_turtle", "")
        assert ontology_turtle, "expected ontology_turtle hydrated from the store"

        # Regression for #536: column_matches was previously written inline into the
        # DDB proposal item (cap-protected only at [:100]) but had zero readers
        # anywhere in the codebase — dead data behind a silent ceiling that risked
        # blowing the 400 KB DDB item limit on wide schemas. The write site was
        # deleted; full match data (including column-level) remains available via
        # InductionReport and the S3 matches.json artifact.
        assert "column_matches" not in (stored.get("metadata") or {})

        # Step 4: Parse and verify the proposal ontology
        g = Graph()
        g.parse(data=ontology_turtle, format="turtle")

        classes = set(g.subjects(RDF.type, OWL.Class))
        obj_props = set(g.subjects(RDF.type, OWL.ObjectProperty))
        dt_props = set(g.subjects(RDF.type, OWL.DatatypeProperty))

        class_names = {str(c).split("#")[-1] for c in classes}
        assert "Claims" in class_names
        assert "Patients" in class_names
        assert "Providers" in class_names

        # FK columns should be object properties
        obj_prop_names = {str(p).split("#")[-1] for p in obj_props}
        assert "claims_patientId" in obj_prop_names
        assert "claims_providerId" in obj_prop_names

        # Non-FK columns should be datatype properties
        dt_prop_names = {str(p).split("#")[-1] for p in dt_props}
        assert "claims_claimId" in dt_prop_names
        assert "patients_dateOfBirth" in dt_prop_names

        # Step 5: Verify R2RML
        r2rml = r2rml_turtle
        assert r2rml
        rg = Graph()
        rg.parse(data=r2rml, format="turtle")
        RR = "http://www.w3.org/ns/r2rml#"
        triples_maps = set(rg.subjects(RDF.type, URIRef(f"{RR}TriplesMap")))
        assert len(triples_maps) == 3  # claims, patients, providers

        # Step 6: List proposals
        resp = http.get("/ontology/proposals/")
        assert resp.status_code == 200
        proposals = resp.json()
        assert len(proposals) >= 1
        assert any(p["proposal_id"] == proposal_id for p in proposals)

        # Step 7: Update proposal (user review)
        modified_turtle = ontology_turtle.replace("Proposal Ontology", "User Reviewed Ontology")
        resp = http.post(
            "/ontology/proposals/update",
            json={
                "proposal_id": proposal_id,
                "ontology_turtle": modified_turtle,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        # Verify update persisted (Turtle served out-of-band; read via the store)
        resp = http.get(f"/ontology/proposals/{proposal_id}")
        assert resp.status_code == 200
        assert "User Reviewed Ontology" in ds.get_proposal_by_id(proposal_id, namespace="default")["ontology_turtle"]

        # Step 7b: Update via the presigned-PUT path (large-ontology bypass).
        # The client first asks for presigned PUT URLs, uploads the edited
        # Turtle straight to S3 (simulated here by writing to the artifact's
        # deterministic key), then commits with ``ontology_uploaded`` instead
        # of sending the (possibly multi-MB) Turtle inline through the 6 MB
        # request cap.
        resp = http.post(f"/ontology/proposals/{proposal_id}/upload-url")
        assert resp.status_code == 200, resp.text
        urls = resp.json()
        assert urls["ontology_put_url"] and urls["r2rml_put_url"]

        uploaded_turtle = ontology_turtle.replace("Proposal Ontology", "Uploaded Reviewed Ontology")
        ds._put_artifact_s3("default", proposal_id, "ontology", uploaded_turtle)
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": proposal_id, "ontology_uploaded": True},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "updated"
        assert (
            "Uploaded Reviewed Ontology" in ds.get_proposal_by_id(proposal_id, namespace="default")["ontology_turtle"]
        )

        # Step 8: Verify files on disk
        job_dir = os.path.join(tmpdir, job_id)
        assert os.path.exists(os.path.join(job_dir, "proposal-ontology.ttl"))
        assert os.path.exists(os.path.join(job_dir, "r2rml-mappings.ttl"))

    def test_induction_multiple_datasources(self, client):
        http, tmpdir, dynamo = client

        resp = http.post(
            "/induce/?namespace=default",
            json={
                "datasource_ids": ["ds-abc123", "ds-ice456"],
                "ontology_uri_prefix": "https://test.com/ontology/multi#",
                "label": "Multi-source Test",
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        import time

        for _ in range(30):
            resp = http.get(f"/induce/jobs/{job_id}?namespace=default")
            if resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)

        result = resp.json()
        assert result["status"] == "completed", f"Job failed: {result.get('error')}"
        # ds-abc123: 2 tables (orders, customers), ds-ice456: 2 tables (products, inventory_snapshots)
        assert result["report"]["tables_processed"] == 4

    def test_induction_unknown_datasource(self, client):
        http, tmpdir, dynamo = client

        resp = http.post(
            "/induce/?namespace=default",
            json={
                "datasource_ids": ["ds-nonexistent"],
                "ontology_uri_prefix": "https://test.com/ontology/bad#",
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        import time

        for _ in range(30):
            resp = http.get(f"/induce/jobs/{job_id}?namespace=default")
            if resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)

        assert resp.json()["status"] == "failed"

    def test_reject_proposal(self, client):
        http, tmpdir, dynamo = client

        # Run induction first
        resp = http.post(
            "/induce/?namespace=default",
            json={
                "datasource_ids": ["ds-abc123"],
                "ontology_uri_prefix": "https://test.com/ontology/reject#",
            },
        )
        job_id = resp.json()["job_id"]

        import time

        for _ in range(30):
            resp = http.get(f"/induce/jobs/{job_id}?namespace=default")
            if resp.json()["status"] == "completed":
                break
            time.sleep(0.5)

        proposal_id = resp.json()["report"]["induced_ontology_id"]

        # Reject it
        resp = http.post(
            "/ontology/proposals/reject",
            json={
                "proposal_ids": [proposal_id],
                "reason": "Not needed",
            },
        )
        assert resp.status_code == 200
        assert resp.json()[0]["status"] == "rejected"

        # Verify status
        resp = http.get(f"/ontology/proposals/{proposal_id}")
        assert resp.json()["status"] == "rejected"

    def test_inflight_proposal_blocks_new_induction(self, client):
        http, tmpdir, dynamo = client

        import time

        # Start first induction
        resp = http.post(
            "/induce/?namespace=default",
            json={
                "datasource_ids": ["ds-abc123"],
                "ontology_uri_prefix": "https://test.com/ontology/first#",
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Wait for completion (creates a pending proposal)
        for _ in range(30):
            resp = http.get(f"/induce/jobs/{job_id}?namespace=default")
            if resp.json()["status"] == "completed":
                break
            time.sleep(0.5)

        # Attempt second induction — should be blocked
        resp = http.post(
            "/induce/?namespace=default",
            json={
                "datasource_ids": ["ds-ice456"],
                "ontology_uri_prefix": "https://test.com/ontology/second#",
            },
        )
        assert resp.status_code == 409
        body = resp.json()["detail"]
        assert "in-flight proposal" in body["message"]
        assert "proposal_id" in body

        # Reject the pending proposal
        proposal_id = body["proposal_id"]
        resp = http.post(
            "/ontology/proposals/reject",
            json={"proposal_ids": [proposal_id], "reason": "clearing"},
        )
        assert resp.status_code == 200

        # Now induction should succeed
        resp = http.post(
            "/induce/?namespace=default",
            json={
                "datasource_ids": ["ds-ice456"],
                "ontology_uri_prefix": "https://test.com/ontology/second#",
            },
        )
        assert resp.status_code == 202

        # Drain the background induction to completion before the fixture tears
        # down tmpdir — otherwise the still-running thread races TemporaryDirectory
        # cleanup ("OSError: Directory not empty").
        job_id = resp.json()["job_id"]
        for _ in range(30):
            resp = http.get(f"/induce/jobs/{job_id}?namespace=default")
            if resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)


class TestInductionInFlightVisibilityAndLock:
    """Regression tests for the in-flight window (issue I-3f1f50cc).

    The prior guard only blocked once a pending PROPOSAL row existed — which
    happens at worker END — so nothing was visible during the run and a second
    trigger slipped through. These tests suppress the worker thread so the
    trigger-time state (stub proposal + lock) is observable, and assert:
      1. a stub proposal (status ``inducing``) is listed immediately;
      2. a second trigger during the in-flight window is blocked with 409;
      3. releasing the lock (worker end) lets a new run start.
    """

    def test_stub_proposal_visible_immediately_at_trigger(self, client):
        http, tmpdir, dynamo = client
        # Suppress the worker so the run stays "in flight" after the POST.
        with patch("coa_ontology.induce_catalog.Thread"):
            resp = http.post(
                "/induce/?namespace=default",
                json={
                    "datasource_ids": ["ds-abc123"],
                    "ontology_uri_prefix": "https://test.com/ontology/vis#",
                    "label": "Vis Test",
                },
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

        # The proposals list shows the run immediately as an in-progress stub —
        # before (indeed without) the worker ever writing the real proposal.
        resp = http.get("/ontology/proposals/")
        assert resp.status_code == 200
        proposals = resp.json()
        stub = next((p for p in proposals if p["proposal_id"] == job_id), None)
        assert stub is not None, "in-flight induction should be visible in the list immediately"
        assert stub["status"] == "inducing"
        # And a hard-refresh-equivalent single GET also surfaces it (durable in DDB,
        # not just in-memory), with no artifact URLs yet.
        resp = http.get(f"/ontology/proposals/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "inducing"
        assert resp.json()["ontology_url"] is None

    def test_second_trigger_during_inflight_window_is_blocked(self, client):
        http, tmpdir, dynamo = client
        # First trigger, worker suppressed → run stays in flight (no proposal
        # row written yet, only the stub + the lock).
        with patch("coa_ontology.induce_catalog.Thread"):
            resp = http.post(
                "/induce/?namespace=default",
                json={
                    "datasource_ids": ["ds-abc123"],
                    "ontology_uri_prefix": "https://test.com/ontology/first#",
                },
            )
            assert resp.status_code == 202
            first_job = resp.json()["job_id"]

            # Second trigger DURING the in-flight window — this is the bug: the
            # old guard let it through because no pending PROPOSAL existed yet.
            resp = http.post(
                "/induce/?namespace=default",
                json={
                    "datasource_ids": ["ds-ice456"],
                    "ontology_uri_prefix": "https://test.com/ontology/second#",
                },
            )
        assert resp.status_code == 409
        body = resp.json()["detail"]
        assert "already in progress" in body["message"]
        # The 409 points at the running job so the UI can navigate to it.
        assert body["job_id"] == first_job

    def test_lock_released_after_run_allows_new_induction(self, client):
        http, tmpdir, dynamo = client
        import coa_ontology.dynamo_store as ds

        # Simulate a completed-then-reviewed run: acquire+release the lock and
        # leave no pending proposal (as reject/cancel would).
        ds.acquire_induction_lock("default", "job-1")
        ds.release_induction_lock("default")

        # A fresh trigger now succeeds (worker suppressed so we only test the
        # acquire path, not the pipeline).
        with patch("coa_ontology.induce_catalog.Thread"):
            resp = http.post(
                "/induce/?namespace=default",
                json={
                    "datasource_ids": ["ds-abc123"],
                    "ontology_uri_prefix": "https://test.com/ontology/again#",
                },
            )
        assert resp.status_code == 202

    def test_acquire_lock_conflicts_then_stale_takeover(self, client):
        """DAO-level: a live lock blocks re-acquire; a stale lock is taken over."""
        http, tmpdir, dynamo = client
        import coa_ontology.dynamo_store as ds

        ds.acquire_induction_lock("nsX", "job-A")
        # A second acquire while held raises, carrying the holder's job_id.
        with pytest.raises(ds.InductionLockHeldError) as ei:
            ds.acquire_induction_lock("nsX", "job-B")
        assert ei.value.job_id == "job-A"

        # Force the existing lock stale, then a new acquire takes it over.
        import decimal

        stale_ts = decimal.Decimal("0")  # epoch 0 → far older than the cutoff
        dynamo[("nsX#INDUCTIONLOCK", "STATUS")]["acquired_ts"] = stale_ts
        ds.acquire_induction_lock("nsX", "job-C")  # no raise
        assert dynamo[("nsX#INDUCTIONLOCK", "STATUS")]["job_id"] == "job-C"

    def test_lock_released_when_post_acquire_step_fails(self, client):
        """If a write AFTER acquiring the lock fails (put_job here), the router
        releases the lock so the namespace isn't wedged until stale-takeover.
        The worker's finally can't cover this — the worker thread never starts.
        (Addresses MR !531 CRITICAL review finding.)"""
        http, tmpdir, dynamo = client
        import coa_ontology.dynamo_store as ds

        # Fail the first post-acquire write. Patch on the module the router uses.
        # Starlette's TestClient re-raises unhandled server exceptions, so the
        # RuntimeError propagates out of the request rather than becoming a 500.
        with (
            patch.object(ds, "put_job", side_effect=RuntimeError("dynamo down")),
            pytest.raises(RuntimeError, match="dynamo down"),
        ):
            http.post(
                "/induce/?namespace=default",
                json={
                    "datasource_ids": ["ds-abc123"],
                    "ontology_uri_prefix": "https://test.com/ontology/leak#",
                },
            )

        # Crucially: the lock item is GONE — a post-acquire failure released it,
        # so a subsequent induction in this namespace is not blocked for an hour.
        assert ("default#INDUCTIONLOCK", "STATUS") not in dynamo, (
            "induction lock leaked: a post-acquire failure must release it"
        )

    def test_proposal_written_just_before_lock_acquire_still_blocks(self, client):
        """Check-then-act race (AppSec finding V2298834428): the running worker
        writes its pending proposal and releases the lock in the gap between a
        new trigger's proposal check and its lock acquire. The proposal check
        must run UNDER the lock so this trigger 409s instead of starting a
        duplicate run, and the lock it took must be released again."""
        http, tmpdir, dynamo = client
        import coa_ontology.dynamo_store as ds

        real_acquire = ds.acquire_induction_lock

        def _acquire_after_worker_finished(namespace, job_id):
            # The previous run's worker lands its pending proposal (and has
            # released the lock) right before this trigger acquires it.
            ds.create_proposal(
                proposal_id="prev-run",
                job_id="prev-run",
                namespace=namespace,
                ontology_id="https://test.com/ontology/prev#",
                proposal_type="induction",
                ontology_turtle="",
                r2rml_turtle="",
                source_type="STRUCTURED",
            )
            real_acquire(namespace, job_id)

        with (
            patch.object(ds, "acquire_induction_lock", side_effect=_acquire_after_worker_finished),
            patch("coa_ontology.induce_catalog.Thread") as thread,
        ):
            resp = http.post(
                "/induce/?namespace=default",
                json={
                    "datasource_ids": ["ds-abc123"],
                    "ontology_uri_prefix": "https://test.com/ontology/dup#",
                },
            )

        assert resp.status_code == 409
        assert resp.json()["detail"]["proposal_id"] == "prev-run"
        thread.assert_not_called()
        assert ("default#INDUCTIONLOCK", "STATUS") not in dynamo, (
            "induction lock leaked: the block-until-reviewed 409 must release it"
        )


if __name__ == "__main__":
    from rdflib import URIRef  # noqa: E402 — needed for test

    pytest.main([__file__, "-v"])


# ── Integration test — uses real services when env vars are set ──────────

_DATA_CATALOG_URL = os.environ.get("TEST_DATA_CATALOG_URL")
_DYNAMODB_TABLE = os.environ.get("TEST_DYNAMODB_TABLE")
_NA_GRAPH_ID = os.environ.get("TEST_NA_GRAPH_ID")
_NA_ENDPOINT = os.environ.get("TEST_NA_ENDPOINT")

_HAS_LIVE_SERVICES = all([_DATA_CATALOG_URL, _DYNAMODB_TABLE, _NA_GRAPH_ID])


@pytest.fixture()
def live_client():
    """Test client using real services — no mocks."""
    os.environ["DATA_CATALOG_URL"] = _DATA_CATALOG_URL
    os.environ["DYNAMODB_TABLE"] = _DYNAMODB_TABLE
    os.environ["NA_GRAPH_ID"] = _NA_GRAPH_ID
    if _NA_ENDPOINT:
        os.environ["NA_ENDPOINT"] = _NA_ENDPOINT

    # ignore_cleanup_errors: the induction runs in a daemon thread that may
    # still be writing artifacts into tmpdir when the context exits, racing
    # rmtree and intermittently raising "OSError: Directory not empty" at
    # teardown (flaky CI failure). Tolerate it — the tmpdir is disposable.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        os.environ["INDUCE_OUTPUT_DIR"] = tmpdir
        # Force reimport to pick up new env vars
        import importlib

        import coa_ontology.dynamo_store as ds

        ds._table = None  # reset cached table
        import coa_ontology.catalog.na_store as ns

        importlib.reload(ns)

        from coa_ontology.main import app

        yield TestClient(app), tmpdir


@pytest.mark.skipif(
    not _HAS_LIVE_SERVICES,
    reason=(
        "Set TEST_DATA_CATALOG_URL, TEST_DYNAMODB_TABLE, TEST_NA_GRAPH_ID "
        "(and optionally TEST_NA_ENDPOINT) to run live integration tests"
    ),
)
class TestInductionLive:
    """Integration tests against real data-catalog, DynamoDB, and Neptune Analytics."""

    def test_induction_multiple_datasources_live(self, live_client):
        http, tmpdir = live_client

        resp = http.post(
            "/induce/?namespace=default",
            json={
                "datasource_ids": ["ds-abc123", "ds-ice456"],
                "ontology_uri_prefix": "https://test.com/ontology/live-multi#",
                "label": "Live Multi-source Test",
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        import time

        for _ in range(60):
            resp = http.get(f"/induce/jobs/{job_id}?namespace=default")
            status = resp.json()["status"]
            if status in ("completed", "failed"):
                break
            time.sleep(1)

        result = resp.json()
        assert result["status"] == "completed", f"Job failed: {result.get('error')}"
        assert result["report"]["tables_processed"] == 4

        # Verify proposal was created
        proposal_id = result["report"]["induced_ontology_id"]
        resp = http.get(f"/ontology/proposals/{proposal_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        assert resp.json()["ontology_url"]

        # Verify files on disk
        assert os.path.exists(os.path.join(tmpdir, job_id, "proposal-ontology.ttl"))
        assert os.path.exists(os.path.join(tmpdir, job_id, "r2rml-mappings.ttl"))
