# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Base class for induction strategies."""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable

from coa_common import sql_ident
from coa_common.bedrock_metrics import CostTracker
from coa_common.constants import VOCAB_URI
from rdflib import RDF, XSD, BNode, Graph, Literal, Namespace, URIRef

from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.data_catalog import (
    CatalogConstraint,
    CatalogTable,
    parse_referred_column,
)

log = logging.getLogger(__name__)

# R2RML namespace

RR = Namespace("http://www.w3.org/ns/r2rml#")

# SCL vocabulary for datasource provenance annotations on TriplesMaps
SCL = Namespace(VOCAB_URI)

# SQL → XSD datatype mapping (shared across strategies)
_SQL_TO_XSD = {
    "INT": XSD.integer,
    "BIGINT": XSD.long,
    "SMALLINT": XSD.short,
    "TINYINT": XSD.byte,
    "FLOAT": XSD.float,
    "DOUBLE": XSD.double,
    "DECIMAL": XSD.decimal,
    "NUMERIC": XSD.decimal,
    "VARCHAR": XSD.string,
    "TEXT": XSD.string,
    "CHAR": XSD.string,
    "STRING": XSD.string,
    "BOOLEAN": XSD.boolean,
    "DATE": XSD.date,
    "TIMESTAMP": XSD.dateTime,
    "DATETIME": XSD.dateTime,
    "TIME": XSD.time,
    "BINARY": XSD.hexBinary,
    "BLOB": XSD.hexBinary,
    "UUID": XSD.string,
}


# Column names that strongly suggest numeric content even when schema says VARCHAR.
# Used by xsd_for_column() to override VARCHAR→string when the column name implies a number.
_NUMERIC_COLUMN_PATTERNS = {
    "amount",
    "total",
    "sum",
    "count",
    "price",
    "cost",
    "revenue",
    "profit",
    "salary",
    "income",
    "balance",
    "budget",
    "spent",
    "remaining",
    "quantity",
    "rate",
    "ratio",
    "percentage",
    "pct",
    "score",
    "rating",
    "weight",
    "height",
    "width",
    "length",
    "size",
    "age",
    "duration",
    "distance",
    "consumption",
    "lat",
    "lng",
    "longitude",
    "latitude",
    "altitude",
    "elevation",
}


def xsd_for(data_type: str) -> URIRef:
    """Map a SQL data type to its XSD datatype URI.

    Args:
        data_type: SQL type name, optionally with a length/precision suffix.

    Returns:
        The mapped XSD datatype URI, defaulting to ``xsd:string``.
    """
    base = (data_type or "").split("(")[0].upper()
    return _SQL_TO_XSD.get(base, XSD.string)


def xsd_for_column(data_type: str, column_name: str) -> URIRef:
    """Determine XSD type considering both SQL type and column name semantics.

    For VARCHAR/TEXT columns whose names suggest numeric content (e.g. 'spent',
    'amount', 'consumption'), returns xsd:decimal instead of xsd:string. This
    enables Ontop to perform numeric aggregation (SUM, AVG) on these columns.
    """
    base = (data_type or "").split("(")[0].upper()
    xsd_type = _SQL_TO_XSD.get(base, XSD.string)

    # If schema says string but column name implies numeric, use decimal
    if xsd_type == XSD.string and column_name:
        col_lower = column_name.lower().strip()
        # Check exact match or suffix match (e.g. 'total_spent' ends with 'spent')
        for pattern in _NUMERIC_COLUMN_PATTERNS:
            if col_lower == pattern or col_lower.endswith(f"_{pattern}") or col_lower.startswith(f"{pattern}_"):
                return XSD.decimal
    return xsd_type


def to_pascal(s: str) -> str:
    """Convert an arbitrary string to PascalCase for use as a class name.

    Args:
        s: Input string (may contain spaces, underscores, or hyphens).

    Returns:
        The PascalCase form, or ``"Entity"`` if nothing usable remains.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9\s_\-]", "", s or "")
    return "".join(w.capitalize() for w in re.split(r"[\s_\-]+", cleaned) if w) or "Entity"


def to_camel(s: str) -> str:
    """Convert an arbitrary string to camelCase for use as a property name.

    Args:
        s: Input string (may contain spaces, underscores, or hyphens).

    Returns:
        The camelCase form derived from :func:`to_pascal`.
    """
    p = to_pascal(s)
    return p[0].lower() + p[1:] if p else p


def _name_discriminator(name: str) -> str:
    """Return a short deterministic suffix distinguishing ``name`` from its peers.

    Derived from the verbatim name, so it is stable across runs (no hashing of
    iteration order or object identity) and identical in every consumer that
    mints IRIs for the same table.
    """
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def pascal_names_for(names: Iterable[str]) -> dict[str, str]:
    r"""Map each verbatim table name to a COLLISION-FREE PascalCase local name.

    :func:`to_pascal` is lossy: it strips every character outside
    ``[a-zA-Z0-9\s_\-]``, treats space/underscore/hyphen as one separator
    class, and normalizes case. So ``order_item``, ``order-item``,
    ``Order_Item``, and ``ORDER ITEM`` all collapse onto ``OrderItem``, and any
    name made entirely of stripped characters collapses onto the ``"Entity"``
    fallback. Minting class / TriplesMap IRIs straight from ``to_pascal`` therefore
    silently FUSES distinct source tables onto one IRI: one class carrying both
    tables' labels, key axioms, and cardinality restrictions, and — worse — one
    TriplesMap carrying two ``rr:logicalTable`` / ``rr:tableName`` values, which
    is invalid R2RML (a TriplesMap has exactly one logical table). Ontop then
    either rejects the whole mapping (VKG load fails for the namespace) or picks
    one table non-deterministically and drops the other's data.

    This helper resolves collisions instead: the first name (in the caller's
    order) keeps the bare PascalCase form so existing single-table-per-name
    deployments mint byte-identical IRIs, and each subsequent colliding name gets
    ``{Pascal}_{sha256(name)[:8]}`` appended. The discriminator is derived from
    the verbatim name, so the assignment is deterministic given the same input
    order and is reproducible across the ontology builder, the R2RML builder, and
    the SHACL config generator — all three must agree or the artifacts diverge.

    Callers should also surface a collision to the user (see the ``log.warning``
    below): a silent merge is the worst of the possible outcomes, and a schema
    that needs a discriminator usually indicates a naming problem worth fixing at
    the source.

    Args:
        names: Verbatim table names, in a stable order.

    Returns:
        ``{verbatim_name: unique_pascal_local_name}`` covering every input name.
    """
    result: dict[str, str] = {}
    taken: set[str] = set()
    for name in names:
        if name in result:
            continue  # duplicate entry for the same table — one mapping is enough
        base = to_pascal(name)
        candidate = base
        if candidate in taken:
            candidate = f"{base}_{_name_discriminator(name)}"
            log.warning(
                "table_name_collides_after_pascal_case",
                extra={"table": name, "collided_on": base, "minted": candidate},
            )
            # Pathological: the discriminated form is itself taken (two tables
            # with the same verbatim name would have been deduped above, so this
            # needs a genuine hash collision). Widen until unique.
            suffix = 2
            while candidate in taken:
                candidate = f"{base}_{_name_discriminator(name)}_{suffix}"
                suffix += 1
        taken.add(candidate)
        result[name] = candidate
    return result


def composite_fk_is_usable(tc: CatalogConstraint) -> bool:
    """True when a composite FK constraint can produce a valid Referencing Object Map.

    :meth:`InductionStrategy.build_r2rml` degrades a composite FK to plain datatype
    ObjectMaps when it is malformed — ``columns`` / ``referredColumns`` of unequal
    length (no way to pair them into join conditions), or ``referredColumns``
    spanning several target tables (a TriplesMap has one parent). The ontology
    builder must apply the SAME test, or it declares an ``owl:ObjectProperty`` with
    ``rdfs:range <ParentClass>`` for a column the mapping exposes as a string or
    integer literal: ``rdfs:range`` and ``rr:datatype`` then disagree, and Ontop's
    type reasoning drops the triples or fails the SPARQL type filter.
    """
    if tc.constraintType != "FOREIGN_KEY" or not tc.referredColumns or len(tc.columns or []) <= 1:
        return False
    if len(tc.columns) != len(tc.referredColumns):
        return False
    fk_tables = {parse_referred_column(rc)[0] for rc in tc.referredColumns if "." in rc}
    return len(fk_tables) <= 1


def composite_fk_columns(table: CatalogTable) -> dict[str, str]:
    """Map each column absorbed into a composite-FK POM to the column that owns it.

    A composite (multi-column) FK is expressed in R2RML as ONE Referencing Object
    Map carrying one ``rr:joinCondition`` per column pair (§7.5) — not one map per
    column. :meth:`InductionStrategy.build_r2rml` therefore emits a single POM,
    anchored on the constraint's FIRST column, and emits nothing for the rest.

    The ontology builder must know which columns those are. Declaring an
    ``owl:ObjectProperty`` per participating column (the obvious reading of the
    constraint) mints properties that have no POM behind them: they appear in the
    published TBox, in the class/property browser, and in the TBox context handed
    to the NL→SPARQL LLM, so the model is actively encouraged to author SPARQL
    against a property Ontop cannot resolve to any column. The query compiles and
    returns nothing, with no mapping-gap signal in the logs. The gap scales with
    arity: a 3-column FK orphaned 2 properties.

    A MALFORMED composite FK (see :func:`composite_fk_is_usable`) behaves
    differently: ``build_r2rml`` gives every one of its columns a datatype
    ObjectMap, so none is absorbed — but none carries the relationship either, and
    the ontology must not declare any of them an object property. Those columns map
    to ``""``, letting the caller distinguish "absorbed into a sibling's POM" from
    "no relationship at all" while treating both as non-object properties.

    Returns:
        ``{column: owning_column}`` for columns absorbed into a sibling's POM, plus
        ``{column: ""}`` for every column of a malformed composite FK. The owning
        (first) column of a USABLE composite FK is absent — it keeps both its POM
        and its object property. Empty when the table has no composite FK.

    Mirrors :meth:`InductionStrategy.build_r2rml`'s CONSUMING walk exactly, which
    matters when two composite FKs share a column. For ``FK1 = (a, b) -> p1`` and
    ``FK2 = (b, c) -> p2``, walking the columns in order assigns ``a`` to FK1
    (absorbing ``b``), and then ``c`` — reached with FK1 already consumed — anchors
    FK2. A non-consuming implementation instead matched ``c`` against FK2 while
    treating it as absorbed by ``b``, which silently dropped the child→p2
    relationship from BOTH artifacts. So the walk order and the removal are
    load-bearing, not incidental.
    """
    absorbed: dict[str, str] = {}
    if not table.tableConstraints:
        return absorbed
    table_columns = {c.name for c in table.columns}

    # Malformed composite FKs first: build_r2rml degrades every one of their
    # columns to a literal regardless of position, so they never anchor anything.
    remaining = []
    for tc in table.tableConstraints:
        if tc.constraintType != "FOREIGN_KEY" or not tc.referredColumns or len(tc.columns or []) <= 1:
            continue
        if composite_fk_is_usable(tc):
            remaining.append(tc)
        else:
            for col_name in tc.columns:
                if col_name in table_columns:
                    absorbed.setdefault(col_name, "")

    # Then replay build_r2rml's walk: for each column in order, claim the first
    # not-yet-claimed constraint it participates in. That column becomes the
    # anchor, and the constraint is consumed so a later column cannot claim it
    # again. Anchors are recorded as they are decided; absorbed columns are
    # resolved afterwards, because a column can be a LATER constraint's anchor
    # even though an earlier constraint also lists it — with (a,b)->p1,
    # (b,c)->p2, (c,d)->p3, `c` anchors FK2 and must not be absorbed by FK3.
    anchors = composite_fk_anchors(table)
    for owner, tc in anchors.items():
        for col_name in tc.columns:
            if col_name != owner and col_name in table_columns:
                absorbed.setdefault(col_name, owner)
    # An anchor is never absorbed: it carries its own relationship. This can only
    # bite when constraints overlap — `composite_fk_anchors` skips folded columns,
    # so a column it chose as an anchor was not folded in by an earlier one.
    for anchor in anchors:
        absorbed.pop(anchor, None)
    return absorbed


def composite_fk_anchors(table: CatalogTable) -> dict[str, CatalogConstraint]:
    """Map each column that ANCHORS a usable composite FK to that constraint.

    Single source of truth for the anchor assignment, consumed by both
    :meth:`InductionStrategy.build_r2rml` (which emits one Referencing Object Map
    per anchor) and :func:`composite_fk_columns` (which derives the absorbed
    columns from it). Splitting the two apart is what let them disagree: the
    mapping walked and CONSUMED constraints column by column while the ontology
    side computed absorption independently, so for two FKs sharing a column they
    picked different anchors and the relationship landed on different properties
    in each artifact.

    The rule reproduces the mapping's original walk exactly: visit columns in table
    order; SKIP a column already folded into an earlier anchor's map; otherwise let
    it claim the first unclaimed constraint it participates in, and consume that
    constraint. Both the skip and the consumption matter. For (a,b)->p1 and
    (b,c)->p2: `a` claims p1 and folds in `b`; `b` is skipped BECAUSE it is folded
    in, so p2 falls to `c`. Dropping the skip hands p2 to `b` instead, which moves
    the relationship onto a different property than the original mapping used.
    """
    anchors: dict[str, CatalogConstraint] = {}
    if not table.tableConstraints:
        return anchors
    remaining = [
        tc
        for tc in table.tableConstraints
        if tc.constraintType == "FOREIGN_KEY"
        and tc.referredColumns
        and len(tc.columns or []) > 1
        and composite_fk_is_usable(tc)
    ]
    folded: set[str] = set()
    for col in table.columns:
        if col.name in folded:
            continue
        claimed = next((tc for tc in remaining if col.name in tc.columns), None)
        if claimed is None:
            continue
        anchors[col.name] = claimed
        remaining.remove(claimed)
        folded.update(c for c in claimed.columns if c != col.name)
    return anchors


def _annotate_triples_map(g: Graph, tmap: URIRef, table: CatalogTable) -> None:
    """Annotate a TriplesMap with datasource provenance (coa:datasourceId, coa:sourceSchema).

    These annotations are ignored by Ontop (custom predicates are transparent
    to R2RML processors) but are parsed by the VKG translation layer to route
    queries to the correct data source at execution time.
    """
    if table.datasourceId:
        g.add((tmap, SCL.datasourceId, Literal(table.datasourceId)))
    if table.sourceSchema:
        g.add((tmap, SCL.sourceSchema, Literal(table.sourceSchema)))


class InductionStrategy(ABC):
    """Abstract induction strategy.

    Each strategy takes table metadata and produces:
      - A proposal ontology graph (novel classes only)
      - A set of novel table names
      - A list of ConceptMatch results

    Each strategy also owns the R2RML mapping generation for its own
    ontology via :meth:`build_r2rml`. The default implementation
    reconstructs predicate IRIs from ``{table}_{col}`` naming which
    matches the ``table_to_ontology`` strategy's output. Strategies
    that generate class/property names via other means (e.g. LLM)
    should override :meth:`build_r2rml` to look up predicates from the
    generated graph.
    """

    @abstractmethod
    def induce(
        self,
        tables: list[CatalogTable],
        ontology_uri_prefix: str,
        config: dict,
        pipeline,
        confidence_threshold: float = 0.80,
        embedding_backend: str | None = None,
        scoring_strategy: str = "lexical",
        structural_weight: float = 0.05,
        grounding_ontology_ids: list[str] | None = None,
        grounding_mode: str = "ENHANCED",
        cost_tracker: CostTracker | None = None,
    ) -> tuple[Graph, set[str], list[ConceptMatch]]:
        """Run induction and return (proposal_graph, novel_tables, matches).

        ``grounding_ontology_ids`` restricts the embedding search to a subset
        of the namespace's loaded ontologies (e.g. user-selected foundationals
        like FIBO Agreements). When None or empty, search runs unrestricted.

        ``cost_tracker`` is an optional per-job Bedrock usage accumulator; when
        present the strategy threads it into every Bedrock seam (grounding
        rerank, LLM generate) so token/cost metrics are captured for the job.
        """
        ...

    # ── R2RML generation ─────────────────────────────────────────────

    def build_r2rml(
        self,
        ontology_uri_prefix: str,
        tables: list[CatalogTable],
        novel_tables: set[str],
        proposal_graph: Graph,
    ) -> Graph:
        """Default R2RML builder matching the ``table_to_ontology`` naming scheme.

        For each table (novel AND grounded):
          - class IRI = ``ind:<PascalCase(table)>``
          - property IRI = ``ind:<camelCase(table)>_<camelCase(col)>``

        FK columns produce Referencing Object Maps (R2RML §7.5) using
        ``rr:parentTriplesMap`` + ``rr:joinCondition``. This is the spec-standard
        mechanism for expressing JOINs and works correctly for all FK cardinalities
        including composite FKs referencing composite PKs.

        Non-FK columns produce datatype ObjectMaps with ``rr:column``.

        NOTE: ontology_uri_prefix MUST end with '#' or '/' to produce valid,
        parseable URIs. We normalize here defensively.

        SQL identifiers (``rr:tableName``, ``rr:column``, and join condition
        child/parent columns) use the **verbatim original** table/column name
        wrapped as a SQL-delimited identifier via :func:`sql_ident`. This
        preserves names exactly as they appear in the source datasource --
        spaces, mixed case, reserved words, special chars -- so the SQL Ontop
        emits references the real columns without any reverse mapping.
        Class/property IRIs keep their readable PascalCase/camelCase form.

        Strategies whose output doesn't follow this naming convention
        should override this method.
        """
        if not ontology_uri_prefix.endswith(("#", "/")):
            ontology_uri_prefix += "#"
        g = Graph()
        ns = Namespace(ontology_uri_prefix)
        g.bind("rr", RR)
        g.bind("scl", SCL)
        g.bind("ind", ns)
        g.bind("xsd", XSD)

        # Build a lookup from table name → TriplesMap URI for parentTriplesMap refs.
        # Local names come from the collision-resolving helper, not bare
        # to_pascal: two tables differing only in separators/case would otherwise
        # share one TriplesMap IRI and produce two rr:tableName values on it
        # (invalid R2RML — see pascal_names_for).
        pascal_by_name = pascal_names_for(t.name for t in tables)
        # Property local names derive from the (possibly discriminated) class
        # local name, so a disambiguated class carries disambiguated predicates.
        camel_by_name = {n: p[0].lower() + p[1:] if p else p for n, p in pascal_by_name.items()}
        tmap_by_name: dict[str, URIRef] = {}
        for table in tables:
            tmap_by_name[table.name] = ns[f"TriplesMap_{pascal_by_name[table.name]}"]

        for table in tables:
            tmap = tmap_by_name[table.name]
            table_cls = ns[pascal_by_name[table.name]]

            g.add((tmap, RDF.type, RR.TriplesMap))
            _annotate_triples_map(g, tmap, table)
            lt = BNode()
            g.add((tmap, RR.logicalTable, lt))
            g.add((lt, RR.tableName, Literal(sql_ident(table.name))))

            pk_cols = []
            if table.tableConstraints:
                for tc in table.tableConstraints:
                    if tc.constraintType == "PRIMARY_KEY" and tc.columns:
                        pk_cols = tc.columns
                        break

            # Build subject URI template from all PK columns to ensure uniqueness
            # for composite PKs (e.g. {prefix}/table/{col1}/{col2}).
            # Fall back to first column, then literal "ID".
            if pk_cols:
                pk_id = "/".join(f"{{{sql_ident(c)}}}" for c in pk_cols)
            elif table.columns:
                pk_id = f"{{{sql_ident(table.columns[0].name)}}}"
            else:
                pk_id = "{ID}"
            subj = URIRef(f"{tmap}/SubjectMap")
            g.add((tmap, RR.subjectMap, subj))
            template = f"{ontology_uri_prefix}{table.name}/{pk_id}"
            g.add((subj, RR.template, Literal(template)))
            g.add((subj, RR["class"], table_cls))

            # Which column carries which composite FK, and which columns it folds
            # in. Both come from the SAME helpers the ontology builder uses, so the
            # two artifacts cannot disagree about where a relationship lives — they
            # previously derived it independently (a consuming walk here, a separate
            # computation there) and picked different anchors for FKs sharing a
            # column.
            composite_anchors = composite_fk_anchors(table)
            absorbed_columns = {name for name, owner in composite_fk_columns(table).items() if owner}

            # Malformed composite FKs never anchor anything (see
            # composite_fk_is_usable); their columns fall through to the datatype
            # path below, which is what build_r2rml has always done.
            malformed_composite_columns = {
                col_name
                for tc in (table.tableConstraints or [])
                if tc.constraintType == "FOREIGN_KEY"
                and tc.referredColumns
                and len(tc.columns or []) > 1
                and not composite_fk_is_usable(tc)
                for col_name in tc.columns
            }

            for col in table.columns:
                # A column absorbed into a sibling's composite-FK Referencing
                # Object Map still gets its OWN datatype POM. The relationship is
                # carried once (by the owning column's referencing map, per R2RML
                # §7.5) — but the column is a real column, and leaving it unmapped
                # meant the ontology's declaration for it had nothing behind it, so
                # any SPARQL touching that property silently returned nothing.
                # Emitting a literal mapping keeps mapping and ontology in step:
                # the ontology declares these columns as datatype properties (see
                # composite_fk_columns).
                if col.name in absorbed_columns:
                    pom = URIRef(f"{tmap}/POM_{to_pascal(col.name)}")
                    g.add((tmap, RR.predicateObjectMap, pom))
                    g.add((pom, RR.predicate, ns[f"{camel_by_name[table.name]}_{to_camel(col.name)}"]))
                    om = URIRef(f"{pom}/ObjectMap")
                    g.add((pom, RR.objectMap, om))
                    g.add((om, RR.column, Literal(sql_ident(col.name))))
                    g.add((om, RR.datatype, xsd_for_column(col.dataType, col.name)))
                    continue

                pom = URIRef(f"{tmap}/POM_{to_pascal(col.name)}")
                g.add((tmap, RR.predicateObjectMap, pom))

                prop_uri = ns[f"{camel_by_name[table.name]}_{to_camel(col.name)}"]
                g.add((pom, RR.predicate, prop_uri))

                om = URIRef(f"{pom}/ObjectMap")
                g.add((pom, RR.objectMap, om))

                # This column anchors a composite FK when the shared anchor table
                # says so — never by re-deriving it here.
                composite_fk = composite_anchors.get(col.name)

                if col.name in malformed_composite_columns:
                    # columns/referredColumns disagree in length, or the targets span
                    # several tables: no join can be derived, so the column degrades
                    # to a literal. composite_fk_columns reports these as
                    # non-object-properties, keeping the ontology in step.
                    log.warning(
                        "Composite FK on %s is malformed (column/target counts differ, or targets span "
                        "several tables); mapping %s as a literal",
                        table.name,
                        col.name,
                    )
                    g.add((om, RR.column, Literal(sql_ident(col.name))))
                    g.add((om, RR.datatype, xsd_for_column(col.dataType, col.name)))
                    continue

                if composite_fk is not None and composite_fk.referredColumns:
                    # Composite FK: emit a single Referencing Object Map for the
                    # entire relationship, with one joinCondition per column pair.
                    fk_target, _ = parse_referred_column(composite_fk.referredColumns[0])
                    parent_tmap = tmap_by_name.get(fk_target)
                    if parent_tmap is None:
                        # Target table is outside this induction run, so it has no
                        # entry in pascal_by_name. Fall back to the bare form —
                        # nothing to collide with here, since a table we did not
                        # process has no TriplesMap in this mapping either.
                        parent_tmap = ns[f"TriplesMap_{to_pascal(fk_target)}"]

                    g.add((om, RR.parentTriplesMap, parent_tmap))
                    for child_col, ref_col in zip(composite_fk.columns, composite_fk.referredColumns, strict=True):
                        _, parsed_parent = parse_referred_column(ref_col)
                        parent_col = parsed_parent if parsed_parent is not None else ref_col
                        jc = BNode()
                        g.add((om, RR.joinCondition, jc))
                        g.add((jc, RR.child, Literal(sql_ident(child_col))))
                        g.add((jc, RR.parent, Literal(sql_ident(parent_col))))

                    # No bookkeeping needed: composite_fk_anchors already assigned
                    # each constraint to exactly one column, so no other column can
                    # re-emit this relationship.
                    continue

                # Check for simple (single-column) FK
                is_fk = False
                simple_fk_target: str | None = None
                fk_parent_col: str | None = None
                if table.tableConstraints:
                    for tc in table.tableConstraints:
                        if (
                            tc.constraintType == "FOREIGN_KEY"
                            and len(tc.columns) == 1
                            and col.name in tc.columns
                            and tc.referredColumns
                        ):
                            is_fk = True
                            simple_fk_target, fk_parent_col = parse_referred_column(tc.referredColumns[0])
                            break

                if is_fk and simple_fk_target and fk_parent_col:
                    # Simple FK: Referencing Object Map with single joinCondition.
                    # fk_parent_col is required — without it we cannot emit a valid
                    # joinCondition and a bare parentTriplesMap would produce a
                    # Cartesian product in Ontop (R2RML §7.5).
                    parent_tmap = tmap_by_name.get(simple_fk_target)
                    if parent_tmap is None:
                        parent_tmap = ns[f"TriplesMap_{to_pascal(simple_fk_target)}"]

                    g.add((om, RR.parentTriplesMap, parent_tmap))
                    jc = BNode()
                    g.add((om, RR.joinCondition, jc))
                    g.add((jc, RR.child, Literal(sql_ident(col.name))))
                    g.add((jc, RR.parent, Literal(sql_ident(fk_parent_col))))
                else:
                    # Non-FK: datatype ObjectMap
                    g.add((om, RR.column, Literal(sql_ident(col.name))))
                    dt = xsd_for_column(col.dataType, col.name)
                    g.add((om, RR.datatype, dt))

        return g
