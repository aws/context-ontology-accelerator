# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Constraint config models + SHACL compiler.

The ConstraintConfig is the structured intermediate representation between
human-reviewable constraints and SHACL Turtle output. Three sources feed it:
  - db_constraint: auto-generated from CatalogTable during induction
  - llm_inferred: LLM proposes semantic rules from ontology context
  - user_added: manual NL rules typed by the user
"""

from enum import StrEnum

from pydantic import BaseModel
from rdflib import XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

# Reuse the canonical SQL→XSD map + naming helpers rather than redefining them
# (they lived in 3+ places). ``base.py`` owns the authoritative copies.
from coa_ontology.inducer.services.data_catalog import parse_referred_column
from coa_ontology.inducer.strategies.base import pascal_names_for as _pascal_names_for
from coa_ontology.inducer.strategies.base import to_camel as _to_camel
from coa_ontology.inducer.strategies.base import to_pascal as _to_pascal
from coa_ontology.inducer.strategies.base import xsd_for as _xsd_for

SH = Namespace("http://www.w3.org/ns/shacl#")


# ── Data models ───────────────────────────────────────────────────────────


class ConstraintType(StrEnum):
    """Kind of property constraint.

    Must cover the FULL vocabulary the LLM inference prompt emits (see
    nl_generator._INFER_SYSTEM), not just the SHACL-compilable subset — the
    LLM legitimately returns ``temporal``/``enum``/``cross_field`` and they
    must validate. ``compile_to_shacl`` only emits SHACL for the structural
    types below (required/unique/positive/reference/datatype/pattern); the
    semantic ones (temporal/enum/cross_field/custom) are advisory metadata
    surfaced to the user and skipped by the compiler.
    """

    # SHACL-compilable (structural)
    REQUIRED = "required"
    UNIQUE = "unique"
    POSITIVE = "positive"
    REFERENCE = "reference"
    DATATYPE = "datatype"
    PATTERN = "pattern"
    # Advisory (LLM-inferred semantic rules; not compiled to SHACL today)
    TEMPORAL = "temporal"
    ENUM = "enum"
    CROSS_FIELD = "cross_field"
    CUSTOM = "custom"


class ConstraintSource(StrEnum):
    """Where a constraint came from."""

    DB_CONSTRAINT = "db_constraint"  # auto-generated from CatalogTable during induction
    LLM_INFERRED = "llm_inferred"  # LLM-proposed semantic rule
    USER_ADDED = "user_added"  # manual NL rule typed by the user


class PropertyConstraint(BaseModel):
    """A single constraint on one property of a class (its type, source, and params)."""

    property_path: str
    property_name: str
    constraint_type: ConstraintType
    enabled: bool = True
    params: dict = {}
    source: ConstraintSource
    description: str


class ClassConstraints(BaseModel):
    """All property constraints that apply to a single ontology class."""

    class_uri: str
    class_name: str
    constraints: list[PropertyConstraint]


class ConstraintConfig(BaseModel):
    """The full set of class constraints for an ontology, prior to SHACL compilation."""

    classes: list[ClassConstraints]


# ── Config generation from DB constraints ─────────────────────────────────


def generate_config_from_db(tables, uri_prefix: str) -> ConstraintConfig:
    """Produce a ConstraintConfig from CatalogTable constraints (deterministic)."""
    ns_str = uri_prefix
    classes: list[ClassConstraints] = []

    # Same collision-resolved local names the ontology and R2RML builders mint,
    # so the shapes target the classes/properties that actually exist.
    pascal_by_name = _pascal_names_for(t.name for t in tables)
    camel_by_name = {n: p[0].lower() + p[1:] if p else p for n, p in pascal_by_name.items()}

    for table in tables:
        class_uri = f"{ns_str}{pascal_by_name[table.name]}"
        constraints: list[PropertyConstraint] = []

        pk_cols: set[str] = set()
        unique_cols: set[str] = set()
        fk_map: dict[str, str] = {}

        if table.tableConstraints:
            for tc in table.tableConstraints:
                if tc.constraintType == "PRIMARY_KEY" and tc.columns:
                    pk_cols.update(tc.columns)
                elif tc.constraintType == "UNIQUE" and tc.columns and len(tc.columns) == 1:
                    unique_cols.update(tc.columns)
                elif tc.constraintType == "FOREIGN_KEY" and tc.columns and tc.referredColumns:
                    fk_target, _ = parse_referred_column(tc.referredColumns[0])
                    for col_name in tc.columns:
                        fk_map[col_name] = fk_target

        for col in table.columns:
            prop_path = f"{ns_str}{camel_by_name[table.name]}_{_to_camel(col.name)}"
            is_pk = col.name in pk_cols
            is_not_null = col.constraint in ("NOT_NULL", "PRIMARY_KEY") or is_pk
            is_unique = col.constraint in ("UNIQUE", "PRIMARY_KEY") or col.name in unique_cols or is_pk
            is_fk = col.name in fk_map

            if is_not_null:
                constraints.append(
                    PropertyConstraint(
                        property_path=prop_path,
                        property_name=col.name,
                        constraint_type=ConstraintType.REQUIRED,
                        source=ConstraintSource.DB_CONSTRAINT,
                        description=f"{col.name} is required" + (" (primary key)" if is_pk else " (NOT NULL)"),
                    )
                )

            if is_unique:
                constraints.append(
                    PropertyConstraint(
                        property_path=prop_path,
                        property_name=col.name,
                        constraint_type=ConstraintType.UNIQUE,
                        source=ConstraintSource.DB_CONSTRAINT,
                        description=f"{col.name} must be unique" + (" (primary key)" if is_pk else ""),
                    )
                )

            if is_fk:
                fk_target_name = fk_map[col.name]
                target_class = f"{ns_str}{pascal_by_name.get(fk_target_name, _to_pascal(fk_target_name))}"
                constraints.append(
                    PropertyConstraint(
                        property_path=prop_path,
                        property_name=col.name,
                        constraint_type=ConstraintType.REFERENCE,
                        params={"target_class": target_class},
                        source=ConstraintSource.DB_CONSTRAINT,
                        description=f"{col.name} must reference a valid {fk_map[col.name]}",
                    )
                )
            elif not is_fk:
                xsd_type = str(_xsd_for(col.dataType))
                constraints.append(
                    PropertyConstraint(
                        property_path=prop_path,
                        property_name=col.name,
                        constraint_type=ConstraintType.DATATYPE,
                        params={"xsd_type": xsd_type},
                        source=ConstraintSource.DB_CONSTRAINT,
                        description=f"{col.name} must be {col.dataType}",
                    )
                )

        if constraints:
            classes.append(ClassConstraints(class_uri=class_uri, class_name=table.name, constraints=constraints))

    return ConstraintConfig(classes=classes)


# ── SHACL compilation ─────────────────────────────────────────────────────


def compile_to_shacl(config: ConstraintConfig, uri_prefix: str, custom_turtle: str | None = None) -> str:
    """Compile a ConstraintConfig into SHACL Turtle.

    Only enabled constraints are included. Custom turtle is appended verbatim.
    """
    g = Graph()
    ns = Namespace(uri_prefix)
    g.bind("sh", SH)
    g.bind("ind", ns)
    g.bind("xsd", XSD)

    for cls in config.classes:
        shape_uri = URIRef(cls.class_uri + "Shape")
        class_uri = URIRef(cls.class_uri)

        g.add((shape_uri, RDF.type, SH.NodeShape))
        g.add((shape_uri, SH.targetClass, class_uri))

        for pc in cls.constraints:
            if not pc.enabled:
                continue

            prop_uri = URIRef(pc.property_path)
            prop_shape = BNode()
            g.add((shape_uri, SH.property, prop_shape))
            g.add((prop_shape, SH.path, prop_uri))
            g.add((prop_shape, SH.name, Literal(pc.property_name)))

            if pc.constraint_type == ConstraintType.REQUIRED:
                g.add((prop_shape, SH.minCount, Literal(1)))
            elif pc.constraint_type == ConstraintType.UNIQUE:
                g.add((prop_shape, SH.maxCount, Literal(1)))
            elif pc.constraint_type == ConstraintType.POSITIVE:
                min_val = pc.params.get("min_exclusive", 0)
                g.add((prop_shape, SH.minExclusive, Literal(min_val)))
            elif pc.constraint_type == ConstraintType.REFERENCE:
                target = pc.params.get("target_class")
                if target:
                    g.add((prop_shape, SH.nodeKind, SH.IRI))
                    g.add((prop_shape, SH["class"], URIRef(target)))
            elif pc.constraint_type == ConstraintType.DATATYPE:
                xsd_type = pc.params.get("xsd_type")
                if xsd_type:
                    g.add((prop_shape, SH.datatype, URIRef(xsd_type)))
            elif pc.constraint_type == ConstraintType.PATTERN:
                pattern = pc.params.get("regex")
                if pattern:
                    g.add((prop_shape, SH.pattern, Literal(pattern)))

    result = g.serialize(format="turtle")

    if custom_turtle and custom_turtle.strip():
        result += "\n\n# ── Custom shapes (user-supplied) ──\n" + custom_turtle.strip() + "\n"

    return result
