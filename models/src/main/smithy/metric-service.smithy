// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Metric service operations and shapes
$version: "2"

namespace com.amazon.semanticcontext.metric

use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#ConflictError
use com.amazon.semanticcontext.common#NotFoundError
use com.amazon.semanticcontext.common#PaginatedInput
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#Uuid
use com.amazon.semanticcontext.common#ValidationError

// ──────────────────────────────────────────────
// Constrained primitives
// ──────────────────────────────────────────────
/// Metric name: alphanumeric + underscores, 1-128 chars.
@pattern("^[a-zA-Z][a-zA-Z0-9_]{0,127}$")
@length(min: 1, max: 128)
string MetricName

/// SQL expression. Must be a full SELECT statement (or set operation) —
/// e.g. "SELECT COUNT(*) AS total FROM orders". Bare aggregate fragments
/// like "COUNT(*)" are rejected: the serve-time SQL firewall only executes
/// top-level SELECT statements, and onboarding validates the same shape.
@length(min: 1, max: 10000)
string SqlExpression

/// Data source identifier reference.
@length(min: 1, max: 128)
string DataSourceId

/// Source table name reference.
@length(min: 1, max: 256)
string SourceTableName

// ──────────────────────────────────────────────
// Enums
// ──────────────────────────────────────────────
/// SQL dialect identifier.
enum SqlDialect {
    /// PostgreSQL SQL dialect.
    POSTGRESQL

    /// Trino SQL dialect.
    TRINO

    /// Amazon Redshift SQL dialect.
    REDSHIFT

    /// Snowflake SQL dialect.
    SNOWFLAKE

    /// Databricks SQL dialect.
    DATABRICKS

    /// MySQL SQL dialect.
    MYSQL
}

/// Default time aggregation granularity for a metric.
enum TimeGrain {
    /// Daily granularity.
    DAY

    /// Weekly granularity.
    WEEK

    /// Monthly granularity.
    MONTH

    /// Quarterly granularity.
    QUARTER

    /// Yearly granularity.
    YEAR
}

/// Severity level attached to a validation warning.
enum WarningSeverity {
    /// Blocking issue: the metric is rejected at create/update. This includes
    /// SQL accepted by neither the persistence gate nor serve-time firewall,
    /// an unapproved source, and a provably absent declared source table.
    ERROR

    /// Warning-level issue; non-fatal but should be reviewed.
    WARNING

    /// Informational, non-blocking notice.
    INFO
}

// ──────────────────────────────────────────────
// Structures
// ──────────────────────────────────────────────
/// A single dialect + expression pair.
structure MetricDialect {
    @required
    dialect: SqlDialect

    @required
    expression: SqlExpression
}

@length(min: 1)
list MetricDialectList {
    member: MetricDialect
}

/// Expression container with multi-dialect support.
structure MetricExpression {
    /// Per-dialect SQL expressions for the metric.
    @required
    dialects: MetricDialectList
}

/// AI context for metric discoverability.
structure MetricAiContext {
    /// Alternative names the metric may be referred to by.
    synonyms: SynonymList

    /// Free-text guidance for how to use or interpret the metric.
    instructions: String

    /// Example questions or usages illustrating the metric.
    examples: ExampleList
}

list SynonymList {
    member: String
}

list ExampleList {
    member: String
}

list OntologyConceptList {
    member: String
}

/// Validation warning returned alongside successful create/update.
structure ValidationWarning {
    /// Name of the field the warning applies to.
    @required
    field: String

    /// Human-readable description of the warning.
    @required
    message: String

    @required
    severity: WarningSeverity
}

list ValidationWarningList {
    member: ValidationWarning
}

/// Full metric definition.
structure MetricDefinition {
    /// Server-assigned unique identifier.
    metricId: String

    /// Namespace this metric belongs to.
    namespaceId: Uuid

    @required
    name: MetricName

    /// Human-readable description of what the metric measures.
    @required
    description: String

    @required
    expression: MetricExpression

    @required
    dataSourceId: DataSourceId

    @required
    sourceTable: SourceTableName

    defaultTimeGrain: TimeGrain

    /// Unit of measure for the metric value (e.g. "USD", "count").
    unit: String

    /// Data type of the value the metric returns (e.g. "integer", "decimal").
    returnType: String

    aiContext: MetricAiContext

    /// Ontology concept URIs this metric is associated with.
    ontologyConcepts: OntologyConceptList

    /// Set by server — principal who created/updated the metric.
    definedBy: String

    /// Set by server — date the metric became effective.
    effectiveFrom: String
}

list MetricDefinitionList {
    member: MetricDefinition
}

// ──────────────────────────────────────────────
// CreateMetric
// ──────────────────────────────────────────────
/// Create a new metric definition in a namespace.
@idempotent
@http(method: "POST", uri: "/namespaces/{namespaceId}/metrics", code: 201)
operation CreateMetric {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        name: MetricName

        /// Human-readable description of what the metric measures.
        @required
        description: String

        @required
        expression: MetricExpression

        @required
        dataSourceId: DataSourceId

        @required
        sourceTable: SourceTableName

        defaultTimeGrain: TimeGrain

        /// Unit of measure for the metric value (e.g. "USD", "count").
        unit: String

        /// Data type of the value the metric returns (e.g. "integer", "decimal").
        returnType: String

        aiContext: MetricAiContext

        /// Ontology concept URIs this metric is associated with.
        ontologyConcepts: OntologyConceptList
    }

    output: CreateMetricOutput

    errors: [
        ValidationError
        ConflictError
        AccessDeniedError
        NotFoundError
    ]
}

/// Result of creating a metric, including the persisted definition and any warnings.
structure CreateMetricOutput {
    @required
    metric: MetricDefinition

    /// Non-fatal validation warnings raised during creation.
    warnings: ValidationWarningList
}

// ──────────────────────────────────────────────
// GetMetric
// ──────────────────────────────────────────────
/// Retrieve a single metric definition by name.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/metrics/{name}")
operation GetMetric {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        name: MetricName
    }

    output: GetMetricOutput

    errors: [
        NotFoundError
        AccessDeniedError
    ]
}

/// Result of retrieving a single metric definition.
structure GetMetricOutput {
    @required
    metric: MetricDefinition
}

// ──────────────────────────────────────────────
// ListMetrics
// ──────────────────────────────────────────────
/// List metric definitions in a namespace, optionally filtered by data source or source table.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/metrics")
operation ListMetrics {
    input := with [PaginatedInput] {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Pagination token from a previous response; omit for the first page.
        @httpQuery("nextToken")
        nextToken: String

        /// Maximum number of metrics to return in one page.
        @httpQuery("maxResults")
        maxResults: Integer

        @httpQuery("dataSourceId")
        dataSourceId: DataSourceId

        @httpQuery("sourceTable")
        sourceTable: SourceTableName
    }

    output: ListMetricsOutput

    errors: [
        AccessDeniedError
    ]
}

/// Paginated list of metric definitions.
structure ListMetricsOutput with [PaginatedOutput] {
    /// Metric definitions matching the request.
    @required
    metrics: MetricDefinitionList
}

// ──────────────────────────────────────────────
// UpdateMetric
// ──────────────────────────────────────────────
/// Replace an existing metric definition.
@idempotent
@http(method: "PUT", uri: "/namespaces/{namespaceId}/metrics/{name}")
operation UpdateMetric {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        name: MetricName

        /// Human-readable description of what the metric measures.
        @required
        description: String

        @required
        expression: MetricExpression

        @required
        dataSourceId: DataSourceId

        @required
        sourceTable: SourceTableName

        defaultTimeGrain: TimeGrain

        /// Unit of measure for the metric value (e.g. "USD", "count").
        unit: String

        /// Data type of the value the metric returns (e.g. "integer", "decimal").
        returnType: String

        aiContext: MetricAiContext

        /// Ontology concept URIs this metric is associated with.
        ontologyConcepts: OntologyConceptList
    }

    output: UpdateMetricOutput

    errors: [
        ValidationError
        NotFoundError
        ConflictError
        AccessDeniedError
    ]
}

/// Result of updating a metric, including the updated definition and any warnings.
structure UpdateMetricOutput {
    @required
    metric: MetricDefinition

    /// Non-fatal validation warnings raised during the update.
    warnings: ValidationWarningList
}

// ──────────────────────────────────────────────
// DeleteMetric
// ──────────────────────────────────────────────
/// Delete a single metric definition by name.
@idempotent
@http(method: "DELETE", uri: "/namespaces/{namespaceId}/metrics/{name}", code: 204)
operation DeleteMetric {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        @httpLabel
        name: MetricName
    }

    errors: [
        NotFoundError
        AccessDeniedError
    ]
}

// ──────────────────────────────────────────────
// ValidateMetric
// ──────────────────────────────────────────────
/// Validate a metric definition without persisting it. ERROR findings predict
/// create/update rejection; INFO findings are advisory.
@http(method: "POST", uri: "/namespaces/{namespaceId}/metrics/validate")
operation ValidateMetric {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        @required
        name: MetricName

        /// Human-readable description of what the metric measures.
        @required
        description: String

        @required
        expression: MetricExpression

        @required
        dataSourceId: DataSourceId

        @required
        sourceTable: SourceTableName

        defaultTimeGrain: TimeGrain

        /// Unit of measure for the metric value (e.g. "USD", "count").
        unit: String

        /// Data type of the value the metric returns (e.g. "integer", "decimal").
        returnType: String

        aiContext: MetricAiContext

        /// Ontology concept URIs this metric is associated with.
        ontologyConcepts: OntologyConceptList
    }

    output: ValidateMetricOutput

    errors: [
        ValidationError
        AccessDeniedError
    ]
}

/// Result of validating a metric definition, listing blocking and advisory findings.
structure ValidateMetricOutput {
    /// Validation findings. Severity ERROR predicts create/update rejection;
    /// INFO is advisory.
    @required
    warnings: ValidationWarningList
}

// ──────────────────────────────────────────────
// BulkDeleteMetrics
// ──────────────────────────────────────────────
/// Delete multiple metrics by name list or filter criteria.
/// Returns the count of metrics deleted.
@http(method: "POST", uri: "/namespaces/{namespaceId}/bulk-delete-metrics")
operation BulkDeleteMetrics {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Explicit list of metric names to delete. Mutually exclusive with filter.
        names: MetricNameList

        /// Filter criteria — deletes all metrics matching the filter.
        /// Mutually exclusive with names.
        filter: BulkDeleteFilter
    }

    output := {
        /// Number of metrics deleted.
        @required
        deleted: Integer

        /// Metric names that were not found (from names list).
        notFound: MetricNameList
    }

    errors: [
        ValidationError
        AccessDeniedError
    ]
}

list MetricNameList {
    member: MetricName
}

/// Filter criteria selecting which metrics a bulk delete should remove.
structure BulkDeleteFilter {
    /// Delete all metrics with this data source ID.
    dataSourceId: String

    /// Delete all metrics with this source table.
    sourceTable: String

    /// Delete all metrics whose name matches this prefix.
    namePrefix: String
}

// ──────────────────────────────────────────────
// ImportOsi
// ──────────────────────────────────────────────
/// Import metrics from an OSI (Open Semantic Interchange) YAML document, inline or from S3.
@http(method: "POST", uri: "/namespaces/{namespaceId}/import-osi")
operation ImportOsi {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// OSI YAML content as a string (inline). Mutually exclusive with s3Key.
        /// Maximum 5MB for inline content; use GetOsiUploadUrl + s3Key for larger files.
        content: String

        /// S3 object key containing the OSI YAML (for large files uploaded via GetOsiUploadUrl).
        /// Mutually exclusive with content.
        s3Key: String
    }

    output: ImportOsiOutput

    errors: [
        ValidationError
        AccessDeniedError
    ]
}

/// Result of an OSI import, summarizing resolved datasets and metric changes.
structure ImportOsiOutput {
    /// Number of datasets resolved from the OSI document.
    @required
    datasetsResolved: Integer

    /// Number of new metrics created by the import.
    @required
    metricsCreated: Integer

    /// Number of existing metrics updated by the import.
    @required
    metricsUpdated: Integer

    /// Non-fatal warnings raised during the import.
    warnings: OsiWarningList

    /// Present when the import is processed asynchronously (metric count > threshold).
    /// Poll GetImportJob with this ID to track progress.
    jobId: String

    /// Import job status. "COMPLETED" for sync imports, "IN_PROGRESS" for async.
    status: ImportJobStatus
}

list OsiWarningList {
    member: String
}

/// Status of an asynchronous import job.
@enum([
    {
        value: "IN_PROGRESS"
        name: "IN_PROGRESS"
    }
    {
        value: "COMPLETED"
        name: "COMPLETED"
    }
    {
        value: "FAILED"
        name: "FAILED"
    }
])
string ImportJobStatus

// ──────────────────────────────────────────────
// GetImportJob
// ──────────────────────────────────────────────
/// Poll the status of an asynchronous OSI import job.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/import-jobs/{jobId}")
operation GetImportJob {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the async import job to poll.
        @required
        @httpLabel
        jobId: String
    }

    output: ImportJobOutput

    errors: [
        NotFoundError
    ]
}

/// Current status and progress counters for an asynchronous OSI import job.
structure ImportJobOutput {
    /// Identifier of the import job.
    @required
    jobId: String

    @required
    status: ImportJobStatus

    /// Total number of metrics in the import.
    @required
    metricsTotal: Integer

    /// Number of metrics processed so far.
    @required
    metricsProcessed: Integer

    /// Number of new metrics created so far.
    @required
    metricsCreated: Integer

    /// Number of existing metrics updated so far.
    @required
    metricsUpdated: Integer

    /// Fatal errors encountered during the import.
    errors: OsiWarningList

    /// Non-fatal warnings raised during the import.
    warnings: OsiWarningList
}

// ──────────────────────────────────────────────
// GetOsiUploadUrl
// ──────────────────────────────────────────────
/// Generate a pre-signed S3 PUT URL for uploading large OSI YAML files.
/// After uploading, call ImportOsi with the returned s3Key.
@http(method: "POST", uri: "/namespaces/{namespaceId}/import-osi/upload-url")
operation GetOsiUploadUrl {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Original filename (used for content-type validation).
        @required
        filename: String
    }

    output := {
        /// Pre-signed S3 PUT URL. Upload the YAML file here with Content-Type: application/x-yaml.
        @required
        uploadUrl: String

        /// S3 key to pass to ImportOsi after upload completes.
        @required
        s3Key: String

        /// Seconds until the pre-signed URL expires.
        @required
        expiresIn: Integer
    }

    errors: [
        ValidationError
        AccessDeniedError
    ]
}

// ──────────────────────────────────────────────
// ExportOsi
// ──────────────────────────────────────────────
/// Export metric definitions as an OSI (Open Semantic Interchange) YAML document.
@readonly
@http(method: "GET", uri: "/namespaces/{namespaceId}/export-osi")
operation ExportOsi {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Response format: "inline" (default) returns content in body,
        /// "s3" writes to S3 and returns a pre-signed download URL.
        @httpQuery("format")
        format: ExportFormat

        /// Optional subset of metric names to export (repeated query param,
        /// e.g. ?names=a&names=b). Omit to export every metric in the
        /// namespace (default, unchanged behavior).
        @httpQuery("names")
        names: MetricNameList
    }

    output: ExportOsiOutput

    errors: [
        AccessDeniedError
        NotFoundError
    ]
}

/// Export format options.
@enum([
    {
        value: "inline"
        name: "INLINE"
    }
    {
        value: "s3"
        name: "S3"
    }
])
string ExportFormat

/// Result of an OSI export, either inline YAML content or a pre-signed download URL.
structure ExportOsiOutput {
    /// OSI YAML content (populated when format=inline or omitted).
    content: String

    /// Pre-signed S3 GET URL for downloading the export (populated when format=s3).
    downloadUrl: String

    /// Seconds until the download URL expires (populated when format=s3).
    expiresIn: Integer
}
