// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Ontology induction — shapes for automated ontology generation from data sources.
// The service definition lives in control-plane.smithy (ControlPlaneService).
// Induction operations are control plane: they orchestrate ontology generation
// from structured data source metadata (schema, constraints, relationships).
$version: "2"

namespace com.amazon.semanticcontext.ontologyinduction

use com.amazon.semanticcontext.common#ClientToken
use com.amazon.semanticcontext.common#ConflictError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#OntologyId
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#PaginationToken
use com.amazon.semanticcontext.common#PresignedUrl
use com.amazon.semanticcontext.common#ServiceUnavailableError
use com.amazon.semanticcontext.common#Uuid
use com.amazon.semanticcontext.common#ValidationError
use com.amazon.semanticcontext.metric#DataSourceId

// ── Local constrained primitives ────────────────────────────────────
/// Ontology URI prefix (IRI base for generated classes/properties).
/// Keep in sync with ONTOLOGY_URI_PREFIX_RE in libs/common/src/coa_common/constants.py.
@length(min: 10, max: 2048)
@pattern("^https?://[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*(?::(?:6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}|[1-9][0-9]{0,3}))?(?:/(?!\\.\\.?(?:[/#]|$))(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)*/?#?$")
string OntologyUriPrefix

/// Induction strategy name.
enum InductionStrategy {
    /// Structured strategy: induce an ontology directly from relational table schemas.
    TABLE_TO_ONTOLOGY = "table_to_ontology"

    /// The RIGOR ontology induction strategy. Available only for namespaces
    /// whose language has the primary subtag `en`; any other namespace is
    /// rejected with a validation error before a job starts.
    RIGOR_ONTOLOGY = "rigor_ontology"

    /// Unstructured strategy: induce an ontology from a lexical knowledge graph.
    UNSTRUCTURED_LEXICAL_GRAPH = "unstructured_lexical_graph"
}

/// Grounding mode — how aggressively induced classes are aligned to existing
/// ontologies. NONE disables grounding; STANDARD auto-grounds exact-name
/// matches; ENHANCED (default) always defers to the LLM reranker for
/// domain verification.
enum GroundingMode {
    /// Grounding disabled — induced classes are never aligned to existing ontologies.
    NONE = "NONE"

    /// Auto-grounds exact-name matches against existing ontologies.
    STANDARD = "STANDARD"

    /// Default — always defers to the LLM reranker for domain verification.
    ENHANCED = "ENHANCED"
}

/// Induction job status.
///
/// The first group of stages (PENDING through FAILED) covers the structured
/// induction pipeline (induce_catalog). The second group covers the
/// unstructured induction pipeline stage progression
/// (querying_neptune → sampling → generating_descriptions → resolving_iris →
/// applying_rules → entailment → serializing → storing → completed | failed).
enum InductionJobStatus {
    /// The job is queued and has not started.
    PENDING = "pending"

    /// Fetching data source metadata (schema, constraints, relationships).
    FETCHING_METADATA = "fetching_metadata"

    /// Loading the existing ontologies to ground against.
    LOADING_GROUNDING_ONTOLOGIES = "loading_grounding_ontologies"

    /// Generating embeddings for matching.
    GENERATING_EMBEDDINGS = "generating_embeddings"

    /// Matching induced concepts against grounding ontologies.
    MATCHING = "matching"

    /// Building the induced ontology.
    BUILDING_ONTOLOGY = "building_ontology"

    /// Persisting the induced ontology and proposal.
    STORING = "storing"

    /// The induction job finished successfully.
    COMPLETED = "completed"

    /// The induction job failed.
    FAILED = "failed"

    // ── Unstructured induction stages ────────────────────────────────
    /// Unstructured stage: querying the Neptune knowledge graph.
    QUERYING_NEPTUNE = "querying_neptune"

    /// Unstructured stage: sampling graph data.
    SAMPLING = "sampling"

    /// Unstructured stage: generating concept descriptions.
    GENERATING_DESCRIPTIONS = "generating_descriptions"

    /// Unstructured stage: resolving concept IRIs.
    RESOLVING_IRIS = "resolving_iris"

    /// Unstructured stage: applying induction rules.
    APPLYING_RULES = "applying_rules"

    /// Unstructured stage: running entailment/reasoning.
    ENTAILMENT = "entailment"

    /// Unstructured stage: serializing the induced ontology.
    SERIALIZING = "serializing"
}

/// Opt-in expansion for `GetInductionJob`. Omitting it returns the slim poll
/// shape: every scalar count, `status`, `proposalId`/`duplicateOf` and `error`
/// are preserved, but `report.matches` is returned empty so the response stays
/// under the API's 6MB response cap (at 50k tables the full match list holds
/// ~836k entries and makes a completed job unpollable). Per-match detail is
/// persisted on the proposal and read via the proposal endpoints.
enum IncludeMode {
    /// Return the full induction report including every entry in `report.matches`.
    /// May exceed the 6MB response cap on large inductions — intended for
    /// debugging, not for polling.
    REPORT = "report"

    /// Synonym for `report`.
    FULL = "full"
}

/// Discriminator distinguishing structured (table-to-ontology) induction runs
/// from unstructured (knowledge-graph-to-ontology) induction runs. Persisted
/// as a top-level attribute on `OntologyProposals` and induction-job items
/// so the steward review queue and list endpoints can filter by origin.
enum InductionSourceType {
    /// A structured (table-to-ontology) induction run.
    STRUCTURED = "STRUCTURED"

    /// An unstructured (knowledge-graph-to-ontology) induction run.
    UNSTRUCTURED = "UNSTRUCTURED"
}

/// Proposal status.
///
/// ``inducing`` is the FIRST in-flight state: the induction was triggered and
/// a background worker is building the ontology. A stub proposal row is written
/// at trigger time (so the run is visible in the proposals list immediately and
/// survives a browser refresh); the same row is upserted to ``pending`` when the
/// worker finishes, or to ``failed`` if it errors. Clients poll ``ListProposals``
/// to observe the transition.
///
/// ``accepting`` is a later in-flight state: the user submitted ``AcceptProposal``
/// and a background worker is merging the ontology. It is followed by
/// ``embeddingsSync`` while the just-written embeddings become searchable in the
/// (eventually-consistent) vector index. The status then flips to ``accepted``
/// on success, or to ``acceptFailed`` with ``acceptError`` naming the pipeline
/// step that failed. An ``acceptFailed`` proposal is re-acceptable — the accept
/// pipeline is idempotent on re-run — so the client may submit
/// ``AcceptProposal`` again once the cause is resolved. Clients poll
/// ``GetProposal`` to observe the transitions.
enum ProposalStatus {
    /// First in-flight state: induction triggered, a worker is building the ontology.
    INDUCING = "inducing"

    /// The ontology finished building and the proposal awaits review.
    PENDING = "pending"

    /// In-flight state: a worker is merging the accepted ontology into the namespace.
    ACCEPTING = "accepting"

    /// In-flight state following ``accepting``: the ontology is merged and the
    /// worker is waiting for the embeddings it wrote to become searchable in the
    /// eventually-consistent vector index.
    EMBEDDINGS_SYNC = "embeddings_sync"

    /// The proposal was accepted and merged successfully.
    ACCEPTED = "accepted"

    /// An accept ran but a pipeline step exhausted its retries. ``acceptError``
    /// names the failing step. Terminal for that attempt, but the proposal stays
    /// re-acceptable: the pipeline is idempotent, so re-submitting
    /// ``AcceptProposal`` after fixing the cause converges.
    ACCEPT_FAILED = "accept_failed"

    /// The proposal was rejected by a reviewer.
    REJECTED = "rejected"

    /// The proposal was cancelled.
    CANCELLED = "cancelled"

    /// The proposal's Turtle/R2RML content was revised by the user.
    UPDATED = "updated"

    /// Induction or acceptance failed.
    FAILED = "failed"
}

/// Concept match type.
enum MatchType {
    /// An exact match to an existing concept.
    EXACT = "exact"

    /// A high-confidence match to an existing concept.
    HIGH_CONFIDENCE = "high_confidence"

    /// An ambiguous match requiring review.
    AMBIGUOUS = "ambiguous"

    /// No match found — a novel concept was created.
    NOVEL = "novel"
}

/// Confidence threshold for matching (0.0 to 1.0).
@range(min: 0, max: 1)
float ConfidenceThreshold

/// Output-token cap for the ENHANCED-mode LLM grounding rerank.
/// The upper bound mirrors the induction pipeline's top-end output-token
/// budget; it is a schema guardrail against typos/absurd values, not an
/// Amazon Bedrock API limit. Omitted → the server default (1000) is used.
@range(min: 1, max: 8192)
integer RerankMaxTokens

// ── Operations ──────────────────────────────────────────────────────
/// Start an ontology induction job from one or more data sources.
/// Returns immediately with a job ID; induction runs asynchronously.
@http(method: "POST", uri: "/namespaces/{namespaceId}/induce", code: 202)
operation StartInduction {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Data source IDs to induct from.
        @required
        @length(max: 1000)
        datasourceIds: DataSourceIdList

        /// URI prefix for generated ontology classes and properties.
        @required
        ontologyUriPrefix: OntologyUriPrefix

        /// Human-readable label for this induction run.
        @length(max: 256)
        label: String

        /// Confidence threshold for matching against existing ontologies.
        confidenceThreshold: ConfidenceThreshold

        /// Output-token cap for the ENHANCED-mode LLM grounding rerank.
        /// Raise it when a reasoning model truncates its JSON answer (the
        /// rerank fails loud naming this knob). Omitted → server default (1000).
        rerankMaxTokens: RerankMaxTokens

        /// Induction strategy to use.
        strategy: InductionStrategy

        /// Existing ontology IDs to ground against (reuse existing concepts).
        groundingOntologyIds: GroundingOntologyIdList

        /// How aggressively to ground induced classes against existing ontologies.
        /// Defaults to ENHANCED when omitted.
        groundingMode: GroundingMode

        /// Caller-selected lexical sources are not supported. A Neptune Analytics
        /// graph ARN or graph id returns HTTP 422 with ``detail.code`` set to
        /// ``UNSUPPORTED_SOURCE``; the ``neptune-db`` sentinel names the
        /// deployment-configured lexical source, the same as omitting the field.
        @deprecated(message: "Caller-selected lexical sources are not supported; omit this field.", since: "2026-10-01")
        @length(min: 1, max: 2048)
        @pattern("^(?:neptune-db|g-[a-z0-9]+|arn:aws[a-z-]*:neptune-graph:[a-z0-9-]+:[0-9]{12}:graph/g-[a-z0-9]+)$")
        graphArn: String

        /// Optional idempotency token. A retry with the same token and the same
        /// parameters within 24 hours returns the original result instead of creating
        /// a duplicate; reusing the token with different parameters is rejected with
        /// ConflictError (409).
        @idempotencyToken
        clientToken: ClientToken
    }

    output := {
        /// Unique job identifier for tracking progress.
        @required
        jobId: Uuid
    }

    errors: [
        ValidationError
        ConflictError
    ]
}

/// Get the status and results of an induction job.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/induce/jobs/{jobId}", code: 200)
operation GetInductionJob {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        jobId: Uuid

        /// Omitted (default) returns a slim poll shape with `report.matches`
        /// emptied so the response stays under the API-proxy Lambda's 6MB
        /// response cap at scale (avoids 413 RequestEntityTooLarge). `report`
        /// or `full` returns the full report including every concept match,
        /// which may exceed that cap on large inductions. See #807.
        @httpQuery("include")
        include: IncludeMode
    }

    output := {
        @required
        jobId: Uuid

        @required
        status: InductionJobStatus

        /// Timestamp when the induction job was created.
        @required
        createdAt: Timestamp

        /// Induction report (populated when status is COMPLETED).
        report: InductionReport

        /// Error message (populated when status is FAILED).
        error: String
    }

    errors: [
        NotFoundError
    ]
}

/// List induction jobs for a namespace, one page at a time.
///
/// A page can hold fewer than `maxResults` jobs, or none, while `nextToken` is
/// present: each request reads a bounded slice of the job table. Keep paging
/// until `nextToken` is absent. Jobs are newest-first within a page only.
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "jobs")
@http(method: "GET", uri: "/namespaces/{namespaceId}/induce/jobs", code: 200)
operation ListInductionJobs {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Filter by job status.
        @httpQuery("status")
        status: InductionJobStatus

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of jobs to return in one page (default 50).
        @httpQuery("maxResults")
        @range(min: 1, max: 200)
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// Induction jobs matching the query.
        @required
        jobs: InductionJobSummaryList
    }
}

/// List data sources that have been successfully inducted, one page at a time,
/// ordered by data source ID.
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "datasources")
@http(method: "GET", uri: "/namespaces/{namespaceId}/induce/datasources/induced", code: 200)
operation ListInducedDatasources {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of data sources to return in one page (default 1000).
        @httpQuery("maxResults")
        @range(min: 1, max: 1000)
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// Data sources that have been successfully inducted.
        @required
        datasources: InducedDatasourceList
    }
}

/// List ontology proposals for a namespace, newest first, one page at a time.
///
/// Proposals are ordered by creation time, newest first, so the first page
/// holds the newest `maxResults` matches and later pages continue with older
/// ones. Keep paging until `nextToken` is absent.
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "proposals")
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals", code: 200)
operation ListProposals {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Filter by proposal status.
        @httpQuery("status")
        status: ProposalStatus

        /// Filter by target ontology ID. The server also accepts the legacy
        /// spelling `ontology_id`; this one wins when both are sent.
        @httpQuery("ontologyId")
        ontologyId: OntologyId

        /// Filter by induction origin. `STRUCTURED` also matches proposals
        /// recorded before the attribute existed.
        @httpQuery("source_type")
        sourceType: InductionSourceType

        /// Filter by proposal type (e.g. `induction`), exact match.
        @httpQuery("type")
        @length(min: 1, max: 64)
        proposalType: String

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: PaginationToken

        /// Maximum number of proposals to return in one page (default 50). The
        /// server also accepts the legacy spelling `limit`; this one wins.
        @httpQuery("maxResults")
        @range(min: 1, max: 200)
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// Proposals matching the query.
        @required
        proposals: ProposalSummaryList
    }
}

/// Get a specific proposal with full ontology content.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}", code: 200)
operation GetProposal {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid
    }

    output := {
        @required
        proposalId: Uuid

        @required
        status: ProposalStatus

        /// Ontology ID this proposal targets.
        @required
        ontologyId: String

        /// OWL ontology in Turtle format. Omitted for large proposals that are
        /// served out-of-band via `ontologyUrl` (a presigned S3 GET URL).
        ontologyTurtle: String

        /// R2RML mappings in Turtle format. Omitted when served via `r2rmlUrl`.
        r2rmlTurtle: String

        /// Presigned S3 GET URL for the OWL ontology Turtle when it is served
        /// out-of-band (proposals exceeding the API payload cap). Null when
        /// `ontologyTurtle` is inlined.
        ontologyUrl: PresignedUrl

        /// Presigned S3 GET URL for the R2RML Turtle when served out-of-band.
        r2rmlUrl: PresignedUrl

        /// Presigned S3 GET URL for the grounding-match list (`matches.json`)
        /// when it is served out-of-band. Null when the match list is small
        /// enough to be returned inline.
        matchesUrl: PresignedUrl

        /// Presigned S3 GET URL for the SHACL constraint config
        /// (`constraints.ttl`) when it is served out-of-band. Null when no
        /// constraints artifact exists, in which case any small inline
        /// `constraintConfig` in `metadata` is returned instead.
        constraintsUrl: PresignedUrl

        /// Proposal metadata (tables processed, classes created, etc.).
        metadata: ProposalMetadata

        /// Error message from the most recent failed accept attempt.
        /// Populated when an ``accepting`` → ``pending`` rollback occurred;
        /// cleared on the next ``AcceptProposal`` call.
        acceptError: String
    }

    errors: [
        NotFoundError
    ]
}

/// Accept a proposal asynchronously — merge its ontology into the target
/// namespace on a background worker. Returns 202 immediately with the
/// proposal flipped to ``accepting``. Clients poll ``GetProposal`` until
/// ``status`` becomes ``accepted`` (success) or ``pending`` (failed —
/// ``acceptError`` on the proposal record carries the reason).
///
/// Naturally idempotent, so there is no `clientToken`: accepting an
/// already-accepted proposal returns ``accepted`` without starting a new job,
/// and a repeat while the first accept is in progress is a ConflictError (409).
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/accept", code: 202)
operation AcceptProposal {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid

        /// Target ontology ID to merge into. If omitted, uses the proposal's own ontology ID.
        ontologyId: OntologyId
    }

    output := {
        @required
        proposalId: Uuid

        @required
        status: ProposalStatus
    }

    errors: [
        NotFoundError
        ConflictError
    ]
}

/// Reject one or more proposals.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/reject", code: 200)
operation RejectProposals {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// IDs of the proposals to reject.
        @required
        @length(max: 1000)
        proposalIds: ProposalIdList

        /// Reason for rejecting the proposals.
        @required
        @length(max: 4096)
        reason: String
    }

    output := {
        /// Number of proposals rejected.
        @required
        rejected: Integer
    }

    errors: [
        ValidationError
    ]
}

/// Cancel a pending proposal.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/cancel", code: 200)
operation CancelProposal {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid
    }

    output := {
        @required
        proposalId: Uuid

        @required
        status: ProposalStatus
    }

    errors: [
        NotFoundError
        ConflictError
    ]
}

/// Update a proposal's Turtle/R2RML content (user revision).
///
/// Uses ``POST /update`` with proposalId in the body rather than
/// ``PATCH /{proposalId}`` because PATCH ergonomics are awkward with
/// API Gateway request validation here. Either Turtle artifact may be
/// independently revised.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/update", code: 200)
operation UpdateProposal {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        proposalId: Uuid

        /// Revised OWL ontology in Turtle format.
        ontologyTurtle: TurtleDocument

        /// Revised R2RML mappings in Turtle format.
        r2rmlTurtle: TurtleDocument

        /// Class-level grounding choices keyed by the match's
        /// sourceTableIdentity, or by its bare sourceTable when that name is
        /// unique in the proposal. A bare sourceTable that several class-level
        /// matches share is rejected with ConflictError and nothing is saved.
        /// Values must be one of the candidates returned with the proposal.
        /// Grounding choices cannot be combined with Turtle/R2RML edits in the
        /// same request.
        @length(max: 1000)
        groundingOverrides: GroundingOverrideMap

        /// Source tables whose grounding should be cleared so they remain
        /// novel, keyed like groundingOverrides. Kept separate from
        /// groundingOverrides because OpenAPI map values cannot faithfully
        /// preserve Smithy sparse-map nulls.
        @length(max: 1000)
        novelTables: GroundingNovelTableList

        /// Set true when the revised OWL ontology was uploaded directly to S3
        /// via a presigned PUT (see GetProposalUploadUrls) instead of being sent
        /// inline in `ontologyTurtle` — for large proposals exceeding the API
        /// payload cap. Defaults to false (inline).
        ontologyUploaded: Boolean

        /// Set true when the revised R2RML was uploaded via presigned PUT
        /// instead of inline `r2rmlTurtle`. Defaults to false (inline).
        r2rmlUploaded: Boolean

        /// Immutable revision returned by GetProposalUploadUrls. Required when
        /// either uploaded flag is true so an upload cannot overwrite the
        /// proposal's currently active artifacts before validation succeeds.
        uploadRevisionId: Uuid
    }

    output := {
        @required
        proposalId: Uuid

        @required
        status: ProposalStatus
    }

    errors: [
        NotFoundError
        ConflictError
        ValidationError
        ServiceUnavailableError
    ]
}

/// Get create-only presigned S3 PUT URLs for a large revised proposal Turtle.
/// Each direct PUT must send `If-None-Match: *`, which is a signed S3 header,
/// and may create its artifact key only once. After every requested upload
/// succeeds, call UpdateProposal with `ontologyUploaded` / `r2rmlUploaded` set.
/// A 409, 412, or ambiguous network outcome requires requesting a fresh
/// revision and re-uploading every changed artifact; callers must not reuse the
/// prior URLs or assume that an uncertain write succeeded.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/upload-url", code: 200)
operation GetProposalUploadUrls {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid
    }

    output := {
        /// Revision containing both create-only upload targets. Pass this value
        /// to UpdateProposal as uploadRevisionId only after every requested PUT
        /// succeeds.
        @required
        revisionId: Uuid

        /// Create-only S3 PUT URL for the revised OWL ontology Turtle. Send
        /// `If-None-Match: *`; after 409, 412, or an ambiguous outcome, request
        /// a fresh revision rather than reusing this URL.
        @required
        ontologyPutUrl: PresignedUrl

        /// Create-only S3 PUT URL for the revised R2RML Turtle. Send
        /// `If-None-Match: *`; after 409, 412, or an ambiguous outcome, request
        /// a fresh revision rather than reusing this URL.
        @required
        r2rmlPutUrl: PresignedUrl
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Start LLM inference of semantic constraints from the proposal's ontology.
/// Runs asynchronously (the LLM call can exceed the API-GW 29s timeout);
/// returns 202 + a job handle. Poll GetInferConstraintsJob for the result.
/// Each call starts a new job; send a `clientToken` to make a retry safe.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints", code: 202)
operation InferConstraints {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid

        /// Optional idempotency token. A retry with the same token and the same
        /// parameters within 24 hours returns the original job handle instead of
        /// starting a second job; reusing the token with different parameters is
        /// rejected with a 409.
        @idempotencyToken
        clientToken: ClientToken
    }

    output := {
        @required
        jobId: Uuid

        @required
        proposalId: Uuid

        /// Initial job status (pending).
        @required
        status: String
    }

    errors: [
        NotFoundError
        ConflictError
    ]
}

/// Poll an async constraint-inference job. Status is pending | running |
/// completed | failed; inferred_constraints is present once completed.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints/jobs/{jobId}", code: 200)
operation GetInferConstraintsJob {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid

        @required
        @httpLabel
        jobId: Uuid
    }

    output := {
        @required
        jobId: Uuid

        @required
        proposalId: Uuid

        /// Job status (pending | running | completed | failed).
        @required
        status: String

        /// Error message if status is failed.
        error: String

        /// Inferred ConstraintConfig (present once status is completed).
        inferred_constraints: Document
    }

    errors: [
        NotFoundError
    ]
}

/// Compile a reviewed ConstraintConfig into SHACL Turtle.
///
/// Pure computation over the request and the stored proposal; nothing is
/// persisted, so a retry is harmless and there is no `clientToken`.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/compile-constraints", code: 200)
operation CompileConstraints {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid

        /// Reviewed ConstraintConfig to compile into SHACL. At most 65536 bytes
        /// once serialized as JSON; larger configs are rejected with a 400.
        @required
        @tags(["bounded-at-runtime"])
        constraint_config: Document

        /// Optional custom SHACL Turtle to merge with the compiled shapes.
        custom_turtle: TurtleDocument
    }

    output := {
        @required
        proposalId: Uuid

        /// Compiled SHACL shapes in Turtle format.
        @required
        shapes_turtle: String
    }

    errors: [
        NotFoundError
    ]
}

/// Trigger validation of a proposal — runs the configured tier validators
/// (HermiT reasoning, structural metrics, OoPS!) and returns a job ID for
/// polling. Each call starts a new job; send a `clientToken` to make a retry
/// safe.
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/validate", code: 202)
operation ValidateProposal {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid

        /// Validator tiers to run (defaults to all three tiers if omitted).
        @length(max: 10)
        tiers: ProposalValidationTierList

        /// Optional SHACL shapes Turtle for tier-1 SHACL validation.
        shaclShapesTurtle: TurtleDocument

        /// Optional competency-question prompts for tier-3 LLM validators.
        competencyQuestions: CompetencyQuestionList

        /// Optional idempotency token. A retry with the same token and the same
        /// parameters within 24 hours returns the original job handle instead of
        /// starting a second validation job; reusing the token with different
        /// parameters is rejected with a 409.
        @idempotencyToken
        clientToken: ClientToken
    }

    output := {
        @required
        jobId: Uuid

        @required
        proposalId: Uuid

        @required
        status: ProposalValidationJobStatus

        /// Timestamp when the validation job was created.
        @required
        createdAt: String
    }

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Get a proposal validation job.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/validate/jobs/{jobId}", code: 200)
operation GetProposalValidationJob {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid

        @required
        @httpLabel
        jobId: Uuid
    }

    output := {
        @required
        jobId: Uuid

        @required
        proposalId: Uuid

        @required
        status: ProposalValidationJobStatus

        /// Timestamp when the validation job was created.
        @required
        createdAt: String

        /// Error message if status is FAILED.
        error: String

        /// Validation report (present once status is COMPLETED).
        report: ProposalValidationReport
    }

    errors: [
        NotFoundError
    ]
}

/// Repair malformed / mis-cased / aliased XSD datatype tokens in a proposal's
/// stored Turtle (e.g. `xsd:datetime` → `xsd:dateTime`, `xsd:varchar` →
/// `xsd:string`). User-initiated + consent-gated: the DatatypeTokenValidator
/// surfaces the offending tokens at validation time, and the user invokes this
/// to apply the fix. Server-authoritative (the canonical XSD map lives on the
/// server, not the client). Rewrites known-malformed tokens only — valid types
/// (incl. `xsd:date`) and unrecognized `xsd:` tokens are left untouched.
///
/// With `dryRun` true, computes and returns the same change list WITHOUT
/// persisting — used by the accept-time guard to detect unrepaired datatype
/// issues before an accept, without mutating the proposal.
///
/// Naturally idempotent, so there is no `clientToken`: a repeat finds the
/// tokens already canonical and rewrites nothing (`repairedCount` 0).
@http(method: "POST", uri: "/namespaces/{namespaceId}/proposals/{proposalId}/repair-datatypes", code: 200)
operation RepairProposalDatatypes {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        proposalId: Uuid

        /// When true, preview the repairs (return the change list) without
        /// persisting anything or changing the proposal's status. Defaults false.
        dryRun: Boolean
    }

    output := {
        @required
        proposalId: Uuid

        /// Number of datatype tokens rewritten (0 when already clean). On a
        /// dry run this is the number that WOULD be rewritten.
        @required
        repairedCount: Integer

        /// The (before → after) rewrites applied (or, on a dry run, that would
        /// be applied), for the preview / post-repair summary.
        @required
        changes: DatatypeRepairChangeList

        /// Echoes the request mode: true when this was a non-persisting preview.
        dryRun: Boolean
    }

    errors: [
        NotFoundError
        ConflictError
        ValidationError
    ]
}

// ── Structures ──────────────────────────────────────────────────────
/// A single datatype-token rewrite applied by RepairProposalDatatypes.
structure DatatypeRepairChange {
    /// The malformed datatype IRI that was found.
    @required
    found: String

    /// The canonical XSD datatype IRI it was rewritten to.
    @required
    canonical: String
}

list DatatypeRepairChangeList {
    member: DatatypeRepairChange
}

/// Report generated after a successful induction run.
structure InductionReport {
    /// Number of tables processed during induction.
    @required
    tablesProcessed: Integer

    /// Number of columns processed during induction.
    @required
    columnsProcessed: Integer

    /// Number of novel classes created (not grounded to existing concepts).
    @required
    novelClassesCreated: Integer

    /// Concept match results produced by induction.
    @required
    matches: ConceptMatchList

    /// Ontology IDs used for grounding.
    groundingOntologiesUsed: OntologyIdList

    /// Proposal ID generated by this induction.
    inducedOntologyId: String

    /// Tables excluded from the induced ontology because they could not be
    /// processed (e.g. the generation LLM returned nothing, or its output
    /// failed to parse). Empty for strategies that never drop tables.
    droppedTables: DroppedTableList
}

/// A table that could not be processed during induction.
structure DroppedTable {
    /// Name of the table that was dropped.
    @required
    tableName: String

    /// Short failure category, e.g. "gen_llm_empty" or "parse_failed".
    @required
    reason: String
}

list DroppedTableList {
    member: DroppedTable
}

/// A single concept match result.
structure ConceptMatch {
    /// Source table the match originates from.
    @required
    sourceTable: String

    /// Stable data-source-qualified identity for the source table. New
    /// structured inductions populate this so same-named tables remain
    /// distinguishable; absent on legacy and unstructured matches.
    sourceTableIdentity: String

    /// Source column the match originates from.
    @required
    sourceColumn: String

    /// Matched class URI (null if novel).
    matchedClassUri: String

    /// Ontology ID containing the matched class.
    matchedOntologyId: String

    /// Similarity score (0.0 to 1.0).
    similarity: Float

    @required
    matchType: MatchType
}

/// Summary of an induction job (for list views).
structure InductionJobSummary {
    @required
    jobId: Uuid

    @required
    status: InductionJobStatus

    /// Timestamp when the induction job was created.
    @required
    createdAt: Timestamp

    /// Origin of this induction run — STRUCTURED for table-to-ontology runs,
    /// UNSTRUCTURED for knowledge-graph-to-ontology runs. Items lacking this
    /// attribute on read SHALL be treated as STRUCTURED for backward compatibility.
    sourceType: InductionSourceType

    /// Number of tables processed (populated after completion).
    tablesProcessed: Integer

    /// Number of novel classes created.
    novelClassesCreated: Integer

    /// Error message if failed.
    error: String
}

/// Summary of a proposal (for list views).
structure ProposalSummary {
    @required
    proposalId: Uuid

    @required
    status: ProposalStatus

    /// Ontology ID this proposal targets.
    @required
    ontologyId: String

    /// Origin of this proposal — STRUCTURED for table-to-ontology induction,
    /// UNSTRUCTURED for knowledge-graph-to-ontology induction. Items lacking
    /// this attribute on read SHALL be treated as STRUCTURED for backward
    /// compatibility.
    sourceType: InductionSourceType

    /// Proposal metadata.
    metadata: ProposalMetadata
}

/// Metadata about a proposal.
structure ProposalMetadata {
    /// Data source IDs this proposal was induced from.
    datasourceIds: DataSourceIdList

    /// Human-readable label for the induction run.
    label: String

    /// Number of tables processed during induction.
    tablesProcessed: Integer

    /// Number of columns processed during induction.
    columnsProcessed: Integer

    /// Number of novel classes created (not grounded to existing concepts).
    novelClassesCreated: Integer
}

/// Summary of an inducted data source.
structure InducedDatasource {
    @required
    datasourceId: DataSourceId

    proposalId: Uuid

    /// Ontology ID produced for this data source.
    ontologyId: String

    /// Human-readable label for the induction run.
    label: String

    /// Number of tables processed during induction.
    tablesProcessed: Integer

    /// Number of columns processed during induction.
    columnsProcessed: Integer

    /// Number of novel classes created (not grounded to existing concepts).
    novelClassesCreated: Integer
}

/// Embedding result from proposal acceptance.
structure EmbeddingResult {
    /// Number of embeddings written.
    count: Integer

    /// Name of the index the embeddings were written to.
    index: String
}

// ── Collections ─────────────────────────────────────────────────────
/// Non-empty absolute grounding target URI. Runtime validation also rejects
/// whitespace, non-printable characters, and relative references for callers
/// that bypass generated Smithy clients.
@length(min: 1, max: 2048)
string GroundingOverrideTargetUri

/// Source-table grounding choices. Values are candidate ontology class URIs.
map GroundingOverrideMap {
    key: GroundingSourceTableKey
    value: GroundingOverrideTargetUri
}

/// Source tables whose grounding should be cleared so they remain novel.
list GroundingNovelTableList {
    member: GroundingSourceTableKey
}

/// A match's sourceTableIdentity, or its bare sourceTable name.
@length(min: 1, max: 1024)
string GroundingSourceTableKey

list DataSourceIdList {
    member: DataSourceId
}

list OntologyIdList {
    member: String
}

/// Existing ontology IDs to ground an induction run against. Input-only; the
/// induced proposal reports what it actually used in `OntologyIdList`.
@length(max: 100)
list GroundingOntologyIdList {
    member: OntologyId
}

/// Natural-language competency questions a proposal is checked against.
@length(max: 100)
list CompetencyQuestionList {
    member: CompetencyQuestion
}

/// A single competency question.
@length(min: 1, max: 4096)
string CompetencyQuestion

/// An RDF Turtle document sent inline. Bounded by the 6 MB synchronous
/// request ceiling of the serving function; larger artifacts go through the
/// presigned-upload path instead.
@length(max: 6000000)
string TurtleDocument

list ConceptMatchList {
    member: ConceptMatch
}

list InductionJobSummaryList {
    member: InductionJobSummary
}

list ProposalSummaryList {
    member: ProposalSummary
}

list InducedDatasourceList {
    member: InducedDatasource
}

list ProposalIdList {
    member: Uuid
}

list StringList {
    member: String
}

// ── Proposal validation ─────────────────────────────────────────────
/// Validation tier identifier — selects which validators to run.
enum ProposalValidationTier {
    /// Tier 1: blocking validators (findings block acceptance).
    TIER1_BLOCKING = "tier1_blocking"

    /// Tier 2: scoring validators (findings contribute to a quality score).
    TIER2_SCORING = "tier2_scoring"

    /// Tier 3: review validators (findings surfaced for human review).
    TIER3_REVIEW = "tier3_review"
}

list ProposalValidationTierList {
    member: ProposalValidationTier
}

/// Status of a proposal validation job.
enum ProposalValidationJobStatus {
    /// The validation job is queued and has not started.
    PENDING = "pending"

    /// The validation job is running.
    RUNNING = "running"

    /// The validation job finished successfully.
    COMPLETED = "completed"

    /// The validation job failed.
    FAILED = "failed"
}

/// Severity of a single validation finding.
enum ProposalValidationSeverity {
    /// An error-level finding.
    ERROR = "error"

    /// A warning-level finding.
    WARNING = "warning"

    /// An informational finding.
    INFO = "info"
}

/// One validation finding (e.g. a HermiT inconsistency, OoPS pitfall).
structure ProposalValidationFinding {
    /// Name of the validator that produced this finding.
    @required
    validator: String

    @required
    tier: ProposalValidationTier

    @required
    severity: ProposalValidationSeverity

    /// Machine-readable code identifying the finding.
    @required
    code: String

    /// Human-readable description of the finding.
    @required
    message: String

    /// Subject IRI the finding refers to (a class/property URI).
    subject: String

    /// Structured, finding-specific payload. For MALFORMED_DATATYPE_TOKENS this
    /// carries `offenders` (a list of `{subject, predicate, found, canonical}`),
    /// `count`, and `element_count` so the UI can preview the proposed repair.
    details: Document
}

list ProposalValidationFindingList {
    member: ProposalValidationFinding
}

/// Aggregated validation report.
structure ProposalValidationReport {
    @required
    jobId: Uuid

    @required
    proposalId: Uuid

    /// Whether the proposal passed validation (no blocking findings).
    @required
    passed: Boolean

    /// All findings produced across the validation tiers.
    @required
    findings: ProposalValidationFindingList

    /// Validation tiers that were run.
    @required
    tiersRun: ProposalValidationTierList

    /// Timestamp when the validation report was created.
    @required
    createdAt: String
}

// ──────────────────────────────────────────────
// Foundational Ontologies
// ──────────────────────────────────────────────
/// List the curated catalog of foundational ontologies available for grounding.
///
/// Not paginated: the catalog is a fixed list compiled into the service (a
/// handful of entries), not data that grows per namespace.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/ontology/foundational", code: 200)
operation ListFoundationalOntologies {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid
    }

    output := {
        /// Catalog of foundational ontologies available for grounding.
        @required
        items: FoundationalOntologyList
    }
}

/// Trigger loading/embedding of a foundational ontology into the namespace.
/// Each call starts a new load job; send a `clientToken` to make a retry safe.
@http(method: "POST", uri: "/namespaces/{namespaceId}/ontology/foundational/{key}/load", code: 202)
operation LoadFoundationalOntology {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Catalog key of the foundational ontology to load.
        @required
        @httpLabel
        @length(min: 1, max: 128)
        key: String

        /// Optional idempotency token. A retry with the same token within 24
        /// hours returns the original job handle instead of starting a second
        /// load; reusing the token for a different key is rejected with a 409.
        @idempotencyToken
        clientToken: ClientToken
    }

    output := {
        /// Load status for the foundational ontology.
        @required
        status: String

        /// Ontology ID assigned to the loaded foundational ontology.
        ontologyId: String

        /// Number of classes loaded.
        classCount: Integer

        /// Number of embeddings generated.
        embeddingCount: Integer
    }

    errors: [
        ValidationError
        NotFoundError
    ]
}

list FoundationalOntologyList {
    member: FoundationalOntologySummary
}

/// A foundational (reference) ontology available to ground induction against,
/// identified by its key and title, with an optional description, URI, and
/// domain tags.
structure FoundationalOntologySummary {
    /// Catalog key identifying the foundational ontology.
    @required
    key: String

    /// Display title of the foundational ontology.
    @required
    title: String

    /// Short description of the ontology's scope.
    description: String

    /// Canonical URI of the foundational ontology.
    uri: String

    /// Domain tags categorizing the ontology.
    domainTags: StringList
}
