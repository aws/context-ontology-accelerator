# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Core induction pipeline.

Steps:
  1. Fetch table metadata from data-catalog
  2. Build source concepts (column name + description → text for embedding)
  3. Generate lexical embeddings for source concepts via embedding-generator
  4. VSS against ontology-catalog to find grounding matches
  5. Build OWL ontology (rdflib): matched → imports/alignment, unmatched → novel classes
  6. Store induced ontology + new embeddings back to ontology-catalog
"""

from __future__ import annotations

import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from coa_common.bedrock_metrics import CostTracker
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SKOS

from coa_ontology.inducer.schemas import ConceptMatch, InductionReport
from coa_ontology.inducer.services.data_catalog import CatalogTable, DataCatalogClient
from coa_ontology.inducer.services.embedding_generator import EmbeddingGeneratorClient
from coa_ontology.inducer.services.grounding import GroundingService
from coa_ontology.inducer.services.ontology_catalog import OntologyCatalogClient
from coa_ontology.inducer.strategies.base import table_identity as catalog_table_identity

# Mapping from data-catalog SQL types to XSD datatypes
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


def _grounding_workers() -> int:
    """First-pass grounding/rerank ThreadPool width (default 24).

    Stays under the shared bedrock-runtime client's connection cap (grounding.py
    _BEDROCK_MAX_POOL_CONNECTIONS = 32) — workers > that cap makes threads starve
    on connections. ``INDUCER_GROUNDING_WORKERS`` stays as an escape hatch for
    tuning without a code change.
    """
    return int(os.getenv("INDUCER_GROUNDING_WORKERS", "24"))


@dataclass
class SourceConcept:
    """A concept extracted from the source schema to be matched."""

    table_fqn: str
    table_name: str
    column_name: str
    description: str
    data_type: str
    constraint: str | None = None
    is_table_level: bool = False  # True if this represents the table itself, not a column
    embedding_text: str = ""
    vector: list[float] = field(default_factory=list)
    source_table_identity: str = ""


def _concept_identity(
    concept: SourceConcept,
    legacy_table_identities: dict[str, str] | None = None,
) -> str:
    """Return the stable table key, with fallbacks for legacy test fixtures."""
    if concept.source_table_identity:
        return concept.source_table_identity
    legacy_key = concept.table_fqn or concept.table_name
    return (legacy_table_identities or {}).get(legacy_key, legacy_key)


def _legacy_table_identity_map(tables: list[CatalogTable]) -> dict[str, str]:
    """Map unambiguous legacy table names and FQNs to canonical identities."""
    candidates: dict[str, set[str]] = {}
    for table in tables:
        identity = catalog_table_identity(table)
        for legacy_key in (table.fullyQualifiedName, table.name):
            if legacy_key:
                candidates.setdefault(legacy_key, set()).add(identity)
    return {legacy_key: next(iter(identities)) for legacy_key, identities in candidates.items() if len(identities) == 1}


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    norm_a = math.sqrt(sum(a[i] * a[i] for i in range(n)))
    norm_b = math.sqrt(sum(b[i] * b[i] for i in range(n)))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class InductionPipeline:
    """Reference table-to-ontology pipeline: fetch, embed, match, build, and store.

    Wraps the data-catalog, embedding-generator, and ontology-catalog clients to turn
    catalog tables into an induced OWL ontology with grounding matches.
    """

    def __init__(
        self,
        data_catalog: DataCatalogClient,
        embedding_generator: EmbeddingGeneratorClient,
        ontology_catalog: OntologyCatalogClient,
    ):
        """Wire up the data-catalog, embedding-generator, and ontology-catalog clients.

        Args:
            data_catalog: Client used to fetch source table metadata.
            embedding_generator: Client used to embed source concepts.
            ontology_catalog: Client used to match against and store ontologies.
        """
        self.dc = data_catalog
        self.eg = embedding_generator
        self.oc = ontology_catalog

    # ── Step 1: Fetch metadata ──────────────────────────────────────

    def fetch_tables(self, table_ids: list[str]) -> list[CatalogTable]:
        """Fetch metadata for the given catalog table ids.

        Args:
            table_ids: Catalog table ids or fully qualified names to fetch.

        Returns:
            The fetched :class:`CatalogTable` objects, in input order.
        """
        return [self.dc.get_table(tid) for tid in table_ids]

    # ── Step 2: Extract source concepts ─────────────────────────────

    def extract_concepts(self, tables: list[CatalogTable]) -> list[SourceConcept]:
        """Derive table- and column-level source concepts with embedding text.

        Args:
            tables: Catalog tables to extract concepts from.

        Returns:
            One :class:`SourceConcept` per table plus one per column.
        """
        concepts = []
        for table in tables:
            source_table_identity = catalog_table_identity(table)
            # Table-level concept
            table_text = f"{table.name}"
            if table.description:
                table_text += f" {table.description}"
            if table.synonyms:
                table_text += f" {' '.join(table.synonyms)}"
            concepts.append(
                SourceConcept(
                    table_fqn=table.fullyQualifiedName,
                    table_name=table.name,
                    column_name="",
                    description=table.description or "",
                    data_type="",
                    is_table_level=True,
                    embedding_text=table_text,
                    source_table_identity=source_table_identity,
                )
            )
            # Column-level concepts
            for col in table.columns:
                col_text = col.name.replace("_", " ")
                if col.description:
                    col_text += f" {col.description}"
                if col.synonyms:
                    col_text += f" {' '.join(col.synonyms)}"
                if col.dataType:
                    col_text += f" {col.dataType}"
                concepts.append(
                    SourceConcept(
                        table_fqn=table.fullyQualifiedName,
                        table_name=table.name,
                        column_name=col.name,
                        description=col.description or "",
                        data_type=col.dataType,
                        constraint=col.constraint,
                        embedding_text=col_text,
                        source_table_identity=source_table_identity,
                    )
                )
        return concepts

    # ── Step 3: Generate embeddings ─────────────────────────────────

    def embed_concepts(
        self,
        concepts: list[SourceConcept],
        backend: str | None = None,
    ) -> tuple[list[SourceConcept], str]:
        """Compute and attach lexical embedding vectors to source concepts.

        Args:
            concepts: Table- and column-level concepts to embed (mutated in place).
            backend: Optional embedding backend override; defaults to the service default.

        Returns:
            The (mutated) concepts and the model id that produced the embeddings.
        """
        # Table-level concepts: single-shot embedding (name + description)
        table_concepts = [c for c in concepts if c.is_table_level]
        col_concepts = [c for c in concepts if not c.is_table_level]

        # Embed tables as before
        if table_concepts:
            table_inputs = [
                {"class_uri": f"source:{c.table_name}", "labels": [c.embedding_text], "comments": []}
                for c in table_concepts
            ]
            results, model_id = self.eg.generate_lexical(
                ontology_id="",
                classes=table_inputs,
                backend=backend,
                store=False,
            )
            for concept, result in zip(table_concepts, results, strict=False):
                concept.vector = result["vector"]
        else:
            model_id = ""

        # Embed columns via multi-channel fusion:
        #   0.6 × name  +  0.2 × description  +  0.2 × dataType
        if col_concepts:
            channels = {
                "name": (
                    [
                        {
                            "class_uri": f"source:{c.table_name}.{c.column_name}",
                            "labels": [c.column_name.replace("_", " ")],
                            "comments": [],
                        }
                        for c in col_concepts
                    ],
                    0.6,
                ),
                "desc": (
                    [
                        {
                            "class_uri": f"source:{c.table_name}.{c.column_name}",
                            "labels": [c.description or c.column_name.replace("_", " ")],
                            "comments": [],
                        }
                        for c in col_concepts
                    ],
                    0.2,
                ),
                "dtype": (
                    [
                        {
                            "class_uri": f"source:{c.table_name}.{c.column_name}",
                            "labels": [c.data_type or "unknown"],
                            "comments": [],
                        }
                        for c in col_concepts
                    ],
                    0.2,
                ),
            }

            # Incremental fusion to bound peak memory: fetch ONE channel at a
            # time, fold its weighted contribution into each concept's fused
            # vector, then drop the channel payload before the next fetch. Only
            # one channel's vectors are resident at a time (plus the accumulating
            # fused vectors) — vs. all three coexisting under the old dict.
            #
            # Accumulation order is PINNED name → desc → dtype (channels dict
            # insertion order). Python float addition is NOT associative, so this
            # must match the historical dict-iteration order to keep every fused
            # vector bit-identical to the previous implementation.
            #
            # model_id is reassigned on each call, so the LAST channel (dtype)
            # wins — preserving the prior return value.
            for ch_idx, (_ch_name, (inputs, weight)) in enumerate(channels.items()):
                results, model_id = self.eg.generate_lexical(
                    ontology_id="",
                    classes=inputs,
                    backend=backend,
                    store=False,
                )
                for i, concept in enumerate(col_concepts):
                    vec = results[i]["vector"]
                    if ch_idx == 0:
                        concept.vector = [0.0] * len(vec)
                    fused = concept.vector
                    for j, v in enumerate(vec):
                        fused[j] += weight * v
                del results  # free this channel's payload before the next fetch

        return concepts, model_id

    # ── Step 4: Match against ontology catalog ──────────────────────

    def _get_candidates(
        self,
        concept: SourceConcept,
        model_id: str,
        entity_type: str,
        ontology_id: str | None = None,
        grounding_ontology_ids: list[str] | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """Return top-K catalog results with lexical cosine scores.

        Search-scope precedence:

        1. ``ontology_id`` (singular) — narrowest scope, used when a column
           concept's parent table already matched a specific ontology and we
           want to find properties only inside that ontology.
        2. ``grounding_ontology_ids`` (list) — when the user picked a list of
           foundational ontologies to ground against, restrict matches to
           that union. Implemented as a sequential per-id search + union;
           dedup by ``entity_uri`` and re-sort by cosine similarity. Loop
           is fine for ≤10 ontologies; an OpenSearch ``terms`` filter is
           filed as follow-up.

        If both ``ontology_id`` and ``grounding_ontology_ids`` are provided,
        ``ontology_id`` wins because it's strictly narrower.

        An empty grounding pool (``grounding_ontology_ids`` is ``[]`` or
        ``None``, with no singular ``ontology_id``) means the caller resolved a
        grounding pool and it is empty → return no candidates. We do NOT fall
        back to a namespace-wide search: that fallback would match a
        loaded-but-unselected ontology and defeat the user's choice not to
        ground against it. Grounding is always scoped to exactly the pool the
        caller provides; there is no unscoped/global recall.
        """
        if not concept.vector:
            return []
        # Singular scope wins
        if ontology_id is not None:
            return self._search_one(concept, model_id, entity_type, ontology_id, top_k)
        # Empty grounding pool ([] or None) → no grounding (see docstring).
        if not grounding_ontology_ids:
            return []
        # List scope — search each, union, dedup, re-sort, top_k
        seen: dict[str, dict] = {}
        for oid in grounding_ontology_ids:
            for cand in self._search_one(concept, model_id, entity_type, oid, top_k):
                uri = cand["entity_uri"]
                if uri not in seen or cand["lexical_sim"] > seen[uri]["lexical_sim"]:
                    seen[uri] = cand
        ranked = sorted(seen.values(), key=lambda c: c["lexical_sim"], reverse=True)
        return ranked[:top_k]

    def _search_one(
        self,
        concept: SourceConcept,
        model_id: str,
        entity_type: str,
        ontology_id: str | None,
        top_k: int,
    ) -> list[dict]:
        """Single-ontology embedding search. Caller handles scoping.

        Lets ``search_embeddings`` errors propagate so an incomplete recall fails
        the job rather than misclassifying a real class as ``novel``. A
        successful-but-empty search yields no candidates and classifies novel.
        """
        if not concept.vector:
            return []
        results = self.oc.search_embeddings(
            vector=concept.vector,
            embedding_type="lexical",
            model_id=model_id,
            entity_type=entity_type,
            ontology_id=ontology_id,
            top_k=top_k,
        )
        candidates = []
        for r in results:
            if not r.get("vector"):
                continue
            sim = _cosine_similarity(concept.vector, r["vector"])
            candidates.append(
                {
                    "entity_uri": r["entity_uri"],
                    "ontology_id": r.get("ontology_id"),
                    "lexical_sim": sim,
                    "vector": r["vector"],
                }
            )
        return candidates

    def _classify(self, sim: float, confidence_threshold: float) -> str:
        if sim >= 0.95:
            return "exact"
        if sim >= confidence_threshold:
            return "high_confidence"
        if sim >= 0.50:
            return "ambiguous"
        return "novel"

    def _pick_winner(
        self,
        concept: SourceConcept,
        candidates: list[dict],
        confidence_threshold: float,
        strategy: str,
        source_table_identity: str | None = None,
    ) -> ConceptMatch:
        """Select the best candidate using the given scoring strategy."""
        from coa_ontology.inducer.schemas import MatchCandidate

        identity = source_table_identity or _concept_identity(concept)
        if not candidates:
            return ConceptMatch(
                source_column=concept.column_name,
                source_table=concept.table_name,
                source_table_identity=identity,
                matched_class_uri=None,
                matched_ontology_id=None,
                similarity=None,
                match_type="novel",
                scoring_strategy=strategy,
            )

        # Score field used for ranking depends on strategy
        score_key = "fused_score" if strategy != "lexical" else "lexical_sim"
        for c in candidates:
            c.setdefault("fused_score", c["lexical_sim"])

        best = max(candidates, key=lambda c: c[score_key])
        sim = best["lexical_sim"]
        match_type = self._classify(best.get("fused_score", sim), confidence_threshold)

        return ConceptMatch(
            source_column=concept.column_name,
            source_table=concept.table_name,
            source_table_identity=identity,
            matched_class_uri=best["entity_uri"],
            matched_ontology_id=best["ontology_id"],
            similarity=sim,
            match_type=match_type,
            scoring_strategy=strategy,
            candidates=[
                MatchCandidate(
                    entity_uri=c["entity_uri"],
                    ontology_id=c["ontology_id"],
                    lexical_sim=c["lexical_sim"],
                    structural_sim=c.get("structural_sim"),
                    fused_score=c.get("fused_score"),
                )
                for c in sorted(candidates, key=lambda c: c[score_key], reverse=True)
            ],
        )

    def _apply_structural_scores(
        self,
        candidates: list[dict],
        class_uri: str,
        structural_weight: float,
    ):
        """Enrich candidates with structural similarity and compute fused scores.

        Uses direct cosine similarity between the class's structural vector
        and each candidate property's structural vector, rather than rank-based
        scoring.  This gives a continuous signal: Product-domain properties
        score ~0.4-0.6, Thing-domain properties score ~0.1-0.3, and unrelated
        properties score near 0.
        """
        class_vec = None
        try:
            results = self.oc.get_embeddings_for_entity(
                class_uri,
                embedding_type="structural",
            )
            if results:
                class_vec = results[0]["vector"]
                results[0]["model_id"]
        except Exception:
            pass

        if not class_vec:
            return

        for c in candidates:
            try:
                prop_results = self.oc.get_embeddings_for_entity(
                    c["entity_uri"],
                    embedding_type="structural",
                )
                if prop_results:
                    struct_sim = _cosine_similarity(class_vec, prop_results[0]["vector"])
                    c["structural_sim"] = struct_sim
                    c["fused_score"] = (1 - structural_weight) * c["lexical_sim"] + structural_weight * struct_sim
                    continue
            except Exception:
                pass
            # No structural embedding — fall back to lexical only
            c["structural_sim"] = None
            c["fused_score"] = (1 - structural_weight) * c["lexical_sim"]

    def match_concepts(
        self,
        concepts: list[SourceConcept],
        confidence_threshold: float,
        model_id: str,
        scoring_strategy: str = "lexical",
        structural_weight: float = 0.3,
        grounding_ontology_ids: list[str] | None = None,
        grounding_mode: str = "ENHANCED",
        tables: list[CatalogTable] | None = None,
        cost_tracker: CostTracker | None = None,
        rerank_max_tokens: int = 1000,
    ) -> list[ConceptMatch]:
        """Ground each source concept against the ontology catalog.

        Delegates to the grounding service to recall and rerank candidate foundational
        entities, producing a match tier (exact, high-confidence, ambiguous, or novel)
        per concept.

        Args:
            concepts: Embedded source concepts to ground.
            confidence_threshold: Minimum similarity to accept a high-confidence match.
            model_id: Embedding model id, used to select the catalog index.
            scoring_strategy: "lexical" or "structural_fusion" scoring.
            structural_weight: Weight of the structural signal when fusing scores.
            grounding_ontology_ids: Optional ontology ids to restrict grounding to.
            grounding_mode: Grounding intensity ("NONE", "STANDARD", or "ENHANCED").
            tables: Source tables, used to supply per-table grounding metadata.
            cost_tracker: Optional tracker for Bedrock usage and stage timings.
            rerank_max_tokens: Output-token cap for the ENHANCED-mode LLM rerank.

        Returns:
            One :class:`ConceptMatch` per successfully processed concept.
        """
        matches: list[ConceptMatch | None] = [None] * len(concepts)

        # Build table metadata lookup for the grounding service
        table_meta: dict[str, CatalogTable] = {}
        legacy_table_identities: dict[str, str] = {}
        if tables:
            table_meta = {catalog_table_identity(t): t for t in tables}
            legacy_table_identities = _legacy_table_identity_map(tables)

        # First pass: match table-level concepts via centralized grounding service (parallel)
        grounding_svc = GroundingService(
            ontology_catalog=self.oc,
            embedding_generator=self.eg,
            cost_tracker=cost_tracker,
        )
        table_ontology: dict[str, str | None] = {}
        table_class_uri: dict[str, str | None] = {}

        table_concepts = [(i, c) for i, c in enumerate(concepts) if c.is_table_level]

        def _ground_one(concept: SourceConcept) -> ConceptMatch:
            identity = _concept_identity(concept, legacy_table_identities)
            tbl = table_meta.get(identity)
            columns_for_grounding = [
                {"name": c.name, "dataType": c.dataType, "description": c.description, "constraint": c.constraint}
                for c in (tbl.columns if tbl else [])
            ]
            result = grounding_svc.ground_table(
                table_name=concept.table_name,
                table_description=concept.description,
                columns=columns_for_grounding,
                concept_vector=concept.vector,
                model_id=model_id,
                confidence_threshold=confidence_threshold,
                grounding_mode=grounding_mode,
                ontology_ids=grounding_ontology_ids,
                rerank_max_tokens=rerank_max_tokens,
            )
            match = grounding_svc.to_concept_match(result)
            match.source_table_identity = identity
            return match

        grounding_workers = _grounding_workers()
        # ponytail: worker cap (24); coupled to the shared bedrock client's
        # connection pool — workers must stay <= _BEDROCK_MAX_POOL_CONNECTIONS (32)
        # in grounding.py or threads starve on connections. Raise
        # INDUCER_GROUNDING_WORKERS (and the pool cap in lockstep) if rerank shows
        # throttle headroom.
        with ThreadPoolExecutor(max_workers=min(grounding_workers, len(table_concepts) or 1)) as executor:
            grounding_results = list(executor.map(_ground_one, [c for _, c in table_concepts]))

        for (idx, concept), match in zip(table_concepts, grounding_results, strict=True):
            matches[idx] = match
            identity = _concept_identity(concept, legacy_table_identities)
            table_ontology[identity] = match.matched_ontology_id
            table_class_uri[identity] = match.matched_class_uri

        # Second pass: match columns against property embeddings, in parallel.
        # Each column is an independent embedding-search + cosine + _pick_winner
        # (no LLM/Bedrock, so no rate concern) — a bounded pool mirrors the
        # first-pass grounding pool above. Results are written BY INDEX so the
        # output is independent of completion order. Time the whole pool as the
        # ColumnMatch stage — emitted as
        # ColumnMatchDurationMs via the per-job CostTracker, same rail as
        # FetchMetadata/GroundingLoad/Induce/Store.
        col_items = [(i, c) for i, c in enumerate(concepts) if not c.is_table_level]
        workers = int(os.getenv("INDUCER_COLUMN_MATCH_WORKERS", "16"))
        # ponytail: fixed worker cap; raise INDUCER_COLUMN_MATCH_WORKERS if AOSS
        # shows headroom. oss_retry backoff self-regulates any blip.

        def _match_one(concept):
            # Read-only over the captured dicts (no shared mutable state), so it
            # is safe to run concurrently. Returns the match; the caller re-pairs
            # by input order so output is independent of completion order.
            identity = _concept_identity(concept, legacy_table_identities)
            scoped_onto_id = table_ontology.get(identity)

            candidates = []
            if scoped_onto_id is not None:
                candidates = self._get_candidates(
                    concept,
                    model_id,
                    "property",
                    ontology_id=scoped_onto_id,
                )
            # Fall back to grounding union (or global) if scoped found nothing.
            if not candidates or max(c["lexical_sim"] for c in candidates) < 0.50:
                candidates = self._get_candidates(
                    concept,
                    model_id,
                    "property",
                    grounding_ontology_ids=grounding_ontology_ids,
                )

            # Apply structural scoring if requested
            if scoring_strategy == "structural_fusion" and candidates:
                class_uri = table_class_uri.get(identity)
                if class_uri:
                    self._apply_structural_scores(
                        candidates,
                        class_uri,
                        structural_weight,
                    )

            return self._pick_winner(
                concept,
                candidates,
                confidence_threshold,
                scoring_strategy,
                source_table_identity=identity,
            )

        _col_match_t0 = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=min(workers, len(col_items) or 1)) as ex:
                col_matches = list(ex.map(_match_one, [c for _, c in col_items]))
            for (idx, _), match in zip(col_items, col_matches, strict=True):
                matches[idx] = match
        finally:
            if cost_tracker is not None:
                cost_tracker.record_stage_duration("ColumnMatch", (time.perf_counter() - _col_match_t0) * 1000)

        return [m for m in matches if m is not None]

    # ── Step 5: Build OWL ontology ──────────────────────────────────

    def build_ontology(
        self,
        uri_prefix: str,
        tables: list[CatalogTable],
        concepts: list[SourceConcept],
        matches: list[ConceptMatch],
    ) -> Graph:
        """Build an OWL RDF graph from tables, concepts, and grounding matches.

        Matched concepts emit alignment/subclass axioms to their foundational classes;
        unmatched concepts become novel classes and properties.

        Args:
            uri_prefix: Namespace prefix for minting induced URIs.
            tables: Source tables providing columns and constraints.
            concepts: Source concepts aligned with ``matches``.
            matches: Grounding matches aligned positionally with ``concepts``.

        Returns:
            The populated :class:`rdflib.Graph`.
        """
        g = Graph()
        ns = Namespace(uri_prefix)
        g.bind("ind", ns)
        g.bind("owl", OWL)
        g.bind("rdfs", RDFS)
        g.bind("xsd", XSD)
        g.bind("skos", SKOS)

        # Ontology declaration
        ont_uri = URIRef(uri_prefix.rstrip("#").rstrip("/"))
        g.add((ont_uri, RDF.type, OWL.Ontology))
        g.add((ont_uri, RDFS.label, Literal("Induced Domain Ontology")))

        # Custom annotation property for match confidence scores
        match_confidence = ns["matchConfidence"]
        g.add((match_confidence, RDF.type, OWL.AnnotationProperty))
        g.add((match_confidence, RDFS.label, Literal("match confidence")))
        g.add(
            (
                match_confidence,
                RDFS.comment,
                Literal("Cosine similarity score from embedding-based matching during induction"),
            )
        )

        # Build a match lookup: (table_name, column_name) → ConceptMatch
        match_map = {(m.source_table, m.source_column): m for m in matches}

        for table in tables:
            # ── Table → OWL Class ──
            table_match = match_map.get((table.name, ""))
            table_cls = ns[_to_pascal(table.name)]
            g.add((table_cls, RDF.type, OWL.Class))
            g.add((table_cls, RDFS.label, Literal(table.name)))

            if table_match and table_match.matched_class_uri:
                grounding_cls = URIRef(table_match.matched_class_uri)
                conf = Literal(table_match.similarity, datatype=XSD.float)
                # Skip a self-match (grounding_cls == table_cls): a reflexive
                # rdfs:subClassOf is a Tier-1 taxonomy-cycle self-loop and a self
                # skos:* match is vacuous. (build_ontology is the retired
                # reference path — not the active TableToOntologyStrategy — but
                # the guard mirrors it defensively.)
                if grounding_cls != table_cls:
                    g.add((grounding_cls, RDF.type, OWL.Class))
                    g.add((grounding_cls, RDFS.label, Literal(_label_from_uri(str(grounding_cls)))))

                    if table_match.match_type == "exact":
                        g.add((table_cls, RDFS.subClassOf, grounding_cls))
                        g.add((table_cls, SKOS.exactMatch, grounding_cls))
                        g.add((table_cls, match_confidence, conf))
                    elif table_match.match_type == "high_confidence":
                        g.add((table_cls, RDFS.subClassOf, grounding_cls))
                        g.add((table_cls, SKOS.closeMatch, grounding_cls))
                        g.add((table_cls, match_confidence, conf))
                    elif table_match.match_type == "ambiguous":
                        g.add((table_cls, SKOS.relatedMatch, grounding_cls))
                        g.add((table_cls, match_confidence, conf))

            if table.description:
                g.add((table_cls, RDFS.comment, Literal(table.description)))

            # ── Columns → Datatype or Object Properties ──
            for col in table.columns:
                col_match = match_map.get((table.name, col.name))
                prop_uri = ns[f"{_to_camel(table.name)}_{_to_camel(col.name)}"]

                # Determine if this is an FK (object property) or datatype property
                is_fk = False
                fk_target_table = None
                if table.tableConstraints:
                    for tc in table.tableConstraints:
                        if tc.constraintType == "FOREIGN_KEY" and col.name in tc.columns and tc.referredColumns:
                            is_fk = True
                            # Extract target table name from FQN
                            ref_parts = tc.referredColumns[0].split(".")
                            fk_target_table = ref_parts[-2] if len(ref_parts) >= 2 else ref_parts[0]
                            break

                if is_fk and fk_target_table:
                    # Object property
                    fk_cls = ns[_to_pascal(fk_target_table)]
                    g.add((prop_uri, RDF.type, OWL.ObjectProperty))
                    g.add((prop_uri, RDFS.domain, table_cls))
                    g.add((prop_uri, RDFS.range, fk_cls))
                    # Ensure FK target class is declared (may not be in induction request)
                    g.add((fk_cls, RDF.type, OWL.Class))
                    g.add((fk_cls, RDFS.label, Literal(fk_target_table)))
                else:
                    # Datatype property
                    g.add((prop_uri, RDF.type, OWL.DatatypeProperty))
                    g.add((prop_uri, RDFS.domain, table_cls))
                    xsd_type = _SQL_TO_XSD.get(col.dataType.upper(), XSD.string)
                    g.add((prop_uri, RDFS.range, xsd_type))

                g.add((prop_uri, RDFS.label, Literal(col.name)))
                if col.description:
                    g.add((prop_uri, RDFS.comment, Literal(col.description)))

                # ── Cardinality axioms from constraints ──
                is_not_null = col.constraint in ("NOT_NULL", "PRIMARY_KEY")
                is_unique = col.constraint in ("UNIQUE", "PRIMARY_KEY")
                # Also check table-level UNIQUE constraints
                if not is_unique and table.tableConstraints:
                    for tc in table.tableConstraints:
                        if tc.constraintType == "UNIQUE" and col.name in tc.columns and len(tc.columns) == 1:
                            is_unique = True
                            break

                if is_not_null and is_unique:
                    restriction = _cardinality_restriction(g, prop_uri, OWL.cardinality, 1)
                    g.add((table_cls, RDFS.subClassOf, restriction))
                elif is_not_null:
                    restriction = _cardinality_restriction(g, prop_uri, OWL.minCardinality, 1)
                    g.add((table_cls, RDFS.subClassOf, restriction))
                elif is_unique:
                    restriction = _cardinality_restriction(g, prop_uri, OWL.maxCardinality, 1)
                    g.add((table_cls, RDFS.subClassOf, restriction))

                if col.constraint == "PRIMARY_KEY":
                    g.add((table_cls, OWL.hasKey, prop_uri))

                # Alignment annotation if matched
                if col_match and col_match.matched_class_uri:
                    matched_uri = URIRef(col_match.matched_class_uri)
                    conf = Literal(col_match.similarity, datatype=XSD.float)

                    # Declare the matched property's type, label, and domain/range
                    # so the ontology is self-contained (fixes OoPS P08-A, P11, P35)
                    matched_type = OWL.ObjectProperty if is_fk else OWL.DatatypeProperty
                    g.add((matched_uri, RDF.type, matched_type))
                    g.add((matched_uri, RDFS.label, Literal(_label_from_uri(str(matched_uri)))))
                    if table_match and table_match.matched_class_uri:
                        g.add((matched_uri, RDFS.domain, URIRef(table_match.matched_class_uri)))
                    if is_fk and fk_target_table:
                        fk_match = match_map.get((fk_target_table, ""))
                        if fk_match and fk_match.matched_class_uri:
                            g.add((matched_uri, RDFS.range, URIRef(fk_match.matched_class_uri)))
                    else:
                        xsd_type = _SQL_TO_XSD.get(col.dataType.upper(), XSD.string)
                        g.add((matched_uri, RDFS.range, xsd_type))

                    # Skip a self-match (prop_uri == matched_uri): a reflexive
                    # owl:equivalentProperty / skos:* link is vacuous. (Retired
                    # reference path; guarded defensively.)
                    if prop_uri != matched_uri:
                        if col_match.match_type == "exact":
                            g.add((prop_uri, OWL.equivalentProperty, matched_uri))
                            g.add((prop_uri, SKOS.exactMatch, matched_uri))
                            g.add((prop_uri, match_confidence, conf))
                        elif col_match.match_type == "high_confidence":
                            g.add((prop_uri, OWL.equivalentProperty, matched_uri))
                            g.add((prop_uri, SKOS.closeMatch, matched_uri))
                            g.add((prop_uri, match_confidence, conf))
                        elif col_match.match_type == "ambiguous":
                            g.add((prop_uri, SKOS.relatedMatch, matched_uri))
                            g.add((prop_uri, match_confidence, conf))

        # NOTE: deliberately do NOT emit owl:imports for grounding ontologies — a
        # live import of a non-dereferenceable foundational URI makes Ontop/OWLAPI
        # network-resolve it at VKG load and fail every query (Ontop #337). The
        # grounding linkage is carried by rdfs:subClassOf + skos:* (this reference
        # path does not emit coa:groundedTo — that is the production strategy).

        return g

    # ── Step 6: Store results ───────────────────────────────────────

    def store_induced_ontology(
        self,
        uri_prefix: str,
        label: str,
        graph: Graph,
        matches: list[ConceptMatch],
    ) -> dict:
        """Persist the induced ontology to the catalog and upload its Turtle file.

        Args:
            uri_prefix: Ontology URI, used as its catalog identifier.
            label: Human-readable title for the catalog record.
            graph: The RDF graph to serialize and upload.
            matches: Grounding matches, used to derive imports and the description.

        Returns:
            The ontology-catalog record created (or found) for the ontology.
        """
        owl_turtle = graph.serialize(format="turtle")
        ontology_data = {
            "uri": uri_prefix,
            "title": label,
            "ontology_type": "induced",
            "description": f"Induced ontology with {len(matches)} concept mappings",
            "format": "turtle",
            "license": "proprietary",
            "license_tier": "restricted",
            "source_registry": "induced",
            "domain_tags": ["induced"],
            "imports": list(
                {
                    m.matched_class_uri.rsplit("#", 1)[0]
                    for m in matches
                    if m.matched_class_uri and m.match_type in ("exact", "high_confidence")
                }
            ),
        }
        result = self.oc.create_ontology(ontology_data)
        if not result.get("file_path"):
            # New record — upload the OWL file
            owl_bytes = owl_turtle.encode("utf-8") if isinstance(owl_turtle, str) else owl_turtle
            self.oc.upload_ontology_file(result["id"], owl_bytes)
        return result

    # ── Full pipeline ───────────────────────────────────────────────

    def run(
        self,
        table_ids: list[str],
        uri_prefix: str,
        label: str = "Induced Structured Ontology",
        confidence_threshold: float = 0.80,
        embedding_backend: str | None = None,
        scoring_strategy: str = "lexical",
        structural_weight: float = 0.3,
    ) -> tuple[InductionReport, Graph]:
        """Run the full table-to-ontology pipeline end to end.

        Fetches tables, extracts and embeds concepts, matches them against the catalog,
        builds the OWL graph, stores it, and assembles a summary report.

        Args:
            table_ids: Catalog table ids or FQNs to induce an ontology from.
            uri_prefix: Namespace prefix for minting induced URIs.
            label: Catalog title for the stored ontology.
            confidence_threshold: Minimum similarity to accept a high-confidence match.
            embedding_backend: Optional embedding backend override.
            scoring_strategy: "lexical" or "structural_fusion" scoring.
            structural_weight: Weight of the structural signal when fusing scores.

        Returns:
            The :class:`InductionReport` summarizing the run and the built RDF graph.
        """
        # 1. Fetch
        tables = self.fetch_tables(table_ids)

        # 2. Extract concepts
        concepts = self.extract_concepts(tables)

        # 3. Embed
        concepts, model_id = self.embed_concepts(concepts, backend=embedding_backend)

        # 4. Match
        matches = self.match_concepts(
            concepts,
            confidence_threshold,
            model_id=model_id,
            scoring_strategy=scoring_strategy,
            structural_weight=structural_weight,
        )

        # 5. Build OWL
        graph = self.build_ontology(uri_prefix, tables, concepts, matches)

        # 6. Store
        stored = self.store_induced_ontology(uri_prefix, label, graph, matches)

        novel_count = sum(1 for m in matches if m.match_type == "novel" and m.source_column == "")
        grounding = list(
            {
                m.matched_class_uri.rsplit("#", 1)[0]
                for m in matches
                if m.matched_class_uri and m.match_type in ("exact", "high_confidence")
            }
        )

        report = InductionReport(
            job_id="",  # filled by caller
            ontology_uri_prefix=uri_prefix,
            tables_processed=len(tables),
            columns_processed=sum(len(t.columns) for t in tables),
            matches=matches,
            novel_classes_created=novel_count,
            grounding_ontologies_used=grounding,
            induced_ontology_id=stored.get("id"),
            induced_ontology_uri=uri_prefix,
        )
        return report, graph


def _cardinality_restriction(g: Graph, prop_uri: URIRef, cardinality_type: URIRef, value: int):
    """Create an anonymous OWL restriction node for cardinality."""
    from rdflib import BNode

    restriction = BNode()
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, prop_uri))
    g.add((restriction, cardinality_type, Literal(value, datatype=XSD.nonNegativeInteger)))
    return restriction


def _label_from_uri(uri: str) -> str:
    """Extract a human-readable label from a URI's local name."""
    local = uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    # Split camelCase: 'dateCreated' → 'date Created'
    import re

    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", local).lower()


def _to_pascal(s: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[\s_\-]+", re.sub(r"[^a-zA-Z0-9\s_\-]", "", s)) if w)


def _to_camel(name: str) -> str:
    parts = name.replace("-", "_").split("_")
    return parts[0].lower() + "".join(w.capitalize() for w in parts[1:])
