// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Serve (Data Layer) — shapes and operations for runtime query, retrieval,
// and graph traversal. The service definition lives in data-layer.smithy.
$version: "2"

namespace com.amazon.semanticcontext.datalayer

use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#Uuid
use com.amazon.semanticcontext.common#ValidationError

// ══════════════════════════════════════════════════════════════════════════════
// Errors
// ══════════════════════════════════════════════════════════════════════════════
/// Returned with HTTP 404 when the target namespace does not exist or is not
/// visible to the caller.
@error("client")
@httpError(404)
structure NamespaceNotFoundError {
    /// Human-readable description of the error.
    @required
    message: String

    /// Identifier of the namespace that was not found.
    namespaceId: String
}

/// Returned with HTTP 404 when a pinned tier produced no data for the query —
/// a "no results" outcome rather than a server error.
@error("client")
@httpError(404)
structure NoResultError {
    /// Human-readable description of the error.
    @required
    message: String

    // The pinned tier that produced no result: 1 = metric match,
    // 2 = structured query. A miss is a "no data found" outcome, not a 500.
    /// Pinned tier that produced no result (1 = metric match, 2 = structured query).
    tier: Integer
}

/// Returned with HTTP 409 when the namespace has no published ontology, so the
/// query cannot be resolved against a schema yet.
@error("client")
@httpError(409)
structure OntologyNotPublishedError {
    /// Human-readable description of the error.
    @required
    message: String

    /// Identifier of the namespace whose ontology is not published.
    namespaceId: String

    /// Additional detail about why the ontology is unavailable.
    detail: String
}

/// Returned with HTTP 400 when a natural-language question cannot be translated
/// into an executable query against the ontology.
@error("client")
@httpError(400)
structure QueryTranslationError {
    /// Human-readable description of the error.
    @required
    message: String

    /// Additional detail about the translation failure.
    detail: String
}

/// Returned with HTTP 429 when the caller exceeds the request rate limit;
/// `retryAfterSeconds` indicates how long to wait before retrying.
@error("client")
@httpError(429)
structure RateLimitExceededError {
    /// Human-readable description of the error.
    @required
    message: String

    /// Number of seconds to wait before retrying the request.
    retryAfterSeconds: Integer
}

/// Returned with HTTP 502 when the virtual knowledge graph fails to execute the
/// generated SPARQL query; `sparqlQuery` carries the query that failed.
@error("server")
@httpError(502)
structure VKGExecutionError {
    /// Human-readable description of the error.
    @required
    message: String

    /// The SPARQL query that failed to execute.
    sparqlQuery: String

    /// Additional detail about the execution failure.
    detail: String
}

/// Returned with HTTP 502 when a backing data source cannot be reached to
/// answer the query; `dataSourceId` identifies the unavailable source.
@error("server")
@httpError(502)
structure DataSourceUnavailableError {
    /// Human-readable description of the error.
    @required
    message: String

    /// Identifier of the data source that could not be reached.
    dataSourceId: String

    /// Additional detail about the unavailability.
    detail: String
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Trace & Conformance
// ══════════════════════════════════════════════════════════════════════════════
/// One step in a query's execution trace, recording what ran, its status,
/// timing, and any tool used or conformance warnings raised.
structure TraceStep {
    /// Name of the execution step.
    @required
    step: String

    /// Status of the step (e.g. success or failure).
    @required
    status: String

    /// Duration of the step in milliseconds.
    durationMs: Long

    /// Additional detail about the step.
    detail: String

    /// Name of the tool invoked during the step, if any.
    toolUsed: String

    /// Identifier of the parallel group this step ran in, if any.
    parallelGroup: String

    /// Wall-clock time for the step in milliseconds.
    wallMs: Long

    /// Conformance warnings raised during the step.
    conformanceWarnings: ConformanceWarningList
}

/// A warning raised when a result row does not conform to the expected SHACL
/// shape, identifying the offending row and shape.
structure ConformanceWarning {
    /// Index of the result row that failed conformance.
    @required
    row: Integer

    /// Identifier of the SHACL shape that was violated.
    @required
    shape: String

    /// Human-readable description of the conformance warning.
    @required
    message: String
}

list ConformanceWarningList {
    member: ConformanceWarning
}

list TraceStepList {
    member: TraceStep
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Confidence
// ══════════════════════════════════════════════════════════════════════════════
/// A confidence score in [0, 1] for a query result, with an optional rationale
/// explaining how it was derived.
structure ConfidenceScore {
    /// Confidence score in the range [0, 1].
    @required
    score: Float

    /// Explanation of how the score was derived.
    rationale: String
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Supporting Content
// ══════════════════════════════════════════════════════════════════════════════
/// A knowledge-base chunk that supports an answer, with its text, source
/// document provenance, and relevance score.
structure SupportingContent {
    /// Identifier of the knowledge-base chunk.
    @required
    chunkId: String

    /// Text content of the chunk.
    @required
    text: String

    /// Opaque, unique identifier of the source document the chunk came from.
    /// Use this value to correlate chunks; it is not a display name.
    sourceDocumentId: String

    /// Human-readable original file name of the source document. This value is
    /// intended for display and is not guaranteed to be unique.
    sourceDocumentName: String

    /// Relevance score of the chunk to the query.
    relevanceScore: Float

    /// Entities annotated within the chunk.
    entityAnnotations: StringList
}

list SupportingContentList {
    member: SupportingContent
}

list StringList {
    member: String
}

/// A bounded, non-empty natural-language question or search query.
///
/// The bound is code points, so it does not shrink for non-Latin scripts. The
/// request BODY carrying it is separately capped at 8,192 bytes by the API's WAF
/// (`AWSManagedRulesCommonRuleSet` / `SizeRestrictions_BODY`), which runs ahead of
/// the service and answers `403 Forbidden` without explanation. A client should
/// therefore treat the practical ceiling as encoding-dependent — roughly 4000
/// code points for ASCII, 2725 for CJK, 2045 for astral-plane characters — and
/// keep the whole serialized body under 8 KB. Natural-language questions are far
/// below any of these; see `MAX_QUERY_CODEPOINTS` in `coa_common.constants` for
/// the measurements and why the WAF limit is left as it is.
@length(min: 1, max: 4000)
string NaturalLanguageQuery

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Result Rows (Tier 1/2 tabular data)
// ══════════════════════════════════════════════════════════════════════════════
map ResultRow {
    key: String
    value: String
}

list ResultRowList {
    member: ResultRow
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Graph Context
// ══════════════════════════════════════════════════════════════════════════════
/// Graph context accompanying an answer: the entities and relationships that
/// were traversed or cited.
structure GraphContext {
    /// Entities traversed or cited for the answer.
    entities: EntityList

    /// Relationships traversed or cited for the answer.
    relationships: RelationshipList
}

/// An entity (graph node) with its IRI, human-readable label, optional type, and
/// arbitrary properties.
structure Entity {
    /// IRI identifying the entity.
    @required
    uri: String

    /// Human-readable label for the entity.
    @required
    label: String

    /// Type (class IRI) of the entity, if known.
    type: String

    /// Arbitrary key-value properties of the entity.
    properties: PropertyMap
}

/// A directed relationship between two entities, identified by source, predicate,
/// and target IRIs.
structure Relationship {
    /// IRI of the source entity.
    @required
    sourceUri: String

    /// IRI of the predicate linking source to target.
    @required
    predicateUri: String

    /// IRI of the target entity.
    @required
    targetUri: String

    /// Human-readable label for the predicate.
    predicateLabel: String
}

list EntityList {
    member: Entity
}

list RelationshipList {
    member: Relationship
}

map PropertyMap {
    key: String
    value: String
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Enums
// ══════════════════════════════════════════════════════════════════════════════
/// Direction to follow relationships when traversing the graph.
enum TraversalDirection {
    /// Follow relationships pointing away from the start entity.
    OUTGOING = "outgoing"

    /// Follow relationships pointing toward the start entity.
    INCOMING = "incoming"

    /// Follow relationships in both directions.
    BOTH = "both"
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Dimension Filters
// ══════════════════════════════════════════════════════════════════════════════
/// Comparison operator applied by a DimensionFilter.
///
/// Equality is the only supported comparison: Tier-1 binds the value as a bare
/// SQL literal into a governed metric's template, which expresses `=` and nothing
/// else. Declared as an enum rather than a free-form String so a client asking
/// for an unsupported comparison is rejected by the contract instead of having it
/// silently applied as equality — a wrong answer returned with a 200.
enum DimensionOperator {
    /// Match rows whose dimension equals the value.
    EQUALS = "="
}

/// A filter constraining a query to rows matching a dimension value, with an
/// optional comparison operator (defaults to equality).
///
/// Server-side invariants NOT expressible as Smithy traits, enforced at
/// validation (a violation is a 400, see the Context Manager's InvokeRequest):
///   - `name` must be non-blank, and is trimmed before use.
///   - `name` is matched case-insensitively, and must be UNIQUE across the list —
///     a mapping cannot express two values for one dimension. (`@uniqueItems`
///     would not capture this: it compares whole structures, so two filters on
///     the same name with different values would still pass.)
///   - every filter must be applicable by the resolved metric; a filter the
///     metric cannot apply fails closed rather than being dropped.
structure DimensionFilter {
    /// Name of the dimension to filter on.
    @required
    name: String

    /// Value to match for the dimension.
    @required
    value: String

    /// Comparison operator to apply (defaults to equality).
    operator: DimensionOperator
}

list DimensionFilterList {
    member: DimensionFilter
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Query Result (top-level response)
// ══════════════════════════════════════════════════════════════════════════════
/// The top-level result of a query: the answering tier, confidence, tabular
/// rows and/or a synthesized answer, the query that ran, supporting content,
/// graph context, and an execution trace.
structure QueryResult {
    /// The answering tier that produced this result.
    @required
    tier: Integer

    @required
    confidence: ConfidenceScore

    /// Tabular result rows returned by the query.
    resultRows: ResultRowList

    /// Natural-language answer synthesized from the results.
    synthesizedAnswer: String

    /// The query that was executed to produce the result.
    queryUsed: String

    /// The SPARQL query generated for the result, if any.
    sparqlGenerated: String

    /// Knowledge-base chunks supporting the answer.
    supportingContent: SupportingContentList

    graphContext: GraphContext

    /// Step-by-step execution trace of the query.
    @required
    trace: TraceStepList

    /// Version of the ontology the query ran against.
    ontologyVersion: String

    /// Identifiers of the data sources that contributed to the result.
    dataSources: StringList

    /// Whether the result is partial (incomplete).
    partial: Boolean
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Schema (for DescribeSchema)
// ══════════════════════════════════════════════════════════════════════════════
/// A class in the namespace's queryable schema, with its IRI, label, optional
/// description, properties, and parent class.
structure SchemaClass {
    /// IRI identifying the class.
    @required
    uri: String

    /// Human-readable label for the class.
    @required
    label: String

    /// Description of the class.
    description: String

    /// Properties defined on the class.
    properties: SchemaPropertyList

    /// IRI of the parent class, if any.
    parentClass: String
}

/// A property of a schema class, with its IRI, label, value range (datatype or
/// target class), and optional description.
structure SchemaProperty {
    /// IRI identifying the property.
    @required
    uri: String

    /// Human-readable label for the property.
    @required
    label: String

    /// Value range of the property (datatype or target class).
    range: String

    /// Description of the property.
    description: String
}

list SchemaClassList {
    member: SchemaClass
}

list SchemaPropertyList {
    member: SchemaProperty
}

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Query strategy
// ══════════════════════════════════════════════════════════════════════════════
/// Which Tier-2 engine answers a structured query. Absent = the serve default
/// (``nl_to_sql_first``).
///
/// This is a DIFFERENT axis from ``mode``: ``mode`` chooses whether the whole
/// T1→T2→T3 cascade is replaced by the Tier-3 reasoning loop, while ``strategy``
/// chooses which engine answers within Tier 2. Both were called "agentic" before
/// the rebrand, which is why the distinction is spelled out here.
///
/// An explicit value is caller intent and is never overridden by automatic tier
/// gating or per-query Tier-2 pruning.
enum QueryStrategy {
    /// Run Ontop and NL→SQL in parallel; return the higher-confidence answer.
    BEST = "best"

    /// Ontop only — the R2RML/VKG semantic path. No NL→SQL fallback.
    ONTOP = "ontop"

    /// NL→SQL only. No Ontop fallback.
    NL_TO_SQL = "nl_to_sql"

    /// Ontop first, falling back to NL→SQL.
    ONTOP_FIRST = "ontop_first"

    /// NL→SQL first, falling back to Ontop. The serve default.
    NL_TO_SQL_FIRST = "nl_to_sql_first"

    /// The bounded tool-use agent only. Opt-in; never reached as a fallback.
    DEEP_REASONING = "deep-reasoning"
}

// ══════════════════════════════════════════════════════════════════════════════
// Operations
// ══════════════════════════════════════════════════════════════════════════════
// ── Query ────────────────────────────────────────────────────────────────────
/// Answer a natural-language question against the namespace. Routes through the
/// tiered resolver (metric match → structured query → knowledge-base retrieval)
/// and returns a typed result with confidence and an execution trace.
@http(method: "POST", uri: "/namespaces/{namespaceId}/query")
operation Query {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Natural-language question to answer.
        @required
        query: NaturalLanguageQuery

        /// Whether to execute the generated query (versus translate only).
        execute: Boolean

        /// Pin resolution to a specific tier, bypassing automatic routing.
        tierOverride: Integer

        /// Execution mode: "standard" (single-shot) or "deep-reasoning" (multi-step
        /// reasoning loop). Orthogonal to the tier cascade. Absent = the serve
        /// deployment default. Deep reasoning is higher-recall but much slower
        /// (p50 ~90s), so a caller behind a short client timeout should send
        /// "standard". "agentic" is the deprecated pre-rename spelling of
        /// "deep-reasoning" and is still accepted.
        mode: String

        /// Which Tier-2 engine answers a structured query. Absent = the serve
        /// default (``nl_to_sql_first``). Orthogonal to ``mode`` — see
        /// ``QueryStrategy``. Ignored when the resolved tier is not 2.
        strategy: QueryStrategy

        /// Dimension filters to constrain the query.
        dimensions: DimensionFilterList

        /// Maximum time to wait for the query, in milliseconds.
        timeoutMs: Long

        /// Whether to include supporting content in the response.
        includeSupporting: Boolean

        /// Maximum number of result rows to return.
        maxResults: Integer
    }

    output := {
        @required
        result: QueryResult

        /// Identifier of the request, for tracing and support.
        requestId: String

        /// Identifier of the session the request belongs to.
        sessionId: String
    }

    errors: [
        ValidationError
        AccessDeniedError
        NamespaceNotFoundError
        NoResultError
        OntologyNotPublishedError
        QueryTranslationError
        VKGExecutionError
        DataSourceUnavailableError
    ]
}

// ── TranslateSPARQL ──────────────────────────────────────────────────────────
/// Translate a natural-language question into SPARQL against the namespace's
/// ontology without executing it. Useful for previewing or debugging the query
/// the resolver would run.
@http(method: "POST", uri: "/namespaces/{namespaceId}/translate")
operation TranslateSPARQL {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Natural-language question to translate into SPARQL.
        @required
        query: NaturalLanguageQuery
    }

    output := {
        /// The SPARQL query generated from the question.
        @required
        sparqlQuery: String

        @required
        confidence: ConfidenceScore

        /// Step-by-step trace of the translation.
        @required
        trace: TraceStepList

        /// Version of the ontology the query was generated against.
        ontologyVersion: String
    }

    errors: [
        ValidationError
        NamespaceNotFoundError
        OntologyNotPublishedError
        QueryTranslationError
    ]
}

// ── KBSearch ─────────────────────────────────────────────────────────────────
/// Semantic search over the namespace's document knowledge base, returning
/// ranked supporting chunks with their source provenance.
@http(method: "POST", uri: "/namespaces/{namespaceId}/kb/search")
operation KBSearch {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Natural-language search query.
        @required
        query: NaturalLanguageQuery

        /// Maximum number of chunks to return.
        topK: Integer

        /// Minimum relevance score a chunk must meet to be returned.
        minScore: Float

        /// Restrict results to these source document identifiers.
        sourceFilter: StringList

        /// Restrict results to chunks annotated with these entities.
        entityFilter: StringList
    }

    output := {
        /// Ranked supporting chunks matching the query.
        @required
        chunks: SupportingContentList

        /// Step-by-step trace of the search.
        @required
        trace: TraceStepList

        /// Identifier of the embedding model used for the query.
        queryEmbeddingModel: String
    }

    errors: [
        ValidationError
        NamespaceNotFoundError
    ]
}

// ── GraphTraverse ────────────────────────────────────────────────────────────
/// Traverse the knowledge graph outward from a starting entity URI, following
/// relationships up to a maximum depth in the requested direction.
@http(method: "POST", uri: "/namespaces/{namespaceId}/graph/traverse")
operation GraphTraverse {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// IRI of the entity to start traversal from.
        @required
        startUri: String

        /// Maximum traversal depth from the starting entity.
        maxDepth: Integer

        /// Restrict traversal to these relationship (predicate) IRIs.
        relationshipFilter: StringList

        /// Direction to traverse relationships in.
        direction: TraversalDirection

        /// Maximum number of entities to return.
        maxResults: Integer
    }

    output := {
        /// Entities reached during traversal.
        @required
        entities: EntityList

        /// Relationships traversed.
        @required
        relationships: RelationshipList

        /// Step-by-step trace of the traversal.
        @required
        trace: TraceStepList
    }

    errors: [
        ValidationError
        NamespaceNotFoundError
        OntologyNotPublishedError
    ]
}

// ── DescribeSchema ───────────────────────────────────────────────────────────
/// Describe the namespace's queryable schema: classes and their properties
/// across every ontology loaded into the namespace (induced, foundational,
/// uploaded). Proxies to the ontology-engine ``/graph/schema`` route via the
/// ontology-api-proxy Lambda — the same source the MCP ``describe_schema``
/// tool reads, so an agent and a UI see the same schema.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/schema")
operation DescribeSchema {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Whether to include class properties in the response.
        @httpQuery("includeProperties")
        includeProperties: Boolean

        /// Maximum number of classes to return.
        @httpQuery("maxResults")
        maxResults: Integer
    }

    output := {
        /// The classes in the namespace's schema.
        @required
        classes: SchemaClassList

        /// Version of the ontology described.
        ontologyVersion: String
    }

    errors: [
        ValidationError
        NamespaceNotFoundError
        OntologyNotPublishedError
    ]
}
