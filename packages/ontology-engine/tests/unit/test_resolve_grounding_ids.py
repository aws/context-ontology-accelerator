# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared grounding-ID resolver.

``resolve_grounding_ontology_ids`` is used by BOTH induction pipelines
(structured ``table_to_ontology`` and unstructured ``lexical_graph``) to turn
the FE's grounding selections — which may be curated catalog KEYS
("fibo-agreements") or already-resolved URIs — into loaded-ontology URIs,
lazily loading+embedding any that aren't present yet. These tests pin the
branch behavior so the two pipelines ground identically.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from coa_ontology.induce_catalog import resolve_grounding_ontology_ids

pytestmark = pytest.mark.unit

_MODULE = "coa_ontology.induce_catalog"
_FND = "coa_ontology.catalog.routers.foundational"
_FIBO_URI = "https://spec.edmcouncil.org/fibo/ontology/FBC/"


def _patch(
    stack: ExitStack,
    *,
    by_key: dict | None = None,
    registry: list | None = None,
    load_side_effect=None,
):
    """Patch the resolver's collaborators: the curated catalog (_BY_KEY),
    the namespace registry lookup, and the SYNCHRONOUS foundational loader
    (``_load_foundational_and_wait``, which the resolver now calls instead of the
    async ``load_foundational_ontology`` endpoint so grounding waits for
    embeddings). Defaults to a successful (True) load."""
    loader = stack.enter_context(patch(f"{_FND}._load_foundational_and_wait"))
    loader.return_value = True if load_side_effect is None else None
    if load_side_effect is not None:
        loader.side_effect = load_side_effect
    stack.enter_context(patch(f"{_FND}._BY_KEY", by_key or {}))
    stack.enter_context(patch(f"{_MODULE}.dynamo_store.list_ontologies_registry", return_value=registry or []))
    return loader


class TestResolveGroundingIds:
    def test_curated_key_resolves_to_uri_and_lazy_loads(self):
        """A curated key not yet in the registry resolves to its URI AND
        triggers the foundational loader."""
        with ExitStack() as stack:
            loader = _patch(
                stack,
                by_key={"fibo-agreements": SimpleNamespace(uri=_FIBO_URI)},
                registry=[],  # nothing loaded yet
            )
            out = resolve_grounding_ontology_ids(grounding_keys=["fibo-agreements"], namespace="ns")
        assert out == [_FIBO_URI]
        # Now calls the SYNC loader with the resolved catalog entry + namespace,
        # and appends the URI only because it returned True (COMPLETED).
        loader.assert_called_once()
        _args, _kwargs = loader.call_args
        assert _args[0].uri == _FIBO_URI  # the FoundationalOntology entry
        assert _kwargs.get("namespace") == "ns"

    def test_already_loaded_key_is_not_reloaded(self):
        """A curated key already embedded in the registry resolves without
        calling the loader (idempotent)."""
        with ExitStack() as stack:
            loader = _patch(
                stack,
                by_key={"fibo-agreements": SimpleNamespace(uri=_FIBO_URI)},
                registry=[{"uri": _FIBO_URI, "embedding_count": 42}],
            )
            out = resolve_grounding_ontology_ids(grounding_keys=["fibo-agreements"], namespace="ns")
        assert out == [_FIBO_URI]
        loader.assert_not_called()

    def test_already_loaded_uri_passthrough_no_load(self):
        """A URI already embedded in the registry passes through untouched."""
        with ExitStack() as stack:
            loader = _patch(
                stack,
                by_key={},  # not a curated key
                registry=[{"uri": _FIBO_URI, "embedding_count": 5}],
            )
            out = resolve_grounding_ontology_ids(grounding_keys=[_FIBO_URI], namespace="ns")
        assert out == [_FIBO_URI]
        loader.assert_not_called()

    def test_registry_row_with_zero_embeddings_is_not_already_loaded(self):
        """A curated key whose registry row has 0 embeddings is treated as
        NOT loaded → the loader fires."""
        with ExitStack() as stack:
            loader = _patch(
                stack,
                by_key={"fibo-agreements": SimpleNamespace(uri=_FIBO_URI)},
                registry=[{"uri": _FIBO_URI, "embedding_count": 0}],
            )
            out = resolve_grounding_ontology_ids(grounding_keys=["fibo-agreements"], namespace="ns")
        assert out == [_FIBO_URI]
        loader.assert_called_once()

    def test_unresolvable_selection_is_skipped(self):
        """A selection that is neither a curated key nor a URI is skipped."""
        with ExitStack() as stack:
            loader = _patch(stack, by_key={}, registry=[])
            out = resolve_grounding_ontology_ids(grounding_keys=["not-a-key-or-uri"], namespace="ns")
        assert out == []
        loader.assert_not_called()

    def test_bare_uri_not_in_catalog_and_not_loaded_is_skipped(self):
        """A URI that isn't a curated entry AND isn't loaded can't be
        auto-fetched → skipped (no loader call, not returned)."""
        with ExitStack() as stack:
            loader = _patch(stack, by_key={}, registry=[])
            out = resolve_grounding_ontology_ids(grounding_keys=["https://example.org/custom-onto/"], namespace="ns")
        assert out == []
        loader.assert_not_called()

    def test_load_failure_is_fail_open(self):
        """A loader exception for one selection is swallowed; that selection
        is skipped but resolution continues for the rest."""
        with ExitStack() as stack:
            _patch(
                stack,
                by_key={
                    "bad": SimpleNamespace(uri="https://ex/bad/"),
                    "good": SimpleNamespace(uri="https://ex/good/"),
                },
                registry=[],
                # New contract: the sync loader returns True on COMPLETED. 'bad'
                # raises (fail-open → dropped); 'good' completes → appended.
                load_side_effect=[RuntimeError("load boom"), True],
            )
            out = resolve_grounding_ontology_ids(grounding_keys=["bad", "good"], namespace="ns")
        # 'bad' failed to load and is dropped; 'good' resolves.
        assert out == ["https://ex/good/"]

    def test_incomplete_load_is_not_appended(self):
        """If the SYNC loader returns False (load did not COMPLETE — e.g.
        embeddings not searchable), the URI must NOT be appended to the grounding
        pool — otherwise grounding recall runs against an empty index (silent
        0-hit grounding)."""
        with ExitStack() as stack:
            _patch(
                stack,
                by_key={"fibo-agreements": SimpleNamespace(uri=_FIBO_URI)},
                registry=[],
                load_side_effect=[False],  # load ran but did not complete
            )
            out = resolve_grounding_ontology_ids(grounding_keys=["fibo-agreements"], namespace="ns")
        assert out == []  # not appended — nothing to ground against yet

    def test_progress_callback_reports_loaded_and_total(self):
        """on_progress is called with (0, total) up front and after each
        resolved selection with the running loaded count."""
        calls: list[tuple[int, int]] = []
        with ExitStack() as stack:
            _patch(
                stack,
                by_key={"fibo-agreements": SimpleNamespace(uri=_FIBO_URI)},
                registry=[{"uri": _FIBO_URI, "embedding_count": 1}],
            )
            resolve_grounding_ontology_ids(
                grounding_keys=["fibo-agreements"],
                namespace="ns",
                on_progress=lambda loaded, total: calls.append((loaded, total)),
            )
        assert calls[0] == (0, 1)  # initial
        assert calls[-1] == (1, 1)  # after resolving the one selection

    def test_empty_selection_returns_empty(self):
        with ExitStack() as stack:
            loader = _patch(stack, by_key={}, registry=[])
            out = resolve_grounding_ontology_ids(grounding_keys=[], namespace="ns")
        assert out == []
        loader.assert_not_called()
