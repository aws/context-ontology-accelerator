# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Steward curation must SUPERSEDE AI-generated annotations on re-accept.

Accepting a proposal ingests in append mode into a shared ontology graph, and
every graph write is additive. So the curate → re-induce → re-accept flow —
accept (AI text), curate, re-induce, accept again into the same ontology — left
BOTH generations of ``rdfs:comment`` and ``skos:altLabel`` live on the same
class, and the model had to pick one. These tests drive ``ingest_ontology``
twice against an rdflib-backed fake whose writes are additive exactly like
Neptune DB's ``INSERT DATA`` / GSP ``POST``, and assert on the resulting graph.

The rule under test is field-scoped: descriptions + alt-labels are
authored-wins; relationship axioms are retained; other subjects (co-merged
proposals) are untouched; a fresh (create-mode) ingest is unaffected.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.catalog.ingest import (
    SUPERSEDED_ALT_LABEL,
    SUPERSEDED_COMMENT,
    IngestMetadata,
    IngestStoreError,
    _supersede_incoming_annotations,
    _supersede_incoming_embeddings,
    ingest_ontology,
)
from coa_ontology.stores.na_graph import NeptuneAnalyticsGraphStore
from coa_ontology.stores.neptune_db_graph import _SUPERSEDE_CHUNK, NeptuneDBGraphStore
from rdflib import OWL, RDF, RDFS, Dataset, Graph, Literal, URIRef
from rdflib.namespace import SKOS

pytestmark = pytest.mark.unit

ONT = "http://example.org/bank#"
CUST = URIRef(ONT + "CustMstr")
ACCT = URIRef(ONT + "Account")
CUST_TYP = URIRef(ONT + "custMstr_custTypCd")
CUST_ACCT = URIRef(ONT + "account_custId")

_PREFIXES = """
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix ex:   <http://example.org/bank#> .
<http://example.org/bank#> a owl:Ontology .
"""

# First accept: the AI-generated generation.
AI_TTL = (
    _PREFIXES
    + """
ex:CustMstr a owl:Class ; rdfs:label "cust_mstr" ;
    rdfs:comment "AI: master table of customers." ;
    skos:altLabel "cust_master", "customer_dimension" .
ex:Account a owl:Class ; rdfs:label "account" ;
    rdfs:comment "AI: accounts." .
ex:custMstr_custTypCd a owl:DatatypeProperty ; rdfs:domain ex:CustMstr ; rdfs:range xsd:string ;
    rdfs:comment "AI: customer type code." ;
    skos:altLabel "cust type" .
ex:account_custId a owl:ObjectProperty ; rdfs:domain ex:Account ; rdfs:range ex:CustMstr ;
    rdfs:comment "AI-inferred FK to CustMstr." .
"""
)

# Second accept: re-induced after the steward curated CustMstr + cust_typ_cd.
# Account was NOT curated, so it carries the same AI text as before.
CURATED_TTL = (
    _PREFIXES
    + """
ex:CustMstr a owl:Class ; rdfs:label "cust_mstr" ;
    rdfs:comment "Steward: customer master. cust_typ_cd is the private-banking flag, NOT cust_seg_cd." ;
    skos:altLabel "Customer Master", "cliente activo", "banca privada" .
ex:Account a owl:Class ; rdfs:label "account" ;
    rdfs:comment "AI: accounts." .
ex:custMstr_custTypCd a owl:DatatypeProperty ; rdfs:domain ex:CustMstr ; rdfs:range xsd:string ;
    rdfs:comment "Steward: 'PRIV' = private banking." ;
    skos:altLabel "customer type" .
ex:account_custId a owl:ObjectProperty ; rdfs:domain ex:Account ; rdfs:range ex:CustMstr ;
    rdfs:comment "AI-inferred FK to CustMstr." .
"""
)

# A DIFFERENT proposal co-merged into the same ontology (its subjects are not in
# the curated payload, so supersession must leave them alone).
OTHER_TTL = (
    _PREFIXES
    + """
ex:Branch a owl:Class ; rdfs:label "branch" ;
    rdfs:comment "AI: bank branches." ;
    skos:altLabel "office" .
"""
)


class AdditiveGraphStore:
    """rdflib-backed GraphStore double with Neptune DB's write semantics.

    ``store_*`` and ``load_turtle`` only ever ADD triples (like ``INSERT DATA``
    and GSP ``POST``); nothing here removes a triple except
    :meth:`supersede_annotations`, which mirrors the NDB implementation's
    scoped ``DELETE WHERE`` over (subject, predicate) pairs.
    """

    def __init__(self) -> None:
        self.g = Graph()
        self.supersede_calls: list[dict] = []

    # -- metadata (unused by these tests beyond existing) -----------------
    def create_ontology(self, data):
        return data

    def get_ontology_by_uri(self, uri):
        return {"uri": uri}

    def update_ontology(self, uri, updates):
        return {"uri": uri, **updates}

    def delete_ontology(self, uri):
        self.g = Graph()
        return True

    # -- additive projection writes ---------------------------------------
    def store_class(self, ontology_uri, class_uri, labels=None, comments=None, super_classes=None, is_mapped=False):
        c = URIRef(class_uri)
        self.g.add((c, RDF.type, OWL.Class))
        for cmt in comments or []:
            self.g.add((c, RDFS.comment, Literal(cmt)))
        return {"uri": class_uri}

    def store_property(self, ontology_uri, prop_uri, label="", comment="", domains=None, ranges=None):
        p = URIRef(prop_uri)
        if comment:
            self.g.add((p, RDFS.comment, Literal(comment)))
        return {"uri": prop_uri}

    def load_turtle(self, ontology_uri, turtle, graph_uri=None):
        self.g.parse(data=turtle, format="turtle")
        return {"status": "ok", "graph_uri": ontology_uri, "bytes_loaded": len(turtle)}

    # -- the supersession primitive under test ----------------------------
    def supersede_annotations(self, ontology_uri, subject_uris, predicates):
        self.supersede_calls.append({"subjects": list(subject_uris), "predicates": dict(predicates)})
        removed = 0
        for s in subject_uris:
            for p, hist in predicates.items():
                for triple in list(self.g.triples((URIRef(s), URIRef(p), None))):
                    self.g.remove(triple)
                    if hist:
                        self.g.add((triple[0], URIRef(hist), triple[2]))
                    removed += 1
        return removed


class BrokenSupersedeStore(AdditiveGraphStore):
    def supersede_annotations(self, ontology_uri, subject_uris, predicates):
        raise RuntimeError("neptune down")


class RecordingVectorStore:
    """Vector-store double: records entity deletes, nothing else (embeddings are patched out)."""

    def __init__(self) -> None:
        self.deleted: list[tuple[list[str], str, str | None]] = []

    def delete_embeddings_for_entities(self, entity_uris, ontology_id, namespace=None):
        uris = list(entity_uris)
        self.deleted.append((uris, ontology_id, namespace))
        return len(uris)


class BrokenVectorStore(RecordingVectorStore):
    def delete_embeddings_for_entities(self, entity_uris, ontology_id, namespace=None):
        raise RuntimeError("aoss down")


def _ingest(store, ttl, *, existing_row, allow_append=True, vector_store=None):
    with (
        patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
        patch(
            "coa_ontology.catalog.ingest._accumulate_embeddings",
            return_value={"status": "ok", "count": 0, "model_id": "t", "index": "t"},
        ),
    ):
        ds.get_ontology_registry.return_value = existing_row
        ds.put_namespace_meta.return_value = None
        ds.extend_ontology_registry.return_value = {"uri": ONT}
        ds.put_ontology_registry.return_value = {"uri": ONT}
        return ingest_ontology(
            store,
            vector_store if vector_store is not None else MagicMock(),
            ttl,
            namespace="ns-1118",
            metadata=IngestMetadata(ontology_id=ONT, format="turtle", source="unit-test"),
            validate=False,
            allow_append=allow_append,
        )


_LIVE_ROW = {"uri": ONT, "status": "active"}


def _values(g, s, p):
    return sorted(str(o) for o in g.objects(s, p))


class TestReAcceptSupersedesAnnotations:
    def test_first_accept_then_curated_reaccept_leaves_only_authored_text(self):
        store = AdditiveGraphStore()
        _ingest(store, AI_TTL, existing_row=None)  # create
        # Precondition: AI generation is live.
        assert _values(store.g, CUST, RDFS.comment) == ["AI: master table of customers."]

        result = _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW)  # append

        # The defect: two rdfs:comment + 5 mixed altLabels. Now: authored only.
        assert _values(store.g, CUST, RDFS.comment) == [
            "Steward: customer master. cust_typ_cd is the private-banking flag, NOT cust_seg_cd."
        ]
        assert _values(store.g, CUST, SKOS.altLabel) == ["Customer Master", "banca privada", "cliente activo"]
        # Column-level property: same rule.
        assert _values(store.g, CUST_TYP, RDFS.comment) == ["Steward: 'PRIV' = private banking."]
        assert _values(store.g, CUST_TYP, SKOS.altLabel) == ["customer type"]
        assert result["appended"] is True
        # Exact count so the assertion catches an off-by-one supersede: 3
        # comments (CUST, CUST_TYP, ACCT) + 4 altLabels — the count depends on
        # which live triples the AI ingest actually wrote. `> 0` used to be
        # `1` under a `MagicMock()` vector store (``int(MagicMock()) == 1``),
        # which would have hidden a real regression from an integer floor.
        assert result["superseded_annotation_count"] == 7

    def test_relationship_axioms_are_retained_across_reaccept(self):
        # Supersede-with-fallback, not supersede-and-discard: the inferred FK
        # edge (the join path the benchmark showed WINNING) must survive.
        store = AdditiveGraphStore()
        _ingest(store, AI_TTL, existing_row=None)
        _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW)
        assert (CUST_ACCT, RDF.type, OWL.ObjectProperty) in store.g
        assert (CUST_ACCT, RDFS.range, CUST) in store.g
        assert (CUST_ACCT, RDFS.domain, ACCT) in store.g
        assert (CUST, RDF.type, OWL.Class) in store.g

    def test_uncurated_subject_in_same_payload_keeps_single_unchanged_comment(self):
        # Account carried identical AI text both times: superseding then
        # re-inserting must yield exactly one comment, not zero and not two.
        store = AdditiveGraphStore()
        _ingest(store, AI_TTL, existing_row=None)
        _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW)
        assert _values(store.g, ACCT, RDFS.comment) == ["AI: accounts."]

    def test_co_merged_proposal_subjects_are_untouched(self):
        # Append mode is shared by multiple proposals merged into one ontology.
        # Superseding must be scoped to the INCOMING subjects, never graph-wide.
        store = AdditiveGraphStore()
        _ingest(store, AI_TTL, existing_row=None)
        _ingest(store, OTHER_TTL, existing_row=_LIVE_ROW)
        branch = URIRef(ONT + "Branch")
        assert _values(store.g, branch, RDFS.comment) == ["AI: bank branches."]

        _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW)

        assert _values(store.g, branch, RDFS.comment) == ["AI: bank branches."]
        assert _values(store.g, branch, SKOS.altLabel) == ["office"]
        # And the supersede call never named Branch.
        last = store.supersede_calls[-1]
        assert str(branch) not in last["subjects"]
        assert str(CUST) in last["subjects"] and str(CUST_TYP) in last["subjects"]

    def test_supersede_predicates_are_annotation_only(self):
        store = AdditiveGraphStore()
        _ingest(store, AI_TTL, existing_row=None)
        _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW)
        preds = store.supersede_calls[-1]["predicates"]
        assert str(RDFS.comment) in preds and str(SKOS.altLabel) in preds and str(SKOS.definition) in preds
        for structural in (RDFS.range, RDFS.domain, RDFS.subClassOf, RDF.type, RDFS.label):
            assert str(structural) not in preds
        # Every live predicate maps to a coa:superseded* history predicate.
        assert preds[str(RDFS.comment)] == str(SUPERSEDED_COMMENT)
        assert preds[str(SKOS.altLabel)] == str(SUPERSEDED_ALT_LABEL)

    def test_displaced_ai_text_is_kept_as_history_not_live(self):
        # Supersede, don't discard: the AI generation moves to the history
        # predicate where it is inspectable but no longer live content.
        store = AdditiveGraphStore()
        _ingest(store, AI_TTL, existing_row=None)
        _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW)
        assert _values(store.g, CUST, SUPERSEDED_COMMENT) == ["AI: master table of customers."]
        assert _values(store.g, CUST, SUPERSEDED_ALT_LABEL) == ["cust_master", "customer_dimension"]
        # ...and none of it is still under the live predicates.
        assert "AI: master table of customers." not in _values(store.g, CUST, RDFS.comment)
        assert not set(_values(store.g, CUST, SKOS.altLabel)) & {"cust_master", "customer_dimension"}

    def test_create_mode_never_supersedes(self):
        store = AdditiveGraphStore()
        result = _ingest(store, AI_TTL, existing_row=None)
        assert store.supersede_calls == []
        assert result["superseded_annotation_count"] == 0

    def test_supersession_failure_aborts_append_instead_of_falling_back(self):
        # Quietly continuing additive would silently reintroduce the defect.
        store = BrokenSupersedeStore()
        _ingest(store, AI_TTL, existing_row=None)
        with pytest.raises(IngestStoreError, match="annotation supersession failed"):
            _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW)
        # Nothing was written by the failed append: the AI generation is still
        # the only one live (no half-applied state).
        assert _values(store.g, CUST, RDFS.comment) == ["AI: master table of customers."]

    def test_backend_without_the_method_is_treated_as_nothing_to_supersede(self):
        class Legacy:
            pass

        assert _supersede_incoming_annotations(Legacy(), ONT, [CUST]) == 0  # type: ignore[arg-type]


class TestNeptuneAnalyticsBackend:
    def test_na_supersede_is_a_noop_returning_zero(self):
        # NA's store_class/store_property SET the node comment (overwrite), so
        # there is nothing to supersede; the method exists for protocol parity.
        store = NeptuneAnalyticsGraphStore.__new__(NeptuneAnalyticsGraphStore)
        assert store.supersede_annotations(ONT, [str(CUST)], {str(RDFS.comment): None}) == 0


class TestNeptuneDBSupersedeSparql:
    """Shape of the SPARQL the NDB backend emits — scoped by VALUES on s AND p."""

    def _store(self):

        s = NeptuneDBGraphStore.__new__(NeptuneDBGraphStore)
        s._graph_for = lambda uri: "http://g/" + uri.rsplit("/", 1)[-1]  # type: ignore[method-assign]
        return s

    def test_counts_then_deletes_only_named_pairs(self):
        store = self._store()
        with (
            patch(
                "coa_ontology.stores.neptune_db_graph._sparql_query",
                return_value={"results": {"bindings": [{"n": {"value": "3"}}]}},
            ) as q,
            patch("coa_ontology.stores.neptune_db_graph._sparql_update") as u,
        ):
            removed = store.supersede_annotations(
                ONT,
                [str(CUST), str(CUST_TYP)],
                {str(RDFS.comment): str(SUPERSEDED_COMMENT), str(SKOS.altLabel): str(SUPERSEDED_ALT_LABEL)},
            )
        assert removed == 3
        updates = [c.args[0] for c in u.call_args_list]
        # Move, not drop: one DELETE/INSERT per (live, history) pair with the
        # predicates inlined (same shape as update_ontology), subjects scoped
        # via VALUES ?s.
        assert len(updates) == 2
        assert any(
            f"DELETE {{ ?s <{RDFS.comment}> ?o }} INSERT {{ ?s <{SUPERSEDED_COMMENT}> ?o }}" in x for x in updates
        )
        assert any(
            f"DELETE {{ ?s <{SKOS.altLabel}> ?o }} INSERT {{ ?s <{SUPERSEDED_ALT_LABEL}> ?o }}" in x for x in updates
        )
        for x in updates:
            assert f"VALUES ?s {{ <{CUST}> <{CUST_TYP}> }}" in x
            assert "WITH <http://g/" in x
            assert "VALUES (?p ?h)" not in x
        # Count query targets the same graph + pattern.
        assert "COUNT(*)" in q.call_args.args[0]

    def test_zero_matches_skips_the_update(self):
        store = self._store()
        with (
            patch(
                "coa_ontology.stores.neptune_db_graph._sparql_query",
                return_value={"results": {"bindings": [{"n": {"value": "0"}}]}},
            ),
            patch("coa_ontology.stores.neptune_db_graph._sparql_update") as u,
        ):
            assert store.supersede_annotations(ONT, [str(CUST)], {str(RDFS.comment): None}) == 0
        u.assert_not_called()

    def test_none_history_drops_outright(self):
        store = self._store()
        with (
            patch(
                "coa_ontology.stores.neptune_db_graph._sparql_query",
                return_value={"results": {"bindings": [{"n": {"value": "1"}}]}},
            ),
            patch("coa_ontology.stores.neptune_db_graph._sparql_update") as u,
        ):
            store.supersede_annotations(ONT, [str(CUST)], {str(RDFS.comment): None})
        update = u.call_args.args[0]
        assert "INSERT" not in update and "DELETE { ?s ?p ?o }" in update

    def test_empty_inputs_do_no_io(self):
        store = self._store()
        with (
            patch("coa_ontology.stores.neptune_db_graph._sparql_query") as q,
            patch("coa_ontology.stores.neptune_db_graph._sparql_update") as u,
        ):
            assert store.supersede_annotations(ONT, [], {str(RDFS.comment): None}) == 0
            assert store.supersede_annotations(ONT, [str(CUST)], {}) == 0
        q.assert_not_called()
        u.assert_not_called()

    def test_unsafe_subject_iris_are_dropped_not_interpolated(self):
        # _iri() rejects injection-shaped URIs; the supersede path filters them
        # out up front rather than letting one abort the whole append.
        store = self._store()
        with (
            patch(
                "coa_ontology.stores.neptune_db_graph._sparql_query",
                return_value={"results": {"bindings": [{"n": {"value": "1"}}]}},
            ),
            patch("coa_ontology.stores.neptune_db_graph._sparql_update") as u,
        ):
            store.supersede_annotations(ONT, ["http://x> ?s ?p ?o } #", str(CUST)], {str(RDFS.comment): None})
        assert "?s ?p ?o } #" not in u.call_args.args[0]
        assert f"<{CUST}>" in u.call_args.args[0]

    def test_emitted_sparql_produces_the_intended_effect_end_to_end(self):
        # Shape assertions above document what the emitted SPARQL should
        # LOOK like; this one runs the actual emitted SPARQL against an
        # in-memory rdflib Dataset and asserts on the RESULTING QUADS.
        # A SPARQL string that parses and coincidentally contains the
        # expected substrings but does the wrong thing (moves the wrong
        # predicate, forgets the VALUES clause, targets the wrong graph)
        # would pass the shape assertions and fail here.

        # Every subject in this test maps to the same named graph — that's
        # what NDB does for a single ontology (`_graph_for` is per-ontology,
        # not per-subject) and matches the store setup below.
        graph_uri = URIRef("http://g/onto1")

        def _seed() -> Dataset:
            ds = Dataset()
            g = ds.graph(graph_uri)
            # Targets: comments + altLabels on the two named subjects.
            g.add((CUST, RDFS.comment, Literal("AI comment on CUST")))
            g.add((CUST, SKOS.altLabel, Literal("cust1")))
            g.add((CUST, SKOS.altLabel, Literal("cust2")))
            g.add((CUST_TYP, RDFS.comment, Literal("AI comment on CUST_TYP")))
            # Non-targets that must not move:
            # (a) untouched predicate on a targeted subject:
            g.add((CUST, RDFS.label, Literal("Customer")))
            g.add((CUST, RDF.type, OWL.Class))
            # (b) targeted predicate on an untouched subject:
            g.add((ACCT, RDFS.comment, Literal("AI comment on ACCT — do not move")))
            # (c) same triple in a DIFFERENT named graph — the WITH clause
            # must scope the update to graph_uri only:
            other_graph = ds.graph(URIRef("http://g/other-ontology"))
            other_graph.add((CUST, RDFS.comment, Literal("sibling ontology — must survive")))
            return ds

        ds = _seed()

        store = NeptuneDBGraphStore.__new__(NeptuneDBGraphStore)
        store._graph_for = lambda _uri: str(graph_uri)  # type: ignore[method-assign]

        def _fake_query(sparql, **_kw):
            # Force the update path so the emitted SPARQL is exercised.
            return {"results": {"bindings": [{"n": {"value": "99"}}]}}

        def _fake_update(sparql, **_kw):
            ds.update(sparql)

        with (
            patch("coa_ontology.stores.neptune_db_graph._sparql_query", side_effect=_fake_query),
            patch("coa_ontology.stores.neptune_db_graph._sparql_update", side_effect=_fake_update),
        ):
            store.supersede_annotations(
                ONT,
                [str(CUST), str(CUST_TYP)],
                {
                    str(RDFS.comment): str(SUPERSEDED_COMMENT),
                    str(SKOS.altLabel): str(SUPERSEDED_ALT_LABEL),
                },
            )

        g = ds.graph(graph_uri)
        superseded_comment = URIRef(SUPERSEDED_COMMENT)
        superseded_altlabel = URIRef(SUPERSEDED_ALT_LABEL)

        # (1) The live values are gone.
        assert (CUST, RDFS.comment, Literal("AI comment on CUST")) not in g
        assert (CUST, SKOS.altLabel, Literal("cust1")) not in g
        assert (CUST, SKOS.altLabel, Literal("cust2")) not in g
        assert (CUST_TYP, RDFS.comment, Literal("AI comment on CUST_TYP")) not in g

        # (2) The displaced values are attached under the HISTORY predicate.
        assert (CUST, superseded_comment, Literal("AI comment on CUST")) in g
        assert (CUST, superseded_altlabel, Literal("cust1")) in g
        assert (CUST, superseded_altlabel, Literal("cust2")) in g
        assert (CUST_TYP, superseded_comment, Literal("AI comment on CUST_TYP")) in g

        # (3) Untouched predicates on the same subjects survive.
        assert (CUST, RDFS.label, Literal("Customer")) in g
        assert (CUST, RDF.type, OWL.Class) in g

        # (4) Same predicate on an untargeted subject survives — VALUES ?s
        # correctly scoped the update.
        assert (ACCT, RDFS.comment, Literal("AI comment on ACCT — do not move")) in g

        # (5) The other named graph is completely untouched — WITH correctly
        # scoped the update to the intended graph.
        other = ds.graph(URIRef("http://g/other-ontology"))
        assert (CUST, RDFS.comment, Literal("sibling ontology — must survive")) in other
        assert (CUST, superseded_comment, Literal("sibling ontology — must survive")) not in other


class TestReAcceptSupersedesEmbeddings:
    """Stale-embedding follow-up: the stale embedding of a superseded description must not
    survive in the namespace index beside the new one."""

    def test_append_retires_embeddings_of_exactly_the_incoming_subjects(self):
        store = AdditiveGraphStore()
        vs = RecordingVectorStore()
        _ingest(store, AI_TTL, existing_row=None, vector_store=vs)
        assert vs.deleted == []  # create mode: nothing stale to retire
        result = _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW, vector_store=vs)
        assert len(vs.deleted) == 1
        uris, oid, ns = vs.deleted[0]
        assert ns == "ns-1118"
        assert oid == ONT
        # Classes AND properties of the incoming payload — and only those.
        assert set(uris) == {str(CUST), str(ACCT), str(CUST_TYP), str(CUST_ACCT)}
        assert result["superseded_embedding_count"] == 4

    def test_co_merged_proposal_embeddings_untouched(self):
        store = AdditiveGraphStore()
        vs = RecordingVectorStore()
        _ingest(store, AI_TTL, existing_row=None, vector_store=vs)
        _ingest(store, OTHER_TTL, existing_row=_LIVE_ROW, vector_store=vs)
        _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW, vector_store=vs)
        # The curated append (last call) names its own subjects only — never
        # Branch, which belongs to the co-merged proposal.
        uris, _oid, _ns = vs.deleted[-1]
        assert ONT + "Branch" not in uris
        assert str(CUST) in uris

    def test_embedding_retire_is_the_first_write_of_an_append(self):
        # Order is load-bearing twice over: retiring embeddings AFTER re-embedding
        # would wipe the new vectors; retiring them AFTER the graph supersession
        # (surfaced in code review) meant a vector-store failure left the graph
        # updated and the index stale — the exact graph/index disagreement this
        # step exists to prevent. So: vector delete -> graph supersede -> turtle
        # load -> embed.
        store = AdditiveGraphStore()
        order: list[str] = []
        vs = RecordingVectorStore()
        orig_delete = vs.delete_embeddings_for_entities
        orig_supersede = store.supersede_annotations
        orig_load = store.load_turtle

        def _delete(uris, ontology_id, namespace=None):
            order.append("delete_embeddings")
            return orig_delete(uris, ontology_id, namespace=namespace)

        def _supersede(*a, **kw):
            order.append("supersede_graph")
            return orig_supersede(*a, **kw)

        def _load(*a, **kw):
            order.append("load_turtle")
            return orig_load(*a, **kw)

        vs.delete_embeddings_for_entities = _delete  # type: ignore[method-assign]
        store.supersede_annotations = _supersede  # type: ignore[method-assign]
        store.load_turtle = _load  # type: ignore[method-assign]
        _ingest(store, AI_TTL, existing_row=None, vector_store=vs)
        order.clear()  # only the append matters
        with (
            patch("coa_ontology.catalog.ingest.dynamo_store") as ds,
            patch(
                "coa_ontology.catalog.ingest._accumulate_embeddings",
                side_effect=lambda **kw: (
                    order.append("embed"),
                    {"status": "ok", "count": 0, "model_id": "t", "index": "t"},
                )[1],
            ),
        ):
            ds.get_ontology_registry.return_value = _LIVE_ROW
            ds.extend_ontology_registry.return_value = {"uri": ONT}
            ingest_ontology(
                store,
                vs,
                CURATED_TTL,
                namespace="ns-1118",
                metadata=IngestMetadata(ontology_id=ONT, format="turtle", source="unit-test"),
                validate=False,
                allow_append=True,
            )
        assert order == ["delete_embeddings", "supersede_graph", "load_turtle", "embed"]

    def test_vector_backend_failure_aborts_before_any_graph_write(self):
        store = AdditiveGraphStore()
        _ingest(store, AI_TTL, existing_row=None)
        before = set(store.g)
        with pytest.raises(IngestStoreError, match="embedding supersession failed"):
            _ingest(store, CURATED_TTL, existing_row=_LIVE_ROW, vector_store=BrokenVectorStore())
        # The graph is byte-for-byte what it was: AI text still live, nothing
        # superseded, nothing loaded — a retry can start clean, and there is no
        # window where the graph says "steward" while the index says "AI".
        assert set(store.g) == before
        assert store.supersede_calls == []

    def test_vector_backend_without_method_is_nothing_to_retire(self):
        class Legacy:
            pass

        assert _supersede_incoming_embeddings(Legacy(), "ns", ONT, [CUST]) == 0  # type: ignore[arg-type]


class TestNeptuneDBSupersedeChunking:
    """Chunk boundaries (200/201/400/401) — every subject in exactly
    one VALUES ?s block per predicate, none skipped, none repeated."""

    @staticmethod
    def _store():

        s = NeptuneDBGraphStore.__new__(NeptuneDBGraphStore)
        s._graph_for = lambda uri: "http://g/x"  # type: ignore[method-assign]
        return s

    @pytest.mark.parametrize("n_subjects", [1, 199, 200, 201, 400, 401])
    def test_every_subject_superseded_exactly_once_per_predicate(self, n_subjects):

        store = self._store()
        subjects = [f"http://ex/S{i}" for i in range(n_subjects)]
        preds = {str(RDFS.comment): str(SUPERSEDED_COMMENT), str(SKOS.altLabel): str(SUPERSEDED_ALT_LABEL)}
        with (
            patch(
                "coa_ontology.stores.neptune_db_graph._sparql_query",
                return_value={"results": {"bindings": [{"n": {"value": "1"}}]}},
            ),
            patch("coa_ontology.stores.neptune_db_graph._sparql_update") as u,
        ):
            store.supersede_annotations(ONT, subjects, preds)
        updates = [c.args[0] for c in u.call_args_list]
        expected_chunks = -(-n_subjects // _SUPERSEDE_CHUNK)  # ceil
        assert len(updates) == expected_chunks * len(preds)
        # Per predicate, the union of VALUES ?s blocks is exactly the subject set,
        # and no subject appears twice.
        for live in preds:
            seen: list[str] = []
            for upd in updates:
                if f"DELETE {{ ?s <{live}> ?o }}" not in upd:
                    continue
                block = re.search(r"VALUES \?s \{ (.*?) \}", upd).group(1)  # type: ignore[union-attr]
                seen.extend(re.findall(r"<([^>]+)>", block))
            assert len(seen) == n_subjects, (live, len(seen))
            assert set(seen) == set(subjects)
            assert all(
                len(re.findall(r"<([^>]+)>", re.search(r"VALUES \?s \{ (.*?) \}", upd).group(1))) <= _SUPERSEDE_CHUNK
                for upd in updates
            )  # type: ignore[union-attr]

    def test_failure_names_chunk_and_predicate(self):
        store = self._store()
        with (
            patch(
                "coa_ontology.stores.neptune_db_graph._sparql_query",
                return_value={"results": {"bindings": [{"n": {"value": "1"}}]}},
            ),
            patch("coa_ontology.stores.neptune_db_graph._sparql_update", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match=r"chunk 0, predicate .*comment -> .*supersededComment"),
        ):
            store.supersede_annotations(ONT, [str(CUST)], {str(RDFS.comment): str(SUPERSEDED_COMMENT)})
