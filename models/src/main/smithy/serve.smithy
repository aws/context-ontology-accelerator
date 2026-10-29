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
}

/// Returned with HTTP 400 when a natural-language question cannot be translated
/// into an executable query against the ontology.
@error("client")
@httpError(400)
structure QueryTranslationError {
    /// Human-readable description of the error.
    @required
    message: String
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
/// generated SPARQL query.
///
/// Carries a fixed, client-safe message only. The failing query and the
/// backend's own error text are deliberately not returned: they name internal
/// schema objects and endpoints. Operators correlate a failure through the
/// request id instead.
@error("server")
@httpError(502)
structure VKGExecutionError {
    /// Human-readable description of the error.
    @required
    message: String
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
    text: CustomerData

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

// Tier-3 responses additionally carry two DEPRECATED, undocumented aliases on each
// item for one release — `sourceDoc` (same value as `sourceDocumentId`) and `label`
// — so a client written against the previous runtime keeps rendering citations.
// They are not part of this contract; read `sourceDocumentId` / `sourceDocumentName`.
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
///
/// Marked sensitive: the text is caller-authored and routinely carries business
/// data or personal information, so generated clients must not log it.
@length(min: 1, max: 4000)
@sensitive
string NaturalLanguageQuery

/// Customer data returned by or sent to a query: result cell values, knowledge-base
/// chunk text, synthesized answers, generated SQL/SPARQL, and dimension filter
/// values. Marked sensitive so generated clients keep it out of logs.
@sensitive
string CustomerData

/// Tier-3 execution mode for a query. Orthogonal to the tier cascade.
enum QueryMode {
    /// Single-shot retrieval and synthesis.
    STANDARD = "standard"

    /// Multi-step reasoning loop. Higher recall, much slower (p50 ~90s).
    DEEP_REASONING = "deep-reasoning"

    /// Deprecated pre-rename spelling of `deep-reasoning`; still accepted.
    AGENTIC = "agentic"
}

/// A bounded list of filter values (source identifiers, entity names, or
/// relationship IRIs) that restricts a retrieval or traversal.
@length(max: 50)
list FilterValueList {
    member: FilterValue
}

/// A single filter value.
@length(min: 1, max: 2048)
string FilterValue

// ══════════════════════════════════════════════════════════════════════════════
// Shared Shapes — Result Rows (Tier 1/2 tabular data)
// ══════════════════════════════════════════════════════════════════════════════
map ResultRow {
    key: String
    value: CustomerData
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
    /// Entities traversed or cited for the answer. Bounded to 200 entities per
    /// response; a traversal that reached more is reported through
    /// `metadata.degradedSources` and the processing trace.
    entities: EntityList

    /// Relationships traversed or cited for the answer. Bounded to 500
    /// relationships per response; a traversal that reached more is reported
    /// through `metadata.degradedSources` and the processing trace. A relationship
    /// the graph carries without a predicate identifier cannot be represented (see
    /// `predicateUri`) and is reported the same way, so an empty list is never
    /// ambiguous between "none found" and "all discarded".
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

    /// Arbitrary key-value properties of the entity. The reserved
    /// `supportingStatements` key contains a JSON-encoded string array of
    /// graph-only evidence. Decode that value as JSON before reading it.
    properties: PropertyMap
}

/// A directed relationship between two entities, identified by source, predicate,
/// and target IRIs.
structure Relationship {
    /// IRI of the source entity.
    @required
    sourceUri: String

    /// IRI of the predicate linking source to target. A property-graph edge that
    /// carries only a human-readable predicate string is given a stable,
    /// tenant-scoped synthetic identifier in the `urn:coa:graph-predicate:` scheme,
    /// derived from that string. Treat such a value as an opaque identity for
    /// correlating edges within one namespace, not as an ontology predicate IRI.
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

/// String-valued entity properties. `supportingStatements` is reserved for a
/// JSON-encoded string array of graph-only evidence. It is bounded to 100
/// statements per entity, 1,000 characters and 2,000 UTF-8 bytes per statement,
/// and 4,000 characters and 6,000 UTF-8 bytes across an assembled graph context.
map PropertyMap {
    key: String
    value: String
}

/// The IRI a graph traversal starts from: an absolute `http`/`https` IRI or a
/// `urn:` name.
///
/// The character set excludes whitespace and the characters that could end or
/// escape an IRI reference in the generated graph query (`<>"{}|\^` and
/// backtick), plus `;`. A value outside it is rejected with 400 rather than
/// silently matching nothing.
@length(min: 1, max: 2048)
@pattern("^(https?://[^\\s<>\"{}|\\\\^`;]+|urn:[a-zA-Z0-9][a-zA-Z0-9._:-]*)$")
string TraversalStartUri

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
    @length(min: 1, max: 256)
    name: String

    /// Value to match for the dimension. For compatibility, the server also
    /// accepts a JSON number (finite) or boolean here and binds it as a typed
    /// literal; `null`, objects, and arrays are rejected.
    @required
    @length(max: 4000)
    value: CustomerData

    /// Comparison operator to apply (defaults to equality). An empty or
    /// whitespace-only string is treated as absent.
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
    synthesizedAnswer: CustomerData

    /// The query that was executed to produce the result.
    queryUsed: CustomerData

    /// The SPARQL query generated for the result, if any.
    sparqlGenerated: CustomerData

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

    /// The execution mode that produced this result: "standard" or
    /// "deep-reasoning". Deep Reasoning is a mode orthogonal to the tier
    /// cascade, so a client labels an answer by this field rather than
    /// inferring mode from `tier` (a Deep Reasoning answer resolves at tier 3
    /// but is not Standard mode's Tier-3 result). Absent on responses that do
    /// not set it.
    executionMode: String
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

        /// Pin resolution to a specific tier (1, 2 or 3), bypassing automatic routing.
        @range(min: 1, max: 3)
        tierOverride: Integer

        /// Execution mode: "standard" (single-shot) or "deep-reasoning" (multi-step
        /// reasoning loop). Orthogonal to the tier cascade. Absent = the serve
        /// deployment default. Deep reasoning is higher-recall but much slower
        /// (p50 ~90s), so a caller behind a short client timeout should send
        /// "standard". "agentic" is the deprecated pre-rename spelling of
        /// "deep-reasoning" and is still accepted.
        mode: QueryMode

        /// Which Tier-2 engine answers a structured query. Absent = the serve
        /// default (``nl_to_sql_first``). Orthogonal to ``mode`` — see
        /// ``QueryStrategy``. Ignored when the resolved tier is not 2.
        strategy: QueryStrategy

        /// Dimension filters to constrain the query.
        @length(max: 100)
        dimensions: DimensionFilterList

        /// Maximum time to wait for the query, in milliseconds. A caller may
        /// narrow the service's own REST budget (29,000 ms) but not widen it.
        @range(min: 1, max: 29000)
        timeoutMs: Long

        /// Whether to include supporting content in the response. When false,
        /// `supportingContent` and the `supportingStatements` property of each
        /// `graphContext` entity are omitted; the entities and relationships are
        /// still returned.
        includeSupporting: Boolean

        /// When true, include the generated SQL (`queryUsed`) and SPARQL
        /// (`sparqlGenerated`) in the response for debugging and observability.
        /// Defaults to false; when unset or false, both fields are omitted.
        ///
        /// **Security:** these fields expose internal schema (table and column
        /// names, joins), so the flag is honoured only for a caller who already
        /// administers that schema: a platform admin, or a namespace owner,
        /// maintainer or data steward of the queried namespace. For any other
        /// caller the request still succeeds, but both fields are omitted as if
        /// the flag were false.
        includeDebugInfo: Boolean

        /// Maximum number of result rows to return.
        @range(min: 1, max: 10000)
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
        @range(min: 1, max: 100)
        topK: Integer

        /// Minimum relevance score a chunk must meet to be returned. Relevance
        /// scores are non-negative; no upper bound is set because the scale
        /// depends on the index's similarity metric.
        @range(min: 0)
        minScore: Float

        /// Not yet supported; a non-empty value is rejected with 400. Reserved
        /// for restricting results to these source document identifiers. An
        /// empty list is accepted and restricts nothing.
        sourceFilter: FilterValueList

        /// Not yet supported; a non-empty value is rejected with 400. Reserved
        /// for restricting results to chunks annotated with these entities. An
        /// empty list is accepted and restricts nothing.
        entityFilter: FilterValueList
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
        startUri: TraversalStartUri

        /// Maximum traversal depth from the starting entity. Defaults to 2.
        /// Depths above 2 are accepted but currently traverse 2 hops.
        @range(min: 1, max: 5)
        maxDepth: Integer

        /// Accepted and validated, but not yet applied: traversal currently
        /// follows every relationship. Reserved for restricting traversal to
        /// these relationship (predicate) IRIs.
        relationshipFilter: FilterValueList

        /// Accepted and validated, but not yet applied: traversal currently
        /// follows relationships in both directions. Defaults to `both`.
        direction: TraversalDirection

        /// Maximum number of entities to return. Defaults to 100.
        @range(min: 1, max: 1000)
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
        @range(min: 1, max: 1000)
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
