# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""#143: large ontologies must not be inlined through the 6 MB Lambda response cap.

/download offloads the Turtle to S3 and returns a presigned URL; ontology-overview
pages each collection and reports the un-paged totals. Both are exercised at the
router level (the store still enumerates fully — the cap is on the API response)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.catalog.routers import graph as graph_router
from coa_ontology.catalog.routers import ontologies as ont_router


@pytest.mark.unit
class TestDownloadOffload:
    def test_download_returns_presigned_url_not_inline(self):
        fake_graph = MagicMock()
        fake_graph.get_graph_turtle.return_value = "@prefix ex: <http://ex/> . ex:A a ex:Class ."
        with (
            patch.object(ont_router, "_graph", return_value=fake_graph),
            patch.object(ont_router, "format_grouped_turtle", side_effect=lambda s: s),
            patch.object(
                ont_router.dynamo_store,
                "write_and_presign_ontology_download",
                return_value="https://s3.example/presigned",
            ) as offload,
        ):
            result = ont_router.download_ontology_file("http://ex/onto", namespace="ns")

        # JSON with a presigned URL — never an inline Turtle body.
        assert result == {
            "downloadUrl": "https://s3.example/presigned",
            "expiresInSeconds": ont_router._DOWNLOAD_URL_TTL_S,
        }
        offload.assert_called_once()
        # Offloaded the serialized graph body for this namespace/ontology.
        args = offload.call_args[0]
        assert args[0] == "ns"
        assert args[1] == "http://ex/onto"
        assert "ex:A" in args[2]


@pytest.mark.unit
class TestOverviewPagination:
    @staticmethod
    def _fake_store(n_classes: int, n_obj: int, n_dt: int) -> MagicMock:
        store = MagicMock()
        store.get_ontology_overview.return_value = {
            "ontology_id": "o",
            "graph_uri": "g",
            "classes": [{"uri": f"c{i}"} for i in range(n_classes)],
            "object_properties": [{"uri": f"op{i}"} for i in range(n_obj)],
            "datatype_properties": [{"uri": f"dp{i}"} for i in range(n_dt)],
        }
        return store

    def test_paginates_each_collection_and_reports_totals(self):
        store = self._fake_store(100, 50, 30)
        with patch.object(graph_router, "_graph", return_value=store):
            res = graph_router.get_ontology_overview("o", namespace="ns", limit=10, offset=20)

        # Bounded page per collection.
        assert len(res.classes) == 10
        assert len(res.object_properties) == 10
        assert len(res.datatype_properties) == 10
        # Offset applied.
        assert res.classes[0].uri == "c20"
        # Un-paged totals so the UI shows the true count while rendering a page.
        assert res.total_classes == 100
        assert res.total_object_properties == 50
        assert res.total_datatype_properties == 30

    def test_no_limit_returns_all_with_totals(self):
        store = self._fake_store(5, 3, 2)
        with patch.object(graph_router, "_graph", return_value=store):
            res = graph_router.get_ontology_overview("o", namespace="ns")

        assert len(res.classes) == 5
        assert len(res.object_properties) == 3
        assert len(res.datatype_properties) == 2
        assert res.total_classes == 5
        assert res.total_object_properties == 3
        assert res.total_datatype_properties == 2

    def test_offset_past_end_yields_empty_page_but_real_totals(self):
        store = self._fake_store(10, 0, 0)
        with patch.object(graph_router, "_graph", return_value=store):
            res = graph_router.get_ontology_overview("o", namespace="ns", limit=5, offset=100)

        assert len(res.classes) == 0
        assert res.total_classes == 10
