# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Constraint config models + SHACL compiler.

The ConstraintConfig is the structured intermediate representation between
human-reviewable constraints and SHACL Turtle output. Three sources feed it:
  - db_constraint: auto-generated from CatalogTable during induction
  - llm_inferred: LLM proposes semantic rules from ontology context
  - user_added: manual NL rules typed by the user
"""

import logging
from enum import StrEnum

from pydantic import BaseModel
from rdflib import XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

# Reuse the canonical SQL→XSD map + naming helpers rather than redefining them
# (they lived in 3+ places). ``base.py`` owns the authoritative copies.
from coa_ontology.inducer.services.data_catalog import parse_referred_column
from coa_ontology.inducer.strategies.base import ambiguous_target_names as _ambiguous_target_names
from coa_ontology.inducer.strategies.base import composite_fk_anchors as _composite_fk_anchors
from coa_ontology.inducer.strategies.base import composite_fk_columns as _composite_fk_columns
from coa_ontology.inducer.strategies.base import fk_edge_allowed as _fk_edge_allowed
from coa_ontology.inducer.strategies.base import fk_property_local_name as _fk_property_local_name
from coa_ontology.inducer.strategies.base import fk_property_qualifiers as _fk_property_qualifiers
from coa_ontology.inducer.strategies.base import pascal_names_for as _pascal_names_for
from coa_ontology.inducer.strategies.base import reference_index as _reference_index
from coa_ontology.inducer.strategies.base import (
    resolve_fk_target_identity as _resolve_fk_target_identity,
)
from coa_ontology.inducer.strategies.base import simple_fk_constraints as _simple_fk_constraints
from coa_ontology.inducer.strategies.base import table_identity as _table_identity
from coa_ontology.inducer.strategies.base import to_camel as _to_camel
from coa_ontology.inducer.strategies.base import xsd_for as _xsd_for

SH = Namespace("http://www.w3.org/ns/shacl#")

_log = logging.getLogger(__name__)


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
    pascal_by_id = _pascal_names_for(tables)
    camel_by_id = {i: p[0].lower() + p[1:] if p else p for i, p in pascal_by_id.items()}
    ref_index = _reference_index(tables)
    ambiguous_names = _ambiguous_target_names(tables)

    for table in tables:
        identity = _table_identity(table)
        class_uri = f"{ns_str}{pascal_by_id[identity]}"
        constraints: list[PropertyConstraint] = []

        pk_cols: set[str] = set()
        unique_cols: set[str] = set()
        # column -> the FK targets it carries: (target table name, target datasource id).
        # EVERY gate-passing single-column FK (#1088 follow-up), through the same
        # selection the ontology and the mapping use, so the shape asserts
        # sh:class on exactly the properties those two artifacts made references —
        # and nothing on a withheld (pending/rejected) relationship.
        fk_targets: dict[str, list[tuple[str, str | None, str | None]]] = {}

        if table.tableConstraints:
            for tc in table.tableConstraints:
                if tc.constraintType == "PRIMARY_KEY" and tc.columns:
                    pk_cols.update(tc.columns)
                elif tc.constraintType == "UNIQUE" and tc.columns and len(tc.columns) == 1:
                    unique_cols.update(tc.columns)

            # Composite FKs: only the anchor column carries the relationship (the
            # mapping emits one Referencing Object Map per anchor, R2RML §7.5).
            # Absorbed and malformed-FK columns are literals, so they must NOT get a
            # target_class. composite_fk_columns marks a malformed FK's columns with
            # "" — those are relationship-free too, and must not be resurrected here.
            absorbed = _composite_fk_columns(table)
            for anchor_col, tc in _composite_fk_anchors(table).items():
                # composite_fk_anchors only yields usable FKs, which by definition
                # have referredColumns; the guard is for the type checker.
                if not tc.referredColumns:
                    continue
                if not _fk_edge_allowed(tc.relationshipType, tc.reviewStatus):
                    continue
                fk_target, fk_target_col = parse_referred_column(tc.referredColumns[0])
                fk_targets[anchor_col] = [(fk_target, tc.targetDatasourceId, fk_target_col)]
            for col in table.columns:
                if col.name in absorbed or col.name in fk_targets:
                    continue
                simple = _simple_fk_constraints(table, col.name)
                if simple:
                    fk_targets[col.name] = [
                        (
                            parse_referred_column(tc.referredColumns[0])[0],  # type: ignore[index]
                            tc.targetDatasourceId,
                            parse_referred_column(tc.referredColumns[0])[1],  # type: ignore[index]
                        )
                        for tc in simple
                    ]

        for col in table.columns:
            base_local = f"{camel_by_id[identity]}_{_to_camel(col.name)}"
            is_pk = col.name in pk_cols
            is_not_null = col.constraint in ("NOT_NULL", "PRIMARY_KEY") or is_pk
            is_unique = col.constraint in ("UNIQUE", "PRIMARY_KEY") or col.name in unique_cols or is_pk

            # Resolve each FK target through the shared index so the shape targets
            # the same class the ontology declared and the mapping joins to. An
            # ambiguous bare name resolves to nothing and THAT relationship is
            # dropped here exactly as the other two artifacts drop it; when none
            # resolve the column falls through to the datatype constraint.
            resolved: list[
                tuple[str, str, str, str | None, str | None]
            ] = []  # (class IRI, target local, target name, target column, target ds)
            for fk_target_name, target_ds, target_col in fk_targets.get(col.name, []):
                target_local: str | None = None
                # The relationship's targetDatasourceId goes to the SAME resolver the
                # ontology and the mapping use, so sh:class names the table they do
                # (and nothing when several same-named tables sit in that datasource).
                target_id = _resolve_fk_target_identity(table, fk_target_name, ref_index, target_ds)
                if target_id in pascal_by_id:
                    target_local = pascal_by_id[target_id]
                elif fk_target_name in ambiguous_names:
                    # Ambiguous bare target: no single class to assert sh:class
                    # against — the column falls through to the datatype
                    # constraint, matching the ontology and the mapping.
                    target_local = None
                else:
                    # Genuinely outside this run: the target table is not
                    # minted this run. A sh:class pointing at a bare ind:<Target>
                    # that is never declared asserts a class-typed reference the
                    # mapping emits as a literal — a false-positive violation on
                    # every row. Degrade to the datatype constraint, mirroring the
                    # ontology's range and the R2RML parentTriplesMap.
                    _log.warning(
                        "fk_target_out_of_run_degraded_to_literal",
                        extra={"referrer": table.name, "target": fk_target_name},
                    )
                    target_local = None
                if target_local is not None:
                    resolved.append((f"{ns_str}{target_local}", target_local, fk_target_name, target_col, target_ds))

            # One property per relationship (qualified only when the column carries
            # several) — the same names the ontology mints and the mapping predicates.
            qualifiers = _fk_property_qualifiers(
                [(local, target_col, target_ds) for _, local, _, target_col, target_ds in resolved]
            )
            properties: list[tuple[str, str | None, str | None]] = (
                [
                    (f"{ns_str}{_fk_property_local_name(base_local, qualifier)}", cls, name)
                    for (cls, _, name, _, _), qualifier in zip(resolved, qualifiers, strict=True)
                ]
                if resolved
                else [(f"{ns_str}{base_local}", None, None)]
            )

            for prop_path, ref_class, ref_name in properties:
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

                if ref_class is not None:
                    constraints.append(
                        PropertyConstraint(
                            property_path=prop_path,
                            property_name=col.name,
                            constraint_type=ConstraintType.REFERENCE,
                            params={"target_class": ref_class},
                            source=ConstraintSource.DB_CONSTRAINT,
                            description=f"{col.name} must reference a valid {ref_name}",
                        )
                    )
                else:
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
