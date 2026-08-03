# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""table_to_ontology strategy — the original embedding-based induction.

Extracts concepts from table/column names, embeds them via Bedrock Titan,
matches against existing ontology classes via vector similarity, and builds
a proposal ontology containing only novel (unmatched) classes.
"""

from __future__ import annotations

import logging
import os
import re
from itertools import batched

import httpx
from coa_common.bedrock_metrics import CostTracker
from opensearchpy.exceptions import OpenSearchException
from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SKOS

from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.data_catalog import CatalogTable
from coa_ontology.inducer.services.subtype_detection import detect_pk_sharing_subtypes
from coa_ontology.inducer.strategies.base import SCL, InductionStrategy, pascal_names_for

log = logging.getLogger(__name__)

# Tables per fusion batch when INDUCER_TABLE_BATCH_SIZE is unset.
DEFAULT_TABLE_BATCH_SIZE = 500

_SQL_TO_XSD = {
    "INT": XSD.integer,
    "BIGINT": XSD.long,
    "SMALLINT": XSD.short,
    "FLOAT": XSD.float,
    "DOUBLE": XSD.double,
    "DECIMAL": XSD.decimal,
    "VARCHAR": XSD.string,
    "TEXT": XSD.string,
    "CHAR": XSD.string,
    "STRING": XSD.string,
    "BOOLEAN": XSD.boolean,
    "DATE": XSD.dateTime,
    "TIMESTAMP": XSD.dateTime,
    "TIME": XSD.time,
}


def _to_pascal(s: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[\s_\-]+", re.sub(r"[^a-zA-Z0-9\s_\-]", "", s)) if w)


def _to_camel(s: str) -> str:
    p = _to_pascal(s)
    return p[0].lower() + p[1:] if p else p


def _xsd_for(data_type: str, column_name: str = "") -> URIRef:
    """Map SQL type to XSD, with column-name heuristic for numeric VARCHAR columns."""
    from .base import xsd_for_column

    if column_name:
        return xsd_for_column(data_type, column_name)
    base = data_type.split("(")[0].upper()
    return _SQL_TO_XSD.get(base, XSD.string)


class TableToOntologyStrategy(InductionStrategy):
    """Original strategy: embed table/column names, match via cosine similarity."""

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
        """Induce an ontology by embedding table/column names and matching by similarity.

        Args:
            tables: Source table metadata to induce from.
            ontology_uri_prefix: Base IRI prefix for minting induced terms.
            config: Strategy configuration.
            pipeline: Owning pipeline, used for extract/embed/match/build helpers.
            confidence_threshold: Minimum similarity to accept a high-confidence match.
            embedding_backend: Optional embedding backend override.
            scoring_strategy: "lexical" or "structural_fusion" scoring.
            structural_weight: Weight of the structural signal when fusing scores.
            grounding_ontology_ids: Optional ontology ids to restrict grounding to.
            grounding_mode: Grounding intensity ("NONE", "STANDARD", or "ENHANCED").
            cost_tracker: Optional per-job Bedrock usage accumulator.

        Returns:
            A tuple of the proposal graph, the set of novel table names, and the
            list of concept matches.
        """
        # Stream fusion by TABLE batch to bound peak memory. The fused
        # per-column vector (1024-dim Cohere embed-v4, plain list[float] ≈ 32 KB)
        # is resident from embed_concepts through match_concepts; embedding ALL
        # columns at once (~886k vectors ≈ 28 GB at 50k tables) OOM-killed the
        # 32 GB task. Processing tables in fixed-size batches through
        # embed→match→free caps resident vectors at O(batch), independent of
        # catalog size, then we build the graph ONCE from all tables + all matches.
        #
        # Behavior-preserving: match_concepts has no cross-table dependency —
        # table-level grounding hits the EXTERNAL AOSS foundational pool
        # (grounding_svc.ground_table) and column-level matching reads only its
        # OWN table's grounding result (table_ontology[concept.table_name]),
        # both computed within the same call; and embed_concepts uses store=False
        # so no batch writes embeddings a later batch could recall. So a table's
        # matches are identical whether it is processed alone, in a batch, or with
        # the whole catalog. detect_pk_sharing_subtypes stays global — it runs in
        # _build_proposal_ontology below, which still receives the full tables list.
        #
        # INDUCER_TABLE_BATCH_SIZE is the tuning lever. Default 500
        # tables/batch ≈ 8.4k columns × 32 KB ≈ 270 MB peak — huge margin under
        # 32 GB. Lower it if per-column fan-out or vector dim grows.
        from coa_ontology.inducer.schemas import ConceptMatch as CM

        batch_size = int(os.getenv("INDUCER_TABLE_BATCH_SIZE", str(DEFAULT_TABLE_BATCH_SIZE)))
        all_matches: list[ConceptMatch] = []
        # match_concepts records the ColumnMatch stage duration into cost_tracker
        # once per call, and record_stage_duration is last-writer-wins (stores,
        # never sums). Batching calls it once per batch, so each batch would
        # OVERWRITE the prior value and ColumnMatchDurationMs would report
        # only the LAST batch (~1/N of the truth). Read each batch's value back
        # and re-write the SUM after the loop.
        total_column_match_ms = 0.0

        for batch_tuple in batched(tables, batch_size):
            table_batch = list(batch_tuple)
            # Drops the PREVIOUS batch's concept objects (vectors already emptied
            # below) and keeps `concepts` bound for the free-loop even if
            # extract_concepts raises.
            concepts: list = []

            # Zero ColumnMatch before the batch runs so the read-back below
            # captures ONLY this batch's recording (last-writer-wins overwrites).
            # A fallback batch that never records then reads back 0, not the prior
            # batch's stale value — so no double-count.
            if cost_tracker is not None:
                cost_tracker.record_stage_duration("ColumnMatch", 0.0)

            # extract → embed → match share ONE try: a failure in extract or embed
            # must be handled with the SAME policy as a match failure. Left outside,
            # it escaped the loop mid-catalog and the caller built an ontology from
            # the partially-filled all_matches — a silently corrupt partial result.
            try:
                concepts = pipeline.extract_concepts(table_batch)
                concepts, model_id = pipeline.embed_concepts(concepts, backend=embedding_backend)
                matches = pipeline.match_concepts(
                    concepts,
                    confidence_threshold,
                    model_id=model_id,
                    scoring_strategy=scoring_strategy,
                    structural_weight=structural_weight,
                    grounding_ontology_ids=grounding_ontology_ids,
                    grounding_mode=grounding_mode,
                    tables=table_batch,
                    cost_tracker=cost_tracker,
                )
            except (OpenSearchException, httpx.HTTPError):
                # Re-raise search/transport failures so an incomplete recall or a
                # failed embed fails the WHOLE job instead of silently dropping a
                # batch (which would degrade to a corrupt partial-novel ontology).
                # Catch the base OpenSearchException, not just TransportError:
                # SerializationError (a 2xx with an unparseable/truncated body) is a
                # real recall failure that does not subclass TransportError. The
                # legacy httpx catalog client and the Bedrock embed path raise
                # httpx.HTTPError.
                raise
            except MemoryError:
                # MemoryError subclasses Exception, so the broad handler below would
                # swallow it. At 50k-table scale an OOM is the failure this batching
                # exists to prevent — it must fail loud, not degrade to all-novel.
                raise
            except Exception:
                # Tolerate non-transport errors (a programming bug): fall back to
                # all-novel FOR THIS BATCH ONLY so the job completes, but log the
                # real exception so the failure is diagnosable rather than
                # masquerading as "nothing grounded". Other batches are unaffected.
                # Built from table_batch, not `concepts`, so it is also safe when
                # extract_concepts raised before `concepts` was assigned — the
                # concept set extract_concepts emits is exactly one table-level
                # concept (column_name "") plus one per column.
                log.exception(
                    "batch induction failed — falling back to all-novel for this batch "
                    "(grounding_mode=%s, n_tables=%d)",
                    grounding_mode,
                    len(table_batch),
                )
                matches = [
                    CM(
                        source_column=column_name,
                        source_table=table.name,
                        matched_class_uri=None,
                        matched_ontology_id=None,
                        similarity=None,
                        match_type="novel",
                    )
                    for table in table_batch
                    for column_name in ("", *(col.name for col in table.columns))
                ]

            # Accumulate this batch's ColumnMatch duration (zeroed above, so this
            # is exactly what match_concepts recorded for this batch — 0 on a
            # fallback batch that never ran the column-match pool).
            if cost_tracker is not None:
                total_column_match_ms += cost_tracker.stage_durations_ms.get("ColumnMatch", 0.0)

            # Free THIS batch's per-column embedding vectors before the next batch
            # embeds: nothing downstream (proposal graph, R2RML, report,
            # fingerprint) reads them. This per-batch freeing is what keeps peak
            # resident vectors O(batch) rather than O(catalog). Covers both the
            # matched path and the all-novel fallback above.
            for concept in concepts:
                concept.vector = []
            all_matches.extend(matches)

        # Overwrite the last-batch ColumnMatch value with the cross-batch SUM so
        # ColumnMatchDurationMs reports the true aggregate wall-clock.
        if cost_tracker is not None:
            cost_tracker.record_stage_duration("ColumnMatch", total_column_match_ms)

        # Build proposal ontology once, from ALL tables + ALL matches (no vectors
        # resident). match_map is a (table, column) dict, so match order is
        # irrelevant, but batches stay grouped for cleanliness.
        proposal_graph, novel_tables = self._build_proposal_ontology(
            ontology_uri_prefix,
            tables,
            all_matches,
        )
        return proposal_graph, novel_tables, all_matches

    def _build_proposal_ontology(
        self,
        uri_prefix: str,
        tables: list[CatalogTable],
        matches: list[ConceptMatch],
    ) -> tuple[Graph, set[str]]:
        g = Graph()
        ns = Namespace(uri_prefix)
        g.bind("ind", ns)
        g.bind("owl", OWL)
        g.bind("rdfs", RDFS)
        g.bind("xsd", XSD)
        g.bind("skos", SKOS)

        ont_uri = URIRef(uri_prefix.rstrip("#").rstrip("/"))
        g.add((ont_uri, RDF.type, OWL.Ontology))
        g.add((ont_uri, RDFS.label, Literal("Proposal Ontology")))

        g.bind("scl", SCL)

        # matchConfidence annotation property — records grounding similarity score
        match_confidence = ns["matchConfidence"]
        g.add((match_confidence, RDF.type, OWL.AnnotationProperty))

        match_map = {(m.source_table, m.source_column): m for m in matches}
        pk_sharing_confirmed, pk_sharing_suggested = detect_pk_sharing_subtypes(tables)
        novel_tables = set()

        # Collision-free class local names, shared with build_r2rml (base.py) and
        # the SHACL config generator so all three artifacts name the same class
        # identically. Bare _to_pascal would fuse tables differing only in
        # separators/case (order_item vs order-item) onto one class IRI.
        pascal_by_name = pascal_names_for(t.name for t in tables)
        # Property local names are derived from the class local name (not from
        # _to_camel(table.name)) so a discriminated class carries discriminated
        # property IRIs too — otherwise the two fused tables' properties would
        # still collide even though their classes no longer do.
        camel_by_name = {n: p[0].lower() + p[1:] if p else p for n, p in pascal_by_name.items()}

        def table_prop(table_name: str, column_name: str) -> URIRef:
            """Mint the property IRI for ``table_name.column_name``."""
            return ns[f"{camel_by_name.get(table_name, _to_camel(table_name))}_{_to_camel(column_name)}"]

        for table in tables:
            tm = match_map.get((table.name, ""))
            is_grounded = tm and tm.match_type in ("exact", "high_confidence")

            if not is_grounded:
                novel_tables.add(table.name)

            table_cls = ns[pascal_by_name[table.name]]
            g.add((table_cls, RDF.type, OWL.Class))
            g.add((table_cls, RDFS.label, Literal(table.name)))
            if table.description:
                g.add((table_cls, RDFS.comment, Literal(table.description)))
            # Synonyms → skos:altLabel so they persist in the graph and flow
            # into the accept-time embedding text (ingest.py _class_text_for).
            for syn in table.synonyms:
                g.add((table_cls, SKOS.altLabel, Literal(syn)))

            if is_grounded and tm and tm.matched_class_uri:
                grounding_cls = URIRef(tm.matched_class_uri)
                # Grounding is carried by rdfs:subClassOf + skos:exactMatch/
                # closeMatch (+ matchConfidence) below. We intentionally do NOT
                # emit the custom coa:groundedTo — it was redundant with those
                # axioms, and this matches how the RIGOR path grounds (subClassOf
                # only).
                g.add((table_cls, RDFS.subClassOf, grounding_cls))
                if tm.match_type == "exact":
                    g.add((table_cls, SKOS.exactMatch, grounding_cls))
                else:
                    g.add((table_cls, SKOS.closeMatch, grounding_cls))
                if tm.similarity is not None:
                    g.add((table_cls, match_confidence, Literal(tm.similarity, datatype=XSD.float)))
            elif tm and tm.match_type == "ambiguous" and tm.matched_class_uri:
                g.add((table_cls, SKOS.relatedMatch, URIRef(tm.matched_class_uri)))
                if tm.similarity is not None:
                    g.add((table_cls, match_confidence, Literal(tm.similarity, datatype=XSD.float)))

            # Emit owl:hasKey for primary key columns
            if table.tableConstraints:
                for tc in table.tableConstraints:
                    if tc.constraintType == "PRIMARY_KEY" and tc.columns:
                        from rdflib.collection import Collection

                        key_props: list = [table_prop(table.name, c) for c in tc.columns]
                        key_bnode = BNode()
                        Collection(g, key_bnode, key_props)
                        g.add((table_cls, OWL.hasKey, key_bnode))
                        break

            for col in table.columns:
                prop_uri = table_prop(table.name, col.name)
                is_fk = False
                fk_target = None
                fk_target_col = None
                fk_provenance: str | None = None
                if table.tableConstraints:
                    for tc in table.tableConstraints:
                        if tc.constraintType == "FOREIGN_KEY" and col.name in tc.columns and tc.referredColumns:
                            is_fk = True
                            parts = tc.referredColumns[0].split(".")
                            fk_target = parts[-2] if len(parts) >= 2 else parts[0]
                            fk_target_col = parts[-1] if len(parts) >= 2 else None
                            fk_provenance = tc.relationshipType
                            break

                if is_fk and fk_target:
                    # In-run tables use the shared (collision-resolved) name;
                    # a target outside this run falls back to the bare form.
                    parent_cls = ns[pascal_by_name.get(fk_target, _to_pascal(fk_target))]
                    if (table.name, fk_target) in pk_sharing_confirmed:
                        g.add((table_cls, RDFS.subClassOf, parent_cls))
                        g.add((table_cls, SCL.subClassProvenance, Literal("PK_SHARING")))
                        # Do NOT ``continue`` here: for the PK-sharing pattern the
                        # child's FK column IS its PK column, so it appears in the
                        # child's own owl:hasKey list. Skipping the property
                        # declaration below would leave owl:hasKey referencing an
                        # undeclared property (OWL-DL structural inconsistency —
                        # fails Tier-1/OoPS/reasoner). Fall through so the key
                        # column still gets its owl:ObjectProperty declaration,
                        # rdfs:label, and cardinality axioms.
                    if (table.name, fk_target) in pk_sharing_suggested:
                        g.add((table_cls, SCL.suggestedSubClassOf, parent_cls))
                        g.add((table_cls, SCL.suggestionReason, Literal("PK_SHARING_SINGLE_CHILD")))
                    g.add((prop_uri, RDF.type, OWL.ObjectProperty))
                    g.add((prop_uri, RDFS.domain, table_cls))
                    g.add((prop_uri, RDFS.range, parent_cls))
                    if fk_provenance:
                        g.add((prop_uri, SCL.fkProvenance, Literal(fk_provenance)))
                    fk_comment = f"Foreign key: {table.name}.{col.name} references {fk_target}.{fk_target_col or 'id'}"
                    g.add((prop_uri, RDFS.comment, Literal(fk_comment)))
                else:
                    g.add((prop_uri, RDF.type, OWL.DatatypeProperty))
                    g.add((prop_uri, RDFS.domain, table_cls))
                    g.add((prop_uri, RDFS.range, _xsd_for(col.dataType, col.name)))

                g.add((prop_uri, RDFS.label, Literal(col.name)))
                if col.description and not (is_fk and fk_target):
                    g.add((prop_uri, RDFS.comment, Literal(col.description)))
                # Column synonyms → skos:altLabel (persisted in the graph).
                for syn in col.synonyms:
                    g.add((prop_uri, SKOS.altLabel, Literal(syn)))
                # Sampled distinct values (low-cardinality categorical columns) →
                # coa:distinctValues, one literal per value. Read by the ingest text
                # builder (_class_text_for) so serve's NL→SQL context can hint the
                # LLM with correct enum literals for WHERE clauses.
                for val in getattr(col, "distinctValues", []) or []:
                    g.add((prop_uri, SCL.distinctValues, Literal(val)))

                # Column-level alignment: if this column matched a foundational property
                cm = match_map.get((table.name, col.name))
                if cm and cm.match_type in ("exact", "high_confidence") and cm.matched_class_uri:
                    matched_prop = URIRef(cm.matched_class_uri)
                    g.add((prop_uri, OWL.equivalentProperty, matched_prop))
                    if cm.similarity is not None:
                        g.add((prop_uri, match_confidence, Literal(cm.similarity, datatype=XSD.float)))

                # Cardinality restrictions from column constraints
                is_not_null = col.constraint in ("NOT_NULL", "PRIMARY_KEY")
                is_unique = col.constraint in ("UNIQUE", "PRIMARY_KEY")
                if not is_unique and table.tableConstraints:
                    for tc in table.tableConstraints:
                        if tc.constraintType == "UNIQUE" and col.name in tc.columns and len(tc.columns) == 1:
                            is_unique = True
                            break

                if is_not_null and is_unique:
                    restriction = self._cardinality_restriction(g, prop_uri, OWL.cardinality, 1)
                    g.add((table_cls, RDFS.subClassOf, restriction))
                elif is_not_null:
                    restriction = self._cardinality_restriction(g, prop_uri, OWL.minCardinality, 1)
                    g.add((table_cls, RDFS.subClassOf, restriction))
                elif is_unique:
                    restriction = self._cardinality_restriction(g, prop_uri, OWL.maxCardinality, 1)
                    g.add((table_cls, RDFS.subClassOf, restriction))

        # Do NOT emit owl:imports: Ontop network-resolves it at VKG load and fails
        # every query on a non-dereferenceable foundational URI (Ontop #337).
        # Grounding is carried by rdfs:subClassOf + skos:exactMatch/closeMatch above.

        return g, novel_tables

    @staticmethod
    def _cardinality_restriction(g: Graph, prop_uri: URIRef, cardinality_type: URIRef, value: int) -> BNode:
        restriction = BNode()
        g.add((restriction, RDF.type, OWL.Restriction))
        g.add((restriction, OWL.onProperty, prop_uri))
        g.add((restriction, cardinality_type, Literal(value, datatype=XSD.nonNegativeInteger)))
        return restriction
