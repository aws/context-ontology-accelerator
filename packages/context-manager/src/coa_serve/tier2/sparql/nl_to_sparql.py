# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NL→SPARQL for native execution against an RDF_SPARQL source (full SPARQL 1.1).

The Ontop translator's prompt forbids subqueries and steers to ``SUM(IF(...))``
because Ontop must compile SPARQL to SQL. A native triple store runs SPARQL as is,
so this translator offers the full SPARQL 1.1 surface — property paths, OPTIONAL,
UNION, subqueries — and is otherwise the same pipeline (T-Box assembly → prompt →
LLM → extract → ``SPARQLValidator`` → retry). It differs in four places only:

* the T-Box is gated to ``RDF_SPARQL`` classes (``backend``);
* the system prompt (engine-agnostic: the engine is chosen per source by the client
  factory, not here);
* no Ontop few-shot examples or Tier-1 metric context — both describe the SQL path;
* its own confidence calibration (see :meth:`NativeNLtoSPARQL._calibrate_confidence`).

Safety is not the translator's job: the strategy runs the result through
``SPARQLFirewall`` before anything executes.
"""

from __future__ import annotations

from collections.abc import Callable

from coa_common.constants import StructuredBackend

from ...clients.base import GraphClient, LLMClient
from ..ontop.nl_to_sparql import NLtoSPARQL
from ..ontop.types import VectorHit

# Full SPARQL 1.1 prompt. Deliberately does NOT carry the Ontop anti-subquery /
# SUM(IF) rules — those exist only because Ontop cannot compile them to SQL. A
# native triple store runs them directly, so the graph-native constructs the ADR
# calls out (property paths, OPTIONAL, UNION) are ENCOURAGED here.
NATIVE_SPARQL_SYSTEM_PROMPT = (
    "You are a SPARQL 1.1 query generator for an RDF graph store. "
    "Given a natural language question and an ontology schema, generate a valid, "
    "read-only SPARQL 1.1 query that runs directly against the graph.\n\n"
    "CRITICAL RULES:\n"
    "- The schema provides a PREFIX declaration (e.g. PREFIX ind: <...>). "
    "You MUST copy that EXACT PREFIX into your query.\n"
    "- Use ONLY the prefixed names shown in the schema (ind:ClassName, ind:propertyName).\n"
    "- NEVER invent your own prefix URI — use exactly what the schema provides.\n"
    "- Generate ONLY read-only queries: SELECT, ASK, or CONSTRUCT (DESCRIBE is rejected). "
    "NEVER emit INSERT/DELETE/DROP/CLEAR/LOAD or any update form.\n"
    "- NEVER use GRAPH, FROM or FROM NAMED clauses — the query is scoped to the "
    "source's graph automatically, and a query naming a graph is rejected.\n"
    "- Return the query in a ```sparql code block.\n"
    "- After the query, rate your confidence (0.0-1.0).\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "FULL SPARQL 1.1 IS AVAILABLE — this is a native triple store\n"
    "═══════════════════════════════════════════════════════════════\n\n"
    "Unlike a SQL-backed VKG, this endpoint executes SPARQL natively, so you MAY "
    "and SHOULD use graph-native constructs when they fit the question:\n"
    "- Property paths: ind:parent+/ind:memberOf*/(ind:a|ind:b) for transitive or\n"
    "  alternative traversals.\n"
    "- OPTIONAL for fields that may be absent on some instances.\n"
    "- UNION to combine alternative patterns.\n"
    "- Subqueries ({ SELECT ... }) and nested aggregates — these are FINE here.\n"
    "- Aggregates: COUNT, SUM, MIN, MAX, AVG, GROUP_CONCAT with GROUP BY / HAVING.\n"
    "- FILTER for numeric/date/string constraints; use LCASE()/CONTAINS() for\n"
    "  case-insensitive or partial matching.\n"
    "- When a property lists 'allowed values: [...]', match one of those literals\n"
    "  exactly and case-sensitively.\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "EXAMPLES\n"
    "═══════════════════════════════════════════════════════════════\n\n"
    "Example 1 (basic filter + projection):\n\n"
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Product (label: product)\n"
    "Properties:\n"
    " ind:productRevenue domain: ind:Product range: xsd:decimal\n"
    " ind:productName domain: ind:Product range: xsd:string\n\n"
    "Question: What products have revenue greater than 1 million?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
    "SELECT ?product ?name ?revenue\n"
    "WHERE {\n"
    "  ?product a ind:Product ;\n"
    "           ind:productName ?name ;\n"
    "           ind:productRevenue ?revenue .\n"
    "  FILTER(?revenue > 1000000)\n"
    "}\n"
    "ORDER BY DESC(?revenue)\n"
    "```\n"
    "Confidence: 0.9\n\n"
    "Example 2 (property path — transitive hierarchy, a graph-native strength):\n\n"
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Employee (label: employee)\n"
    "Properties:\n"
    " ind:reportsTo domain: ind:Employee range: ind:Employee\n"
    " ind:employeeName domain: ind:Employee range: xsd:string\n\n"
    "Question: Who are all the people (directly or indirectly) reporting to Alice?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "SELECT ?name\n"
    "WHERE {\n"
    '  ?manager ind:employeeName "Alice" .\n'
    "  ?report ind:reportsTo+ ?manager ;\n"
    "          ind:employeeName ?name .\n"
    "}\n"
    "```\n"
    "Confidence: 0.85\n\n"
    "Example 3 (aggregate + GROUP BY):\n\n"
    "Question: What is the total revenue per product category?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "SELECT ?category (SUM(?revenue) AS ?totalRevenue)\n"
    "WHERE {\n"
    "  ?p a ind:Product ;\n"
    "     ind:productCategory ?category ;\n"
    "     ind:productRevenue ?revenue .\n"
    "}\n"
    "GROUP BY ?category\n"
    "```\n"
    "Confidence: 0.85\n\n"
    "Example 4 (OPTIONAL + COUNT DISTINCT):\n\n"
    "Question: How many distinct customers placed an order, including those with no email?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "SELECT (COUNT(DISTINCT ?customer) AS ?customerCount)\n"
    "WHERE {\n"
    "  ?order a ind:Order ;\n"
    "         ind:orderCustomer ?customer .\n"
    "  OPTIONAL { ?customer ind:customerEmail ?email . }\n"
    "}\n"
    "```\n"
    "Confidence: 0.85\n\n"
    "SELECT VARIABLE RULES:\n"
    "- SELECT only the specific variables needed to answer the question; use\n"
    "  descriptive named variables, not ?s or *.\n"
    "- For a count/aggregate, SELECT only the aggregate (with an explicit\n"
    "  descriptive `AS ?alias`).\n"
    "- Count typed entities with COUNT(DISTINCT ?e) when ?e is bound via `?e a :Class`,\n"
    "  so 1-to-many joins do not inflate the count.\n"
)

# Calibration for native execution. Not eval-tuned: the NL→SPARQL benchmark that
# gates fleet-wide auto-detect (LLD task 5) is what tunes these. Complexity is
# penalised less than on the Ontop path, because a triple store executes joins,
# paths and subqueries natively instead of compiling them to SQL.
_NATIVE_VALIDATION_BOOST = 0.05
_NATIVE_STRONG_HIT_THRESHOLD = 0.8
_NATIVE_STRONG_HIT_BOOST = 0.05
_NATIVE_WEAK_HIT_THRESHOLD = 0.5
_NATIVE_WEAK_HIT_PENALTY = 0.1
_NATIVE_HIGH_COMPLEXITY = 12
_NATIVE_HIGH_COMPLEXITY_PENALTY = 0.05


class NativeNLtoSPARQL(NLtoSPARQL):
    """Full-SPARQL-1.1 NL→SPARQL translator, gated to the ``RDF_SPARQL`` backend."""

    def __init__(
        self,
        graph_client: GraphClient,
        llm_client: LLMClient,
        guardrail_id: str = "",
        timeout_s: float | None = None,
        graph_uri_template: str | None = None,
        *,
        guardrail_id_provider: Callable[[], str] | None = None,
    ):
        """Wire the shared pipeline to the graph backend and the full SPARQL prompt.

        Args:
            graph_client: The SHARED ontology endpoint client — T-Box assembly and
                URI validation read the ontology, never a source's data endpoint.
            llm_client: LLM client that performs the translation.
            guardrail_id: Optional Bedrock guardrail applied during translation.
            timeout_s: Overall translation timeout; ``None`` uses the Ontop default.
            graph_uri_template: Named-graph URI template of the ontology graphs.
            guardrail_id_provider: Optional live guardrail-id resolver; takes
                precedence over ``guardrail_id``.
        """
        super().__init__(
            graph_client=graph_client,
            llm_client=llm_client,
            guardrail_id=guardrail_id,
            timeout_s=timeout_s,
            graph_uri_template=graph_uri_template,
            guardrail_id_provider=guardrail_id_provider,
            backend=StructuredBackend.RDF_SPARQL,
            system_prompt=NATIVE_SPARQL_SYSTEM_PROMPT,
        )

    def _calibrate_confidence(
        self,
        base: float,
        *,
        validation_passed: bool,
        vector_hits: list[VectorHit] | None,
        sparql: str | None,
    ) -> float:
        """Calibrate from validation, retrieval grounding and (lightly) complexity."""
        score = base
        if validation_passed:
            score += _NATIVE_VALIDATION_BOOST
        top = max((h.score for h in (vector_hits or []) if getattr(h, "score", None) is not None), default=None)
        if top is not None:
            if top >= _NATIVE_STRONG_HIT_THRESHOLD:
                score += _NATIVE_STRONG_HIT_BOOST
            elif top < _NATIVE_WEAK_HIT_THRESHOLD:
                score -= _NATIVE_WEAK_HIT_PENALTY
        if sparql and sparql.count(" .") + sparql.upper().count("FILTER") > _NATIVE_HIGH_COMPLEXITY:
            score -= _NATIVE_HIGH_COMPLEXITY_PENALTY
        return min(1.0, max(0.0, score))
