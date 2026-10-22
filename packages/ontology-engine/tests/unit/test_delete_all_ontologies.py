# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for DELETE /ontologies/ (delete all ontologies in namespace)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


@pytest.fixture()
def client():
    with patch("coa_ontology.stores.build_stores") as mock_build:
        mock_graph = MagicMock()
        mock_vector = MagicMock()
        mock_build.return_value = (mock_graph, mock_vector)

        from coa_ontology.main import app

        yield TestClient(app), mock_graph, mock_vector


class TestDeleteAllOntologies:
    @patch("coa_ontology.dynamo_store.list_ontologies_registry")
    @patch("coa_ontology.dynamo_store.delete_all_namespace_items")
    def test_deletes_graphs_index_and_ddb(self, mock_delete_all, mock_list, client):
        test_client, mock_graph, mock_vector = client
        mock_list.return_value = [
            {"ontology_id": "http://ex.org/ont1"},
            {"ontology_id": "http://ex.org/ont2"},
        ]
        mock_graph.delete_ontology.return_value = True
        mock_vector.delete_index.return_value = True
        mock_delete_all.return_value = 15

        resp = test_client.delete("/ontologies/?namespace=test-ns")

        assert resp.status_code == 200
        body = resp.json()
        assert body["namespace"] == "test-ns"
        assert body["graphsDropped"] == 2
        assert body["indexDeleted"] is True
        assert body["dynamoItemsDeleted"] == 15

        assert mock_graph.delete_ontology.call_count == 2
        mock_vector.delete_index.assert_called_once_with(namespace="test-ns")
        mock_delete_all.assert_called_once_with("test-ns")

    @patch("coa_ontology.dynamo_store.list_ontologies_registry")
    @patch("coa_ontology.dynamo_store.delete_all_namespace_items")
    def test_empty_namespace_returns_zeros(self, mock_delete_all, mock_list, client):
        test_client, mock_graph, mock_vector = client
        mock_list.return_value = []
        mock_vector.delete_index.return_value = False
        mock_delete_all.return_value = 0

        resp = test_client.delete("/ontologies/?namespace=empty-ns")

        assert resp.status_code == 200
        body = resp.json()
        assert body["graphsDropped"] == 0
        assert body["indexDeleted"] is False
        assert body["dynamoItemsDeleted"] == 0

    @patch("coa_ontology.dynamo_store.list_ontologies_registry")
    @patch("coa_ontology.dynamo_store.delete_all_namespace_items")
    def test_graph_failure_is_suppressed(self, mock_delete_all, mock_list, client):
        test_client, mock_graph, mock_vector = client
        mock_list.return_value = [{"ontology_id": "http://ex.org/ont1"}]
        mock_graph.delete_ontology.side_effect = RuntimeError("Neptune down")
        mock_vector.delete_index.return_value = True
        mock_delete_all.return_value = 3

        resp = test_client.delete("/ontologies/?namespace=test-ns")

        assert resp.status_code == 200
        body = resp.json()
        assert body["graphsDropped"] == 0
        assert body["indexDeleted"] is True

    @patch("coa_ontology.dynamo_store.list_ontologies_registry")
    @patch("coa_ontology.dynamo_store.delete_all_namespace_items")
    def test_index_delete_failure_is_a_hard_gate(self, mock_delete_all, mock_list, client):
        """An AOSS index delete failure must NOT be swallowed.

        The caller (namespace deletion pipeline) deletes the namespace record
        after this returns, so a swallowed failure orphans the index forever —
        nothing can target a namespace that no longer exists — until the
        collection hits its hard 1000-index cap and every embedding write in
        the deployment fails with index_limit_breached. Failing loudly makes
        the pipeline retry and, on exhaustion, land a recoverable
        DELETE_FAILED. Contrast test_graph_failure_is_suppressed: a dropped
        graph is bad but consumes no quota that can wedge the deployment.
        """
        test_client, mock_graph, mock_vector = client
        mock_list.return_value = [{"ontology_id": "http://ex.org/ont1"}]
        mock_graph.delete_ontology.return_value = True
        mock_vector.delete_index.side_effect = RuntimeError("AOSS throttled")

        with pytest.raises(RuntimeError, match="AOSS throttled"):
            test_client.delete("/ontologies/?namespace=test-ns")

    @patch("coa_ontology.dynamo_store.list_ontologies_registry")
    @patch("coa_ontology.dynamo_store.delete_all_namespace_items")
    def test_index_delete_failure_preserves_the_ddb_recovery_anchor(self, mock_delete_all, mock_list, client):
        """Don't purge the registry while the index is still there.

        The DDB rows are the recovery anchor for a retried teardown (same
        rationale as _delete_ontology_backend deleting its registry row last).
        Purging them while the index survives would strand the index with
        nothing left pointing at it.
        """
        test_client, mock_graph, mock_vector = client
        mock_list.return_value = [{"ontology_id": "http://ex.org/ont1"}]
        mock_graph.delete_ontology.return_value = True
        mock_vector.delete_index.side_effect = RuntimeError("AOSS throttled")

        with pytest.raises(RuntimeError):
            test_client.delete("/ontologies/?namespace=test-ns")

        mock_delete_all.assert_not_called()


class _InlineThread:
    """Run a spawned Thread's target inline so async teardown is testable."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


class TestDeleteSingleOntology:
    """DELETE /ontologies/?ontology_id=<iri> — async single-ontology delete
    that marks the row "deleting", tears down graph + embeddings + the accepted
    proposals that fed the ontology, then removes the registry row last."""

    @pytest.fixture(autouse=True)
    def _no_induced_present(self):
        """Default: no induced ontologies in the namespace, so the "induced
        ontologies present" delete-guard passes. Guard-specific tests override
        this mock's return value.
        """
        with patch("coa_ontology.dynamo_store.list_ontologies_registry") as m:
            m.return_value = []
            yield m

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_deletes_ontology_and_source_proposals(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client
    ):
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/ont#"
        # Registry row carries the authoritative source_proposals list.
        mock_get_reg.return_value = {"ontology_id": oid, "source_proposals": ["p-A", "p-B"]}
        mock_del_reg.return_value = True
        # A scan surfaces one extra accepted proposal not in source_proposals.
        mock_list_props.return_value = [{"proposal_id": "p-C"}]
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 202
        # Row flipped to "deleting" (stays listed as "Delete in progress"),
        # clearing any stale delete_error from a prior attempt.
        mock_upd_reg.assert_called_once_with("test-ns", oid, status="deleting", delete_error="")
        # Graph + embeddings torn down.
        mock_graph.delete_ontology.assert_called_once_with(oid)
        mock_vector.delete_embeddings_for_ontology.assert_called_once_with(oid, namespace="test-ns")
        # Union of registry list + scan → p-A, p-B, p-C all deleted.
        deleted = {c.args[1] for c in mock_del_prop.call_args_list}
        assert deleted == {"p-A", "p-B", "p-C"}
        # Registry row removed LAST (after graph/embeddings/proposals).
        mock_del_reg.assert_called_once_with("test-ns", oid)

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_deletes_proposals_grounded_against_the_ontology(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client
    ):
        """Proposals grounded AGAINST the deleted ontology are removed too.

        The helper calls list_proposals twice: (1) filtered by ontology_id +
        accepted for feeder proposals, (2) unfiltered to find proposals whose
        metadata.grounding_ontologies_used names this ontology.
        """
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/ont#"
        mock_get_reg.return_value = {"ontology_id": oid, "source_proposals": ["p-feed"]}
        mock_del_reg.return_value = True
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}

        def _list_side_effect(*args, **kwargs):
            # First call: feeder proposals (filtered by ontology_id + accepted).
            if kwargs.get("ontology_id") == oid and kwargs.get("status") == "accepted":
                return []
            # Second call: full namespace scan for grounded proposals.
            return [
                {"proposal_id": "p-grounded", "metadata": {"grounding_ontologies_used": [oid, "http://other#"]}},
                {"proposal_id": "p-other", "metadata": {"grounding_ontologies_used": ["http://other#"]}},
                {"proposal_id": "p-nogrounding", "metadata": {}},
            ]

        mock_list_props.side_effect = _list_side_effect

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 202
        deleted = {c.args[1] for c in mock_del_prop.call_args_list}
        # Feeder (source_proposals) + the proposal grounded against oid — but
        # NOT the ones grounded only against other ontologies / not grounded.
        assert deleted == {"p-feed", "p-grounded"}, deleted

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_409_deleting_uploaded_ontology_while_induced_present(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client, _no_induced_present
    ):
        """A non-induced (user-loaded) ontology can't be deleted while an induced
        ontology exists — the induced one is derived from it. Returns 409 with a
        clear message and touches nothing."""
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/uploaded#"
        mock_get_reg.return_value = {"ontology_id": oid, "ontology_type": "user_created"}
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        # An induced ontology is present in the namespace.
        _no_induced_present.return_value = [
            {"ontology_id": "http://ex.org/induced#", "ontology_type": "induced"},
        ]

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 409
        assert "induced ontologies exist" in resp.json()["detail"]
        # Refused up front — nothing marked or torn down.
        mock_upd_reg.assert_not_called()
        mock_del_reg.assert_not_called()
        mock_del_prop.assert_not_called()
        mock_graph.delete_ontology.assert_not_called()

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_409_deleting_foundational_ontology_while_induced_present(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client, _no_induced_present
    ):
        """Foundational ontologies are gated too — an induced ontology grounds
        against them, so they can't be pulled out while induced ones exist."""
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/foundational#"
        mock_get_reg.return_value = {"ontology_id": oid, "ontology_type": "foundational"}
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        _no_induced_present.return_value = [
            {"ontology_id": "http://ex.org/induced#", "ontology_type": "induced"},
        ]

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 409
        mock_upd_reg.assert_not_called()
        mock_del_reg.assert_not_called()

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_induced_ontology_delete_allowed_even_with_other_induced_present(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client, _no_induced_present
    ):
        """Deleting an induced ontology is always allowed — it excludes itself
        from the guard, so induced ontologies remain removable (no deadlock).
        Here another induced ontology is present and the delete still proceeds."""
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/induced-A#"
        mock_get_reg.return_value = {"ontology_id": oid, "ontology_type": "induced", "source_proposals": []}
        mock_del_reg.return_value = True
        mock_list_props.return_value = []
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        # The guard lists induced ontologies; the row being deleted is one of
        # them and must be excluded. A DIFFERENT induced ontology also exists —
        # its presence must NOT block deleting this one.
        _no_induced_present.return_value = [
            {"ontology_id": oid, "ontology_type": "induced"},
            {"ontology_id": "http://ex.org/induced-B#", "ontology_type": "induced"},
        ]

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 202
        mock_upd_reg.assert_called_once_with("test-ns", oid, status="deleting", delete_error="")

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_uploaded_ontology_delete_allowed_when_no_induced_present(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client, _no_induced_present
    ):
        """With no induced ontologies in the namespace, deleting an uploaded
        ontology proceeds normally (202)."""
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/uploaded#"
        mock_get_reg.return_value = {"ontology_id": oid, "ontology_type": "user_uploaded", "source_proposals": []}
        mock_del_reg.return_value = True
        mock_list_props.return_value = []
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        _no_induced_present.return_value = []  # no induced ontologies

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 202
        mock_upd_reg.assert_called_once_with("test-ns", oid, status="deleting", delete_error="")

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_404_when_ontology_absent(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client
    ):
        test_client, mock_graph, mock_vector = client
        mock_get_reg.return_value = None
        mock_list_props.return_value = []
        mock_graph.get_ontology_by_uri.return_value = None

        resp = test_client.delete(
            "/ontologies/", params={"namespace": "test-ns", "ontology_id": "http://ex.org/missing#"}
        )

        assert resp.status_code == 404
        # Nothing deleted or marked when the ontology doesn't exist.
        mock_upd_reg.assert_not_called()
        mock_del_reg.assert_not_called()
        mock_del_prop.assert_not_called()

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_proposal_delete_failure_does_not_break_response(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client
    ):
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/ont#"
        mock_get_reg.return_value = {"ontology_id": oid, "source_proposals": ["p-A"]}
        mock_del_reg.return_value = True
        mock_list_props.return_value = []
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        mock_del_prop.side_effect = RuntimeError("DDB throttled")

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        # Proposal-delete failure is logged + skipped — the response still
        # succeeds and the registry row is still removed last (a leftover
        # proposal row is a minor inconsistency, not orphaned graph data).
        assert resp.status_code == 202
        mock_del_prop.assert_called_once()
        mock_del_reg.assert_called_once_with("test-ns", oid)

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_graph_delete_failure_keeps_registry_row(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client
    ):
        """Neptune DROP failure must NOT remove the registry row — otherwise the
        ontology vanishes from the UI while its triples linger (orphaned quads).
        The row stays in 'deleting' for a re-issued DELETE to retry."""
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/ont#"
        mock_get_reg.return_value = {"ontology_id": oid, "source_proposals": ["p-A"]}
        mock_list_props.return_value = []
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        mock_graph.delete_ontology.side_effect = RuntimeError("Neptune throttled")

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 202  # still 202 — teardown is async
        # Flipped to "deleting" up front, then stamped with delete_error so a
        # stuck row is distinguishable from one still in progress.
        mock_upd_reg.assert_any_call("test-ns", oid, status="deleting", delete_error="")
        err_calls = [c for c in mock_upd_reg.call_args_list if "delete_error" in c.kwargs and c.kwargs["delete_error"]]
        assert err_calls, "expected a delete_error to be recorded on graph-delete failure"
        assert "Graph delete failed" in err_calls[0].kwargs["delete_error"]
        # Returned by ListOntologies as deleteError: the type, never the backend's message.
        assert err_calls[0].kwargs["delete_error"] == "Graph delete failed: RuntimeError"
        # Hard gate: registry row NOT removed, embeddings + proposals not touched.
        mock_del_reg.assert_not_called()
        mock_vector.delete_embeddings_for_ontology.assert_not_called()
        mock_del_prop.assert_not_called()

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_embedding_delete_failure_keeps_registry_row(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client
    ):
        """AOSS teardown failure must also NOT remove the registry row."""
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/ont#"
        mock_get_reg.return_value = {"ontology_id": oid, "source_proposals": ["p-A"]}
        mock_list_props.return_value = []
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        mock_vector.delete_embeddings_for_ontology.side_effect = RuntimeError("AOSS 503")

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 202
        mock_graph.delete_ontology.assert_called_once_with(oid)
        # delete_error records the embedding-teardown failure.
        err_calls = [c for c in mock_upd_reg.call_args_list if "delete_error" in c.kwargs and c.kwargs["delete_error"]]
        assert err_calls, "expected a delete_error to be recorded on embedding-delete failure"
        assert "Embedding delete failed" in err_calls[0].kwargs["delete_error"]
        assert "AOSS 503" not in err_calls[0].kwargs["delete_error"]
        # Hard gate: registry row NOT removed, proposals not touched.
        mock_del_reg.assert_not_called()
        mock_del_prop.assert_not_called()

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_embedding_delete_incomplete_keeps_registry_row(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client
    ):
        """A delete that stalls with embeddings still matching is a teardown
        failure too: it used to return normally, so the row was removed with the
        embeddings left behind. Now it trips the same hard gate."""
        from coa_common.opensearch import AossVectorClient
        from coa_common.opensearch import client as oss_client

        # The real delete loop, over one doc whose per-doc delete is rejected.
        aoss = AossVectorClient(endpoint="https://example.invalid", region="us-east-1")
        aoss._client = MagicMock()
        aoss._client.delete.side_effect = RuntimeError("simulated per-doc delete failure")
        aoss._client.search.side_effect = lambda index, body: (
            {"hits": {"hits": [], "total": {"value": 1}}}
            if body.get("size") == 0
            else {"hits": {"hits": [{"_id": "stale"}], "total": {"value": 1}}}
        )

        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/ont#"
        mock_get_reg.return_value = {"ontology_id": oid, "source_proposals": ["p-A"]}
        mock_list_props.return_value = []
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        mock_vector.list_searchable_entity_uris.return_value = []
        mock_vector.delete_embeddings_for_ontology.side_effect = lambda ontology_id, namespace=None: (
            aoss.delete_by_term("idx", "ontology_id", ontology_id)
        )

        with patch.object(oss_client, "_DELETE_VERIFY_STALL_TIMEOUT_S", 0.0):
            resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 202
        err_calls = [c for c in mock_upd_reg.call_args_list if "delete_error" in c.kwargs and c.kwargs["delete_error"]]
        assert err_calls, "expected a delete_error to be recorded on an incomplete embedding delete"
        assert "Embedding delete failed: DeleteIncompleteError" in err_calls[0].kwargs["delete_error"]
        mock_del_reg.assert_not_called()
        mock_del_prop.assert_not_called()

    @patch("coa_ontology.catalog.routers.ontologies.Thread", _InlineThread)
    @patch("coa_ontology.dynamo_store.delete_proposal")
    @patch("coa_ontology.dynamo_store.list_proposals")
    @patch("coa_ontology.dynamo_store.update_ontology_registry")
    @patch("coa_ontology.dynamo_store.delete_ontology_registry")
    @patch("coa_ontology.dynamo_store.get_ontology_registry")
    def test_status_flip_failure_aborts_with_500(
        self, mock_get_reg, mock_del_reg, mock_upd_reg, mock_list_props, mock_del_prop, client
    ):
        """If marking the row 'deleting' fails, abort (500) rather than start
        teardown — otherwise the row stays status=None and the ontology would
        vanish abruptly instead of showing 'Delete in progress'."""
        test_client, mock_graph, mock_vector = client
        oid = "http://ex.org/ont#"
        mock_get_reg.return_value = {"ontology_id": oid, "source_proposals": []}
        mock_list_props.return_value = []
        mock_graph.get_ontology_by_uri.return_value = {"uri": oid}
        mock_upd_reg.side_effect = RuntimeError("DDB update throttled")

        resp = test_client.delete("/ontologies/", params={"namespace": "test-ns", "ontology_id": oid})

        assert resp.status_code == 500
        # Teardown never started.
        mock_graph.delete_ontology.assert_not_called()
        mock_del_reg.assert_not_called()
        mock_del_prop.assert_not_called()
