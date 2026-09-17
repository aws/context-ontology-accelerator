# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the KG Build ECS task (graph_build.py).

Tests mock all external dependencies (S3, DDB via DynamoDBDAO, GraphRAG
Toolkit) so they run without any AWS infrastructure.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX

pytestmark = pytest.mark.unit

import types

from botocore.exceptions import ClientError
from coa_control_plane_server.models.source_status import SourceStatus

# ---------------------------------------------------------------------------
# Stub llama_index and graphrag_toolkit — not installed in the dev venv.
# graph_build.py imports graphrag_toolkit lazily (inside functions), so
# @patch decorators handle those. But patch("llama_index.core.Document")
# needs the module to exist in sys.modules first.
# ---------------------------------------------------------------------------


def _stub_missing_modules() -> None:
    for dotted in [
        "llama_index",
        "llama_index.core",
        "llama_index.core.base",
        "llama_index.core.base.embeddings",
        "llama_index.core.base.embeddings.base",
        "llama_index.core.node_parser",
        "graphrag_toolkit",
        "graphrag_toolkit.lexical_graph",
        "graphrag_toolkit.lexical_graph.storage",
        "graphrag_toolkit.lexical_graph.indexing",
        "graphrag_toolkit.lexical_graph.indexing.build",
        "graphrag_toolkit.lexical_graph.indexing.extract",
        "graphrag_toolkit.lexical_graph.indexing.load",
        "graphrag_toolkit.lexical_graph.versioning",
    ]:
        sys.modules.setdefault(dotted, types.ModuleType(dotted))

    # make_llama_index_embedding (via coa_common) subclasses
    # BaseEmbedding at call time — provide a minimal stand-in so _setup_graphrag
    # can run without the real llama_index installed.
    be = sys.modules["llama_index.core.base.embeddings.base"]
    if not hasattr(be, "BaseEmbedding"):

        class _StubBaseEmbedding:
            def __init__(self, *args, **kwargs) -> None:
                self.model_name = kwargs.get("model_name")

        be.BaseEmbedding = _StubBaseEmbedding

    # Populate attributes that @patch decorators need to find on the stub modules.
    # patch() requires the attribute to already exist before it can replace it.
    lg = sys.modules["graphrag_toolkit.lexical_graph"]
    for attr in ["LexicalGraphIndex", "GraphRAGConfig", "ExtractionConfig", "IndexingConfig", "add_versioning_info"]:
        if not hasattr(lg, attr):
            setattr(lg, attr, MagicMock())

    st = sys.modules["graphrag_toolkit.lexical_graph.storage"]
    for attr in ["GraphStoreFactory", "VectorStoreFactory"]:
        if not hasattr(st, attr):
            setattr(st, attr, MagicMock())

    bd = sys.modules["graphrag_toolkit.lexical_graph.indexing.build"]
    if not hasattr(bd, "Checkpoint"):
        bd.Checkpoint = MagicMock()

    ex = sys.modules["graphrag_toolkit.lexical_graph.indexing.extract"]
    for attr in ["BatchConfig", "InferClassificationsConfig"]:
        if not hasattr(ex, attr):
            setattr(ex, attr, MagicMock())

    ld = sys.modules["graphrag_toolkit.lexical_graph.indexing.load"]
    if not hasattr(ld, "S3BasedDocs"):
        ld.S3BasedDocs = MagicMock()

    vs = sys.modules["graphrag_toolkit.lexical_graph.versioning"]
    for attr in ["VersioningConfig", "VersioningMode"]:
        if not hasattr(vs, attr):
            setattr(vs, attr, MagicMock())

    # llama_index.core.Document
    sys.modules["llama_index.core"].Document = MagicMock()

    # llama_index.core.node_parser.SentenceSplitter — imported by
    # graph_build._build_indexing_config when CHUNK_SIZE > 0.
    np = sys.modules["llama_index.core.node_parser"]
    if not hasattr(np, "SentenceSplitter"):
        np.SentenceSplitter = MagicMock()


_stub_missing_modules()

# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------

_BASE_ENV = {
    "DOC_SOURCE_ID": "ds-001",
    "NAMESPACE_ID": "tenant-a",
    "TENANT_ID": "tenanta",
    "STAGING_PREFIX": "tenant-a/staging/ds-001/",
    "BUCKET_NAME": f"{RESOURCE_PREFIX}-dev-unstructured-data-123456789012",
    "DOC_SOURCES_TABLE": f"{RESOURCE_PREFIX}-dev-doc-sources",
    "NEPTUNE_ENDPOINT": "my-neptune.us-east-1.neptune.amazonaws.com",
    "OPENSEARCH_ENDPOINT": "abc123.us-east-1.aoss.amazonaws.com",
    "AWS_REGION": "us-east-1",
    "EXTRACTION_MODE": "continuous",  # passed via container override (from extraction_config)
    "USE_BATCH_INFERENCE": "false",  # passed via container override (from extraction_config)
    "ENABLE_PROPOSITION_EXTRACTION": "true",  # passed via container override
    "ENABLE_VERSIONING": "false",  # disable for most tests to avoid mocking add_versioning_info
    "DELETE_PREV_VERSIONS": "false",  # passed via container override
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key, val in _BASE_ENV.items():
        monkeypatch.setenv(key, val)
    sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
    pkg = sys.modules.get("coa_sources.documents.kg_build")
    if pkg and hasattr(pkg, "graph_build"):
        delattr(pkg, "graph_build")


@pytest.fixture()
def mod():
    sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
    from coa_sources.documents.kg_build import graph_build as entrypoint

    return entrypoint


@pytest.fixture()
def mod_factory():
    """Import graph_build *after* the test has set env vars.

    Module-level constants (PREFERRED_TOPICS, PREFERRED_ENTITY_CLASSIFICATIONS,
    CHUNK_SIZE, …) are evaluated at import, so a test that changes the
    environment must trigger the import itself rather than rely on the ``mod``
    fixture, which resolves at setup time.
    """

    def _import():
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        from coa_sources.documents.kg_build import graph_build as entrypoint

        return entrypoint

    return _import


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s3_obj(key: str, size: int = 1024) -> dict:
    return {"Key": key, "Size": size}


def _staged(filenames: list[str]) -> list[dict]:
    return [_s3_obj(f"tenant-a/staging/ds-001/{fn}") for fn in filenames]


def _bytes(text: str) -> bytes:
    return text.encode("utf-8")


def _sidecar(stem: str, **overrides) -> dict:
    base = {
        "original_format": ".pdf",
        "page_count": 3,
        "char_count": 1200,
        "processing_method": "unstructured_partition_pdf",
        "source_s3_key": f"s3://src/raw/{stem}.pdf",
    }
    base.update(overrides)
    return base


def _client_error() -> ClientError:
    return ClientError({"Error": {"Code": "Internal", "Message": "x"}}, "Op")


def _mock_ddb_access():
    ddb = MagicMock()
    ddb.get_attribute.return_value = "ingesting"
    ddb.update.return_value = True
    return ddb


def _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls):
    mock_gs, mock_vs = MagicMock(), MagicMock()
    mock_gsf.for_graph_store.return_value.__enter__ = MagicMock(return_value=mock_gs)
    mock_gsf.for_graph_store.return_value.__exit__ = MagicMock(return_value=False)
    mock_vsf.for_vector_store.return_value.__enter__ = MagicMock(return_value=mock_vs)
    mock_vsf.for_vector_store.return_value.__exit__ = MagicMock(return_value=False)
    mock_index = MagicMock()
    mock_idx_cls.return_value = mock_index
    return mock_index, mock_gs, mock_vs


# ===================================================================
# 1. Environment & input validation
# ===================================================================


class TestEnvValidation:
    @pytest.mark.parametrize(
        "missing_var",
        [
            "DOC_SOURCE_ID",
            "NAMESPACE_ID",
            "STAGING_PREFIX",
            "BUCKET_NAME",
            "DOC_SOURCES_TABLE",
        ],
    )
    @patch("coa_common.s3.boto3")
    @patch("coa_common.dao.dynamodb.boto3")
    def test_missing_env_var_exits(self, _ddb, _s3, missing_var, monkeypatch, mod):
        monkeypatch.delenv(missing_var)
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        with pytest.raises(SystemExit):
            entrypoint.main()

    def test_invalid_extraction_mode_raises(self, monkeypatch):
        monkeypatch.setenv("EXTRACTION_MODE", "invalid")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        with pytest.raises(ValueError, match="is not a valid ExtractionMode"):
            from coa_sources.documents.kg_build import graph_build as entrypoint  # noqa: F401


class TestInputValidation:
    @patch("coa_common.s3.boto3")
    @patch("coa_common.dao.dynamodb.boto3")
    def test_invalid_namespace_id_exits(self, _ddb, _s3, monkeypatch, mod):
        monkeypatch.setenv("NAMESPACE_ID", "bad namespace!")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        with pytest.raises(SystemExit):
            entrypoint.main()

    @patch("coa_common.s3.boto3")
    @patch("coa_common.dao.dynamodb.boto3")
    def test_invalid_doc_source_id_exits(self, _ddb, _s3, monkeypatch, mod):
        monkeypatch.setenv("DOC_SOURCE_ID", "../traversal")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        with pytest.raises(SystemExit):
            entrypoint.main()


# ===================================================================
# 2. Idempotency
# ===================================================================


class TestIdempotency:
    @patch("coa_common.s3.boto3")
    @patch("coa_common.dao.dynamodb.boto3")
    def test_already_completed_skips(self, mock_ddb_boto, _s3, mod):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"status": "COMPLETED"}}
        mock_ddb_boto.resource.return_value.Table.return_value = mock_table
        mod.main()
        _s3.client.return_value.get_paginator.assert_not_called()

    @patch("coa_common.s3.boto3")
    @patch("coa_common.dao.dynamodb.boto3")
    def test_ingesting_continues(self, mock_ddb_boto, mock_s3, mod):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"status": "ingesting"}}
        mock_ddb_boto.resource.return_value.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mod.main()
        mock_s3.client.return_value.get_paginator.assert_called_once()


# ===================================================================
# 3. Loading staged documents + sidecar metadata
# ===================================================================


class TestLoadStagedDocuments:
    @patch("coa_common.s3.boto3")
    def test_loads_text_with_sidecar_metadata(self, mock_s3, mod):
        sidecar_data = _sidecar("report")
        all_files = _staged(["report.md", "report.metadata.json"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(sidecar_data).encode())},
            {"Body": MagicMock(read=lambda: _bytes("# Report\n\nContent"))},
        ]

        with patch("llama_index.core.Document") as MockDoc:
            captured = []
            MockDoc.side_effect = lambda **kw: (captured.append(kw), MagicMock())[1]
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1
        meta = captured[0]["metadata"]
        assert meta["original_format"] == ".pdf"
        assert meta["page_count"] == 3
        assert meta["source_s3_key"] == "s3://src/raw/report.pdf"

    @patch("coa_common.s3.boto3")
    def test_works_without_sidecar(self, mock_s3, mod):
        all_files = _staged(["notes.txt"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.return_value = {
            "Body": MagicMock(read=lambda: _bytes("Some notes")),
        }

        with patch("llama_index.core.Document") as MockDoc:
            captured = []
            MockDoc.side_effect = lambda **kw: (captured.append(kw), MagicMock())[1]
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1
        assert "source_s3_key" not in captured[0]["metadata"]
        assert captured[0]["metadata"]["filename"] == "notes.txt"

    @patch("coa_common.s3.boto3")
    def test_bad_sidecar_doesnt_crash(self, mock_s3, mod):
        all_files = _staged(["doc.txt", "doc.metadata.json"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: b"NOT VALID JSON")},
            {"Body": MagicMock(read=lambda: _bytes("Content"))},
        ]

        with patch("llama_index.core.Document") as MockDoc:
            MockDoc.side_effect = lambda **kw: MagicMock(**kw)
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1

    @patch("coa_common.s3.boto3")
    def test_sanitized_metadata_key_collision_disambiguated(self, mock_s3, mod):
        # "report.id" and "report-id" both sanitize to "report_id"; neither value
        # may be silently dropped — the collision must be disambiguated.
        sidecar_data = {"report.id": "A", "report-id": "B"}
        all_files = _staged(["doc.txt", "doc.metadata.json"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(sidecar_data).encode())},
            {"Body": MagicMock(read=lambda: _bytes("Content"))},
        ]

        with patch("llama_index.core.Document") as MockDoc:
            captured = []
            MockDoc.side_effect = lambda **kw: (captured.append(kw), MagicMock())[1]
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1
        meta = captured[0]["metadata"]
        # Both values survive: one under report_id, the other under report_id_2.
        assert meta.get("report_id") == "A"
        assert meta.get("report_id_2") == "B"

    @patch("coa_common.s3.boto3")
    def test_sanitized_metadata_key_three_way_collision(self, mock_s3, mod):
        # Three distinct source keys collapse to the same sanitized key. Each
        # must land under a unique, monotonically-suffixed name with NO value
        # lost or overwritten. This pins the loop-carried suffix bookkeeping:
        # the free-slot search must account for suffixes already minted earlier
        # in the same loop (report_id, report_id_2) so the third collider gets
        # report_id_3 rather than reusing an occupied slot.
        sidecar_data = {"report.id": "A", "report-id": "B", "report_id": "C"}
        all_files = _staged(["doc.txt", "doc.metadata.json"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(sidecar_data).encode())},
            {"Body": MagicMock(read=lambda: _bytes("Content"))},
        ]

        with patch("llama_index.core.Document") as MockDoc:
            captured = []
            MockDoc.side_effect = lambda **kw: (captured.append(kw), MagicMock())[1]
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1
        meta = captured[0]["metadata"]
        assert meta.get("report_id") == "A"
        assert meta.get("report_id_2") == "B"
        assert meta.get("report_id_3") == "C"
        # All three original values survive — nothing was silently dropped.
        assert {meta["report_id"], meta["report_id_2"], meta["report_id_3"]} == {"A", "B", "C"}

    @patch("coa_common.s3.boto3")
    def test_skips_empty_files(self, mock_s3, mod):
        """An empty staged file is skipped AND counted.

        It used to be skipped with a bare ``continue`` that touched no counter, so
        a source whose files all extracted to zero bytes reported a clean success
        having put nothing in the graph.
        """
        all_files = _staged(["empty.txt"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.return_value = {
            "Body": MagicMock(read=lambda: _bytes("   \n\t  ")),
        }

        with patch("llama_index.core.Document") as MockDoc:
            MockDoc.side_effect = lambda **kw: MagicMock(**kw)
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 0
        assert load_failed == 1, "an empty staged file is absent from the graph and must be counted"

    @patch("coa_common.s3.boto3")
    def test_empty_file_does_not_taint_the_ones_that_loaded(self, mock_s3, mod):
        """The count is per-file — a good file still loads alongside an empty one."""
        all_files = _staged(["good.txt", "empty.txt"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: _bytes("Real content"))},
            {"Body": MagicMock(read=lambda: b"")},
        ]

        with patch("llama_index.core.Document") as MockDoc:
            MockDoc.side_effect = lambda **kw: MagicMock(**kw)
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1
        assert load_failed == 1

    @patch("coa_common.s3.boto3")
    def test_continues_on_file_error(self, mock_s3, mod):
        all_files = _staged(["good.txt", "bad.txt"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: _bytes("Good"))},
            Exception("Access denied"),
        ]

        with patch("llama_index.core.Document") as MockDoc:
            MockDoc.side_effect = lambda **kw: MagicMock(**kw)
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1
        # The file that failed to download is COUNTED (not silently dropped), so
        # main() can fold it into the partial-failure total.
        assert load_failed == 1

    @patch("coa_common.s3.boto3")
    def test_s3_listing_failure_raises(self, mock_s3, mod):
        # A listing failure must RAISE (not return empty): an empty result must
        # be distinguishable from a transient S3/IAM error so the job can mark
        # SCAN_FAILED instead of reporting success having ingested nothing.
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = _client_error()
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator

        with patch("llama_index.core.Document"), pytest.raises(RuntimeError, match="Failed to list staged files"):
            mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

    @patch("coa_common.s3.boto3")
    def test_metadata_excluded_from_llm_chunk_budget(self, mock_s3, mod):
        """All metadata keys are excluded from LlamaIndex's chunk token budget.

        This prevents long fields like source_s3_key from eating into the
        chunk_size, which caused ValueError when metadata > chunk_size.
        """
        sidecar_data = {
            "original_format": ".pdf",
            "source_s3_key": "ns-uuid/raw/ds-uuid/" + "A" * 150 + ".pdf",
            "doc_source_id": "2a7fccf1-4c3b-4663-a645-db2d575e855c",
            "namespace_id": "f7086035-560f-4953-8db4-02bd754754fc",
            "page_count": 5,
            "char_count": 30000,
            "processing_method": "unstructured_partition_pdf",
        }
        all_files = _staged(["contract.md", "contract.metadata.json"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(sidecar_data).encode())},
            {"Body": MagicMock(read=lambda: _bytes("Contract content here."))},
        ]

        # Use a real-ish Document class that records attribute assignments
        class FakeDoc:
            def __init__(self, **kwargs):
                self.text = kwargs.get("text")
                self.metadata = kwargs.get("metadata", {})
                self.excluded_llm_metadata_keys = []
                self.excluded_embed_metadata_keys = []

        with patch("llama_index.core.Document", FakeDoc):
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1
        doc = docs[0]

        # All sidecar fields + filename should be in metadata
        assert "source_s3_key" in doc.metadata
        assert "namespace_id" in doc.metadata
        assert "filename" in doc.metadata

        # All metadata keys excluded from chunk token budget (both LLM and embed)
        assert set(doc.excluded_llm_metadata_keys) == set(doc.metadata.keys())
        assert set(doc.excluded_embed_metadata_keys) == set(doc.metadata.keys())

    @patch("coa_common.s3.boto3")
    def test_metadata_excluded_even_without_sidecar(self, mock_s3, mod):
        """Even with no sidecar, the filename key is excluded."""
        all_files = _staged(["simple.txt"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.return_value = {
            "Body": MagicMock(read=lambda: _bytes("Hello world")),
        }

        class FakeDoc:
            def __init__(self, **kwargs):
                self.text = kwargs.get("text")
                self.metadata = kwargs.get("metadata", {})
                self.excluded_llm_metadata_keys = []
                self.excluded_embed_metadata_keys = []

        with patch("llama_index.core.Document", FakeDoc):
            docs, load_failed = mod._load_staged_documents(_BASE_ENV["BUCKET_NAME"], _BASE_ENV["STAGING_PREFIX"])

        assert len(docs) == 1
        doc = docs[0]
        assert doc.metadata == {"filename": "simple.txt"}
        assert doc.excluded_llm_metadata_keys == ["filename"]
        assert doc.excluded_embed_metadata_keys == ["filename"]


# ===================================================================
# 4. Continuous mode (_run_continuous)
# ===================================================================


class TestRunContinuous:
    @patch("graphrag_toolkit.lexical_graph.indexing.build.Checkpoint")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_happy_path(self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _ckpt, mod):
        mock_index, _, _ = _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)
        ddb = _mock_ddb_access()

        docs = [MagicMock() for _ in range(5)]
        ok, fail = mod._run_continuous(
            docs,
            "wss://nep:8182/sparql",
            "aoss://oss",
            "t.a.ds.001",
            None,
            ddb,
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            None,
        )

        assert ok == 5
        assert fail == 0
        mock_index.extract_and_build.assert_called_once_with(docs, checkpoint=_ckpt.return_value, progress_monitor=None)
        assert ddb.update.call_count == 2
        ddb.update.assert_any_call(
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            {"status": SourceStatus.SCANNING_ENTITY_EXTRACTION},
        )
        ddb.update.assert_any_call(
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            {"docsGraphIngested": 5},
        )

    @patch("graphrag_toolkit.lexical_graph.indexing.build.Checkpoint")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_failure_returns_zero_ok(self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _ckpt, mod):
        mock_index, _, _ = _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)
        mock_index.extract_and_build.side_effect = RuntimeError("boom")
        ddb = _mock_ddb_access()

        docs = [MagicMock() for _ in range(3)]
        ok, fail = mod._run_continuous(
            docs,
            "wss://nep:8182/sparql",
            "aoss://oss",
            "t.a.ds.001",
            None,
            ddb,
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            None,
        )

        assert ok == 0
        assert fail == 3

    @patch("graphrag_toolkit.lexical_graph.indexing.build.Checkpoint")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_failure_updates_ddb_status_to_failed(self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _ckpt, mod):
        """When extract_and_build fails, DDB status should be set to FAILED."""
        mock_index, _, _ = _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)
        mock_index.extract_and_build.side_effect = RuntimeError("boom")
        ddb = _mock_ddb_access()

        docs = [MagicMock() for _ in range(3)]
        mod._run_continuous(
            docs,
            "wss://nep:8182/sparql",
            "aoss://oss",
            "t.a.ds.001",
            None,
            ddb,
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            None,
        )

        # Verify DDB was updated with SCAN_FAILED status
        assert ddb.update.call_count == 2
        last_call = ddb.update.call_args_list[-1]
        assert last_call[0][1]["status"] == "SCAN_FAILED"


# ===================================================================
# 5. Separated mode (_run_separated)
# ===================================================================


class TestRunSeparated:
    @patch("graphrag_toolkit.lexical_graph.indexing.load.S3BasedDocs")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_happy_path(self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _s3docs, mod):
        mock_index, _, _ = _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)
        ddb = _mock_ddb_access()

        docs = [MagicMock() for _ in range(4)]
        ok, fail = mod._run_separated(
            docs,
            "wss://nep:8182/sparql",
            "aoss://oss",
            "t.a.ds.001",
            None,
            _BASE_ENV["BUCKET_NAME"],
            "tenant-a",
            "ds-001",
            ddb,
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            None,
        )

        assert ok == 4
        assert fail == 0
        mock_index.extract.assert_called_once()
        mock_index.build.assert_called_once()
        assert ddb.update.call_count == 3
        ddb.update.assert_any_call(
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            {"status": SourceStatus.SCANNING_ENTITY_EXTRACTION},
        )
        ddb.update.assert_any_call(
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            {"status": SourceStatus.SCANNING_KG_BUILD},
        )
        ddb.update.assert_any_call(
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            {"docsGraphIngested": 4},
        )

    @patch("graphrag_toolkit.lexical_graph.indexing.load.S3BasedDocs")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_extract_failure(self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _s3docs, mod):
        mock_index, _, _ = _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)
        mock_index.extract.side_effect = RuntimeError("Bedrock down")
        ddb = _mock_ddb_access()

        docs = [MagicMock() for _ in range(3)]
        ok, fail = mod._run_separated(
            docs,
            "wss://nep:8182/sparql",
            "aoss://oss",
            "t.a.ds.001",
            None,
            _BASE_ENV["BUCKET_NAME"],
            "tenant-a",
            "ds-001",
            ddb,
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            None,
        )

        assert ok == 0
        assert fail == 3
        mock_index.build.assert_not_called()

    @patch("graphrag_toolkit.lexical_graph.indexing.load.S3BasedDocs")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_build_failure(self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _s3docs, mod):
        mock_index, _, _ = _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)
        mock_index.build.side_effect = RuntimeError("Neptune down")
        ddb = _mock_ddb_access()

        docs = [MagicMock() for _ in range(3)]
        ok, fail = mod._run_separated(
            docs,
            "wss://nep:8182/sparql",
            "aoss://oss",
            "t.a.ds.001",
            None,
            _BASE_ENV["BUCKET_NAME"],
            "tenant-a",
            "ds-001",
            ddb,
            {"namespace_id": "tenant-a", "doc_source_id": "ds-001"},
            None,
        )

        assert ok == 0
        assert fail == 3
        mock_index.extract.assert_called_once()  # extract succeeded


# ===================================================================
# 6. GraphRAG configuration
# ===================================================================


class TestGraphRAGConfig:
    @patch("graphrag_toolkit.lexical_graph.indexing.build.Checkpoint")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_store_uris_and_tenant(self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _ckpt, mod):
        _, mock_gs, mock_vs = _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)

        mod._setup_graphrag("tenant-x")

        mock_gsf.for_graph_store.assert_not_called()  # setup doesn't open stores
        assert _cfg.enable_versioning is False  # from _BASE_ENV

    @patch("graphrag_toolkit.lexical_graph.indexing.build.Checkpoint")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_extraction_llm_is_json_with_max_tokens(
        self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _ckpt, monkeypatch, mod
    ):
        """extraction_llm must be a JSON config carrying max_tokens, not a bare ARN.

        The toolkit's ``GraphRAGConfig.to_llm()`` hardcodes ``max_tokens=4096`` when
        given a bare model string and only honours ``max_tokens`` from the JSON form.
        At 4096 output tokens, extraction truncates mid-response on dense chunks and
        that chunk's trailing entities/statements are silently lost.
        """
        monkeypatch.setattr(mod, "BEDROCK_MODEL_ARN", "arn:aws:bedrock:us-east-1:1:inference-profile/m")
        _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)

        mod._setup_graphrag("tenant-x")

        assigned = json.loads(_cfg.extraction_llm)
        assert assigned["model"] == "arn:aws:bedrock:us-east-1:1:inference-profile/m"
        assert assigned["max_tokens"] == 16384

    @patch("graphrag_toolkit.lexical_graph.indexing.build.Checkpoint")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    def test_extraction_max_tokens_env_override(self, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _ckpt, monkeypatch, mod):
        """EXTRACTION_MAX_TOKENS is overridable without a redeploy."""
        monkeypatch.setattr(mod, "BEDROCK_MODEL_ARN", "arn:aws:bedrock:us-east-1:1:inference-profile/m")
        monkeypatch.setattr(mod, "EXTRACTION_MAX_TOKENS", 8192)
        _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)

        mod._setup_graphrag("tenant-x")

        assert json.loads(_cfg.extraction_llm)["max_tokens"] == 8192


# ===================================================================
# 7. Indexing config builder
# ===================================================================


class TestBuildIndexingConfig:
    """``_build_indexing_config`` must ALWAYS return a fully-specified
    IndexingConfig — never None, never a bare ``ExtractionConfig()``. Both would
    let the toolkit fall back to its hardcoded ``DEFAULT_ENTITY_CLASSIFICATIONS``
    (news/finance list). These tests are the regression guard.
    """

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_default_flags_build_infer_config(self, mock_ic, mock_ec, mock_icc, mod):
        """Under default flags an ExtractionConfig is still built, with
        preferred_entity_classifications=[] and infer_entity_classifications
        wired to an InferClassificationsConfig — i.e. no news vocabulary."""
        config = mod._build_indexing_config("bucket", "ns", "ds")

        assert config is not None
        mock_ec.assert_called_once()
        kwargs = mock_ec.call_args.kwargs
        # These three together are the fix: empty seed + infer + replace.
        assert kwargs["preferred_entity_classifications"] == []
        assert kwargs["enable_proposition_extraction"] is True
        assert kwargs["infer_entity_classifications"] is mock_icc.return_value
        mock_icc.assert_called_once_with(replace_default_classifications=True)

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_infer_disabled_via_env(self, mock_ic, mock_ec, mock_icc, monkeypatch, mod):
        """INFER_ENTITY_CLASSIFICATIONS=false → infer_entity_classifications=False.
        Empty preferred list still stands, so the toolkit gets [] rather than
        its own default list.
        """
        monkeypatch.setenv("INFER_ENTITY_CLASSIFICATIONS", "false")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        entrypoint._build_indexing_config("bucket", "ns", "ds")
        kwargs = mock_ec.call_args.kwargs
        assert kwargs["preferred_entity_classifications"] == []
        assert kwargs["infer_entity_classifications"] is False
        mock_icc.assert_not_called()

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_proposition_disabled(self, mock_ic, mock_ec, mock_icc, monkeypatch, mod):
        monkeypatch.setenv("ENABLE_PROPOSITION_EXTRACTION", "false")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        entrypoint._build_indexing_config("bucket", "ns", "ds")
        kwargs = mock_ec.call_args.kwargs
        assert kwargs["enable_proposition_extraction"] is False

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.indexing.extract.BatchConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_batch_inference_with_role(self, mock_ic, mock_ec, mock_bc, mock_icc, monkeypatch, mod):
        monkeypatch.setenv("USE_BATCH_INFERENCE", "true")
        monkeypatch.setenv("BATCH_INFERENCE_ROLE_ARN", "arn:aws:iam::123:role/batch")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        entrypoint._build_indexing_config("bucket", "ns", "ds")
        mock_bc.assert_called_once()
        assert mock_bc.call_args[1]["role_arn"] == "arn:aws:iam::123:role/batch"

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_batch_inference_without_role_falls_back(self, mock_ic, mock_ec, mock_icc, monkeypatch, mod):
        """USE_BATCH_INFERENCE=true but no role → warning, no BatchConfig. An
        ExtractionConfig is still built (batch is orthogonal to vocabulary)."""
        monkeypatch.setenv("USE_BATCH_INFERENCE", "true")
        monkeypatch.setenv("BATCH_INFERENCE_ROLE_ARN", "")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        config = entrypoint._build_indexing_config("bucket", "ns", "ds")
        # New contract: still returns an IndexingConfig (with the vocabulary
        # fix); only BatchConfig is absent.
        assert config is not None
        mock_ec.assert_called_once()

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_explicit_preferred_list_wins_over_infer(self, mock_ic, mock_ec, mock_icc, monkeypatch, mod):
        """A non-empty PREFERRED_ENTITY_CLASSIFICATIONS is
        authoritative. Inference must NOT run — "these labels, exactly"."""
        monkeypatch.setenv("PREFERRED_ENTITY_CLASSIFICATIONS", '["Policy", "Claim", "Loss Ratio"]')
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        entrypoint._build_indexing_config("bucket", "ns", "ds")
        kwargs = mock_ec.call_args.kwargs
        assert kwargs["preferred_entity_classifications"] == ["Policy", "Claim", "Loss Ratio"]
        assert kwargs["infer_entity_classifications"] is False
        mock_icc.assert_not_called()

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_preferred_empty_json_falls_through_to_infer(self, mock_ic, mock_ec, mock_icc, monkeypatch, mod):
        """Empty list stringified as "[]" (the default from the trigger) must
        NOT block infer — otherwise every default request would run unguided.
        """
        monkeypatch.setenv("PREFERRED_ENTITY_CLASSIFICATIONS", "[]")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        entrypoint._build_indexing_config("bucket", "ns", "ds")
        kwargs = mock_ec.call_args.kwargs
        assert kwargs["preferred_entity_classifications"] == []
        assert kwargs["infer_entity_classifications"] is mock_icc.return_value

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_invalid_preferred_json_raises_at_import(self, mock_ic, mock_ec, mock_icc, monkeypatch, mod):
        """Malformed JSON now fails loud instead of falling through to infer.

        The API 400s bad vocabulary before ingest, so a malformed env var here is a
        pipeline defect; crashing (→ SCAN_FAILED) surfaces it, where the old
        fall-through silently produced a graph ignoring the configured vocabulary.
        """
        monkeypatch.setenv("PREFERRED_ENTITY_CLASSIFICATIONS", "not json")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        with pytest.raises(ValueError, match="not a JSON array of strings"):
            from coa_sources.documents.kg_build import graph_build  # noqa: F401

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_wellformed_json_wrong_type_raises_at_import(self, mock_ic, mock_ec, mock_icc, monkeypatch, mod):
        """Valid JSON that is NOT a list-of-strings (e.g. an object) now raises
        rather than being swallowed and falling through to infer."""
        monkeypatch.setenv("PREFERRED_ENTITY_CLASSIFICATIONS", '{"Policy": 1}')
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        with pytest.raises(ValueError, match="not a JSON array of strings"):
            from coa_sources.documents.kg_build import graph_build  # noqa: F401

    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_both_infer_and_preferred_off_warns(self, mock_ic, mock_ec, mock_icc, monkeypatch, mod, caplog):
        """If a user disables both, extraction runs unguided — log it loudly."""
        monkeypatch.setenv("INFER_ENTITY_CLASSIFICATIONS", "false")
        monkeypatch.setenv("PREFERRED_ENTITY_CLASSIFICATIONS", "[]")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        with caplog.at_level("WARNING"):
            entrypoint._build_indexing_config("bucket", "ns", "ds")
        kwargs = mock_ec.call_args.kwargs
        assert kwargs["preferred_entity_classifications"] == []
        assert kwargs["infer_entity_classifications"] is False

    @patch("llama_index.core.node_parser.SentenceSplitter")
    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_chunk_size_override_wires_splitter(self, mock_ic, mock_ec, mock_icc, mock_ss, monkeypatch, mod):
        """CHUNK_SIZE>0 pins a SentenceSplitter into IndexingConfig(chunking=[...])."""
        monkeypatch.setenv("CHUNK_SIZE", "1024")
        monkeypatch.setenv("CHUNK_OVERLAP", "50")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        entrypoint._build_indexing_config("bucket", "ns", "ds")
        mock_ss.assert_called_once_with(chunk_size=1024, chunk_overlap=50)
        assert "chunking" in mock_ic.call_args.kwargs

    @patch("llama_index.core.node_parser.SentenceSplitter")
    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_chunk_overlap_ge_size_is_clamped(self, mock_ic, mock_ec, mock_icc, mock_ss, monkeypatch, mod):
        """CHUNK_OVERLAP >= CHUNK_SIZE would make SentenceSplitter emit
        degenerate/empty chunks. The per-field @range constraints cannot express
        this cross-field invariant, so the code must clamp overlap to size-1.
        Regression guard for the review finding."""
        monkeypatch.setenv("CHUNK_SIZE", "100")
        monkeypatch.setenv("CHUNK_OVERLAP", "200")
        sys.modules.pop("coa_sources.documents.kg_build.graph_build", None)
        pkg = sys.modules.get("coa_sources.documents.kg_build")
        if pkg and hasattr(pkg, "graph_build"):
            delattr(pkg, "graph_build")
        from coa_sources.documents.kg_build import graph_build as entrypoint

        entrypoint._build_indexing_config("bucket", "ns", "ds")
        mock_ss.assert_called_once_with(chunk_size=100, chunk_overlap=99)

    @patch("llama_index.core.node_parser.SentenceSplitter")
    @patch("graphrag_toolkit.lexical_graph.indexing.extract.InferClassificationsConfig")
    @patch("graphrag_toolkit.lexical_graph.ExtractionConfig")
    @patch("graphrag_toolkit.lexical_graph.IndexingConfig")
    def test_chunk_size_zero_keeps_toolkit_default(self, mock_ic, mock_ec, mock_icc, mock_ss, mod):
        """CHUNK_SIZE=0 (default) must not pass a chunking= kwarg — otherwise
        the toolkit's SentenceSplitter fallback (256/25) never runs. Regression
        guard: this is what preserves the SEC-10-Q baseline."""
        mod._build_indexing_config("bucket", "ns", "ds")
        mock_ss.assert_not_called()
        assert "chunking" not in mock_ic.call_args.kwargs


# ===================================================================
# 8. End-to-end main() flow
# ===================================================================


class TestMainFlow:
    @patch("coa_common.s3.boto3")
    @patch("coa_common.dao.dynamodb.boto3")
    def test_no_staged_docs_exits_gracefully(self, mock_ddb_boto, mock_s3, mod):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"status": "ingesting"}}
        mock_ddb_boto.resource.return_value.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mod.main()

    @patch("coa_common.s3.boto3")
    @patch("coa_common.dao.dynamodb.boto3")
    def test_s3_listing_failure_exits_nonzero_and_marks_failed(self, mock_ddb_boto, mock_s3, mod):
        # A transient ListObjects failure must NOT be treated as "no documents":
        # main() marks the source SCAN_FAILED and exits 1 (finding #4).
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"status": "ingesting"}}
        mock_ddb_boto.resource.return_value.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = _client_error()
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator

        with patch("llama_index.core.Document"), pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 1
        # A SCAN_FAILED status write was issued (not a silent success/return).
        # The DAO writes status via an UpdateExpression, so the value lands in
        # ExpressionAttributeValues.
        wrote_failed = False
        for c in mock_table.update_item.call_args_list:
            values = c.kwargs.get("ExpressionAttributeValues", {})
            if any("fail" in str(v).lower() for v in values.values()):
                wrote_failed = True
        assert wrote_failed

    @patch("graphrag_toolkit.lexical_graph.indexing.build.Checkpoint")
    @patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory")
    @patch("graphrag_toolkit.lexical_graph.LexicalGraphIndex")
    @patch("graphrag_toolkit.lexical_graph.GraphRAGConfig")
    @patch("coa_common.s3.boto3")
    @patch("coa_common.dao.dynamodb.boto3")
    def test_continuous_failure_exits_nonzero(
        self, mock_ddb_boto, mock_s3, _cfg, mock_idx_cls, mock_gsf, mock_vsf, _ckpt, mod
    ):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"status": "ingesting"}}
        mock_ddb_boto.resource.return_value.Table.return_value = mock_table

        all_files = _staged(["doc.txt"])
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": all_files}]
        mock_s3.client.return_value.get_paginator.return_value = mock_paginator
        mock_s3.client.return_value.get_object.return_value = {"Body": MagicMock(read=lambda: _bytes("Content"))}

        mock_index, _, _ = _setup_graphrag_mocks(mock_gsf, mock_vsf, mock_idx_cls)
        mock_index.extract_and_build.side_effect = RuntimeError("fail")

        with patch("llama_index.core.Document") as MockDoc:
            MockDoc.side_effect = lambda **kw: MagicMock(**kw)
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 1


# ===================================================================
# 9. AOSS NEXTGEN patch
# ===================================================================


class TestAossNextgenPatch:
    """_patch_graphrag_toolkit_for_aoss_nextgen replaces index_exists with
    a version that omits the 'engine' field rejected by AOSS NEXTGEN."""

    def _make_ovi_stub(self):
        """Build a minimal stub of opensearch_vector_indexes with required attrs."""
        ovi = types.ModuleType("graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes")
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = False
        mock_client.indices.create.return_value = {}
        ovi.create_os_client = MagicMock(return_value=mock_client)
        ovi.index_is_available = MagicMock(return_value=True)
        ovi.index_exists = MagicMock()  # original — will be replaced
        return ovi, mock_client

    def _graphrag_modules(self, ovi):
        """Return a sys.modules overlay that makes the full graphrag import chain resolvable."""
        re_stub = types.ModuleType("opensearchpy.exceptions")
        re_stub.RequestError = Exception
        # All intermediate packages must be registered so Python can resolve the dotted path.
        storage_stub = sys.modules["graphrag_toolkit.lexical_graph.storage"]
        vector_stub = types.ModuleType("graphrag_toolkit.lexical_graph.storage.vector")
        vector_stub.opensearch_vector_indexes = ovi
        storage_stub.vector = vector_stub
        return {
            "graphrag_toolkit.lexical_graph.storage.vector": vector_stub,
            "graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes": ovi,
            "opensearchpy": types.ModuleType("opensearchpy"),
            "opensearchpy.exceptions": re_stub,
        }

    def test_patch_replaces_index_exists_and_omits_engine(self, mod):
        """After patching, creating an index must NOT include the 'engine' key."""
        ovi, mock_client = self._make_ovi_stub()

        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_aoss_patched = False
            mod._patch_graphrag_toolkit_for_aoss_nextgen()

            assert mod._graphrag_aoss_patched is True
            result = ovi.index_exists("endpoint", "my-index", 1024, True)
            assert result is True

            create_call = mock_client.indices.create.call_args
            assert create_call is not None, "indices.create must have been called"
            body = create_call.kwargs.get("body") or (create_call.args[1] if len(create_call.args) > 1 else None)
            assert body is not None
            props = body["mappings"]["properties"]["embedding"]
            assert props["type"] == "knn_vector"
            assert props["dimension"] == 1024
            assert "method" not in props, "engine/method block must not be sent to AOSS NEXTGEN"
            mock_client.close.assert_called_once()

    def test_allow_create_false_never_creates_a_missing_index(self, mod):
        """The DELETION path must not resurrect a graphrag index.

        graphrag's VectorIndex.writeable defaults to True and its index_exists
        creates when writeable, so a cleanup run against a tenant whose indexes
        are already gone would recreate chunk_*/topic_* as empty shells for a
        namespace on its way out. Nothing reclaims those and AOSS caps a
        collection at 1000 indexes, so they accumulate until every embedding
        write fails with index_limit_breached. Same reads-must-not-create rule
        as OpenSearchVectorStore._read_index.
        """
        ovi, mock_client = self._make_ovi_stub()
        mock_client.indices.exists.return_value = False  # index is absent

        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_aoss_patched = False
            mod._patch_graphrag_toolkit_for_aoss_nextgen(allow_create=False)

            # writeable=True is what graphrag passes by default — the point is
            # that allow_create=False overrides it.
            result = ovi.index_exists("endpoint", "chunk_deadtenant", 1024, True)

            assert result is False, "a missing index must report absent, not be created"
            mock_client.indices.create.assert_not_called()

    def test_allow_create_false_still_reports_an_existing_index(self, mod):
        """Deletion must still find and operate on indexes that DO exist."""
        ovi, mock_client = self._make_ovi_stub()
        mock_client.indices.exists.return_value = True

        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_aoss_patched = False
            mod._patch_graphrag_toolkit_for_aoss_nextgen(allow_create=False)

            assert ovi.index_exists("endpoint", "chunk_livetenant", 1024, True) is True
            mock_client.indices.create.assert_not_called()

    def test_build_path_still_creates(self, mod):
        """The build/ingest path must keep creating — it is the only writer."""
        ovi, mock_client = self._make_ovi_stub()
        mock_client.indices.exists.return_value = False

        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_aoss_patched = False
            mod._patch_graphrag_toolkit_for_aoss_nextgen()  # default allow_create=True

            assert ovi.index_exists("endpoint", "chunk_newtenant", 1024, True) is True
            mock_client.indices.create.assert_called_once()

    def test_patch_is_idempotent(self, mod):
        """Calling the patch twice must not fail and must not double-wrap index_exists."""
        ovi, _ = self._make_ovi_stub()

        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_aoss_patched = False
            mod._patch_graphrag_toolkit_for_aoss_nextgen()
            first_fn = ovi.index_exists
            mod._patch_graphrag_toolkit_for_aoss_nextgen()
            assert ovi.index_exists is first_fn

    def test_patch_noop_when_module_unavailable(self, mod):
        """ImportError during import must not raise — patch is silently skipped."""
        mod._graphrag_aoss_patched = False
        # Removing the module entirely triggers the ImportError guard in the patch fn
        with patch.dict(
            sys.modules,
            {
                "graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes": None,
            },
        ):
            mod._patch_graphrag_toolkit_for_aoss_nextgen()  # must not raise


class TestPaginatedSearchRetryPatch:
    """_patch_graphrag_paginated_search_retry replaces OpenSearchIndex.paginated_search
    with a version that retries transient AOSS 5xx/429 and fixes the upstream
    unbound-variable bug (``raise e`` where ``e`` is never bound)."""

    class _TransportError(Exception):
        def __init__(self, status_code, info=None):
            super().__init__(f"transport error {status_code}")
            self.status_code = status_code
            self.info = info

    class _NotFoundError(_TransportError):
        pass

    def _make_ovi_stub(self):
        ovi = types.ModuleType("graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes")

        class OpenSearchIndex:
            def paginated_search(self, query, page_size=10000, max_pages=None, ids_only=False):
                # original — will be replaced by the patch
                yield from ()

        ovi.OpenSearchIndex = OpenSearchIndex
        return ovi

    def _graphrag_modules(self, ovi):
        ex_stub = types.ModuleType("opensearchpy.exceptions")
        ex_stub.NotFoundError = self._NotFoundError
        ex_stub.TransportError = self._TransportError
        storage_stub = sys.modules["graphrag_toolkit.lexical_graph.storage"]
        vector_stub = types.ModuleType("graphrag_toolkit.lexical_graph.storage.vector")
        vector_stub.opensearch_vector_indexes = ovi
        storage_stub.vector = vector_stub
        return {
            "graphrag_toolkit.lexical_graph.storage.vector": vector_stub,
            "graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes": ovi,
            "opensearchpy": types.ModuleType("opensearchpy"),
            "opensearchpy.exceptions": ex_stub,
        }

    def _make_index(self, ovi, search_side_effect):
        """Build an OpenSearchIndex instance whose ._os_client.search uses side_effect."""
        inst = ovi.OpenSearchIndex()
        mock_os_client = MagicMock()
        mock_os_client.search.side_effect = search_side_effect
        inst.client = MagicMock()
        inst.client._os_client = mock_os_client
        inst.underlying_index_name = MagicMock(return_value="chunk_tenant")
        return inst, mock_os_client

    def test_retries_transient_5xx_then_succeeds(self, mod, monkeypatch):
        """A transient 500 is retried and the search ultimately succeeds (no crash)."""
        monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
        ovi = self._make_ovi_stub()
        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_paginated_search_patched = False
            mod._patch_graphrag_paginated_search_retry()
            assert mod._graphrag_paginated_search_patched is True

            page = {"hits": {"hits": [{"_id": "a", "sort": ["a"]}]}}
            empty = {"hits": {"hits": []}}
            # 500 once, then a page, then empty (terminate)
            inst, client = self._make_index(ovi, [self._TransportError(500, info={"error": "boom"}), page, empty])
            results = list(inst.paginated_search({"match_all": {}}))
            assert results == [page["hits"]["hits"]]
            assert client.search.call_count == 3  # 1 fail + 1 retry-success + 1 empty

    def test_persistent_5xx_reraises_bound_error(self, mod, monkeypatch):
        """A persistent 500 exhausts retries and re-raises the REAL error (no UnboundLocalError)."""
        monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
        ovi = self._make_ovi_stub()
        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_paginated_search_patched = False
            mod._patch_graphrag_paginated_search_retry()
            inst, client = self._make_index(ovi, self._TransportError(500, info={"error": "persistent"}))
            with pytest.raises(self._TransportError) as exc_info:
                list(inst.paginated_search({"match_all": {}}))
            assert exc_info.value.status_code == 500
            # 1 initial + _PAGINATED_MAX_RETRIES retries
            assert client.search.call_count == mod._PAGINATED_MAX_RETRIES + 1

    def test_non_retryable_4xx_reraises_immediately(self, mod):
        """A 400 is not retried — re-raised on the first attempt."""
        ovi = self._make_ovi_stub()
        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_paginated_search_patched = False
            mod._patch_graphrag_paginated_search_retry()
            inst, client = self._make_index(ovi, self._TransportError(400))
            with pytest.raises(self._TransportError):
                list(inst.paginated_search({"match_all": {}}))
            assert client.search.call_count == 1

    def test_patch_is_idempotent(self, mod):
        ovi = self._make_ovi_stub()
        with patch.dict(sys.modules, self._graphrag_modules(ovi)):
            mod._graphrag_paginated_search_patched = False
            mod._patch_graphrag_paginated_search_retry()
            first = ovi.OpenSearchIndex.paginated_search
            mod._patch_graphrag_paginated_search_retry()
            assert ovi.OpenSearchIndex.paginated_search is first

    def test_patch_noop_when_module_unavailable(self, mod):
        mod._graphrag_paginated_search_patched = False
        with patch.dict(
            sys.modules,
            {"graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes": None},
        ):
            mod._patch_graphrag_paginated_search_retry()  # must not raise


class TestBulkIngestRetryPatch:
    """_patch_graphrag_bulk_ingest_retry replaces OpensearchVectorClient._bulk_ingest_embeddings
    with a version that retries transient AOSS circuit-breaker 429/503 items with
    backoff (instead of letting graphrag's raise_on_error=True bulk kill the build)."""

    def _make_client_module(self):
        """Stub llama_index.vector_stores.opensearch with an OpensearchVectorClient class."""
        osvs = types.ModuleType("llama_index.vector_stores.opensearch")

        class OpensearchVectorClient:
            def _import_not_found_error(self):
                class _NotFound(Exception):
                    pass

                return _NotFound

            def _bulk_ingest_embeddings(self, *a, **k):  # original — replaced by patch
                return []

        osvs.OpensearchVectorClient = OpensearchVectorClient
        return osvs

    def _modules(self, osvs, bulk_fn):
        helpers = types.ModuleType("opensearchpy.helpers")
        helpers.bulk = bulk_fn
        li = types.ModuleType("llama_index")
        li_vs = types.ModuleType("llama_index.vector_stores")
        return {
            "llama_index": li,
            "llama_index.vector_stores": li_vs,
            "llama_index.vector_stores.opensearch": osvs,
            "opensearchpy": types.ModuleType("opensearchpy"),
            "opensearchpy.helpers": helpers,
        }

    def _call(self, osvs):
        """Invoke the patched _bulk_ingest_embeddings with 2 docs against a mock client."""
        inst = osvs.OpensearchVectorClient()
        client = MagicMock()
        return inst._bulk_ingest_embeddings(
            client,
            "chunk_tenant",
            embeddings=[[0.1], [0.2]],
            texts=["a", "b"],
            metadatas=[{}, {}],
            ids=["id-a", "id-b"],
            is_aoss=True,
        )

    def test_retries_breaker_429_then_succeeds(self, mod, monkeypatch):
        """A 429 circuit_breaking_exception on one item is retried, then succeeds.

        Also asserts the patch calls bulk() with raise_on_error=False — the
        load-bearing kwarg that turns a fatal BulkIndexError into a retriable
        error list.
        """
        monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
        calls = {"n": 0, "kwargs": []}

        def fake_bulk(client, requests, **kwargs):
            calls["n"] += 1
            calls["kwargs"].append(kwargs)
            if calls["n"] == 1:
                # first attempt: item id-b throttled (429)
                return (1, [{"index": {"id": "id-b", "status": 429, "error": {"type": "circuit_breaking_exception"}}}])
            return (len(requests), [])  # retry succeeds

        osvs = self._make_client_module()
        with patch.dict(sys.modules, self._modules(osvs, fake_bulk)):
            mod._graphrag_bulk_ingest_patched = False
            mod._patch_graphrag_bulk_ingest_retry()
            assert mod._graphrag_bulk_ingest_patched is True
            result = self._call(osvs)
            assert result == []  # no unresolved errors
            assert calls["n"] == 2  # initial + 1 retry
            # the patch MUST disable raise_on_error, else bulk would throw BulkIndexError
            assert calls["kwargs"][0].get("raise_on_error") is False
            # the retry must re-send ONLY the throttled item (id-b), not both
            assert len(calls["kwargs"]) == 2

    def test_persistent_breaker_raises_after_max_retries(self, mod, monkeypatch):
        monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)

        def fake_bulk(client, requests, **kwargs):
            return (0, [{"index": {"id": "id-a", "status": 429, "error": {"type": "circuit_breaking_exception"}}}])

        osvs = self._make_client_module()
        with patch.dict(sys.modules, self._modules(osvs, fake_bulk)):
            mod._graphrag_bulk_ingest_patched = False
            mod._patch_graphrag_bulk_ingest_retry()
            with pytest.raises(RuntimeError, match="circuit breaker|throttled"):
                self._call(osvs)

    def test_retries_aoss_500_internal_error(self, mod, monkeypatch):
        """AOSS HTTP 500 'Internal error' is transient and must be retried, not fatal."""
        monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
        calls = {"n": 0}

        def fake_bulk(client, requests, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    0,
                    [
                        {
                            "index": {
                                "id": "id-a",
                                "status": 500,
                                "error": {
                                    "type": "exception",
                                    "reason": "Internal error occurred while processing request",
                                },
                            }
                        }
                    ],
                )
            return (len(requests), [])

        osvs = self._make_client_module()
        with patch.dict(sys.modules, self._modules(osvs, fake_bulk)):
            mod._graphrag_bulk_ingest_patched = False
            mod._patch_graphrag_bulk_ingest_retry()
            result = self._call(osvs)
            assert result == []
            assert calls["n"] == 2  # initial + 1 retry

    def test_non_retryable_error_raises_immediately(self, mod):
        def fake_bulk(client, requests, **kwargs):
            return (1, [{"index": {"id": "id-a", "status": 400, "error": {"type": "mapper_parsing_exception"}}}])

        osvs = self._make_client_module()
        with patch.dict(sys.modules, self._modules(osvs, fake_bulk)):
            mod._graphrag_bulk_ingest_patched = False
            mod._patch_graphrag_bulk_ingest_retry()
            with pytest.raises(RuntimeError, match="non-retryable"):
                self._call(osvs)

    def test_unidentifiable_throttled_item_raises(self, mod):
        # A retryable-status (429) error item with no _id/id can't be mapped
        # back to a pending doc — it must fail loud, not be silently dropped
        # from the retry set (which would lose that embedding).
        def fake_bulk(client, requests, **kwargs):
            return (
                0,
                [{"index": {"status": 429, "error": {"type": "circuit_breaking_exception"}}}],
            )

        osvs = self._make_client_module()
        with patch.dict(sys.modules, self._modules(osvs, fake_bulk)):
            mod._graphrag_bulk_ingest_patched = False
            mod._patch_graphrag_bulk_ingest_retry()
            with pytest.raises(RuntimeError, match="non-retryable"):
                self._call(osvs)

    def test_clean_bulk_returns_empty(self, mod):
        def fake_bulk(client, requests, **kwargs):
            return (len(requests), [])

        osvs = self._make_client_module()
        with patch.dict(sys.modules, self._modules(osvs, fake_bulk)):
            mod._graphrag_bulk_ingest_patched = False
            mod._patch_graphrag_bulk_ingest_retry()
            assert self._call(osvs) == []

    def test_patch_is_idempotent(self, mod):
        osvs = self._make_client_module()
        with patch.dict(sys.modules, self._modules(osvs, lambda *a, **k: (0, []))):
            mod._graphrag_bulk_ingest_patched = False
            mod._patch_graphrag_bulk_ingest_retry()
            first = osvs.OpensearchVectorClient._bulk_ingest_embeddings
            mod._patch_graphrag_bulk_ingest_retry()
            assert osvs.OpensearchVectorClient._bulk_ingest_embeddings is first

    def test_patch_noop_when_module_unavailable(self, mod):
        mod._graphrag_bulk_ingest_patched = False
        with patch.dict(sys.modules, {"llama_index.vector_stores.opensearch": None}):
            mod._patch_graphrag_bulk_ingest_retry()  # must not raise


# ---------------------------------------------------------------------------
# Extraction vocabulary + topics (env → ExtractionConfig)
# ---------------------------------------------------------------------------


class TestPreferredTopics:
    """PREFERRED_TOPICS is parsed at import and passed to ExtractionConfig."""

    def test_defaults_to_empty_list(self, mod):
        assert mod.PREFERRED_TOPICS == []

    def test_parses_json_array(self, monkeypatch, mod_factory):
        monkeypatch.setenv("PREFERRED_TOPICS", '["Black Tie Gala","Everyday Elegance"]')
        m = mod_factory()
        assert m.PREFERRED_TOPICS == ["Black Tie Gala", "Everyday Elegance"]

    def test_strips_and_drops_empty_entries(self, monkeypatch, mod_factory):
        monkeypatch.setenv("PREFERRED_TOPICS", '["  Black Tie Gala  ","", "   "]')
        m = mod_factory()
        assert m.PREFERRED_TOPICS == ["Black Tie Gala"]

    def test_malformed_json_raises_at_import(self, monkeypatch, mod_factory):
        # Fail loud: the API 400s bad vocab before ingest, so a malformed env var
        # here is a pipeline bug. Crashing (→ SCAN_FAILED) beats extracting with no
        # vocabulary and a warning nobody reads.
        monkeypatch.setenv("PREFERRED_TOPICS", "Black Tie Gala,Everyday Elegance")
        with pytest.raises(ValueError, match="not a JSON array of strings"):
            mod_factory()

    def test_non_string_members_raise_at_import(self, monkeypatch, mod_factory):
        monkeypatch.setenv("PREFERRED_TOPICS", '["ok", 42]')
        with pytest.raises(ValueError, match="not a JSON array of strings"):
            mod_factory()

    def test_passed_to_extraction_config(self, monkeypatch, mod_factory):
        monkeypatch.setenv("PREFERRED_TOPICS", '["Black Tie Gala"]')
        m = mod_factory()
        with (
            patch("graphrag_toolkit.lexical_graph.ExtractionConfig") as ec,
            patch("graphrag_toolkit.lexical_graph.IndexingConfig"),
        ):
            m._build_indexing_config("bucket", "tenant-a", "ds-001")
        assert ec.call_args.kwargs["preferred_topics"] == ["Black Tie Gala"]

    def test_empty_list_still_passed_explicitly(self, mod):
        """An empty list is forwarded, not omitted — the toolkit reads it as
        'no preferred topics', matching pre-existing behaviour."""
        with (
            patch("graphrag_toolkit.lexical_graph.ExtractionConfig") as ec,
            patch("graphrag_toolkit.lexical_graph.IndexingConfig"),
        ):
            mod._build_indexing_config("bucket", "tenant-a", "ds-001")
        assert ec.call_args.kwargs["preferred_topics"] == []


class TestEnvStrList:
    """The shared JSON-list env parser used by both vocabulary axes."""

    def test_valid_list(self, mod):
        assert mod._env_str_list("NOPE", '["a","b"]') == ["a", "b"]

    def test_empty_string_default(self, mod):
        assert mod._env_str_list("NOPE", "") == []

    def test_object_instead_of_array_raises(self, mod):
        with pytest.raises(ValueError, match="not a JSON array of strings"):
            mod._env_str_list("NOPE", '{"a":1}')

    def test_non_string_member_raises(self, mod):
        with pytest.raises(ValueError, match="not a JSON array of strings"):
            mod._env_str_list("NOPE", '["ok", 42]')

    def test_over_cap_still_truncates_not_raises(self, mod):
        # Over-cap is the one case that degrades: the first N entries are a valid
        # steer, so truncate with a warning rather than fail the ingest.
        import json as _json

        from coa_common.constants import MAX_VOCABULARY_ENTRIES

        big = _json.dumps([f"C{i}" for i in range(MAX_VOCABULARY_ENTRIES + 5)])
        out = mod._env_str_list("NOPE", big)
        assert len(out) == MAX_VOCABULARY_ENTRIES


@pytest.mark.unit
class TestEnvStrListCap:
    """The container clamps an over-long list even though the API already caps it.

    Defence in depth for an execution started outside the API — every entry is
    injected into the prompt for every chunk, so an unbounded list is a cost and
    adherence problem rather than merely untidy.
    """

    def test_over_cap_list_is_truncated_not_rejected(self, monkeypatch, mod_factory):
        import json as _json

        from coa_common.constants import MAX_VOCABULARY_ENTRIES

        too_many = [f"Class {i}" for i in range(MAX_VOCABULARY_ENTRIES + 25)]
        monkeypatch.setenv("PREFERRED_TOPICS", _json.dumps(too_many))
        m = mod_factory()
        # Truncated, not emptied: the first N entries are still a usable steer, and
        # failing the ingest over a too-long list would be worse than trimming it.
        assert len(m.PREFERRED_TOPICS) == MAX_VOCABULARY_ENTRIES
        assert m.PREFERRED_TOPICS[0] == "Class 0"

    def test_list_at_the_cap_is_untouched(self, monkeypatch, mod_factory):
        import json as _json

        from coa_common.constants import MAX_VOCABULARY_ENTRIES

        at_cap = [f"Class {i}" for i in range(MAX_VOCABULARY_ENTRIES)]
        monkeypatch.setenv("PREFERRED_ENTITY_CLASSIFICATIONS", _json.dumps(at_cap))
        m = mod_factory()
        assert len(m.PREFERRED_ENTITY_CLASSIFICATIONS) == MAX_VOCABULARY_ENTRIES
