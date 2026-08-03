# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""rigor_ontology strategy — LLM-driven ontology induction (RIGOR approach).

Based on: "Retrieval-Augmented Generation of Ontologies from Relational
Databases" (Nayyeri et al., arXiv:2506.01232).

Pipeline (per table, FK-topologically ordered):
  1. RAG: embed table + columns, retrieve top-k related concepts from
     the existing ontology store (classes AND properties).
  2. Prompt Gen-LLM with schema + retrieved context + core-ontology-so-far
     to produce a Turtle delta-ontology fragment.
  3. Judge-LLM refines the fragment for coherence, alignment, validity.
  4. Parse into rdflib Graph, merge into core.
Finally, run a global novelty check: embed every generated class/property
and compare against the existing store. Retain only novel terms.
"""

from __future__ import annotations

import logging
import os
import re

import boto3
from coa_common.bedrock_metrics import CostTracker
from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SKOS

from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.data_catalog import CatalogTable, parse_referred_column
from coa_ontology.inducer.strategies.base import InductionStrategy

log = logging.getLogger(__name__)

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _load_prompt(name: str) -> str:
    with open(os.path.join(_PROMPTS_DIR, name)) as f:
        return f.read()


_SYSTEM_PROMPT = _load_prompt("rigor_system.prompt")
_TABLE_PROMPT_TEMPLATE = _load_prompt("rigor_generate_delta.prompt")
_TABLE_PROMPT_FALLBACK_TEMPLATE = _load_prompt("rigor_generate_delta_fallback.prompt")
_JUDGE_PROMPT_TEMPLATE = _load_prompt("rigor_judge.prompt")

# Opus 4.7 supports large output; raise from the paper's 2k so rich
# ontology fragments for wide tables are not truncated.
_MAX_OUTPUT_TOKENS = 8192
_MAX_CORE_FRAGMENTS_IN_CONTEXT = 5
_RAG_TOP_K_CLASSES = 5
_RAG_TOP_K_PROPERTIES = 5


def _to_pascal(s: str) -> str:
    """Convert snake_case / kebab-case / spaced names to PascalCase."""
    if not s:
        return "Entity"
    cleaned = re.sub(r"[^a-zA-Z0-9\s_\-]", "", s)
    return "".join(w.capitalize() for w in re.split(r"[\s_\-]+", cleaned) if w)


def _iri_local_name(iri: str) -> str:
    """Extract a human-readable local name from an IRI."""
    if "#" in iri:
        return iri.rsplit("#", 1)[1]
    if "/" in iri:
        return iri.rsplit("/", 1)[1]
    return iri


def _format_schema_context(table: CatalogTable) -> str:
    """Format the full schema context for a table (columns + constraints)."""
    lines = [f"Table: {table.name}"]
    if table.description:
        lines.append(f"Description: {table.description}")
    lines.append("Columns:")
    for col in table.columns:
        parts = [f"  - {col.name} : {col.dataType}"]
        if col.constraint:
            parts.append(f"[{col.constraint}]")
        if col.description:
            parts.append(f"-- {col.description}")
        lines.append(" ".join(parts))
    if table.tableConstraints:
        lines.append("Constraints:")
        for tc in table.tableConstraints:
            ref = f" REFERENCES {tc.referredColumns}" if tc.referredColumns else ""
            lines.append(f"  - {tc.constraintType}({', '.join(tc.columns)}){ref}")
    return "\n".join(lines)


def _strip_markdown_fences(text: str) -> str:
    """Remove ```turtle / ``` fences if the LLM adds them despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        # Drop first fence line
        parts = t.split("\n", 1)
        t = parts[1] if len(parts) > 1 else ""
        if t.endswith("```"):
            t = t[:-3].rstrip()
        elif "```" in t:
            t = t.rsplit("```", 1)[0].rstrip()
    return t


class RigorOntologyStrategy(InductionStrategy):
    """RIGOR: iterative Gen-LLM + Judge-LLM with RAG context, Turtle output."""

    def __init__(self) -> None:
        """Initialize the strategy with no per-run cost tracker set yet."""
        super().__init__()
        # Set in induce(); declared here so _call_llm can read it directly
        # instead of a fragile getattr. None means "no metering for this run".
        self._cost_tracker: CostTracker | None = None

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
        """Induce a Turtle ontology via iterative Gen-LLM + Judge-LLM with RAG context.

        Args:
            tables: Source table metadata to induce from.
            ontology_uri_prefix: Base IRI prefix for minting induced terms.
            config: Strategy configuration (model ids, iteration limits, etc.).
            pipeline: Owning pipeline, used for shared clients and helpers.
            confidence_threshold: Minimum similarity to accept a high-confidence match.
            embedding_backend: Optional embedding backend override.
            scoring_strategy: "lexical" or "structural_fusion" scoring.
            structural_weight: Weight of the structural signal when fusing scores.
            grounding_ontology_ids: Accepted for signature parity; unused by this strategy.
            grounding_mode: Accepted for signature parity; unused by this strategy.
            cost_tracker: Optional per-job Bedrock usage accumulator threaded into LLM calls.

        Returns:
            A tuple of the proposal graph, the set of novel table names, and the
            list of concept matches.
        """
        # rigor_ontology uses LLM Gen+Judge rather than embedding match, so
        # grounding_ontology_ids and grounding_mode have no effect on its
        # current path. Accept for signature parity with the abstract base.
        del grounding_mode
        del grounding_ontology_ids
        # Every Gen/Judge/fallback LLM call flows through _call_llm; stash the
        # tracker so those converse calls record token usage under "generate".
        self._cost_tracker = cost_tracker
        # Normalize: URI prefix must end with '#' or '/' for valid IRI construction.
        if not ontology_uri_prefix.endswith(("#", "/")):
            ontology_uri_prefix += "#"
        llm_client = self._get_llm_client(config)
        model_id = config.get("llm_model_id", "us.anthropic.claude-sonnet-4-6")
        provenance_base = ontology_uri_prefix.rstrip("#").rstrip("/") + "/provenance"

        # Order tables so referenced tables are generated first (FK deps)
        ordered_tables = self._order_by_fk(tables)
        log.info("RIGOR: processing %d tables in FK order", len(ordered_tables))

        # Growing core ontology (Turtle fragments accumulated for prompt context)
        core_ontology_fragments: list[str] = []

        # Track which table each RDF subject was generated from (provenance)
        uri_to_table: dict[str, str] = {}

        # Merged RDF graph
        merged_graph = self._new_graph(ontology_uri_prefix, "RIGOR Induced Ontology")

        for table in ordered_tables:
            table_class_name = _to_pascal(table.name)

            # Step 1: RAG retrieval (classes + properties from existing store)
            existing_context = self._retrieve_ontology_context(table, pipeline)

            schema_context = _format_schema_context(table)
            documents = table.description or "(no documentation available for this table)"
            core_so_far = (
                "\n\n---\n\n".join(core_ontology_fragments[-_MAX_CORE_FRAGMENTS_IN_CONTEXT:])
                if core_ontology_fragments
                else "(empty — this is the first table)"
            )

            # Step 2: Generate delta-ontology
            prompt = _TABLE_PROMPT_TEMPLATE.format(
                ontology_prefix=ontology_uri_prefix,
                table_class_name=table_class_name,
                table_name=table.name,
                schema_context=schema_context,
                documents=documents,
                existing_ontology=existing_context,
                core_ontology=core_so_far,
                provenance_base=provenance_base,
            )
            delta_fragment = self._call_llm(llm_client, model_id, prompt, _SYSTEM_PROMPT)
            if not delta_fragment:
                log.warning("RIGOR: Gen-LLM returned empty for table %s", table.name)
                continue

            # Step 3: Judge-LLM refinement
            refined_fragment = self._judge_refine(
                llm_client,
                model_id,
                delta_fragment,
                core_so_far,
                schema_context,
            )

            # Step 4: Parse & merge
            frag_graph = self._parse_turtle(
                refined_fragment,
                ontology_uri_prefix,
                table.name,
            )
            if frag_graph is None:
                # Fallback 1: try the unrefined delta (skip the judge-refine pass).
                frag_graph = self._parse_turtle(
                    delta_fragment,
                    ontology_uri_prefix,
                    table.name,
                )
            if frag_graph is None:
                # Fallback 2: re-call the LLM with a stricter "flat triples only"
                # prompt that explicitly forbids every construct we've seen cause
                # parse failures. Last line of defense before dropping the table.
                log.warning(
                    "RIGOR: both refined and raw delta failed to parse for table %s; retrying with fallback prompt",
                    table.name,
                )
                fallback_prompt = _TABLE_PROMPT_FALLBACK_TEMPLATE.format(
                    ontology_prefix=ontology_uri_prefix,
                    table_class_name=table_class_name,
                    table_name=table.name,
                    schema_context=schema_context,
                    provenance_base=provenance_base,
                )
                fallback_fragment = self._call_llm(llm_client, model_id, fallback_prompt, _SYSTEM_PROMPT)
                if fallback_fragment:
                    frag_graph = self._parse_turtle(
                        fallback_fragment,
                        ontology_uri_prefix,
                        table.name,
                    )
            if frag_graph is not None:
                # Track source table for every subject in the fragment
                for s in set(frag_graph.subjects()):
                    if isinstance(s, URIRef):
                        uri_to_table[str(s)] = table.name
                merged_graph += frag_graph
                core_ontology_fragments.append(refined_fragment)
            else:
                log.error("RIGOR: failed to parse delta for table %s; dropping", table.name)

        # Step 4b: Sanitize dual-typed properties (OWL 2 QL compliance)
        # LLM occasionally emits a property as both owl:ObjectProperty and
        # owl:DatatypeProperty (e.g. FK columns that are also integers).
        # Ontop rejects this; resolve by keeping ObjectProperty (FK semantics).
        self._fix_dual_typed_properties(merged_graph)

        # Step 5: Novelty check
        novel_graph, novel_tables, matches = self._check_novelty(
            merged_graph,
            tables,
            ontology_uri_prefix,
            pipeline,
            confidence_threshold,
            uri_to_table,
        )
        return novel_graph, novel_tables, matches

    # ── Graph helpers ────────────────────────────────────────────────

    @staticmethod
    def _fix_dual_typed_properties(g: Graph) -> None:
        """Remove owl:DatatypeProperty from properties also typed as owl:ObjectProperty.

        OWL 2 QL requires a property to be exclusively one or the other.
        When both exist (LLM generated FK column as both), ObjectProperty
        wins because it carries referential semantics. Also removes any
        xsd:* range triples that conflict with the object-property range.
        """
        obj_props = set(g.subjects(RDF.type, OWL.ObjectProperty))
        data_props = set(g.subjects(RDF.type, OWL.DatatypeProperty))
        dual = obj_props & data_props
        for prop in dual:
            g.remove((prop, RDF.type, OWL.DatatypeProperty))
            for _, _, o in list(g.triples((prop, RDFS.range, None))):
                if str(o).startswith(str(XSD)):
                    g.remove((prop, RDFS.range, o))
            log.info("RIGOR: fixed dual-typed property %s → ObjectProperty only", prop)

    def _new_graph(self, ontology_uri_prefix: str, label: str) -> Graph:
        g = Graph()
        ns = Namespace(ontology_uri_prefix)
        g.bind("ind", ns)
        g.bind("owl", OWL)
        g.bind("rdfs", RDFS)
        g.bind("xsd", XSD)
        g.bind("skos", SKOS)
        g.bind("prov", Namespace("http://www.w3.org/ns/prov#"))
        ont_uri = URIRef(ontology_uri_prefix.rstrip("#").rstrip("/"))
        g.add((ont_uri, RDF.type, OWL.Ontology))
        g.add((ont_uri, RDFS.label, Literal(label)))
        return g

    def _parse_turtle(
        self,
        text: str,
        ontology_uri_prefix: str,
        table_name: str,
    ) -> Graph | None:
        """Parse LLM Turtle output, prepending the needed prefix declarations."""
        cleaned = _strip_markdown_fences(text)
        if not cleaned.strip():
            return None

        # Prepend standard prefix declarations so the LLM output doesn't need them
        prefix_block = (
            f"@prefix ind:  <{ontology_uri_prefix}> .\n"
            f"@prefix owl:  <http://www.w3.org/2002/07/owl#> .\n"
            f"@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
            f"@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            f"@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .\n"
            f"@prefix prov: <http://www.w3.org/ns/prov#> .\n\n"
        )

        # Only prepend if LLM didn't already include any prefix
        full = cleaned if "@prefix" in cleaned else (prefix_block + cleaned)

        # Defensive sanitization — even with the prompt forbidding blank nodes and
        # restrictions (see rigor_generate_delta.prompt step 7), LLMs occasionally
        # emit them. These patterns are the ones that cause rdflib parse failures,
        # which would otherwise drop the entire table's fragment. Downstream code
        # in _check_novelty discards all blank-node objects anyway, so stripping
        # them up-front loses no information but avoids catastrophic parse failure.
        #
        # (1) Strip any full triple of form `X P [ ... ] .` on a single line.
        #     This covers empty `[]`, empty-with-whitespace `[ ]`, and populated
        #     single-line restrictions.
        full = re.sub(
            r"^\s*\S+\s+\S+\s+\[[^\]]*?\]\s*\.\s*$",
            "",
            full,
            flags=re.MULTILINE,
        )
        # (2) Strip lines with an unclosed `[` — nothing downstream can use these
        #     and rdflib's multi-line-blank-node handling is what trips parsing.
        full = re.sub(
            r"^[^\n]*\[[^\]\n]*$",
            "",
            full,
            flags=re.MULTILINE,
        )

        try:
            g = Graph()
            g.parse(data=full, format="turtle")
            return g
        except Exception as e:
            log.warning(
                "RIGOR: failed to parse Turtle for table %s (%s). First 200 chars: %s",
                table_name,
                e,
                cleaned[:200],
            )
            return None

    # ── FK ordering ──────────────────────────────────────────────────

    def _order_by_fk(self, tables: list[CatalogTable]) -> list[CatalogTable]:
        """Topological sort: referenced tables before referencing ones."""
        table_map = {t.name: t for t in tables}
        visited: set[str] = set()
        in_progress: set[str] = set()
        ordered: list[CatalogTable] = []

        def visit(name: str):
            if name in visited or name in in_progress:
                return  # already done or cycle (skip the back-edge)
            in_progress.add(name)
            table = table_map.get(name)
            if table and table.tableConstraints:
                for tc in table.tableConstraints:
                    if tc.constraintType == "FOREIGN_KEY" and tc.referredColumns:
                        ref_table, _ = parse_referred_column(tc.referredColumns[0])
                        if ref_table in table_map and ref_table != name:
                            visit(ref_table)
            in_progress.discard(name)
            visited.add(name)
            if table:
                ordered.append(table)

        for t in tables:
            visit(t.name)
        return ordered

    # ── RAG retrieval ────────────────────────────────────────────────

    def _retrieve_ontology_context(self, table: CatalogTable, pipeline) -> str:
        """RAG: retrieve related classes + properties for table and its columns.

        Returns a human-readable block the LLM can use to decide reuse.
        Each hit is rendered as `- <IRI>  (label)`.
        """
        model_id = getattr(pipeline.eg, "model_id", "")
        lines: list[str] = []

        # 1) Query for the table itself
        try:
            query = f"{table.name} {table.description or ''}".strip()
            if query:
                table_vec = pipeline.eg.embed_text(query)
                table_classes = pipeline.oc.search_embeddings(
                    vector=table_vec,
                    embedding_type="lexical",
                    model_id=model_id,
                    entity_type="class",
                    top_k=_RAG_TOP_K_CLASSES,
                )
                if table_classes:
                    lines.append("Related classes for this table:")
                    for h in table_classes:
                        uri = h.get("entity_uri", "")
                        score = h.get("score", 0.0)
                        lines.append(f"  - <{uri}>  (similarity={score:.2f}, name={_iri_local_name(uri)})")
        except Exception as e:
            log.warning("RIGOR: table-level RAG retrieval failed for %s: %s", table.name, e)

        # 2) Query per-column for potential property reuse
        col_queries = [
            (col, f"{col.name.replace('_', ' ')} {col.description or ''} {col.dataType}".strip())
            for col in table.columns
        ]
        col_queries = [(c, q) for c, q in col_queries if q]
        if col_queries:
            try:
                col_vectors = pipeline.eg.embed_texts([q for _, q in col_queries])
                prop_hits_by_col: list[tuple[str, list[dict]]] = []
                for (col, _), vec in zip(col_queries, col_vectors, strict=False):
                    # Search for matching properties (datatype + object)
                    hits = pipeline.oc.search_embeddings(
                        vector=vec,
                        embedding_type="lexical",
                        model_id=model_id,
                        entity_type="property",
                        top_k=_RAG_TOP_K_PROPERTIES,
                    )
                    if hits:
                        prop_hits_by_col.append((col.name, hits))
                if prop_hits_by_col:
                    lines.append("")
                    lines.append("Related properties for columns:")
                    for col_name, hits in prop_hits_by_col:
                        top = hits[0]
                        uri = top.get("entity_uri", "")
                        score = top.get("score", 0.0)
                        lines.append(
                            f"  - column '{col_name}' → <{uri}>  (similarity={score:.2f}, name={_iri_local_name(uri)})"
                        )
            except Exception as e:
                log.warning("RIGOR: per-column RAG retrieval failed for %s: %s", table.name, e)

        if not lines:
            return "(no related existing concepts found — generate fresh terms)"
        return "\n".join(lines)

    # ── LLM calls ────────────────────────────────────────────────────

    def _get_llm_client(self, config: dict):
        return boto3.client(
            "bedrock-runtime",
            region_name=config.get("llm_region", "us-east-1"),
        )

    def _call_llm(
        self,
        llm_client,
        model_id: str,
        prompt: str,
        system_prompt: str,
    ) -> str | None:
        import time as _t

        _t0 = _t.perf_counter()
        try:
            resp = llm_client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                system=[{"text": system_prompt}],
                inferenceConfig={
                    "maxTokens": _MAX_OUTPUT_TOKENS,
                },
            )
            # Record token usage on the success path (rigor's LLM calls are
            # generation, not rerank). No-op when no tracker was threaded in.
            if self._cost_tracker is not None:
                self._cost_tracker.record_from_converse("generate", model_id, resp)
            result = resp["output"]["message"]["content"][0]["text"]
            log.info(
                "RIGOR LLM: model=%s prompt_chars=%d response_chars=%d %.0fms",
                model_id,
                len(prompt),
                len(result),
                (_t.perf_counter() - _t0) * 1000,
            )
            return result
        except Exception as e:
            log.error(
                "RIGOR: LLM generation failed after %.0fms: %s",
                (_t.perf_counter() - _t0) * 1000,
                e,
            )
            return None

    def _judge_refine(
        self,
        llm_client,
        model_id: str,
        delta: str,
        core_ontology: str,
        schema_context: str,
    ) -> str:
        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            core_ontology=core_ontology,
            schema_context=schema_context,
            delta_ontology=delta,
        )
        result = self._call_llm(
            llm_client,
            model_id,
            prompt,
            "You are an OWL 2 DL ontology quality reviewer. Output only Turtle.",
        )
        if result and result.strip():
            return result
        log.debug("RIGOR: judge returned empty, keeping original delta")
        return delta

    # ── Novelty check ────────────────────────────────────────────────

    def _check_novelty(
        self,
        merged_graph: Graph,
        tables: list[CatalogTable],
        ontology_uri_prefix: str,
        pipeline,
        confidence_threshold: float,
        uri_to_table: dict[str, str],
    ) -> tuple[Graph, set[str], list[ConceptMatch]]:
        """Embed each generated class/property; compare against existing store."""
        classes: list[dict] = []
        class_uris = set(merged_graph.subjects(RDF.type, OWL.Class)) | set(merged_graph.subjects(RDF.type, RDFS.Class))
        for s in class_uris:
            if not isinstance(s, URIRef):
                continue
            label = str(merged_graph.value(s, RDFS.label) or _iri_local_name(str(s)))
            comment = str(merged_graph.value(s, RDFS.comment) or "")
            classes.append(
                {
                    "uri": str(s),
                    "label": label,
                    "comment": comment,
                    "kind": "class",
                    "table": uri_to_table.get(str(s), ""),
                }
            )

        properties: list[dict] = []
        for prop_type in (OWL.DatatypeProperty, OWL.ObjectProperty):
            for s in merged_graph.subjects(RDF.type, prop_type):
                if not isinstance(s, URIRef):
                    continue
                label = str(merged_graph.value(s, RDFS.label) or _iri_local_name(str(s)))
                comment = str(merged_graph.value(s, RDFS.comment) or "")
                properties.append(
                    {
                        "uri": str(s),
                        "label": label,
                        "comment": comment,
                        "kind": "property",
                        "table": uri_to_table.get(str(s), ""),
                    }
                )

        all_terms = classes + properties
        if not all_terms:
            log.warning("RIGOR: merged graph contains no classes or properties")
            return self._new_graph(ontology_uri_prefix, "RIGOR Induced Ontology — Empty"), set(), []

        texts = [f"{t['label']} {t['comment']}".strip() for t in all_terms]
        try:
            vectors = pipeline.eg.embed_texts(texts)
        except Exception as e:
            log.error("RIGOR: embedding of generated terms failed: %s", e)
            # Without novelty check, treat everything as novel
            vectors = []

        model_id = getattr(pipeline.eg, "model_id", "")
        matches: list[ConceptMatch] = []
        novel_uris: set[str] = set()
        tables_with_novel: set[str] = set()

        for idx, term in enumerate(all_terms):
            vec = vectors[idx] if idx < len(vectors) else None
            best_hit = None
            best_score = 0.0
            if vec is not None:
                try:
                    # Match class-to-class, property-to-property
                    search_entity_type = term["kind"]
                    hits = pipeline.oc.search_embeddings(
                        vector=vec,
                        embedding_type="lexical",
                        model_id=model_id,
                        entity_type=search_entity_type,
                        top_k=3,
                    )
                    if hits:
                        best_hit = hits[0]
                        best_score = best_hit.get("score", 0.0) or 0.0
                except Exception as e:
                    log.debug("RIGOR: novelty search failed for %s: %s", term["label"], e)

            if best_score >= 0.95:
                match_type = "exact"
            elif best_score >= confidence_threshold:
                match_type = "high_confidence"
            elif best_score >= 0.50:
                match_type = "ambiguous"
            else:
                match_type = "novel"

            if match_type in ("exact", "high_confidence"):
                matches.append(
                    ConceptMatch(
                        source_column=term["label"] if term["kind"] == "property" else "",
                        source_table=term["table"],
                        matched_class_uri=best_hit.get("entity_uri") if best_hit else None,
                        matched_ontology_id=best_hit.get("ontology_id") if best_hit else None,
                        similarity=best_score,
                        match_type=match_type,
                    )
                )
            else:
                novel_uris.add(term["uri"])
                if term["table"]:
                    tables_with_novel.add(term["table"])
                matches.append(
                    ConceptMatch(
                        source_column=term["label"] if term["kind"] == "property" else "",
                        source_table=term["table"],
                        matched_class_uri=best_hit.get("entity_uri") if best_hit else None,
                        matched_ontology_id=best_hit.get("ontology_id") if best_hit else None,
                        similarity=best_score if best_score > 0 else None,
                        match_type=match_type,
                    )
                )

        # Build novel-only graph (include all triples involving novel URIs)
        novel_graph = self._new_graph(ontology_uri_prefix, "RIGOR Induced Ontology — Novel")
        for uri in novel_uris:
            uri_ref = URIRef(uri)
            for p, o in merged_graph.predicate_objects(uri_ref):
                novel_graph.add((uri_ref, p, o))
            for s, p in merged_graph.subject_predicates(uri_ref):
                if str(s) in novel_uris:
                    novel_graph.add((s, p, uri_ref))

        # Ensure the graph is self-consistent: any class/property referenced
        # as a domain/range of a novel term must also have its declaration
        # present, even if the class itself was semantically matched and
        # not marked novel. Without this, the accepted proposal and its
        # downstream R2RML mapping have dangling references to classes
        # that are defined in other ontology graphs.
        referenced_classes: set[URIRef] = set()
        for novel_uri in novel_uris:
            nref = URIRef(novel_uri)
            for prop in (RDFS.domain, RDFS.range):
                for target in merged_graph.objects(nref, prop):
                    if (
                        isinstance(target, URIRef)
                        and str(target).startswith(ontology_uri_prefix)
                        and str(target) not in novel_uris
                    ):
                        referenced_classes.add(target)
        from rdflib import BNode

        for cls_ref in referenced_classes:
            for p, o in merged_graph.predicate_objects(cls_ref):
                # Only keep structural declarations and human-readable
                # annotations; skip cross-class restrictions or anything
                # pulling in unrelated fragments.
                if p not in (RDF.type, RDFS.label, RDFS.comment, RDFS.subClassOf):
                    continue
                # Drop triples whose object is a blank node: Opus 4.7 loves
                # to attach owl:Restriction blank-node cardinality
                # constraints via rdfs:subClassOf, but we don't copy the
                # blank-node's own triples here, so the resulting graph
                # would have dangling BNs that strict RDF parsers (e.g.
                # Neptune) reject as "Invalid syntax".
                if isinstance(o, BNode):
                    continue
                novel_graph.add((cls_ref, p, o))
            # If this class's source table isn't already flagged, add it —
            # otherwise R2RML wouldn't iterate over it.
            src_table = uri_to_table.get(str(cls_ref))
            if src_table:
                tables_with_novel.add(src_table)

        log.info(
            "RIGOR: %d/%d terms novel, across %d tables",
            len(novel_uris),
            len(all_terms),
            len(tables_with_novel),
        )
        return novel_graph, tables_with_novel, matches

    # ── R2RML generation (override) ──────────────────────────────────

    def build_r2rml(
        self,
        ontology_uri_prefix: str,
        tables: list[CatalogTable],
        novel_tables: set[str],
        proposal_graph: Graph,
    ) -> Graph:
        """Build R2RML by looking up predicates from the generated graph.

        RIGOR produces LLM-named classes (``ind:Customer``) and properties
        (``ind:hasEmail``) that do NOT follow the ``{table}_{col}``
        naming convention the default builder assumes. For each table in
        ``novel_tables`` we:

          1. Find the class in the proposal graph whose
             ``prov:wasDerivedFrom`` annotation references the table.
             Fall back to label-matching if provenance is missing.
          2. For each column, find a property whose ``rdfs:domain`` is
             that class AND whose ``prov:wasDerivedFrom`` references
             the column. Fall back to label-matching the column name.
          3. Build a ``rr:predicateObjectMap`` using the matched
             property IRI. FK columns get an IRI template ObjectMap
             pointing at the referenced table's class; non-FK columns
             get a datatype ObjectMap.

        Classes in the proposal graph that do NOT correspond to any
        table (e.g. LLM-inferred parent concepts, auxiliary classes)
        are intentionally ignored — there is no source data for them.
        They remain in the ontology as pure schema-level concepts.
        """
        from coa_common import sql_ident

        from coa_ontology.inducer.strategies.base import (
            RR,
            SCL,
            _annotate_triples_map,
            xsd_for_column,
        )

        # Normalize: URI prefix must end with '#' or '/' for valid IRI construction.
        if not ontology_uri_prefix.endswith(("#", "/")):
            ontology_uri_prefix += "#"

        g = Graph()
        ns = Namespace(ontology_uri_prefix)
        g.bind("rr", RR)
        g.bind("scl", SCL)
        g.bind("ind", ns)
        g.bind("xsd", XSD)

        # Build lookup indices from the proposal graph
        class_by_table = self._index_classes_by_table(
            proposal_graph,
            ontology_uri_prefix,
            tables,
        )
        prov_base = ontology_uri_prefix.rstrip("#").rstrip("/") + "/provenance"

        for table in tables:
            if table.name not in novel_tables:
                continue

            table_cls = class_by_table.get(table.name)
            if table_cls is None:
                log.warning(
                    "RIGOR R2RML: no generated class found for table '%s'; skipping R2RML mapping for this table",
                    table.name,
                )
                continue

            tmap = ns[f"TriplesMap_{table.name}"]
            g.add((tmap, RDF.type, RR.TriplesMap))
            _annotate_triples_map(g, tmap, table)
            lt = BNode()
            g.add((tmap, RR.logicalTable, lt))
            g.add((lt, RR.tableName, Literal(sql_ident(table.name))))

            pk_col = self._primary_key_col(table)
            pk_id = sql_ident(pk_col) if pk_col else '"ID"'
            subj = URIRef(f"{tmap}/SubjectMap")
            g.add((tmap, RR.subjectMap, subj))
            g.add((subj, RR.template, Literal(f"{ontology_uri_prefix}{table.name}/{{{pk_id}}}")))
            g.add((subj, RR["class"], table_cls))

            # Pre-compute the property-by-column map for this class
            prop_by_col = self._index_properties_by_column(
                proposal_graph,
                table_cls,
                prov_base,
                table,
            )

            for col in table.columns:
                prop_uri = prop_by_col.get(col.name)
                if prop_uri is None:
                    log.info(
                        "RIGOR R2RML: no property generated for %s.%s; skipping this column's predicate-object map",
                        table.name,
                        col.name,
                    )
                    continue

                pom = URIRef(f"{tmap}/POM_{_to_pascal(col.name)}")
                g.add((tmap, RR.predicateObjectMap, pom))
                g.add((pom, RR.predicate, prop_uri))

                om = URIRef(f"{pom}/ObjectMap")
                g.add((pom, RR.objectMap, om))

                fk_target, _ = self._fk_info(col.name, table)
                # Determine whether the generated property is an ObjectProperty
                is_object_property = (
                    prop_uri,
                    RDF.type,
                    OWL.ObjectProperty,
                ) in proposal_graph

                col_id = sql_ident(col.name)
                if is_object_property and fk_target:
                    # Build an IRI template referencing the target table
                    g.add((om, RR.termType, RR.IRI))
                    g.add((om, RR.template, Literal(f"{ontology_uri_prefix}{fk_target}/{{{col_id}}}")))
                else:
                    g.add((om, RR.column, Literal(col_id)))
                    # Use the property's rdfs:range if it's an xsd type;
                    # otherwise fall back to SQL-type mapping
                    declared_range = proposal_graph.value(prop_uri, RDFS.range)
                    if declared_range is not None and str(declared_range).startswith(str(XSD)):
                        g.add((om, RR.datatype, declared_range))
                    else:
                        g.add((om, RR.datatype, xsd_for_column(col.dataType, col.name)))

        return g

    # ── R2RML lookup helpers ─────────────────────────────────────────

    def _index_classes_by_table(
        self,
        graph: Graph,
        ontology_uri_prefix: str,
        tables: list[CatalogTable],
    ) -> dict[str, URIRef]:
        """Map each table name to the IRI of its generated owl:Class.

        Strategies in priority order:
          1. prov:wasDerivedFrom contains ``/provenance/<table_name>``
             (exact suffix match). This is the canonical marker RIGOR's
             prompt instructs the LLM to emit.
          2. rdfs:label equals the table name (case-insensitive).
          3. Local name of the class IRI equals the PascalCase
             conversion of the table name.
        """
        PROV = Namespace("http://www.w3.org/ns/prov#")

        # Collect all generated classes
        gen_classes: list[URIRef] = [
            s
            for s in graph.subjects(RDF.type, OWL.Class)
            if isinstance(s, URIRef) and str(s).startswith(ontology_uri_prefix)
        ]

        result: dict[str, URIRef] = {}
        for table in tables:
            # Strategy 1: provenance match
            for cls in gen_classes:
                provs = [str(o) for o in graph.objects(cls, PROV.wasDerivedFrom)]
                if any(p.rstrip("/").endswith(f"/provenance/{table.name}") for p in provs):
                    result[table.name] = cls
                    break
            if table.name in result:
                continue

            # Strategy 2: label match (case-insensitive)
            table_name_lower = table.name.lower()
            for cls in gen_classes:
                label = graph.value(cls, RDFS.label)
                if label is not None and str(label).strip().lower() == table_name_lower:
                    result[table.name] = cls
                    break
            if table.name in result:
                continue

            # Strategy 3: local-name match against PascalCase(table)
            expected_local = _to_pascal(table.name)
            for cls in gen_classes:
                if _iri_local_name(str(cls)) == expected_local:
                    result[table.name] = cls
                    break

        return result

    def _index_properties_by_column(
        self,
        graph: Graph,
        class_iri: URIRef,
        prov_base: str,
        table: CatalogTable,
    ) -> dict[str, URIRef]:
        """Map each column of ``table`` to the IRI of the property generated for it.

        Resolves each column name to the generated ``owl:DatatypeProperty`` or
        ``owl:ObjectProperty`` whose domain is ``class_iri``.

        Priority:
          1. prov:wasDerivedFrom ends with ``/provenance/<table>/<column>``
             (case-insensitive). The LLM is asked to put this URI on
             every property in the prompt.
          2. rdfs:label equals the column name (case-insensitive),
             restricted to properties with the correct domain.
        """
        PROV = Namespace("http://www.w3.org/ns/prov#")

        # Collect properties with the right domain
        candidates: list[URIRef] = []
        for prop_type in (OWL.DatatypeProperty, OWL.ObjectProperty):
            for prop in graph.subjects(RDF.type, prop_type):
                if not isinstance(prop, URIRef):
                    continue
                if (prop, RDFS.domain, class_iri) in graph:
                    candidates.append(prop)

        result: dict[str, URIRef] = {}
        used: set[URIRef] = set()

        for col in table.columns:
            col_lower = col.name.lower()
            expected_prov_suffix = f"/provenance/{table.name}/{col.name}".lower()

            # Strategy 1: provenance match
            matched: URIRef | None = None
            for prop in candidates:
                if prop in used:
                    continue
                provs = [str(o).lower() for o in graph.objects(prop, PROV.wasDerivedFrom)]
                if any(p.rstrip("/").endswith(expected_prov_suffix) for p in provs):
                    matched = prop
                    break

            # Strategy 2: label match
            if matched is None:
                for prop in candidates:
                    if prop in used:
                        continue
                    label = graph.value(prop, RDFS.label)
                    if label is not None and str(label).strip().lower() == col_lower:
                        matched = prop
                        break

            # Strategy 3: local-name similarity (camelCase(col) appears in local name)
            if matched is None:
                needle = _to_pascal(col.name).lower()
                for prop in candidates:
                    if prop in used:
                        continue
                    local = _iri_local_name(str(prop)).lower()
                    if needle in local:
                        matched = prop
                        break

            if matched is not None:
                result[col.name] = matched
                used.add(matched)

        return result

    @staticmethod
    def _primary_key_col(table: CatalogTable) -> str | None:
        if not table.tableConstraints:
            return None
        for tc in table.tableConstraints:
            if tc.constraintType == "PRIMARY_KEY" and tc.columns:
                return tc.columns[0]
        return None

    @staticmethod
    def _fk_info(col_name: str, table: CatalogTable) -> tuple[str | None, str | None]:
        """Return (target_table, target_pk) if col_name is an FK; else (None, None)."""
        if not table.tableConstraints:
            return None, None
        for tc in table.tableConstraints:
            if tc.constraintType == "FOREIGN_KEY" and col_name in tc.columns and tc.referredColumns:
                return parse_referred_column(tc.referredColumns[0])
        return None, None
