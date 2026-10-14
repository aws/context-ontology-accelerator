# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""ASCII schemas must induce byte-identical artifacts across identifier changes.

Every ontology induced so far has ASCII table and column names, and its IRIs are
referenced by stored proposals, published VKG mappings and saved queries. A change
to how the structured path mints local names (``to_pascal`` / ``to_camel``, the
per-table column dedupe, the POM node segment) must therefore leave ASCII output
untouched.

The golden files under ``tests/fixtures/structured_ascii_golden/`` are the output
of the engine at ``main`` commit ``e674953673f61ed6051b345276e965479613f5c3``,
the merge base of the Unicode identifier change (#927 Part B1), so this guard
compares the current engine against pre-change behavior rather than against
itself. The fixture exercises the naming paths that matter: separators of every
kind, mixed case, PK / UNIQUE / NOT NULL, a simple FK, a composite FK with an
absorbed column, a column carrying two FKs (``fk_property_qualifiers``), and two
tables sharing a bare name across datasources (``pascal_names_for``
discriminator).

The fixture deliberately stays inside the domain that change left untouched:
every name is ASCII, and no two columns of one table share a camelCase form or
fall back to ``entity``. Columns that collide (``OrderID`` / ``orderId``, a
literal ``entity`` beside a symbol-only ``%``) and symbol-only names changed on
purpose, so they are covered in ``test_non_ascii_identifiers`` instead
(``TestAsciiBehaviourChangesAreCollisionFixesOnly`` and the tests after it).
``test_fixture_stays_inside_the_unchanged_ascii_domain`` keeps them out of here.

Each artifact is compared as canonical N-Triples (``rdflib.compare``), so blank
node labels and serializer ordering do not matter but every IRI, literal and
datatype does.

To reproduce the goldens from that commit, run from the repository root. The
``PYTHONPATH`` entry shadows the workspace's editable install, so ``_render``
below runs against the old engine and rewrites the goldens in place;
``git diff`` must then be empty::

    rm -rf /tmp/golden-e67495367 && mkdir -p /tmp/golden-e67495367
    git archive e674953673f61ed6051b345276e965479613f5c3 packages/ontology-engine/src \\
        | tar -x -C /tmp/golden-e67495367
    PYTHONPATH=/tmp/golden-e67495367/packages/ontology-engine/src \\
        uv run --frozen python packages/ontology-engine/tests/unit/test_structured_ascii_golden.py

Running the last line without ``PYTHONPATH`` regenerates from the working tree.
Do that only for an intended, reviewed ASCII change, and replace the commit
recorded above with the one the new goldens represent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogConstraint, CatalogTable
from coa_ontology.inducer.strategies.rigor_ontology import RigorOntologyStrategy
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy
from coa_ontology.validation.shapes.config import compile_to_shacl, generate_config_from_db
from rdflib import Graph
from rdflib.compare import to_canonical_graph

pytestmark = pytest.mark.unit

PREFIX = "http://example.org/base/"
GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "structured_ascii_golden"
ARTIFACTS = ("ontology", "r2rml", "rigor_r2rml", "shacl")


def _col(name: str, data_type: str = "STRING", **kwargs) -> CatalogColumn:
    return CatalogColumn(name=name, dataType=data_type, **kwargs)


def _tables() -> list[CatalogTable]:
    return [
        CatalogTable(
            id="a:customers",
            name="customers",
            fullyQualifiedName="public.customers",
            datasourceId="ds-a",
            sourceSchema="public",
            description="Retail customers",
            columns=[
                _col("customer_id", "INT", constraint="PRIMARY_KEY"),
                _col("email", "VARCHAR", constraint="UNIQUE"),
                _col("First Name", constraint="NOT_NULL"),
                _col("ship-to address"),
                _col("total_spent", "DECIMAL"),
                _col(
                    "status",
                    description="Lifecycle status",
                    synonyms=["state"],
                    distinctValues=["active", "churned"],
                ),
                _col("addr2"),
            ],
            tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["customer_id"])],
        ),
        CatalogTable(
            id="a:orders",
            name="orders",
            fullyQualifiedName="public.orders",
            datasourceId="ds-a",
            sourceSchema="public",
            columns=[
                _col("order_id", "BIGINT"),
                _col("customer_id", "INT"),
                _col("ORDER DATE", "TIMESTAMP"),
                _col("amount", "DOUBLE"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["order_id"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["customer_id"],
                    referredColumns=["customers.customer_id"],
                ),
            ],
        ),
        CatalogTable(
            id="a:product_variants",
            name="product_variants",
            fullyQualifiedName="public.product_variants",
            datasourceId="ds-a",
            sourceSchema="public",
            columns=[_col("product_id", "INT"), _col("variant_id", "INT"), _col("sku")],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["product_id", "variant_id"]),
            ],
        ),
        CatalogTable(
            id="a:order_items",
            name="order_items",
            fullyQualifiedName="public.order_items",
            datasourceId="ds-a",
            sourceSchema="public",
            columns=[
                _col("order_id", "BIGINT"),
                _col("line_no", "INT"),
                _col("product_id", "INT"),
                _col("variant_id", "INT"),
                _col("unit-price", "DECIMAL"),
            ],
            tableConstraints=[
                CatalogConstraint(constraintType="PRIMARY_KEY", columns=["order_id", "line_no"]),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["order_id"],
                    referredColumns=["orders.order_id"],
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["product_id", "variant_id"],
                    referredColumns=["product_variants.product_id", "product_variants.variant_id"],
                ),
            ],
        ),
        # Same bare name and schema as ds-a's customers: pascal_names_for discriminates.
        CatalogTable(
            id="b:customers",
            name="customers",
            fullyQualifiedName="public.customers",
            datasourceId="ds-b",
            sourceSchema="public",
            columns=[_col("id", "INT"), _col("segment")],
        ),
        CatalogTable(
            id="b:billing_account",
            name="billing_account",
            fullyQualifiedName="billing.billing_account",
            datasourceId="ds-b",
            sourceSchema="billing",
            columns=[_col("id", "INT")],
        ),
        CatalogTable(
            id="c:account_xref",
            name="account_xref",
            fullyQualifiedName="mart.account_xref",
            datasourceId="ds-c",
            sourceSchema="mart",
            columns=[_col("id", "INT")],
        ),
        # One column carrying two FKs, one local and one cross-source: qualified properties.
        CatalogTable(
            id="b:invoice",
            name="invoice",
            fullyQualifiedName="billing.invoice",
            datasourceId="ds-b",
            sourceSchema="billing",
            columns=[_col("id", "INT"), _col("ba_no", "INT"), _col("Due-Date", "DATE")],
            tableConstraints=[
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["ba_no"],
                    referredColumns=["billing_account.id"],
                ),
                CatalogConstraint(
                    constraintType="FOREIGN_KEY",
                    columns=["ba_no"],
                    referredColumns=["account_xref.id"],
                    relationshipType="CROSS_SOURCE",
                    reviewStatus="APPROVED",
                    targetDatasourceId="ds-c",
                ),
            ],
        ),
    ]


def _canonical_nt(graph: Graph) -> str:
    lines = to_canonical_graph(graph).serialize(format="nt").splitlines()
    return "\n".join(sorted(line for line in lines if line.strip())) + "\n"


def _render() -> dict[str, str]:
    tables = _tables()
    strategy = TableToOntologyStrategy()
    ontology, novel = strategy._build_proposal_ontology(PREFIX, tables, [])
    r2rml = strategy.build_r2rml(PREFIX, tables, novel, ontology)
    rigor_r2rml = RigorOntologyStrategy().build_r2rml(PREFIX, tables, {t.name for t in tables}, ontology)
    shacl = Graph().parse(data=compile_to_shacl(generate_config_from_db(tables, PREFIX), PREFIX), format="turtle")
    return {
        "ontology": _canonical_nt(ontology),
        "r2rml": _canonical_nt(r2rml),
        "rigor_r2rml": _canonical_nt(rigor_r2rml),
        "shacl": _canonical_nt(shacl),
    }


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_ascii_artifact_matches_golden_captured_from_main(artifact):
    golden = (GOLDEN_DIR / f"{artifact}.nt").read_text(encoding="utf-8")
    rendered = _render()[artifact]
    assert rendered == golden, (
        f"{artifact} for an ASCII-only schema differs from the golden captured from main; "
        "an ASCII IRI moved. Regenerate only if that is intended and reviewed."
    )


def test_fixture_exercises_the_naming_paths():
    """Guard against the fixture silently degenerating into a trivial schema."""
    rendered = _render()
    assert "TriplesMap_Customers_" in rendered["r2rml"], "pascal_names_for discriminator not exercised"
    assert "joinCondition" in rendered["r2rml"], "no FK join in the mapping"
    assert "hasKey" in rendered["ontology"], "no owl:hasKey in the ontology"
    assert "invoice_baNo_" in rendered["ontology"], "multi-FK qualifier not exercised"
    assert "POM_" in rendered["rigor_r2rml"], "RIGOR mapping emitted no POMs"


def test_fixture_stays_inside_the_unchanged_ascii_domain():
    """Keep out the names whose IRIs #927 Part B1 changed on purpose.

    A golden taken from ``e67495367`` is only a valid regression guard for names
    that change left alone. Adding a non-ASCII name, two columns of one table that
    camel-case alike, or a name with no letters or digits would make this golden
    disagree with the current engine for an intended reason.
    """
    # Imported here, not at module level: ``camel_names_for`` does not exist at
    # e67495367, and the regeneration command in the module docstring imports this
    # file against that engine.
    from coa_ontology.inducer.strategies.base import camel_names_for, to_camel

    for table in _tables():
        names = [c.name for c in table.columns]
        locals_ = [to_camel(name) for name in names]
        assert all(name.isascii() for name in [table.name, *names]), f"{table.id}: non-ASCII name"
        assert len(set(locals_)) == len(locals_), f"{table.id}: columns collide after camelCase: {locals_}"
        assert "entity" not in locals_, f"{table.id}: a column falls back to the entity sentinel"
        assert camel_names_for(table) == dict(zip(names, locals_, strict=True)), f"{table.id}: dedupe engaged"


if __name__ == "__main__":
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in _render().items():
        (GOLDEN_DIR / f"{name}.nt").write_text(content, encoding="utf-8")
