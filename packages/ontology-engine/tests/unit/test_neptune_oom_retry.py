# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune ``MemoryLimitExceededException`` handling and the kind-scoped search scan.

Under concurrent integ load the shared Neptune cluster answered
``/graph/search`` with HTTP 500 ``MemoryLimitExceededException`` (pipelines
2861430, 2861563, 2871155, 2871465). These tests pin the three parts of the fix:

* the transport retries an OOM (and only an OOM), then raises
  :class:`NeptuneMemoryLimitError` on exhaustion;
* the API maps that error to a retryable 503 instead of a bare 500;
* a ``kind``-filtered search/count no longer scans every quad in the cluster.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from coa_ontology.main import app
from coa_ontology.stores import neptune_db_graph as ndb
from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore, NeptuneMemoryLimitError
from fastapi.testclient import TestClient

NS = "test-ns"
_OOM_BODY = {
    "code": "MemoryLimitExceededException",
    "detailedMessage": "Operation terminated (out of memory)",
}
_EMPTY_RESULTS = {"head": {"vars": []}, "results": {"bindings": []}}

_real_client = httpx.Client


@pytest.fixture
def neptune(monkeypatch):
    """Route the store's httpx calls to a scripted handler; no signing, no sleeps.

    Yields a list the test fills with responses (one per request, in order) and
    reads back as the number of requests actually made.
    """
    responses: list[httpx.Response] = []
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    monkeypatch.setattr(ndb, "NDB_ENDPOINT", "https://fake:8182")
    monkeypatch.setattr(ndb, "_sign", lambda *a, **k: {})
    monkeypatch.setattr(ndb.httpx, "Client", lambda **kw: _real_client(transport=httpx.MockTransport(handler), **kw))
    with patch("coa_common.opensearch.retry.time.sleep"):
        yield responses, calls


@pytest.mark.unit
class TestOomRetry:
    def test_query_retries_oom_then_succeeds(self, neptune):
        responses, calls = neptune
        responses += [httpx.Response(500, json=_OOM_BODY), httpx.Response(200, json=_EMPTY_RESULTS)]
        assert ndb._sparql_query("SELECT * WHERE { ?s ?p ?o }") == _EMPTY_RESULTS
        assert len(calls) == 2

    def test_query_raises_memory_limit_error_on_exhaustion(self, neptune):
        responses, calls = neptune
        responses += [httpx.Response(500, json=_OOM_BODY)] * (ndb._NDB_MAX_RETRIES + 1)
        with pytest.raises(NeptuneMemoryLimitError) as exc:
            ndb._sparql_query("SELECT * WHERE { ?s ?p ?o }")
        # Still an HTTPStatusError, so existing ``except`` sites keep catching it.
        assert isinstance(exc.value, httpx.HTTPStatusError)
        assert len(calls) == ndb._NDB_MAX_RETRIES + 1

    def test_other_500_is_not_retried(self, neptune):
        responses, calls = neptune
        responses.append(httpx.Response(500, json={"code": "MalformedQueryException"}))
        with pytest.raises(httpx.HTTPStatusError) as exc:
            ndb._sparql_query("SELECT nonsense")
        assert not isinstance(exc.value, NeptuneMemoryLimitError)
        assert len(calls) == 1

    def test_non_json_error_body_is_not_retried(self, neptune):
        responses, calls = neptune
        responses.append(httpx.Response(502, text="Bad Gateway"))
        with pytest.raises(httpx.HTTPStatusError):
            ndb._sparql_query("SELECT * WHERE { ?s ?p ?o }")
        assert len(calls) == 1

    @pytest.mark.parametrize("body", [["not", "an", "object"], "oops", None])
    def test_non_object_json_error_body_raises_http_error(self, neptune, body):
        # Must surface as HTTPStatusError, not AttributeError — /graph/search maps
        # AttributeError to 501, which the integ test would silently skip.
        responses, calls = neptune
        responses.append(httpx.Response(500, json=body))
        with pytest.raises(httpx.HTTPStatusError) as exc:
            ndb._sparql_query("SELECT * WHERE { ?s ?p ?o }")
        assert not isinstance(exc.value, NeptuneMemoryLimitError)
        assert len(calls) == 1

    def test_update_retries_oom(self, neptune):
        responses, calls = neptune
        responses += [httpx.Response(500, json=_OOM_BODY), httpx.Response(200, json={})]
        ndb._sparql_update("DROP SILENT GRAPH <urn:g>")
        assert len(calls) == 2

    def test_gsp_post_retries_oom(self, neptune):
        responses, calls = neptune
        responses += [httpx.Response(500, json=_OOM_BODY), httpx.Response(200, json={})]
        assert ndb._gsp_post_turtle("urn:g", "<urn:s> <urn:p> <urn:o> .")["status"] == "ok"
        assert len(calls) == 2

    def test_gsp_get_retries_oom(self, neptune):
        responses, calls = neptune
        responses += [httpx.Response(500, json=_OOM_BODY), httpx.Response(200, text="# empty")]
        assert ndb._gsp_get_turtle("urn:g") == "# empty"
        assert len(calls) == 2


@pytest.mark.unit
def test_api_maps_memory_limit_error_to_503():
    graph = MagicMock()
    graph.search_entities.side_effect = NeptuneMemoryLimitError(
        "Neptune MemoryLimitExceededException (500)",
        request=httpx.Request("POST", "https://fake:8182/sparql"),
        response=httpx.Response(500, json=_OOM_BODY),
    )
    with patch("coa_ontology.catalog.routers.graph.build_stores", return_value=(graph, MagicMock())):
        resp = TestClient(app).get("/graph/search", params={"q": "*", "kind": "class", "namespace": NS})
    assert resp.status_code == 503


@pytest.mark.unit
class TestKindScopedScan:
    """A kind filter must not also carry the unbound ``GRAPH ?g { ?s ?_p ?_o }`` scan."""

    @staticmethod
    def _captured_queries(fn) -> list[str]:
        captured: list[str] = []

        def capture(q):
            captured.append(q)
            return _EMPTY_RESULTS

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            fn()
        return captured

    def test_kind_search_and_count_use_type_pattern_only(self):
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        queries = self._captured_queries(
            lambda: (
                store.search_entities(query="*", namespace=NS, kind="class", limit=100),
                store.count_entities(query="*", namespace=NS, kind="class"),
            )
        )
        assert len(queries) == 2
        for q in queries:
            assert "?_p ?_o" not in q
            assert "?s a ?requiredType" in q
            assert "STRSTARTS(STR(?g)" in q  # namespace scoping is unchanged

    def test_no_kind_keeps_full_subject_pattern(self):
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        queries = self._captured_queries(lambda: store.count_entities(query="pub", namespace=NS))
        assert "?s ?_p ?_o" in queries[0]
