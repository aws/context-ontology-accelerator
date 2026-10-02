# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A column carrying several foreign keys emits EVERY allowed relationship — in every artifact.

0.3.3 feedback (#1088 follow-up, "the graph stays disconnected"): induction took
the FIRST ``FOREIGN_KEY`` constraint on a column and ignored the rest, in the
ontology AND the R2RML mapping. Local FKs are stored first, so the local one always
won and every cross-source relationship on the same column — including the ones a
steward had just approved — was silently discarded. The review gate also ran after
that first-wins pick, so a PENDING relationship sorting first demoted the column
and took an APPROVED one down with it; and the mapping had no gate at all, so a
pending relationship could become a live Ontop join.

These tests drive the three artifacts (ontology, R2RML, SHACL) from one fixture and
assert the INVARIANT the previous drift broke: the set of relationship properties
the ontology declares == the set of predicates the mapping joins on == the set of
properties the shape asserts ``sh:class`` on. Plus the per-case expectations.
"""

from __future__ import annotations

import pytest
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogConstraint, CatalogTable
from coa_ontology.inducer.strategies.base import RR, fk_property_local_name, simple_fk_constraints
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from coa_ontology.validation.shapes.config import ConstraintType, generate_config_from_db
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

pytestmark = pytest.mark.unit

PREFIX = "http://example.org/base/"
NS = Namespace(PREFIX)


def _fk(col: str, target: str, *, source: str | None = None, status: str | None = None, ds: str | None = None):
    return CatalogConstraint(
        constraintType="FOREIGN_KEY",
        columns=[col],
        referredColumns=[f"{target}.id"],
        relationshipType=source,
        reviewStatus=status,
        targetDatasourceId=ds,
    )


def _table(name: str, ds: str, cols: list[str], constraints: list[CatalogConstraint] | None = None) -> CatalogTable:
    return CatalogTable(
        id=f"{ds}:{name}",
        name=name,
        fullyQualifiedName=f"db_{ds}.{name}",
        datasourceId=ds,
        columns=[CatalogColumn(name=c, dataType="INT") for c in cols],
        tableConstraints=constraints or [],
    )


def _invoice(fks: list[CatalogConstraint]) -> list[CatalogTable]:
    """billing.invoice.ba_no with the given FKs; targets in two other sources."""
    return [
        _table("invoice", "1", ["id", "ba_no"], fks),
        _table("billing_account", "1", ["id"]),
        _table("account_xref", "2", ["id"]),
        _table("mart_customer_360", "3", ["id"]),
    ]


def _artifacts(tables: list[CatalogTable]):
    strategy = TableToOntologyStrategy()
    onto, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
    r2rml = strategy.build_r2rml(PREFIX, tables, novel, onto)
    shacl = generate_config_from_db(tables, PREFIX)
    return onto, r2rml, shacl


def _onto_object_props(onto: Graph, domain: URIRef) -> dict[str, str]:
    """{property IRI: range IRI} for the object properties whose domain is ``domain``."""
    return {
        str(p): str(onto.value(p, RDFS.range))
        for p in onto.subjects(RDF.type, OWL.ObjectProperty)
        if (p, RDFS.domain, domain) in onto
    }


def _r2rml_join_predicates(r2rml: Graph, tmap: URIRef) -> dict[str, str]:
    """{predicate IRI: parent TriplesMap IRI} for the referencing object maps of ``tmap``."""
    out: dict[str, str] = {}
    for pom in r2rml.objects(tmap, RR.predicateObjectMap):
        om = r2rml.value(pom, RR.objectMap)
        parent = r2rml.value(om, RR.parentTriplesMap)
        if parent is not None:
            out[str(r2rml.value(pom, RR.predicate))] = str(parent)
    return out


def _shacl_reference_props(shacl, class_uri: str) -> dict[str, str]:
    """{property path: target class} for REFERENCE constraints on ``class_uri``."""
    for cls in shacl.classes:
        if cls.class_uri == class_uri:
            return {
                c.property_path: c.params["target_class"]
                for c in cls.constraints
                if c.constraint_type == ConstraintType.REFERENCE
            }
    return {}


def _assert_three_way_invariant(tables: list[CatalogTable], referrer: str, expected: dict[str, str]) -> None:
    """The ontology's, the mapping's and the shape's relationship-property sets agree, and equal ``expected``.

    ``expected`` maps property local name -> target class local name.
    """
    onto, r2rml, shacl = _artifacts(tables)
    pascal = {"invoice": "Invoice"}
    domain = NS[pascal[referrer]]
    onto_props = _onto_object_props(onto, domain)
    r2rml_props = _r2rml_join_predicates(r2rml, NS[f"TriplesMap_{pascal[referrer]}"])
    shacl_props = _shacl_reference_props(shacl, str(domain))

    want = {str(NS[k]): str(NS[v]) for k, v in expected.items()}
    assert onto_props == want, f"ontology: {onto_props} != {want}"
    assert set(r2rml_props) == set(want), f"r2rml predicates: {set(r2rml_props)} != {set(want)}"
    for pred, parent in r2rml_props.items():
        assert parent == f"{PREFIX}TriplesMap_{want[pred].rsplit('/', 1)[-1]}", (pred, parent)
    assert shacl_props == want, f"shacl: {shacl_props} != {want}"


class TestSingleForeignKeyIsUnchanged:
    def test_plain_property_name_and_one_join(self):
        # Every existing ontology/mapping/shape/embedding uses the plain name; a
        # single-FK column must not be renamed by this change.
        tables = _invoice([_fk("ba_no", "billing_account", source="DETERMINISTIC")])
        _assert_three_way_invariant(tables, "invoice", {"invoice_baNo": "BillingAccount"})


class TestMultipleForeignKeysOnOneColumn:
    def test_local_plus_one_cross_source_local_keeps_plain_name(self):
        # Naming stability: an existing local FK's IRI must not change when a
        # cross-source FK is later approved on the same column. The local FK
        # (targetDatasourceId=None) keeps the plain `{table}_{col}` name; the
        # cross-source additions are qualified.
        tables = _invoice(
            [
                _fk("ba_no", "billing_account", source="DETERMINISTIC"),
                _fk("ba_no", "account_xref", source="STEWARD_SPECIFIED", ds="2"),
            ]
        )
        _assert_three_way_invariant(
            tables,
            "invoice",
            {"invoice_baNo": "BillingAccount", "invoice_baNo__AccountXref": "AccountXref"},
        )

    def test_local_fk_plus_approved_cross_source_fks_all_reach_the_graph(self):
        # The reporter's exact shape: invoice.ba_no -> local billing_account
        # (deterministic) AND -> account_xref crosswalk AND -> mart_customer_360,
        # the latter two approved cross-source inferences. All three are correct
        # and all three must be edges — before, only the local one survived.
        # Local keeps plain name (stability); cross-sources are qualified.
        tables = _invoice(
            [
                _fk("ba_no", "billing_account", source="DETERMINISTIC"),
                _fk("ba_no", "account_xref", source="AI_INFERRED", status="APPROVED", ds="2"),
                _fk("ba_no", "mart_customer_360", source="AI_INFERRED", status="APPROVED", ds="3"),
            ]
        )
        _assert_three_way_invariant(
            tables,
            "invoice",
            {
                "invoice_baNo": "BillingAccount",
                "invoice_baNo__AccountXref": "AccountXref",
                "invoice_baNo__MartCustomer360": "MartCustomer360",
            },
        )

    def test_grandfathered_local_ai_fk_stored_first_does_not_shadow_approved_cross_source(self):
        # The reporter's EXACT shape. The within-source pass stores its inferred FK
        # with no review status (pre-#1088 contract: grandfathered, always emitted)
        # and it is stored FIRST; the cross-source pass appends approved FKs on the
        # same column afterwards. First-wins always picked the local one.
        # Local (ds=None) keeps plain name; cross-sources are qualified.
        tables = _invoice(
            [
                _fk("ba_no", "billing_account", source="AI_INFERRED"),  # status None -> grandfathered
                _fk("ba_no", "account_xref", source="AI_INFERRED", status="APPROVED", ds="2"),
                _fk("ba_no", "mart_customer_360", source="AI_INFERRED", status="APPROVED", ds="3"),
            ]
        )
        _assert_three_way_invariant(
            tables,
            "invoice",
            {
                "invoice_baNo": "BillingAccount",
                "invoice_baNo__AccountXref": "AccountXref",
                "invoice_baNo__MartCustomer360": "MartCustomer360",
            },
        )

    def test_two_cross_source_fks_both_qualified(self):
        # No local FK on the column (both have targetDatasourceId set), so
        # BOTH get qualified names — no "plain-name winner" is possible.
        tables = _invoice(
            [
                _fk("ba_no", "account_xref", source="AI_INFERRED", status="APPROVED", ds="2"),
                _fk("ba_no", "mart_customer_360", source="AI_INFERRED", status="APPROVED", ds="3"),
            ]
        )
        _assert_three_way_invariant(
            tables,
            "invoice",
            {"invoice_baNo__AccountXref": "AccountXref", "invoice_baNo__MartCustomer360": "MartCustomer360"},
        )

    def test_column_level_axioms_apply_to_every_minted_property(self):
        # Column-level axioms (rdfs:label = column name) apply to every property
        # minted from the column, whether plain or qualified.
        tables = _invoice(
            [
                _fk("ba_no", "billing_account", source="DETERMINISTIC"),
                _fk("ba_no", "account_xref", source="STEWARD_SPECIFIED", ds="2"),
            ]
        )
        onto, _, _ = _artifacts(tables)
        for local in ("invoice_baNo", "invoice_baNo__AccountXref"):
            assert str(onto.value(NS[local], RDFS.label)) == "ba_no"


class TestReviewGateRunsBeforeSelection:
    def test_pending_fk_listed_first_no_longer_hides_the_approved_one(self):
        # Bug 3: first-wins picked the PENDING constraint, the gate demoted the
        # column, and the APPROVED relationship behind it was lost.
        tables = _invoice(
            [
                _fk("ba_no", "account_xref", source="AI_INFERRED", status="PENDING_REVIEW", ds="2"),
                _fk("ba_no", "billing_account", source="AI_INFERRED", status="APPROVED"),
            ]
        )
        # One survivor -> plain name (qualification only when several are emitted).
        _assert_three_way_invariant(tables, "invoice", {"invoice_baNo": "BillingAccount"})

    def test_pending_only_is_a_datatype_property_in_all_three_artifacts(self):
        # Bug 4 for the mapping: a PENDING relationship used to become a live
        # Ontop join while the ontology withheld its edge.
        tables = _invoice([_fk("ba_no", "account_xref", source="AI_INFERRED", status="PENDING_REVIEW", ds="2")])
        onto, r2rml, shacl = _artifacts(tables)
        assert _onto_object_props(onto, NS.Invoice) == {}
        assert (NS.invoice_baNo, RDF.type, OWL.DatatypeProperty) in onto
        assert _r2rml_join_predicates(r2rml, NS.TriplesMap_Invoice) == {}
        # The column still has a (datatype) map so the property resolves to data.
        poms = list(r2rml.objects(NS.TriplesMap_Invoice, RR.predicateObjectMap))
        assert any(str(r2rml.value(p, RR.predicate)) == str(NS.invoice_baNo) for p in poms)
        assert _shacl_reference_props(shacl, str(NS.Invoice)) == {}

    def test_rejected_fk_is_dropped_but_approved_sibling_kept(self):
        tables = _invoice(
            [
                _fk("ba_no", "account_xref", source="AI_INFERRED", status="REJECTED", ds="2"),
                _fk("ba_no", "mart_customer_360", source="AI_INFERRED", status="APPROVED", ds="3"),
            ]
        )
        _assert_three_way_invariant(tables, "invoice", {"invoice_baNo": "MartCustomer360"})


class TestSharedSelectionHelper:
    def test_simple_fk_constraints_gates_then_returns_all_in_order_deduped(self):
        t = _table(
            "invoice",
            "1",
            ["ba_no"],
            [
                _fk("ba_no", "a", source="AI_INFERRED", status="PENDING_REVIEW"),
                _fk("ba_no", "b", source="DETERMINISTIC"),
                _fk("ba_no", "b", source="AI_INFERRED", status="APPROVED"),  # duplicate target
                _fk("ba_no", "c", source="AI_INFERRED", status="APPROVED"),
                _fk("other", "d", source="DETERMINISTIC"),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY", columns=["ba_no", "x"], referredColumns=["e.id", "e.y"]
                ),
            ],
        )
        got = [tc.referredColumns[0] for tc in simple_fk_constraints(t, "ba_no")]
        assert got == ["b.id", "c.id"]

    def test_fk_property_local_name(self):
        assert fk_property_local_name("invoice_baNo", None) == "invoice_baNo"
        assert fk_property_local_name("invoice_baNo", "AccountXref") == "invoice_baNo__AccountXref"

    def test_simple_fk_constraints_dedup_includes_target_datasource(self):
        # Two same-named target tables in DIFFERENT datasources are DISTINCT
        # relationships. Under a bare (table, column) dedup key they would
        # collapse into one and the approved cross-source edge would be
        # silently dropped from all three artifacts.
        t = _table(
            "invoice",
            "1",
            ["ba_no"],
            [
                # Local billing_account (no target ds).
                _fk("ba_no", "billing_account", source="DETERMINISTIC"),
                # Cross-source billing_account in ds=2 — must survive the dedup.
                _fk("ba_no", "billing_account", source="AI_INFERRED", status="APPROVED", ds="2"),
            ],
        )
        got = simple_fk_constraints(t, "ba_no")
        assert len(got) == 2
        assert [tc.targetDatasourceId for tc in got] == [None, "2"]

    def test_simple_fk_constraints_drops_entries_without_target_column(self):
        # An FK whose referredColumns entry carries no target column can't
        # yield a valid rr:joinCondition (R2RML §7.5). R2RML used to filter
        # these; ontology + SHACL counted them and the three-way invariant
        # broke. simple_fk_constraints now filters them so all three agree.
        t = _table(
            "invoice",
            "1",
            ["ba_no"],
            [
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["ba_no"],
                    referredColumns=["billing_account."],  # trailing dot, no column
                    relationshipType="STEWARD_SPECIFIED",
                ),
                _fk("ba_no", "account_xref", source="DETERMINISTIC"),
            ],
        )
        got = simple_fk_constraints(t, "ba_no")
        assert [tc.referredColumns[0] for tc in got] == ["account_xref.id"]

    def test_column_qualifier_disambiguation_by_target_column(self):
        # Two FKs from one column to the same target class but DIFFERENT
        # target columns (e.g. billing_account.id AND billing_account.legacy_id):
        # they must not share a qualified name — under the old rule both got
        # `invoice_baNo__BillingAccount` and R2RML AND'd two joinConditions on
        # one POM, returning nothing. Both are cross-source here (no local FK
        # to keep plain), so both are qualified: the qualifier is
        # disambiguated by target column when the target class repeats.
        tables = _invoice(
            [
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["ba_no"],
                    referredColumns=["billing_account.id"],
                    relationshipType="AI_INFERRED",
                    reviewStatus="APPROVED",
                    targetDatasourceId="2",
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["ba_no"],
                    referredColumns=["billing_account.legacy_id"],
                    relationshipType="AI_INFERRED",
                    reviewStatus="APPROVED",
                    targetDatasourceId="2",
                ),
            ]
        )
        _assert_three_way_invariant(
            tables,
            "invoice",
            {
                "invoice_baNo__BillingAccount_id": "BillingAccount",
                "invoice_baNo__BillingAccount_legacyId": "BillingAccount",
            },
        )
