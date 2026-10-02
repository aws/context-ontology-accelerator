# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared AOSS vector client (coa_common.opensearch).

Covers the query-body shapes (server-side k-NN filter, count, filter search),
the canonical index mapping, the direct-vs-proxy transport split, and the
transient-retry helpers. ``time.sleep`` is patched so backoff doesn't slow the
suite; no real AOSS is contacted.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import ConnectionTimeout, NotFoundError, RequestError, TransportError

pytestmark = pytest.mark.unit

from coa_common.opensearch import (
    AossVectorClient,
    IncompatibleEngineError,
    build_index_mapping,
    bulk_with_retry,
    is_transient,
    oss_retry,
)
from coa_common.opensearch import client as client_mod
from coa_common.opensearch import retry as retry_mod


def _faiss_mapping(index: str = "idx") -> dict:
    """A get_mapping response whose embedding resolved to Faiss (filter-capable)."""
    return {index: {"mappings": {"properties": {"embedding": {"type": "knn_vector", "method": {"engine": "faiss"}}}}}}


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch.object(retry_mod.time, "sleep", return_value=None):
        yield


# ── ensure_index engine verification & fallback (#174) ───────────────────


def _direct_client_with_indices(indices_mock) -> AossVectorClient:
    """A direct client whose signed _client.indices is the given mock."""
    c = AossVectorClient(endpoint="https://x.aoss.us-west-2.on.aws", region="us-west-2", dimensions=4)
    fake = MagicMock()
    fake.indices = indices_mock
    c._client = fake
    return c


class TestEnsureIndexEngineVerification:
    """ensure_index must (1) try explicit Faiss first, (2) fall back to
    method-less if the service rejects the method block, and (3) read the
    mapping back and RAISE if the resolved engine can't serve filtered k-NN.
    """

    def test_creates_explicit_faiss_and_accepts_faiss_engine(self):
        idx = MagicMock()
        idx.exists.return_value = False
        idx.get_mapping.return_value = _faiss_mapping("idx")
        c = _direct_client_with_indices(idx)
        assert c.ensure_index("idx") == "idx"
        # Created with an explicit Faiss/HNSW method block.
        body = idx.create.call_args.kwargs["body"]
        assert body["mappings"]["properties"]["embedding"]["method"]["engine"] == "faiss"
        idx.get_mapping.assert_called_once()  # engine was verified

    def test_falls_back_to_method_less_when_explicit_rejected(self):
        idx = MagicMock()
        idx.exists.return_value = False
        # First create (explicit method) rejected; second (method-less) succeeds.
        reject = RequestError(400, "illegal_argument_exception", {})
        idx.create.side_effect = [reject, None]
        # Service auto-resolved to faiss on the method-less create.
        idx.get_mapping.return_value = _faiss_mapping("idx")
        c = _direct_client_with_indices(idx)
        assert c.ensure_index("idx") == "idx"
        assert idx.create.call_count == 2
        first_body = idx.create.call_args_list[0].kwargs["body"]
        second_body = idx.create.call_args_list[1].kwargs["body"]
        assert "method" in first_body["mappings"]["properties"]["embedding"]
        assert "method" not in second_body["mappings"]["properties"]["embedding"]

    def test_raises_when_resolved_engine_is_nmslib(self):
        idx = MagicMock()
        idx.exists.return_value = False
        idx.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"embedding": {"type": "knn_vector", "method": {"engine": "nmslib"}}}}}
        }
        c = _direct_client_with_indices(idx)
        with pytest.raises(IncompatibleEngineError, match="nmslib"):
            c.ensure_index("idx")

    def test_raises_when_no_engine_reported_on_explicit_path(self):
        """DEFAULT path: we sent an explicit Faiss method, so get_mapping MUST
        echo a filter-capable engine. Silence (no method block) is fatal — the
        create did not apply what we asked for."""
        idx = MagicMock()
        idx.exists.return_value = False
        # Explicit create "succeeded" but the mapping reports no method/engine.
        idx.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"embedding": {"type": "knn_vector", "dimension": 4}}}}
        }
        c = _direct_client_with_indices(idx)
        with pytest.raises(IncompatibleEngineError, match="no ANN engine"):
            c.ensure_index("idx")

    def test_tolerates_no_engine_reported_on_methodless_fallback(self):
        """FALLBACK path (F1): when the service REJECTED the explicit method and
        we created method-less, an UNREPORTED engine in get_mapping is tolerated
        (the service does not always echo a server-resolved default). Raising
        here would false-reject a valid Faiss index on the exact generation the
        fallback exists to serve — filter-capability is confirmed by the live
        integ test, not by this mapping read."""
        idx = MagicMock()
        idx.exists.return_value = False
        reject = RequestError(400, "illegal_argument_exception", {})
        idx.create.side_effect = [reject, None]  # explicit rejected, method-less ok
        # Method-less create resolved to (presumably) faiss but echoes no engine.
        idx.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"embedding": {"type": "knn_vector", "dimension": 4}}}}
        }
        c = _direct_client_with_indices(idx)
        assert c.ensure_index("idx") == "idx"  # tolerated, not raised
        assert idx.create.call_count == 2
        # Collection is now classified method-less-only.
        assert c._explicit_method_supported is False

    def test_nmslib_still_fatal_on_methodless_fallback(self):
        """FALLBACK path: an UNREPORTED engine is tolerated, but a REPORTED
        NMSLIB is still fatal — that is a known-broken index, not ambiguity."""
        idx = MagicMock()
        idx.exists.return_value = False
        reject = RequestError(400, "illegal_argument_exception", {})
        idx.create.side_effect = [reject, None]
        idx.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"embedding": {"method": {"engine": "nmslib"}}}}}
        }
        c = _direct_client_with_indices(idx)
        with pytest.raises(IncompatibleEngineError, match="nmslib"):
            c.ensure_index("idx")

    def test_collection_classification_is_cached_across_indexes(self):
        """LONG-TERM design: the explicit-vs-method-less classification is probed
        ONCE and cached, so later indexes take the known path with no re-probe
        and no per-index guessing."""
        idx = MagicMock()
        idx.exists.return_value = False
        # Collection rejects explicit method: first index probes (2 creates:
        # explicit-reject then method-less); the cache flips to method-less-only.
        reject = RequestError(400, "illegal_argument_exception", {})
        idx.create.side_effect = [reject, None, None]  # idx-a: reject+methodless; idx-b: methodless
        idx.get_mapping.return_value = _faiss_mapping("idx-a")
        c = _direct_client_with_indices(idx)

        c.ensure_index("idx-a")
        assert c._explicit_method_supported is False
        assert idx.create.call_count == 2  # explicit(reject) + method-less

        idx.get_mapping.return_value = _faiss_mapping("idx-b")
        c.ensure_index("idx-b")
        # Second index went STRAIGHT to method-less — no wasted explicit probe.
        assert idx.create.call_count == 3
        last_body = idx.create.call_args_list[-1].kwargs["body"]
        assert "method" not in last_body["mappings"]["properties"]["embedding"]

    def test_verifies_engine_on_preexisting_index(self):
        """A pre-existing index (exists=True, no create) is still engine-verified
        — it could have been created before this check existed."""
        idx = MagicMock()
        idx.exists.return_value = True
        idx.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"embedding": {"method": {"engine": "nmslib"}}}}}
        }
        c = _direct_client_with_indices(idx)
        with pytest.raises(IncompatibleEngineError):
            c.ensure_index("idx")
        idx.create.assert_not_called()

    def test_preexisting_index_with_unreported_engine_is_tolerated_on_fresh_client(self):
        """A pre-existing index on a FRESH process (no collection classification
        yet) must not be held to the explicit-method rule. We did not create it,
        so an unreported engine is not evidence of a broken create. Regression:
        the readiness probe and proposal-accept ingest both rejected every
        existing index after a container restart."""
        idx = MagicMock()
        idx.exists.return_value = True
        idx.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"embedding": {"type": "knn_vector", "dimension": 4}}}}
        }
        c = _direct_client_with_indices(idx)
        assert c._explicit_method_supported is None
        assert c.ensure_index("idx") == "idx"
        idx.create.assert_not_called()

    def test_preexisting_index_lenient_even_when_collection_accepts_explicit(self):
        """A collection classified explicit-capable (an earlier create here
        succeeded with the method block) can still hold indexes created
        method-less by older code. How a pre-existing index was created is
        unknown, so it gets the lenient check either way."""
        idx = MagicMock()
        idx.exists.return_value = True
        idx.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"embedding": {"type": "knn_vector", "dimension": 4}}}}
        }
        c = _direct_client_with_indices(idx)
        c._explicit_method_supported = True
        assert c.ensure_index("idx") == "idx"
        idx.create.assert_not_called()

    def test_non_method_request_error_propagates_without_fallback(self):
        """A create 400 that is NOT about the method block must propagate, not
        silently fall back to method-less."""
        idx = MagicMock()
        idx.exists.return_value = False
        idx.create.side_effect = RequestError(400, "mapper_exception_something_else", {})
        c = _direct_client_with_indices(idx)
        with pytest.raises(RequestError):
            c.ensure_index("idx")
        assert idx.create.call_count == 1  # no method-less retry

    def test_lucene_engine_is_accepted(self):
        idx = MagicMock()
        idx.exists.return_value = False
        idx.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"embedding": {"method": {"engine": "lucene"}}}}}
        }
        c = _direct_client_with_indices(idx)
        assert c.ensure_index("idx") == "idx"


# ── Canonical mapping ────────────────────────────────────────────────────


class TestBuildIndexMapping:
    def test_embedding_defaults_to_explicit_faiss_hnsw(self):
        # Default (method=True): explicit Faiss/HNSW so creation does not depend
        # on the service's observed-unstable method-less default engine (#174).
        m = build_index_mapping(1024)["mappings"]["properties"]
        assert m["embedding"]["type"] == "knn_vector"
        assert m["embedding"]["dimension"] == 1024
        assert m["embedding"]["method"] == {"name": "hnsw", "engine": "faiss", "space_type": "l2"}

    def test_embedding_method_less_when_requested(self):
        # Fallback path used by ensure_index when the service rejects an
        # explicit method.engine at creation.
        m = build_index_mapping(768, method=False)["mappings"]["properties"]
        assert m["embedding"] == {"type": "knn_vector", "dimension": 768}
        assert "method" not in m["embedding"]

    def test_filterable_fields_are_keyword(self):
        m = build_index_mapping(768)["mappings"]["properties"]
        keyword_fields = (
            "entity_uri",
            "ontology_id",
            "entity_type",
            "embedding_type",
            "model_id",
            "namespace",
            "data_source_id",
        )
        for f in keyword_fields:
            assert m[f] == {"type": "keyword"}, f

    def test_prompt_fields_are_text(self):
        m = build_index_mapping(1024)["mappings"]["properties"]
        assert m["text"] == {"type": "text"}
        assert m["context_text"] == {"type": "text"}

    def test_knn_setting_enabled(self):
        assert build_index_mapping(1024)["settings"]["index"]["knn"] is True


# ── is_transient / oss_retry / bulk_with_retry ───────────────────────────


class TestIsTransient:
    def test_429_and_5xx_are_transient(self):
        for status in (429, 500, 502, 503, 504):
            assert is_transient(TransportError(status, "x")) is True

    def test_4xx_not_transient(self):
        for status in (400, 403, 404, 409):
            assert is_transient(TransportError(status, "x")) is False

    def test_connection_errors_are_transient(self):
        assert is_transient(OSConnectionError("N/A", "refused", Exception())) is True
        assert is_transient(ConnectionTimeout("t")) is True

    def test_generic_not_transient(self):
        assert is_transient(ValueError("nope")) is False


class TestOssRetry:
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransportError(503, "unavailable")
            return "ok"

        assert oss_retry("op", flaky) == "ok"
        assert calls["n"] == 3

    def test_terminal_raises_immediately(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RequestError(400, "bad", {})

        with pytest.raises(RequestError):
            oss_retry("op", boom)
        assert calls["n"] == 1

    def test_transient_reraises_on_exhaustion(self):
        """A never-recovering transient fault re-raises the REAL error after
        OSS_MAX_RETRIES+1 attempts (not a swallowed empty result). This is the
        exhaustion path the correctness fix relies on: an exhausted retry must
        propagate so callers fail loud instead of silently degrading."""
        calls = {"n": 0}

        def always_503():
            calls["n"] += 1
            raise TransportError(503, "unavailable")

        with pytest.raises(TransportError):
            oss_retry("op", always_503)
        assert calls["n"] == retry_mod.OSS_MAX_RETRIES + 1


class TestBulkWithRetry:
    """bulk_with_retry now inspects helpers.bulk's (ok_count, errors) return
    (raise_on_error=False) rather than catching BulkIndexError, so an exhausted
    per-item 429 that opensearch-py silently drops is still visible as an error
    entry. It re-submits only transient failures and RAISES PartialIndexError if
    any doc fails to land — a partial write is never reported as success (#173).
    """

    def _ok_errors(self, error_items):
        """Shape a (success_count, errors) return like helpers.bulk gives."""
        return (0, error_items)

    def test_resubmits_only_transiently_failed(self):
        client = MagicMock()
        actions = [
            {"_op_type": "index", "_index": "i", "entity_uri": "a"},
            {"_op_type": "index", "_index": "i", "entity_uri": "b"},
        ]
        # First call: 'b' fails transiently (500). Second call: clean.
        first = self._ok_errors([{"index": {"status": 500, "data": {"entity_uri": "b"}}}])
        second = (1, [])
        with patch.object(retry_mod, "bulk", side_effect=[first, second]) as mock_bulk:
            bulk_with_retry(client, actions)  # returns cleanly — everything landed
        assert mock_bulk.call_count == 2
        # Only the failed doc is re-submitted, rebuilt into an index action.
        assert mock_bulk.call_args_list[1].args[1] == [{"_op_type": "index", "_index": "i", "entity_uri": "b"}]

    def test_terminal_item_raises_partial_index_error(self):
        client = MagicMock()
        actions = [{"_op_type": "index", "_index": "i", "entity_uri": "a"}]
        # A non-transient 400 can never be fixed by retrying → raise immediately.
        errs = self._ok_errors([{"index": {"status": 400, "data": {"entity_uri": "a"}}}])
        with (
            patch.object(retry_mod, "bulk", return_value=errs) as mock_bulk,
            pytest.raises(retry_mod.PartialIndexError) as ei,
        ):
            bulk_with_retry(client, actions)
        assert mock_bulk.call_count == 1  # no retry on a terminal failure
        assert ei.value.failed == 1 and ei.value.submitted == 1

    def test_transient_exhaustion_raises_partial_index_error(self):
        """The core #173 fix: docs that keep failing transiently until retries
        are exhausted must RAISE, not return cleanly — otherwise the caller
        reports a full write while docs were silently dropped."""
        client = MagicMock()
        actions = [{"_op_type": "index", "_index": "i", "entity_uri": "a"}]
        # Always returns the same transient (429) error → never converges.
        errs = self._ok_errors([{"index": {"status": 429, "data": {"entity_uri": "a"}}}])
        with (
            patch.object(retry_mod, "bulk", return_value=errs) as mock_bulk,
            pytest.raises(retry_mod.PartialIndexError) as ei,
        ):
            bulk_with_retry(client, actions)
        # Attempts = initial + OSS_MAX_RETRIES re-submits.
        assert mock_bulk.call_count == retry_mod.OSS_MAX_RETRIES + 1
        assert ei.value.failed == 1

    def test_clean_write_returns_without_raising(self):
        client = MagicMock()
        actions = [{"_op_type": "index", "_index": "i", "entity_uri": "a"}]
        with patch.object(retry_mod, "bulk", return_value=(1, [])) as mock_bulk:
            bulk_with_retry(client, actions)  # no error
        assert mock_bulk.call_count == 1

    def test_empty_is_noop(self):
        client = MagicMock()
        with patch.object(retry_mod, "bulk") as mock_bulk:
            bulk_with_retry(client, [])
        mock_bulk.assert_not_called()


# ── AossVectorClient query shapes ────────────────────────────────────────


def _client_with_capture(response):
    """Direct client whose underlying (retry-wrapped) search captures the body."""
    cap = {}
    c = AossVectorClient(endpoint="https://x.aoss.us-west-2.on.aws", region="us-west-2", dimensions=4)

    def _search(index, body):
        cap["index"] = index
        cap["body"] = body
        return response

    # Inject a fake retry-wrapped client exposing .search (bypasses real signing).
    fake = MagicMock()
    fake.search.side_effect = _search
    c._client = fake
    return c, cap


class TestKnnSearch:
    def test_filters_pushed_into_knn_and_exact_top_k(self):
        c, cap = _client_with_capture({"hits": {"hits": [{"_id": "1", "_score": 0.9, "_source": {"entity_uri": "u"}}]}})
        filters = [{"term": {"entity_type": "class"}}, {"exists": {"field": "data_source_id"}}]
        hits = c.knn_search("idx", [0.1] * 4, top_k=5, filters=filters)

        body = cap["body"]
        assert body["size"] == 5
        knn = body["query"]["knn"]["embedding"]
        assert knn["k"] == 5
        assert knn["filter"] == {"bool": {"filter": filters}}
        assert hits[0]["entity_uri"] == "u" and hits[0]["_score"] == 0.9

    def test_no_filters_omits_filter_clause(self):
        c, cap = _client_with_capture({"hits": {"hits": []}})
        c.knn_search("idx", [0.1] * 4, top_k=7)
        knn = cap["body"]["query"]["knn"]["embedding"]
        assert "filter" not in knn and knn["k"] == 7

    def test_short_vector_is_padded(self):
        c, cap = _client_with_capture({"hits": {"hits": []}})
        c.knn_search("idx", [0.1, 0.2], top_k=3)  # dims=4 → padded to 4
        assert cap["body"]["query"]["knn"]["embedding"]["vector"] == [0.1, 0.2, 0.0, 0.0]


class TestCount:
    def test_builds_filter_and_returns_total(self):
        c, cap = _client_with_capture({"hits": {"total": {"value": 3}}})
        n = c.count("idx", filters=[{"exists": {"field": "data_source_id"}}])
        assert n == 3
        assert cap["body"]["size"] == 0
        assert cap["body"]["query"] == {"bool": {"filter": [{"exists": {"field": "data_source_id"}}]}}

    def test_no_filters_uses_match_all(self):
        c, cap = _client_with_capture({"hits": {"total": {"value": 10}}})
        assert c.count("idx") == 10
        assert cap["body"]["query"] == {"match_all": {}}


# ── delete_by_term (delete-then-count-to-zero) ─────────────────────────────


def _delete_client(search_pages, count_seq):
    """Direct client scripted for delete_by_term.

    delete_by_term issues two kinds of ``search`` against the same fake client,
    told apart by ``body["size"]``:
      - ``size == page`` (>0): a delete-page fetch → returns the next id list
        from ``search_pages`` shaped as ``{"hits": {"hits": [{"_id": …}]}}``.
      - ``size == 0``: the terminal ``count`` (a size-0 search) → returns the
        next value from ``count_seq`` shaped as ``{"hits": {"total": {…}}}``.
    ``client.delete`` is a plain MagicMock so call_count / side_effect work.
    """
    c = AossVectorClient(endpoint="https://x.aoss.us-west-2.on.aws", region="us-west-2", dimensions=4)
    searches = iter(search_pages)
    counts = iter(count_seq)

    def _search(index, body):
        if body.get("size") == 0:  # count query
            return {"hits": {"total": {"value": next(counts, 0)}}}
        return {"hits": {"hits": [{"_id": i} for i in next(searches, [])]}}

    fake = MagicMock()
    fake.search.side_effect = _search
    c._client = fake
    return c, fake


class TestDeleteByTerm:
    """Regression: delete must reach a *verified zero* count, not break on a
    single stale-empty ``search`` page. AOSS ``search`` is not read-your-writes
    consistent, so an empty page mid-drain would strand the tail (observed live:
    orphaned property embeddings after an ontology delete). The terminal gate is
    ``count()`` == 0; an empty page must NOT end the loop on its own.
    """

    def test_happy_path_single_pass_to_zero(self):
        c, fake = _delete_client(search_pages=[["a", "b", "c"]], count_seq=[0])
        with patch.object(client_mod.time, "sleep"):
            deleted = c.delete_by_term("idx", "ontology_id", "urn:o")
        assert deleted == 3
        assert fake.delete.call_count == 3

    def test_returns_zero_when_no_hits(self):
        c, fake = _delete_client(search_pages=[[]], count_seq=[0])
        with patch.object(client_mod.time, "sleep"):
            assert c.delete_by_term("idx", "ontology_id", "urn:o") == 0
        fake.delete.assert_not_called()

    def test_does_not_stop_on_stale_empty_search_while_count_nonzero(self):
        # Pass 1: search returns 2 ids (deleted) but count still says 4 (2 more
        #         docs the stale search view hasn't surfaced yet).
        # Pass 2: search returns EMPTY (the buggy code broke here!), count → 2 →
        #         loop MUST continue rather than strand the tail.
        # Pass 3: search returns the last 2 ids (deleted), count → 0 → done.
        c, fake = _delete_client(search_pages=[["a", "b"], [], ["c", "d"]], count_seq=[4, 2, 0])
        with patch.object(client_mod.time, "sleep"):
            deleted = c.delete_by_term("idx", "ontology_id", "urn:o")
        assert deleted == 4  # every matching doc was issued a delete (2 + 0 + 2)
        assert fake.delete.call_count == 4

    def test_list_of_values_uses_one_terms_query_and_one_verify_loop(self):
        # #1118: retiring the stale embeddings of N subjects on re-accept must
        # not pay the refresh-lag verify wait N times — one `terms` query, one
        # count gate, for the whole set.
        c, fake = _delete_client(search_pages=[["a", "b"]], count_seq=[0])
        with patch.object(client_mod.time, "sleep"):
            deleted = c.delete_by_term("idx", "entity_uri", ["http://x/A", "http://x/B", "http://x/A"])
        assert deleted == 2
        bodies = [call.kwargs.get("body") or call.args[1] for call in fake.search.call_args_list]
        page_q = next(b["query"] for b in bodies if b.get("size", 0) > 0)
        assert page_q == {"terms": {"entity_uri": ["http://x/A", "http://x/B"]}}  # deduped, order kept
        count_q = next(b["query"] for b in bodies if b.get("size") == 0)
        assert "terms" in str(count_q)
        # Exactly one delete pass + one count: the verify loop ran once for the set.
        assert len(bodies) == 2

    def test_empty_list_of_values_is_a_noop(self):
        c, fake = _delete_client(search_pages=[], count_seq=[])
        assert c.delete_by_term("idx", "entity_uri", []) == 0
        fake.search.assert_not_called()

    def test_list_that_is_empty_after_dropping_blanks_is_a_noop(self):
        c, fake = _delete_client(search_pages=[], count_seq=[])
        assert c.delete_by_term("idx", "entity_uri", ["", "", ""]) == 0
        fake.search.assert_not_called()

    def test_blanks_and_nones_are_dropped_from_the_terms_clause(self):
        c, fake = _delete_client(search_pages=[["a"]], count_seq=[0])
        with patch.object(client_mod.time, "sleep"):
            assert c.delete_by_term("idx", "entity_uri", [None, "http://x/A", "", None]) == 1  # type: ignore[list-item]
        bodies = [call.kwargs.get("body") or call.args[1] for call in fake.search.call_args_list]
        page_q = next(b["query"] for b in bodies if b.get("size", 0) > 0)
        assert page_q == {"terms": {"entity_uri": ["http://x/A"]}}

    def test_swallows_per_doc_delete_errors(self):
        c, fake = _delete_client(search_pages=[["1", "2"]], count_seq=[0])
        fake.delete.side_effect = [None, RuntimeError("gone")]
        with patch.object(client_mod.time, "sleep"):
            # One delete raises; the loop swallows it and only the success counts.
            assert c.delete_by_term("idx", "ontology_id", "urn:o") == 1

    def test_missing_index_on_count_is_treated_as_done(self):
        from opensearchpy.exceptions import NotFoundError

        c = AossVectorClient(endpoint="https://x.aoss.us-west-2.on.aws", region="us-west-2", dimensions=4)
        fake = MagicMock()

        def _search(index, body):
            if body.get("size") == 0:
                raise NotFoundError(404, "index_not_found_exception", {})
            return {"hits": {"hits": []}}

        fake.search.side_effect = _search
        c._client = fake
        with patch.object(client_mod.time, "sleep"):
            assert c.delete_by_term("idx", "ontology_id", "urn:o") == 0

    def test_stall_gives_up_without_infinite_loop(self):
        # search always empty + count never drops → no progress. The no-progress
        # stall deadline must end the loop (this test hangs if it doesn't).
        c, fake = _delete_client(search_pages=[[], [], []], count_seq=[7, 7, 7])
        with (
            patch.object(client_mod.time, "sleep"),
            patch.object(client_mod, "_DELETE_VERIFY_STALL_TIMEOUT_S", 0),
        ):
            deleted = c.delete_by_term("idx", "ontology_id", "urn:o")
        assert deleted == 0  # nothing deletable; returned rather than hung


# ── signed-client auth ─────────────────────────────────────────────────────


class TestSignedClientAuth:
    """The direct client signs with the LIVE boto3 credentials object (not a
    frozen snapshot) so a cached client survives Fargate task-role token
    rotation, using the ``aoss`` service (AWSV4SignerAuth defaults to ``es``)."""

    def test_uses_live_credentials_and_aoss_service(self):
        from coa_common.opensearch import client as client_mod

        fake_creds = object()
        session = MagicMock()
        session.get_credentials.return_value = fake_creds
        with (
            patch.object(client_mod.boto3, "Session", return_value=session),
            patch.object(client_mod, "AWSV4SignerAuth") as mock_auth,
            patch.object(client_mod, "OpenSearch") as mock_os,
        ):
            mock_os.return_value.indices.exists.return_value = False
            mock_os.return_value.indices.get_mapping.return_value = _faiss_mapping("idx")
            c = AossVectorClient(endpoint="https://x.aoss.us-west-2.on.aws", region="us-west-2", dimensions=4)
            c.ensure_index("idx")  # forces the signed client to be built
            args = mock_auth.call_args.args
            assert args[0] is fake_creds  # LIVE object, not get_frozen_credentials()
            assert args[1] == "us-west-2"
            assert args[2] == "aoss"
            mock_os.assert_called_once()

    def test_missing_credentials_raises(self):
        from coa_common.opensearch import client as client_mod

        session = MagicMock()
        session.get_credentials.return_value = None
        with patch.object(client_mod.boto3, "Session", return_value=session):
            c = AossVectorClient(endpoint="https://x.aoss.us-west-2.on.aws", region="us-west-2")
            with pytest.raises(RuntimeError, match="credentials"):
                c.ensure_index("idx")


# ── signed-client connection pool ───────────────────────────────────────────


class TestSignedClientPoolSize:
    """The shared client is hit by BOTH induction ThreadPools: first-pass
    grounding recall (INDUCER_GROUNDING_WORKERS, default 24) and second-pass
    column match (INDUCER_COLUMN_MATCH_WORKERS, default 16). ``pool_maxsize`` must
    cover the WIDEST sharer or the wider pass starves ("Connection pool is full")."""

    def _build_and_capture_pool_maxsize(self):
        from coa_common.opensearch import client as client_mod

        session = MagicMock()
        session.get_credentials.return_value = object()
        with (
            patch.object(client_mod.boto3, "Session", return_value=session),
            patch.object(client_mod, "AWSV4SignerAuth"),
            patch.object(client_mod, "OpenSearch") as mock_os,
        ):
            mock_os.return_value.indices.exists.return_value = False
            mock_os.return_value.indices.get_mapping.return_value = _faiss_mapping("idx")
            c = AossVectorClient(endpoint="https://x.aoss.us-west-2.on.aws", region="us-west-2", dimensions=4)
            c.ensure_index("idx")  # forces the signed client to be built
            return mock_os.call_args.kwargs["pool_maxsize"]

    def test_defaults_honor_wider_grounding_width(self, monkeypatch):
        monkeypatch.delenv("INDUCER_COLUMN_MATCH_WORKERS", raising=False)
        monkeypatch.delenv("INDUCER_GROUNDING_WORKERS", raising=False)
        # grounding default (24) > column-match default (16) → grounding wins.
        assert self._build_and_capture_pool_maxsize() == 24

    def test_grounding_width_wins_when_wider(self, monkeypatch):
        monkeypatch.setenv("INDUCER_COLUMN_MATCH_WORKERS", "16")
        monkeypatch.setenv("INDUCER_GROUNDING_WORKERS", "40")
        assert self._build_and_capture_pool_maxsize() == 40

    def test_column_match_width_wins_when_wider(self, monkeypatch):
        monkeypatch.setenv("INDUCER_COLUMN_MATCH_WORKERS", "32")
        monkeypatch.setenv("INDUCER_GROUNDING_WORKERS", "24")
        assert self._build_and_capture_pool_maxsize() == 32

    def test_floor_of_ten_when_both_smaller(self, monkeypatch):
        monkeypatch.setenv("INDUCER_COLUMN_MATCH_WORKERS", "4")
        monkeypatch.setenv("INDUCER_GROUNDING_WORKERS", "8")
        assert self._build_and_capture_pool_maxsize() == 10


# ── transport split ──────────────────────────────────────────────────────


class TestTransportSplit:
    def test_proxy_client_routes_search_through_transport(self):
        seen = {}

        def transport(action, index, body):
            seen["action"] = action
            seen["index"] = index
            return {"hits": {"hits": [{"_id": "1", "_score": 0.5, "_source": {"entity_uri": "u"}}]}}

        c = AossVectorClient(transport=transport, dimensions=4)
        assert c.is_proxy is True
        hits = c.knn_search("idx", [0.1] * 4, top_k=2, filters=[{"term": {"entity_type": "class"}}])
        assert seen["action"] == "search" and seen["index"] == "idx"
        assert hits[0]["entity_uri"] == "u"

    def test_proxy_client_writes_raise(self):
        c = AossVectorClient(transport=lambda a, i, b: {}, dimensions=4)
        with pytest.raises(RuntimeError):
            c.bulk_index("idx", [{"entity_uri": "a"}])
        with pytest.raises(RuntimeError):
            c.ensure_index("idx")

    def test_direct_client_without_endpoint_raises(self):
        c = AossVectorClient(dimensions=4)  # no endpoint, no transport
        with pytest.raises(RuntimeError):
            c.ensure_index("idx")


# ── missing index reads as "no data" (index-resurrection guard) ──────────


def _client_raising(exc: Exception):
    """Direct client whose underlying search always raises ``exc``."""
    c = AossVectorClient(endpoint="https://x.aoss.us-west-2.on.aws", region="us-west-2", dimensions=4)
    fake = MagicMock()
    fake.search.side_effect = exc
    c._client = fake
    return c, fake


class TestMissingIndexReadsEmpty:
    """A read against a non-existent index must return empty, not raise.

    This is what lets callers stop calling ``ensure_index`` on the read path.
    An ``ensure_index`` on a read resurrects an empty index for a namespace that
    was already torn down; nothing reclaims those, and AOSS caps a collection at
    1000 indexes, so they pile up until every write fails with
    ``index_limit_breached``.
    """

    # The 404 surfaces through several opensearch-py shapes; _is_index_not_found
    # matches on class name AND message, so cover both.
    @pytest.fixture(
        params=[
            TransportError(404, "index_not_found_exception", {}),
            NotFoundError(404, "index_not_found_exception", {}),
            Exception("no such index [foo]"),
        ],
        ids=["transport-404", "notfound", "message-only"],
    )
    def missing(self, request):
        return request.param

    def test_knn_search_returns_empty(self, missing):
        c, _ = _client_raising(missing)
        assert c.knn_search("gone-idx", [0.1] * 4, top_k=5) == []

    def test_filter_search_returns_empty(self, missing):
        c, _ = _client_raising(missing)
        assert c.filter_search("gone-idx", [{"term": {"ontology_id": "o"}}]) == []

    def test_count_returns_zero(self, missing):
        c, _ = _client_raising(missing)
        assert c.count("gone-idx") == 0

    def test_iter_source_field_returns_empty(self, missing):
        c, _ = _client_raising(missing)
        assert c.iter_source_field("gone-idx", [{"term": {"ontology_id": "o"}}], "entity_uri") == []

    def test_delete_by_term_returns_zero(self, missing):
        """Nothing to delete in an index that doesn't exist — and no raise."""
        c, _ = _client_raising(missing)
        assert c.delete_by_term("gone-idx", "ontology_id", "o") == 0

    def test_real_errors_still_propagate(self):
        """Only index-not-found is swallowed; a genuine failure must surface."""
        c, _ = _client_raising(TransportError(500, "internal_server_error", {}))
        with pytest.raises(TransportError):
            c.filter_search("idx", [{"term": {"x": "y"}}])

    def test_missing_index_is_not_created(self):
        """The whole point: a read must not create the index it just missed."""
        c, fake = _client_raising(NotFoundError(404, "index_not_found_exception", {}))
        c.filter_search("gone-idx", [{"term": {"ontology_id": "o"}}])
        fake.indices.create.assert_not_called()
