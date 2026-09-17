// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Ontology graph + catalog query operations.
//
// Read-only namespace-scoped queries against the ontology graph and
// catalog registry. Used by the web UI's Graph Search and Catalog views.
// Backend FastAPI routers live under packages/ontology-engine/.../catalog/.
$version: "2"

namespace com.amazon.semanticcontext.ontologygraph

use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#Uuid
use com.amazon.semanticcontext.common#ValidationError

// ── Catalog ─────────────────────────────────────────────────────────
/// Coarse-grained ontology category for filtering the catalog.
enum OntologyType {
    /// A foundational (upper / reference) ontology.
    FOUNDATIONAL = "foundational"

    /// An ontology induced from data sources (e.g. tables).
    INDUCED = "induced"

    /// An ontology created manually by a user.
    USER_CREATED = "user_created"
}

/// Summary of an ontology in the namespace registry.
structure OntologyRecord {
    /// Unique identifier of the ontology within the namespace.
    @required
    ontologyId: String

    /// Canonical IRI of the ontology.
    @required
    uri: String

    /// Human-readable display title.
    title: String

    /// Human-readable description of the ontology.
    description: String

    /// Category of the ontology (foundational, induced, or user_created).
    ontologyType: String

    /// Ingest lifecycle marker set by the async parse/embed worker: "pending"
    /// while ingesting, "ok" once parsed, "parse_error" on failure. Absent on
    /// legacy rows persisted before this field existed, for which a positive
    /// embeddingCount means the ingest completed.
    parseStatus: String

    /// Lifecycle status of the registry row. Set to "deleting" while an async
    /// ontology delete tears down its graph + embeddings (the row stays listed
    /// as "Delete in progress" until teardown completes, then is removed).
    /// Absent for normal, fully-materialized ontologies.
    status: String

    /// Set when an async delete's teardown failed (graph/embedding hard gate),
    /// leaving the row in status "deleting". Lets a client distinguish a stuck
    /// delete (re-issue DELETE to retry) from one still in progress. Cleared
    /// when a fresh delete starts; absent otherwise.
    deleteError: String

    /// Serialization format of the source file (e.g. turtle, rdfxml, jsonld).
    format: String

    /// Domain tags used to categorize and filter the ontology.
    domainTags: StringList

    /// Number of classes in the ontology.
    classCount: Integer

    /// Number of properties in the ontology.
    propertyCount: Integer

    /// Number of axioms in the ontology.
    axiomCount: Integer

    /// Number of vector embeddings generated for the ontology.
    embeddingCount: Integer

    /// IRI of the named graph holding the ontology's triples.
    graphUri: String

    /// Creation timestamp of the registry row.
    createdAt: String

    /// Last-updated timestamp of the registry row.
    updatedAt: String
}

list OntologyRecordList {
    member: OntologyRecord
}

list StringList {
    member: String
}

/// List ontologies registered in a namespace.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/ontologies", code: 200)
operation ListOntologies {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @httpQuery("ontology_type")
        ontologyType: OntologyType

        /// Filters the listing to ontologies carrying this domain tag.
        @httpQuery("domain_tag")
        domainTag: String

        /// Maximum number of ontologies to return.
        @httpQuery("limit")
        limit: Integer
    }

    output := {
        /// The ontologies registered in the namespace.
        @required
        ontologies: OntologyRecordList
    }
}

/// Get a single ontology by ID.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/ontologies/{ontologyId}", code: 200)
operation GetOntology {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the ontology to fetch.
        @required
        @httpLabel
        ontologyId: String
    }

    output := {
        /// The requested ontology record.
        @required
        ontology: Document
    }
}

/// Delete an induced ontology and all its artifacts (graph triples,
/// vector embeddings, and registry entry) from a namespace.
///
/// Async — returns **202** immediately: the registry entry is removed
/// synchronously (so ``ListOntologies`` no longer lists it — the client-visible
/// status), then the slower Neptune named-graph DROP and per-embedding AOSS
/// teardown run in the background. Clients poll ``ListOntologies`` until the id
/// is gone. This mirrors the async accept flow and avoids the API Gateway 29s
/// integration timeout (504) on large ontologies.
///
/// The ontology id is a full IRI (contains ``://`` and ``#``), so it is passed
/// as a query parameter rather than a path label — API Gateway mangles a
/// URL-encoded ``#`` in a path segment (falls through to IAM auth → 403). This
/// mirrors the graph vertex-lookup endpoints (``/graph/class?uri=``).
@idempotent
@http(method: "DELETE", uri: "/namespaces/{namespaceId}/ontologies", code: 202)
operation DeleteOntology {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Full IRI of the ontology to delete (passed as a query parameter).
        @required
        @httpQuery("ontology_id")
        ontologyId: String
    }
}

/// Upload an ontology file (Turtle, RDF/XML, or JSON-LD).
@http(method: "POST", uri: "/namespaces/{namespaceId}/ontologies/{ontologyId}/upload", code: 200)
operation UploadOntology {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the ontology the file belongs to.
        @required
        @httpLabel
        ontologyId: String

        /// Raw ontology file contents (Turtle, RDF/XML, or JSON-LD).
        @required
        @httpPayload
        body: Blob
    }

    output := {
        /// Result of the upload operation.
        @required
        result: Document
    }
}

/// Download an ontology as Turtle.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/ontologies/{ontologyId}/download", code: 200)
operation DownloadOntology {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the ontology to download.
        @required
        @httpLabel
        ontologyId: String
    }

    output := {
        /// Presigned S3 GET URL for the ontology serialized as Turtle. The
        /// ontology is written to the artifacts bucket and served out-of-band
        /// rather than inlined, because a large induced ontology (6+ MB) would
        /// exceed the API Gateway / Lambda response limit and 502 the call.
        @required
        downloadUrl: String

        /// Seconds until the presigned URL expires.
        expiresInSeconds: Integer
    }
}

/// Fetch an ontology from its source URL and ingest it.
@http(method: "POST", uri: "/namespaces/{namespaceId}/ontologies/{ontologyId}/fetch", code: 200)
operation FetchOntology {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier to register the fetched ontology under.
        @required
        @httpLabel
        ontologyId: String

        /// Source URL to fetch the ontology from.
        sourceUrl: String
    }

    output := {
        /// Result of the fetch-and-ingest operation.
        @required
        result: Document
    }
}

/// Generate a presigned S3 URL for ontology file upload.
@http(method: "POST", uri: "/namespaces/{namespaceId}/ontologies/upload-url", code: 200)
operation GetOntologyUploadUrl {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Name of the file to be uploaded.
        @required
        filename: String

        /// MIME content type of the file to be uploaded.
        contentType: String

        /// Identifier to register the uploaded ontology under.
        ontologyId: String

        /// Human-readable display title for the ontology.
        title: String
    }

    output := {
        /// Presigned S3 URL to PUT the file to.
        @required
        uploadUrl: String

        /// S3 object key where the file will be stored.
        @required
        s3Key: String

        /// Identifier assigned to the ontology.
        @required
        ontologyId: String
    }
}

/// Lifecycle states of an async ontology-ingest job.
enum IngestJobState {
    /// The ingest job is queued and has not started yet.
    PENDING = "pending"

    /// The ingest job is currently running.
    RUNNING = "running"

    /// Ingest finished writing; waiting for the just-written embeddings to
    /// become searchable in the (eventually-consistent) vector index before
    /// the job is reported COMPLETED.
    EMBEDDINGS_SYNC = "embeddings_sync"

    /// The ingest job finished successfully.
    COMPLETED = "completed"

    /// The ingest job failed.
    FAILED = "failed"
}

/// Counts produced once an ingest job completes.
structure IngestJobResult {
    /// Identifier of the ingested ontology.
    ontologyId: String

    /// Number of classes ingested.
    classCount: Integer

    /// Number of embeddings generated.
    embeddingCount: Integer
}

/// Async ingest job handle / status — returned by ingest-from-s3 (202) and
/// the ingest-status poll. Typed so FE/BE share one contract.
structure IngestJob {
    /// Identifier of the async ingest job.
    @required
    jobId: String

    /// Identifier of the ontology being ingested.
    @required
    ontologyId: String

    @required
    status: IngestJobState

    /// Error message when the job failed; absent otherwise.
    error: String

    result: IngestJobResult
}

/// Ingest an ontology file from S3 (async — returns 202, poll ingest-status).
@http(method: "POST", uri: "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-from-s3", code: 202)
operation IngestOntologyFromS3 {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier to register the ingested ontology under.
        @required
        @httpLabel
        ontologyId: String

        /// S3 object key of the uploaded ontology file.
        @required
        s3Key: String

        /// Serialization format of the file (e.g. turtle, rdfxml, jsonld).
        format: String

        /// Human-readable display title for the ontology.
        title: String

        /// Category of the ontology (foundational, induced, or user_created).
        ontologyType: String
    }

    output := {
        @required
        result: IngestJob
    }
}

/// Poll ingest job status.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-status/{jobId}", code: 200)
operation GetIngestStatus {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the ontology whose ingest job is being polled.
        @required
        @httpLabel
        ontologyId: String

        /// Identifier of the ingest job to poll.
        @required
        @httpLabel
        jobId: String
    }

    output := {
        @required
        result: IngestJob
    }
}

// ── Graph ───────────────────────────────────────────────────────────
/// Vertex kind discriminator.
enum GraphVertexKind {
    /// An OWL/RDFS class.
    CLASS = "class"

    /// An object property (relates individuals to individuals).
    OBJECT_PROPERTY = "object-property"

    /// A datatype property (relates individuals to literal values).
    DATATYPE_PROPERTY = "datatype-property"

    /// An annotation property (metadata such as labels or comments).
    ANNOTATION_PROPERTY = "annotation-property"

    /// The ontology header node itself.
    ONTOLOGY = "ontology"

    /// A property of unspecified kind.
    PROPERTY = "property"

    /// A literal value node.
    LITERAL = "literal"

    /// A blank (anonymous) node.
    BLANK_NODE = "blank-node"

    /// A vertex whose kind could not be determined.
    UNKNOWN = "unknown"
}

/// A single result from a graph search query, identifying a matched vertex
/// by its IRI, label, kind, and the named graph it lives in.
structure GraphSearchHit {
    /// IRI of the matched vertex.
    @required
    uri: String

    /// Human-readable label of the matched vertex.
    label: String

    /// Vertex kind (class, object-property, datatype-property, etc.).
    @required
    kind: String

    /// IRIs of the named graphs the vertex is defined in. A class IRI asserted
    /// in more than one ontology (e.g. a shared IRI referenced by both
    /// schema.org and FIBO) lists every source graph, not just one.
    graphUris: StringList
}

list GraphSearchHitList {
    member: GraphSearchHit
}

/// A neighboring vertex reached by traversing an edge, identified by its IRI,
/// label, and kind.
structure GraphNeighbor {
    /// IRI of the neighboring vertex.
    @required
    uri: String

    /// Human-readable label of the neighboring vertex.
    label: String

    /// Vertex kind of the neighbor.
    @required
    kind: String
}

/// A relationship edge between two graph vertices, carrying the connecting
/// predicate, its direction, and the neighboring vertex on the other end.
structure GraphEdge {
    /// IRI of the connecting predicate.
    @required
    predicate: String

    /// Human-readable label of the predicate.
    predicateLabel: String

    /// Direction of the edge relative to the source vertex (incoming/outgoing).
    @required
    direction: String

    @required
    neighbor: GraphNeighbor
}

list GraphEdgeList {
    member: GraphEdge
}

/// A node in the ontology graph — a class, property, or individual — with its
/// IRI, kind, namespace, associated labels/comments, and outgoing edges.
structure GraphVertex {
    /// IRI of the vertex.
    @required
    uri: String

    /// Vertex kind (class, object-property, datatype-property, etc.).
    @required
    kind: String

    /// Namespace the vertex belongs to.
    @required
    namespace: String

    /// RDF types (rdf:type) declared on the vertex.
    types: StringList

    /// Human-readable labels (rdfs:label) of the vertex.
    labels: StringList

    /// Descriptive comments (rdfs:comment) on the vertex.
    comments: StringList

    /// Alternative labels (skos:altLabel) — carries source synonyms curated
    /// during enrichment and emitted onto the induced class/property.
    altLabels: StringList

    /// IRIs of the named graphs the vertex appears in.
    graphUris: StringList

    /// Whether this vertex carries the ``coa:isMapped`` marker — i.e. it is
    /// backed by an R2RML mapping and therefore answerable by the structured
    /// Tier-2 (NL→SPARQL / NL→SQL) path. Structured / table-backed classes are
    /// true; unstructured-induced and foundational classes are false (the marker
    /// is absent). Derived from the top-level ``coa:isMapped`` triple and
    /// excluded from ``edges``.
    isMapped: Boolean

    /// Outgoing relationship edges from this vertex.
    edges: GraphEdgeList
}

/// Substring search over entity IRIs and labels in a namespace's graphs.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/graph/search", code: 200)
operation GraphSearch {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Substring search term matched against entity IRIs and labels.
        @required
        @httpQuery("q")
        q: String

        @httpQuery("kind")
        kind: GraphVertexKind

        /// Restricts results to the specified ontology's named graph.
        @httpQuery("ontology_id")
        ontologyId: String

        /// Excludes results from the specified ontology's named graph
        /// (e.g., to filter out governed metrics classes).
        @httpQuery("exclude_ontology_id")
        excludeOntologyId: String

        /// Maximum number of hits to return (the page size). Bounded because the
        /// value drives the underlying query's LIMIT clause; out-of-range values
        /// are rejected with a 400.
        @httpQuery("limit")
        @range(min: 1, max: 500)
        limit: Integer

        /// Number of leading hits to skip, for pagination. Combined with
        /// ``limit`` this yields a stable page window over the full,
        /// deterministically-ordered result set.
        @httpQuery("offset")
        @range(min: 0)
        offset: Integer
    }

    output := {
        /// The matching graph search hits (the requested page window).
        @required
        hits: GraphSearchHitList

        /// Total number of hits matching the query across all pages, so a
        /// client can render a known page count. Independent of ``limit`` /
        /// ``offset``.
        totalCount: Integer
    }
}

/// Look up a class vertex by IRI.
///
/// The IRI is passed as a query parameter (``?uri=…``) rather than a
/// path label to avoid double-encoding issues with slashes and hash
/// fragments common in OWL IRIs (e.g. ``http://example.org/ont#Foo``).
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/graph/class", code: 200)
operation GetGraphClass {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// IRI of the class vertex to look up.
        @required
        @httpQuery("uri")
        uri: String
    }

    output: GraphVertex

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Look up an object-property vertex by IRI (query param).
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/graph/object-property", code: 200)
operation GetGraphObjectProperty {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// IRI of the object-property vertex to look up.
        @required
        @httpQuery("uri")
        uri: String
    }

    output: GraphVertex

    errors: [
        NotFoundError
        ValidationError
    ]
}

/// Look up a datatype-property vertex by IRI (query param).
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/graph/datatype-property", code: 200)
operation GetGraphDatatypeProperty {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// IRI of the datatype-property vertex to look up.
        @required
        @httpQuery("uri")
        uri: String
    }

    output: GraphVertex

    errors: [
        NotFoundError
        ValidationError
    ]
}

// ── Ontology overview ───────────────────────────────────────────────
/// One class persisted in an ontology's named graph.
structure OntologyClassSummary {
    /// IRI of the class.
    @required
    uri: String

    /// Human-readable label (rdfs:label) of the class.
    label: String

    /// Descriptive comment (rdfs:comment) on the class.
    comment: String

    /// IRI of the previously-loaded class this class was grounded to
    /// (coa:groundedTo / skos:exactMatch / skos:closeMatch). Absent when the
    /// class is novel (net-new this induction).
    groundedTo: String

    /// "exact" or "close" when grounded via a SKOS mapping; otherwise absent.
    matchType: String

    /// IRIs of this class's direct super-classes (``rdfs:subClassOf``). Lets a
    /// caller reconstruct the taxonomy (roots + parent/child edges) from the
    /// overview alone, without an N-per-class vertex fan-out. Empty/absent for
    /// a top-level class with no loaded parent.
    subClassOf: StringList
}

list OntologyClassSummaryList {
    member: OntologyClassSummary
}

/// One property persisted in an ontology's named graph. Object properties
/// carry a class IRI in ``range`` (a relationship); datatype properties carry
/// a datatype IRI (e.g. xsd:string).
structure OntologyPropertySummary {
    /// IRI of the property.
    @required
    uri: String

    /// Human-readable label (rdfs:label) of the property.
    label: String

    /// IRI of the property's domain class.
    domain: String

    /// IRI of the property's range (a class IRI for object properties, a
    /// datatype IRI for datatype properties).
    range: String
}

list OntologyPropertySummaryList {
    member: OntologyPropertySummary
}

/// Everything persisted in one ontology's named graph, grouped for display.
/// Read from the graph store (not the registry), so it reflects the triples
/// that actually landed in the knowledge graph.
structure OntologyOverview {
    /// Identifier of the ontology.
    @required
    ontologyId: String

    /// Namespace the ontology belongs to.
    @required
    namespace: String

    /// IRI of the named graph holding the ontology's triples.
    graphUri: String

    /// Classes persisted in the ontology's named graph.
    classes: OntologyClassSummaryList

    /// Object properties — relationships between classes (domain → range class).
    objectProperties: OntologyPropertySummaryList

    /// Datatype properties — attributes of a class with a literal range.
    datatypeProperties: OntologyPropertySummaryList

    /// Total number of classes in the ontology (independent of limit/offset),
    /// so a client can show the true count while rendering one page.
    totalClasses: Integer

    /// Total number of object properties in the ontology (independent of limit/offset).
    totalObjectProperties: Integer

    /// Total number of datatype properties in the ontology (independent of limit/offset).
    totalDatatypeProperties: Integer
}

/// Enumerate the classes + properties persisted in one ontology's graph.
///
/// Reads the ontology's named graph directly, returning its classes,
/// object properties (relationships), and datatype properties. Used by the
/// web UI's induced-ontology detail view.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/graph/ontology-overview", code: 200)
operation GetOntologyOverview {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the ontology to summarize.
        @required
        @httpQuery("ontology_id")
        ontologyId: String

        /// Max entities to return per collection (classes, object properties,
        /// datatype properties are each capped independently). Bounds the
        /// response so a large ontology does not exceed the API Gateway / Lambda
        /// response limit. Omit for the server default.
        @httpQuery("limit")
        limit: Integer

        /// Zero-based offset into each collection, for paging. Combined with
        /// ``limit`` this yields a stable page window over the full ontology.
        @httpQuery("offset")
        offset: Integer
    }

    output: OntologyOverview

    errors: [
        ValidationError
    ]
}
