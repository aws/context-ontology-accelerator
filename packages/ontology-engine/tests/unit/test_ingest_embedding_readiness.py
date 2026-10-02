# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared ingest embedding-readiness wait.

Any flow that loads an ontology and then relies on its embeddings being
searchable (grounding recall against a freshly-loaded external ontology,
serve-time retrieval) must wait until every embedding it wrote is
queryable in the eventually-consistent vector index.
:func:`wait_for_embeddings_searchable` polls the index until the set of
indexed ``entity_uri`` values covers the exact set written.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
class TestWaitForEmbeddingsSearchable:
    """The wait polls VectorStore.list_searchable_entity_uris, which returns a
    flat list of entity_uri strings (vectors excluded)."""

    def test_returns_true_immediately_when_no_expected_uris(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=[],
        )
        assert ok is True
        vec.list_searchable_entity_uris.assert_not_called()

    def test_returns_true_when_all_present_first_poll(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        vec.list_searchable_entity_uris.return_value = ["a", "b", "c"]  # extra URIs are fine
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b"],
        )
        assert ok is True
        assert vec.list_searchable_entity_uris.call_count == 1

    def test_waits_until_index_catches_up(self):
        """First poll missing one URI, second poll complete (AOSS caught up)."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        vec.list_searchable_entity_uris.side_effect = [
            ["a"],  # 'b' not yet searchable
            ["a", "b"],
        ]
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b"],
            stall_timeout_s=10,
            poll_s=0,
        )
        assert ok is True
        assert vec.list_searchable_entity_uris.call_count == 2

    def test_returns_false_on_timeout(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        vec.list_searchable_entity_uris.return_value = ["a"]  # never gets 'b'
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b"],
            stall_timeout_s=0.05,
            poll_s=0,
        )
        assert ok is False

    def test_probe_error_is_tolerated_then_recovers(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        vec.list_searchable_entity_uris.side_effect = [
            RuntimeError("transient AOSS error"),
            ["a", "b"],
        ]
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b"],
            stall_timeout_s=10,
            poll_s=0,
        )
        assert ok is True
        assert vec.list_searchable_entity_uris.call_count == 2

    def test_persistent_probe_error_times_out_false(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        vec.list_searchable_entity_uris.side_effect = RuntimeError("down")
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a"],
            stall_timeout_s=0.05,
            poll_s=0,
        )
        assert ok is False

    def test_blank_expected_uris_are_ignored(self):
        """Empty/None URIs in the written list don't block readiness."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        vec.list_searchable_entity_uris.return_value = ["a"]
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "", None],
            stall_timeout_s=10,
            poll_s=0,
        )
        assert ok is True

    def test_slow_steady_progress_is_not_cut_off(self):
        """A sync that adds one embedding per poll completes even across many
        polls — each step resets the stall timer, so a tiny stall_timeout that
        would trip without progress still succeeds here."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        # 5 expected; index reveals one more URI each poll. With a 0.05s stall
        # timeout and no inter-poll sleep, this only completes because every
        # iteration shows progress and resets the deadline.
        vec.list_searchable_entity_uris.side_effect = [
            ["a"],
            ["a", "b"],
            ["a", "b", "c"],
            ["a", "b", "c", "d"],
            ["a", "b", "c", "d", "e"],
        ]
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b", "c", "d", "e"],
            stall_timeout_s=0.05,
            poll_s=0,
        )
        assert ok is True
        assert vec.list_searchable_entity_uris.call_count == 5

    def test_on_progress_fires_per_advance_so_a_long_wait_can_heartbeat(self):
        """This wait has NO total wall-clock cap (the timeout is a no-progress
        deadline that resets on every advance), so a large-but-healthy sync can
        outlive the proposal stale-sweep window. ``on_progress`` lets the accept
        worker heartbeat from inside the wait; without it the sweep would flip a
        LIVE accept to accept_failed and a re-accept would run a second worker
        concurrently with the first."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        seen: list[tuple[int, int]] = []
        vec = MagicMock()
        vec.list_searchable_entity_uris.side_effect = [
            ["a"],
            ["a", "b"],
            ["a", "b", "c"],
        ]
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b", "c"],
            stall_timeout_s=0.05,
            poll_s=0,
            on_progress=lambda done, total: seen.append((done, total)),
        )
        assert ok is True
        # One heartbeat per partial advance. The completing poll returns early,
        # so it does not heartbeat — by then the step's own checkpoint fires.
        assert seen == [(1, 3), (2, 3)]

    def test_on_progress_not_called_when_index_does_not_advance(self):
        """No advance → no heartbeat. A wedged sync must NOT keep refreshing the
        heartbeat, or the stale-sweep could never reclaim a dead worker."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        seen: list[tuple[int, int]] = []
        vec = MagicMock()
        vec.list_searchable_entity_uris.return_value = ["a"]  # stuck at 1/2
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b"],
            stall_timeout_s=0.05,
            poll_s=0,
            on_progress=lambda done, total: seen.append((done, total)),
        )
        assert ok is False
        assert seen == [(1, 2)]  # the initial observation only, then silence

    def test_on_progress_failure_does_not_abort_a_healthy_wait(self):
        """The heartbeat is best-effort: a DDB hiccup must not fail a sync that
        is completing normally."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        vec.list_searchable_entity_uris.return_value = ["a", "b"]

        def boom(done: int, total: int) -> None:
            raise RuntimeError("DDB hiccup")

        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b"],
            stall_timeout_s=10,
            poll_s=0,
            on_progress=boom,
        )
        assert ok is True

    def test_stall_with_partial_progress_then_stuck_times_out(self):
        """Once progress stops, the stall timeout trips even though some
        embeddings became searchable earlier."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_searchable

        vec = MagicMock()
        # Reaches 2/3 then never advances — must return False after the stall
        # window with the remaining one still missing.
        vec.list_searchable_entity_uris.return_value = ["a", "b"]
        ok = wait_for_embeddings_searchable(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            expected_uris=["a", "b", "c"],
            stall_timeout_s=0.05,
            poll_s=0,
        )
        assert ok is False


@pytest.mark.unit
class TestWaitForEmbeddingsDeleted:
    """The inverse wait: after deleting an ontology's embeddings, block until
    none of the deleted entity_uris are searchable in the (eventually
    consistent) vector index anymore."""

    def test_returns_true_immediately_when_no_deleted_uris(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_deleted

        vec = MagicMock()
        ok = wait_for_embeddings_deleted(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            deleted_uris=[],
        )
        assert ok is True
        vec.list_searchable_entity_uris.assert_not_called()

    def test_returns_true_when_already_gone_first_poll(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_deleted

        vec = MagicMock()
        vec.list_searchable_entity_uris.return_value = []  # nothing searchable → all gone
        ok = wait_for_embeddings_deleted(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            deleted_uris=["a", "b"],
        )
        assert ok is True
        assert vec.list_searchable_entity_uris.call_count == 1

    def test_waits_until_index_drops_deleted_uris(self):
        """First poll still returns 'b', second poll shows it gone."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_deleted

        vec = MagicMock()
        vec.list_searchable_entity_uris.side_effect = [
            ["b"],  # 'a' already gone, 'b' still visible
            [],  # both gone now
        ]
        ok = wait_for_embeddings_deleted(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            deleted_uris=["a", "b"],
            stall_timeout_s=10,
            poll_s=0,
        )
        assert ok is True
        assert vec.list_searchable_entity_uris.call_count == 2

    def test_ignores_unrelated_uris_still_present(self):
        """Other ontologies' URIs coming back from the probe don't block us —
        we only wait on the deleted set."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_deleted

        vec = MagicMock()
        vec.list_searchable_entity_uris.return_value = ["other-1", "other-2"]
        ok = wait_for_embeddings_deleted(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            deleted_uris=["a", "b"],
            stall_timeout_s=10,
            poll_s=0,
        )
        assert ok is True

    def test_returns_false_on_timeout(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_deleted

        vec = MagicMock()
        vec.list_searchable_entity_uris.return_value = ["b"]  # 'b' never disappears
        ok = wait_for_embeddings_deleted(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            deleted_uris=["a", "b"],
            stall_timeout_s=0.05,
            poll_s=0,
        )
        assert ok is False

    def test_probe_error_is_tolerated_then_recovers(self):
        from coa_ontology.catalog.ingest import wait_for_embeddings_deleted

        vec = MagicMock()
        vec.list_searchable_entity_uris.side_effect = [
            RuntimeError("transient AOSS error"),
            [],
        ]
        ok = wait_for_embeddings_deleted(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            deleted_uris=["a"],
            stall_timeout_s=10,
            poll_s=0,
        )
        assert ok is True
        assert vec.list_searchable_entity_uris.call_count == 2

    def test_index_not_found_counts_as_deleted_immediately(self):
        """A missing vector index means the embeddings are definitively gone —
        treat it as success on the first probe, not a retryable error that burns
        the whole stall timeout (regression: a 404 index_not_found used to stall
        ~120s and blow the delete's downstream timing)."""

        class _NotFoundError(Exception):
            pass

        _NotFoundError.__name__ = "NotFoundError"

        from coa_ontology.catalog.ingest import wait_for_embeddings_deleted

        vec = MagicMock()
        vec.list_searchable_entity_uris.side_effect = _NotFoundError("-dev-vector-store-x]'")
        ok = wait_for_embeddings_deleted(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            deleted_uris=["a", "b"],
            stall_timeout_s=30,
            poll_s=0,
        )
        assert ok is True
        # Returned on the FIRST probe — did not retry into the stall window.
        assert vec.list_searchable_entity_uris.call_count == 1

    def test_is_index_not_found_matches_signatures(self):
        from coa_ontology.catalog.ingest import _is_index_not_found

        assert _is_index_not_found(Exception("no such index [foo]"))
        assert _is_index_not_found(Exception("index_not_found_exception"))
        assert not _is_index_not_found(Exception("connection reset"))

    def test_slow_steady_removal_is_not_cut_off(self):
        """Deletions propagating one-per-poll complete even with a tiny stall
        timeout, because each step resets the deadline."""
        from coa_ontology.catalog.ingest import wait_for_embeddings_deleted

        vec = MagicMock()
        vec.list_searchable_entity_uris.side_effect = [
            ["a", "b", "c", "d"],
            ["b", "c", "d"],
            ["c", "d"],
            ["d"],
            [],
        ]
        ok = wait_for_embeddings_deleted(
            vector_store=vec,
            ontology_id="o",
            namespace="ns",
            deleted_uris=["a", "b", "c", "d"],
            stall_timeout_s=0.05,
            poll_s=0,
        )
        assert ok is True
        assert vec.list_searchable_entity_uris.call_count == 5


@pytest.mark.unit
class TestIngestSurfacesEntityUris:
    """ingest_ontology must surface the exact URIs it wrote so callers can wait."""

    def test_accumulate_embeddings_returns_entity_uris(self):
        """_accumulate_embeddings result carries entity_uris matching what was written."""
        from unittest.mock import patch

        from coa_ontology.catalog import ingest as ingest_mod
        from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

        g = Graph()
        c1 = URIRef("http://ex.com/A")
        c2 = URIRef("http://ex.com/B")
        for c in (c1, c2):
            g.add((c, RDF.type, OWL.Class))
            g.add((c, RDFS.label, Literal(c.split("/")[-1])))

        vec = MagicMock()
        # #173: _accumulate_embeddings now derives entity_uris/count from the
        # store's ACKNOWLEDGED docs, not len(submitted). Echo back a doc per
        # submitted item so the store confirms what it wrote (an empty return
        # would now correctly be treated as a 0-of-N partial write).
        vec.store_embeddings_batch.side_effect = lambda items: [dict(it) for it in items]
        vec._index_for_namespace = lambda ns: f"idx-{ns}"

        fake_bedrock = MagicMock()
        fake_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        fake_bedrock.embed_texts.side_effect = lambda texts: [[0.1] * 4 for _ in texts]

        with patch(
            "coa_ontology.bedrock_embeddings.BedrockEmbeddingClient",
            return_value=fake_bedrock,
        ):
            result = ingest_mod._accumulate_embeddings(
                vector_store=vec,
                graph=g,
                ontology_id="http://ex.com/onto",
                classes={c1, c2},
                obj_props=set(),
                dt_props=set(),
                namespace="ns",
            )

        assert result["status"] == "ok"
        assert set(result["entity_uris"]) == {str(c1), str(c2)}
        assert result["count"] == len(result["entity_uris"])


@pytest.mark.unit
class TestIngestOntologyAndWait:
    """ingest_ontology_and_wait — the single entry point for ontology loading:
    ingest, then block until the written embeddings are searchable."""

    def _patch(self, monkeypatch, *, ingest_result, wait_spy):
        import coa_ontology.catalog.ingest as ingest_mod

        monkeypatch.setattr(ingest_mod, "ingest_ontology", lambda **kw: ingest_result)
        monkeypatch.setattr(ingest_mod, "wait_for_embeddings_searchable", wait_spy)
        return ingest_mod

    def test_waits_for_written_uris_and_returns_result(self, monkeypatch):
        seen = {}

        def _wait_spy(*, vector_store, ontology_id, namespace, expected_uris):
            seen["ontology_id"] = ontology_id
            seen["expected_uris"] = list(expected_uris)
            return True

        result = {"ontology_id": "http://ex/o", "embeddings": {"entity_uris": ["a", "b"]}, "registry": {"x": 1}}
        mod = self._patch(monkeypatch, ingest_result=result, wait_spy=_wait_spy)

        out = mod.ingest_ontology_and_wait(MagicMock(), MagicMock(), b"ttl", namespace="ns")

        assert out is result  # result passed through unchanged
        assert seen["ontology_id"] == "http://ex/o"
        assert seen["expected_uris"] == ["a", "b"]

    def test_on_embeddings_sync_fires_before_wait(self, monkeypatch):
        order = []

        def _wait_spy(**kw):
            order.append("wait")
            return True

        result = {"ontology_id": "http://ex/o", "embeddings": {"entity_uris": ["a"]}}
        mod = self._patch(monkeypatch, ingest_result=result, wait_spy=_wait_spy)

        mod.ingest_ontology_and_wait(
            MagicMock(),
            MagicMock(),
            b"ttl",
            namespace="ns",
            on_embeddings_sync=lambda: order.append("sync"),
        )

        assert order == ["sync", "wait"]

    def test_no_embeddings_skips_callback_and_wait(self, monkeypatch):
        calls = {"wait": 0, "sync": 0}

        def _wait_spy(**kw):
            calls["wait"] += 1
            return True

        # ingest wrote nothing → no callback, and the wait short-circuits.
        result = {"ontology_id": "http://ex/o", "embeddings": {"entity_uris": []}}
        mod = self._patch(monkeypatch, ingest_result=result, wait_spy=_wait_spy)

        mod.ingest_ontology_and_wait(
            MagicMock(),
            MagicMock(),
            b"ttl",
            namespace="ns",
            on_embeddings_sync=lambda: calls.__setitem__("sync", calls["sync"] + 1),
        )

        assert calls["sync"] == 0
        # wait_for_embeddings_searchable is still invoked, but with an empty set.
        assert calls["wait"] == 1
