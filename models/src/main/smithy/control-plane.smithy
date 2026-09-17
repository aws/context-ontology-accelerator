// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Control Plane service definition
$version: "2"

namespace com.amazon.semanticcontext

use aws.apigateway#authorizer
use aws.apigateway#authorizers
use aws.protocols#restJson1
use com.amazon.semanticcontext.common#ValidationError
use com.amazon.semanticcontext.grant#CreateGrant
use com.amazon.semanticcontext.grant#CreatePlatformGrant
use com.amazon.semanticcontext.grant#DeleteGrant
use com.amazon.semanticcontext.grant#DeletePlatformGrant
use com.amazon.semanticcontext.grant#ListNamespaceGrants
use com.amazon.semanticcontext.grant#ListPlatformGrants
use com.amazon.semanticcontext.grant#ListPrincipalGrants
use com.amazon.semanticcontext.metric#BulkDeleteMetrics
use com.amazon.semanticcontext.metric#CreateMetric
use com.amazon.semanticcontext.metric#DeleteMetric
use com.amazon.semanticcontext.metric#ExportOsi
use com.amazon.semanticcontext.metric#GetImportJob
use com.amazon.semanticcontext.metric#GetMetric
use com.amazon.semanticcontext.metric#GetOsiUploadUrl
use com.amazon.semanticcontext.metric#ImportOsi
use com.amazon.semanticcontext.metric#ListMetrics
use com.amazon.semanticcontext.metric#UpdateMetric
use com.amazon.semanticcontext.metric#ValidateMetric
use com.amazon.semanticcontext.namespace#CreateNamespace
use com.amazon.semanticcontext.namespace#DeleteNamespace
use com.amazon.semanticcontext.namespace#GetNamespace
use com.amazon.semanticcontext.namespace#ListNamespaces
use com.amazon.semanticcontext.namespace#UpdateNamespace
use com.amazon.semanticcontext.namespace#UpdateNamespaceStatus
use com.amazon.semanticcontext.ontologygraph#DeleteOntology
use com.amazon.semanticcontext.ontologygraph#DownloadOntology
use com.amazon.semanticcontext.ontologygraph#FetchOntology
use com.amazon.semanticcontext.ontologygraph#GetGraphClass
use com.amazon.semanticcontext.ontologygraph#GetGraphDatatypeProperty
use com.amazon.semanticcontext.ontologygraph#GetGraphObjectProperty
use com.amazon.semanticcontext.ontologygraph#GetIngestStatus
use com.amazon.semanticcontext.ontologygraph#GetOntology
use com.amazon.semanticcontext.ontologygraph#GetOntologyOverview
use com.amazon.semanticcontext.ontologygraph#GetOntologyUploadUrl
use com.amazon.semanticcontext.ontologygraph#GraphSearch
use com.amazon.semanticcontext.ontologygraph#IngestOntologyFromS3
use com.amazon.semanticcontext.ontologygraph#ListOntologies
use com.amazon.semanticcontext.ontologygraph#UploadOntology
use com.amazon.semanticcontext.ontologyinduction#AcceptProposal
use com.amazon.semanticcontext.ontologyinduction#CancelProposal
use com.amazon.semanticcontext.ontologyinduction#CompileConstraints
use com.amazon.semanticcontext.ontologyinduction#GetInductionJob
use com.amazon.semanticcontext.ontologyinduction#GetInferConstraintsJob
use com.amazon.semanticcontext.ontologyinduction#GetProposal
use com.amazon.semanticcontext.ontologyinduction#GetProposalUploadUrls
use com.amazon.semanticcontext.ontologyinduction#GetProposalValidationJob
use com.amazon.semanticcontext.ontologyinduction#InferConstraints
use com.amazon.semanticcontext.ontologyinduction#ListFoundationalOntologies
use com.amazon.semanticcontext.ontologyinduction#ListInducedDatasources
use com.amazon.semanticcontext.ontologyinduction#ListInductionJobs
use com.amazon.semanticcontext.ontologyinduction#ListProposals
use com.amazon.semanticcontext.ontologyinduction#LoadFoundationalOntology
use com.amazon.semanticcontext.ontologyinduction#RejectProposals
use com.amazon.semanticcontext.ontologyinduction#RepairProposalDatatypes
use com.amazon.semanticcontext.ontologyinduction#StartInduction
use com.amazon.semanticcontext.ontologyinduction#UpdateProposal
use com.amazon.semanticcontext.ontologyinduction#ValidateProposal
use com.amazon.semanticcontext.role#GetNamespaceRole
use com.amazon.semanticcontext.role#ListNamespaceRoles
use com.amazon.semanticcontext.role#ListPlatformRoles
use com.amazon.semanticcontext.unifiedsources#ApproveSource
use com.amazon.semanticcontext.unifiedsources#CreateSource
use com.amazon.semanticcontext.unifiedsources#DeleteSource
use com.amazon.semanticcontext.unifiedsources#GetSource
use com.amazon.semanticcontext.unifiedsources#GetSourceScanJob
use com.amazon.semanticcontext.unifiedsources#GetSourceTable
use com.amazon.semanticcontext.unifiedsources#GetSourceUploadUrls
use com.amazon.semanticcontext.unifiedsources#KeepRescanRemoval
use com.amazon.semanticcontext.unifiedsources#ListSourceScanJobs
use com.amazon.semanticcontext.unifiedsources#ListSourceTables
use com.amazon.semanticcontext.unifiedsources#ListSources
use com.amazon.semanticcontext.unifiedsources#RejectSource
use com.amazon.semanticcontext.unifiedsources#RescanSource
use com.amazon.semanticcontext.unifiedsources#ReviewSourceColumn
use com.amazon.semanticcontext.unifiedsources#ReviewSourceTable
use com.amazon.semanticcontext.unifiedsources#UpdateSourceColumnMetadata
use com.amazon.semanticcontext.unifiedsources#UpdateSourceMetadata
use com.amazon.semanticcontext.unifiedsources#UpdateSourceTableKeys
use com.amazon.semanticcontext.unifiedsources#UpdateSourceTableMetadata

@title("Context Ontology Accelerator - Control Plane")
@restJson1
@httpBearerAuth
@authorizer("TokenAuthorizer")
@authorizers(
    TokenAuthorizer: {
        scheme: "smithy.api#httpBearerAuth"
        type: "request"
        uri: "${AuthorizerLambdaUri}"
        credentials: "${AuthorizerRoleArn}"
        identitySource: "method.request.header.authorization"
        // Authorizer result cache DISABLED. This is a REQUEST-type authorizer;
        // API Gateway keys its result cache by the concatenation of identity
        // sources (here, just the bearer token). With caching on, the first
        // allowed request cached a wildcard Allow that was then reused for every
        // (method, resource) on that token until TTL expiry — bypassing
        // per-request Cedar evaluation (privilege escalation). With TTL=0 the
        // authorizer runs on every request and Cedar evaluates each one, so the
        // wildcard Allow the handler returns is harmless (it applies only to the
        // request that produced it). The wildcard is still needed to avoid a
        // method-scoped-ARN false-deny; it must NOT be paired with a non-zero
        // TTL. Do not re-enable caching here.
        resultTtlInSeconds: 0
    }
)
@cors(
    origin: "${CorsOrigin}"
    additionalAllowedHeaders: ["Authorization", "Content-Type", "x-amz-content-sha256"]
    maxAge: 3600
)
service ControlPlaneService {
    version: "2025-04-18"
    operations: [
        HealthCheck
        GetSystemHealth
        // ── Namespace management ────────────────────────────────────
        CreateNamespace
        GetNamespace
        ListNamespaces
        UpdateNamespace
        DeleteNamespace
        UpdateNamespaceStatus
        // ── Role management ─────────────────────────────────────────────
        ListPlatformRoles
        ListNamespaceRoles
        GetNamespaceRole
        // ── Grant management ────────────────────────────────────────────
        CreateGrant
        ListNamespaceGrants
        DeleteGrant
        ListPrincipalGrants
        // ── Platform-scoped grant management ────────────────────────────
        CreatePlatformGrant
        ListPlatformGrants
        DeletePlatformGrant
        // ── Unified sources ──────────────────────────────────────────
        ListSources
        CreateSource
        GetSource
        DeleteSource
        RescanSource
        GetSourceUploadUrls
        ListSourceTables
        ListSourceScanJobs
        GetSourceTable
        // Resource-oriented review (replaces the bulk POST /review god endpoint)
        ApproveSource
        RejectSource
        ReviewSourceTable
        UpdateSourceTableMetadata
        ReviewSourceColumn
        UpdateSourceColumnMetadata
        UpdateSourceTableKeys
        // Re-scan: decline a flagged removal so approve keeps the table/column
        KeepRescanRemoval
        GetSourceScanJob
        UpdateSourceMetadata
        // ── Metric service ──────────────────────────────────────────────
        CreateMetric
        GetMetric
        ListMetrics
        UpdateMetric
        DeleteMetric
        ValidateMetric
        BulkDeleteMetrics
        ImportOsi
        ExportOsi
        GetOsiUploadUrl
        GetImportJob
        // ── Ontology induction ──────────────────────────────────────────
        StartInduction
        GetInductionJob
        ListInductionJobs
        ListInducedDatasources
        ListProposals
        GetProposal
        AcceptProposal
        RejectProposals
        CancelProposal
        UpdateProposal
        GetProposalUploadUrls
        InferConstraints
        CompileConstraints
        ValidateProposal
        GetProposalValidationJob
        RepairProposalDatatypes
        GetInferConstraintsJob
        // ── Foundational ontologies ─────────────────────────────────────
        ListFoundationalOntologies
        LoadFoundationalOntology
        // ── Ontology graph + catalog ────────────────────────────────────
        ListOntologies
        GetOntology
        DeleteOntology
        UploadOntology
        DownloadOntology
        FetchOntology
        GetOntologyUploadUrl
        IngestOntologyFromS3
        GetIngestStatus
        GraphSearch
        GetGraphClass
        GetGraphObjectProperty
        GetGraphDatatypeProperty
        GetOntologyOverview
    ]
    errors: [
        ValidationError
    ]
}

/// Lightweight liveness probe that reports whether the service is up.
@http(method: "GET", uri: "/health")
@readonly
@optionalAuth
operation HealthCheck {
    output := {
        /// Liveness status; "ok" when the service is up.
        @required
        status: String
    }
}

/// Report the aggregate health of the platform's downstream dependencies.
@http(method: "GET", uri: "/system-health")
@readonly
operation GetSystemHealth {
    output := {
        /// Overall system status: "ok" or "degraded"
        @required
        status: String

        /// Ontology induction subsystem health (graph store, vector store, Bedrock)
        ontologyInduction: OntologyInductionHealth

        /// DynamoDB sources table status
        dynamodb: String

        /// Neptune KG (GraphRAG) status
        neptuneKg: String

        /// OpenSearch Serverless collection status
        opensearch: String
    }
}

/// Health of the ontology-induction subsystem's dependencies: the graph store (Neptune Analytics),
/// vector store (OpenSearch), Bedrock embeddings backend, and description LLM (Bedrock Converse).
structure OntologyInductionHealth {
    /// Graph store (Neptune Analytics) status
    graphStore: String

    /// Vector store (OpenSearch) status
    vectorStore: String

    /// Bedrock embeddings backend status
    bedrockEmbeddings: String

    /// Description LLM (Bedrock Converse) status
    descriptionLlm: String
}
