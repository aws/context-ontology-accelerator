// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Unified source registry — CRUD operations for the unified sources experience.
//
// The service definition lives in control-plane.smithy (ControlPlaneService).
$version: "2"

namespace com.amazon.semanticcontext.unifiedsources

use com.amazon.semanticcontext.common#DisplayName
use com.amazon.semanticcontext.common#IamRoleArn
use com.amazon.semanticcontext.common#PaginatedInput
use com.amazon.semanticcontext.common#PaginatedOutput
use com.amazon.semanticcontext.common#S3BucketArn
use com.amazon.semanticcontext.common#S3Prefix
use com.amazon.semanticcontext.common#Uuid

/// Table identifier (database.table format).
@length(min: 1, max: 512)
@pattern("^[a-zA-Z0-9_\\-:.]+$")
string TableId

/// Column name identifier.
@length(min: 1, max: 256)
@pattern("^[a-zA-Z_][a-zA-Z0-9_]*$")
string ColumnName

/// STS ExternalId used when assuming a cross-account access role
/// (confused-deputy protection). Must match the ExternalId on the role's
/// trust policy. See AWS STS ExternalId character/length constraints.
@length(min: 2, max: 1224)
@pattern("^[a-zA-Z0-9+=,.@:\\/-]+$")
string ExternalId

/// Review status for enriched metadata.
enum ReviewStatus {
    /// Awaiting steward review.
    PENDING_REVIEW

    /// Reviewed and approved.
    APPROVED

    /// Reviewed and rejected.
    REJECTED
}

/// Decision a steward can make on enriched metadata.
enum ReviewDecision {
    /// Approve the metadata.
    APPROVED

    /// Reject the metadata.
    REJECTED
}

/// AWS account ID (12 digits).
@pattern("^\\d{12}$")
@length(min: 12, max: 12)
string AwsAccountId

/// Glue Data Catalog identifier: a 12-digit account id, or `account:catalogName`
/// for a nested catalog.
@pattern("^\\d{12}(:[a-zA-Z0-9_/-]+)?$")
@length(min: 12, max: 256)
string GlueCatalogId

/// Athena DataCatalog name (1-256 chars, alphanumeric + underscore).
@length(min: 1, max: 256)
@pattern("^[a-zA-Z][a-zA-Z0-9_]*$")
string AthenaDataCatalogName

/// Amazon Redshift Serverless workgroup name (3-64 chars, lowercase
/// alphanumeric and hyphens; must start with a letter — Redshift Serverless
/// naming rules).
@length(min: 3, max: 64)
@pattern("^[a-z][a-z0-9-]*$")
string RedshiftWorkgroupName

/// AWS region identifier.
@length(min: 1, max: 64)
string AwsRegion

/// Database hostname or endpoint.
@length(min: 1, max: 512)
string Hostname

/// Database port number.
@range(min: 1, max: 65535)
integer Port

/// Database name.
@length(min: 1, max: 256)
string DatabaseName

/// Regex filter pattern for schema/table filtering.
@length(min: 1, max: 1024)
string FilterPattern

/// Secrets Manager ARN for database credentials.
@pattern("^arn:(aws|aws-us-gov):secretsmanager:[a-z0-9-]+:\\d{12}:secret:.+$")
@length(min: 20, max: 2048)
string SecretArn

/// AWS Lambda function ARN, optionally qualified with a version number, an
/// alias, or `$LATEST`. Format:
/// arn:{partition}:lambda:{region}:{account-id}:function:{name}[:{qualifier}]
///
/// A full ARN is required: Lambda's partial-ARN
/// (`{account-id}:function:{name}`) and name-only forms are deliberately
/// rejected, because the connector runs in the customer's account and an
/// unqualified name would resolve against this deployment's account instead.
@pattern("^arn:aws[a-z-]*:lambda:[a-z0-9-]+:\\d{12}:function:[a-zA-Z0-9_-]+(:(\\$LATEST|[a-zA-Z0-9_-]+))?$")
@length(min: 20, max: 2048)
string LambdaFunctionArn

/// Database engine type for JDBC connections.
enum DatabaseEngine {
    /// PostgreSQL database engine.
    POSTGRESQL

    /// MySQL database engine.
    MYSQL

    /// Amazon Redshift database engine.
    REDSHIFT

    /// Oracle database engine.
    ORACLE

    /// Microsoft SQL Server database engine.
    SQLSERVER

    /// Snowflake database engine.
    SNOWFLAKE
}

/// Engine type for the underlying data behind a Glue database.
enum ExecutionEngine {
    /// PostgreSQL execution engine.
    POSTGRESQL

    /// MySQL execution engine.
    MYSQL

    /// Amazon Redshift execution engine.
    REDSHIFT

    /// Oracle execution engine.
    ORACLE

    /// Microsoft SQL Server execution engine.
    SQLSERVER

    /// Snowflake execution engine.
    SNOWFLAKE

    /// Amazon DynamoDB execution engine.
    DYNAMODB

    /// S3-backed Apache Iceberg execution engine.
    S3_ICEBERG
}

/// How the consumption layer should execute queries against a source.
enum QueryEngine {
    /// Query via Amazon Athena — native Glue catalog, or a JDBC federated catalog.
    ATHENA

    /// Query the database directly over JDBC (no Athena).
    JDBC

    /// Query Glue/Iceberg tables via Amazon Redshift Serverless using the
    /// `awsdatacatalog` auto-mount (an alternative execution engine to Athena
    /// for Glue-backed sources; opt-in per source via
    /// GlueConfiguration.executionEngine).
    REDSHIFT
}

/// JDBC database connection configuration.
structure JdbcConfiguration {
    @required
    engine: DatabaseEngine

    @required
    host: Hostname

    @required
    port: Port

    @required
    databaseName: DatabaseName

    schemaFilter: FilterPattern

    schemaExcludeFilter: FilterPattern

    tableFilter: FilterPattern

    tableExcludeFilter: FilterPattern

    credentialSecretArn: SecretArn

    crossAccountRoleArn: IamRoleArn

    /// IGNORED. The ExternalId presented on the cross-account assume is derived
    /// from the namespace (`NamespaceDetail$datasourceExternalId`) and is no
    /// longer accepted from the request: a caller-chosen value would hand back
    /// control of what binds the assume to its namespace. Sources onboarded
    /// before this change keep the value already stored on their record.
    @deprecated(message: "Ignored. Condition the role's trust policy on NamespaceDetail$datasourceExternalId instead.", since: "2026-08-28")
    externalId: ExternalId

    /// Snowflake only: the virtual warehouse used to run INFORMATION_SCHEMA
    /// queries during discovery (Snowflake requires an active warehouse).
    warehouse: String

    /// Snowflake only: optional Snowflake RBAC role name to assume for the
    /// session (a Snowflake construct — not an AWS IAM role; see crossAccountRoleArn).
    role: String
}

/// Glue Data Catalog database connection configuration.
structure GlueConfiguration {
    /// Glue Data Catalog id: a 12-digit AWS account id (the account's root
    /// catalog) or a nested catalog id of the form `account:catalogName`
    /// (federated / S3 Tables / Redshift-backed / cross-account catalogs).
    @required
    catalogId: GlueCatalogId

    @required
    region: AwsRegion

    @required
    databaseName: DatabaseName

    tableFilter: FilterPattern

    tableExcludeFilter: FilterPattern

    crossAccountRoleArn: IamRoleArn

    /// IGNORED. The ExternalId presented on the cross-account assume is derived
    /// from the namespace (`NamespaceDetail$datasourceExternalId`) and is no
    /// longer accepted from the request: a caller-chosen value would hand back
    /// control of what binds the assume to its namespace. Sources onboarded
    /// before this change keep the value already stored on their record.
    @deprecated(message: "Ignored. Condition the role's trust policy on NamespaceDetail$datasourceExternalId instead.", since: "2026-08-28")
    externalId: ExternalId

    /// Nested Athena/Glue catalog name when the database lives in a federated or
    /// other non-root catalog. When set, the source is queried as
    /// `AwsDataCatalog.<athenaDataCatalogName>.<databaseName>.<table>`; when
    /// omitted, as `AwsDataCatalog.<databaseName>.<table>`.
    athenaDataCatalogName: AthenaDataCatalogName

    /// Execution engine for this Glue/Iceberg source's serve-time queries.
    /// Defaults to ATHENA when omitted. Set REDSHIFT to route queries through
    /// Amazon Redshift Serverless (`awsdatacatalog` auto-mount); requires
    /// `redshiftWorkgroup` to be set.
    executionEngine: GlueExecutionEngine

    /// Redshift Serverless workgroup used to execute queries when
    /// `executionEngine` is REDSHIFT. Ignored (and unnecessary) for ATHENA.
    redshiftWorkgroup: RedshiftWorkgroupName
}

/// Custom Athena Query Federation connector configuration — the
/// CUSTOM_CONNECTOR sub-type. The connector is a Lambda the customer authors
/// and deploys in their own account, which this service registers as a
/// Lambda-backed Athena data catalog in its own account; both discovery and
/// serve-time queries run through Athena against that catalog.
///
/// Unlike GlueConfiguration there is no Glue database behind the source, so
/// there is no catalogId and no cross-account IAM role to assume: the customer
/// grants access on the connector Lambda's own resource policy instead. There
/// is also no region member — the connector must live in this deployment's
/// region (see connectorFunctionArn).
structure CustomConnectorConfiguration {
    /// ARN of the connector Lambda. ONE Lambda serves both metadata and record
    /// requests — what Athena calls a composite handler. The ARN's region must
    /// equal this deployment's own region, because Athena can only invoke a
    /// data-source connector co-located with the query.
    ///
    /// Athena also accepts a split metadata/record PAIR of Lambdas, which this
    /// member was once one half of. The pair is deliberately not modelled: the
    /// reference connector and the CDK template customers are given both deploy a
    /// single composite Lambda, so a second ARN asked every customer to reason
    /// about a distinction almost none of them would make — and an ARN placed in
    /// the wrong one of two adjacent members fails at query time rather than at
    /// onboarding. Restoring it is purely additive (one optional member, plus the
    /// record-function catalog parameter) if a customer ever needs the split form.
    @required
    connectorFunctionArn: LambdaFunctionArn

    /// The single database inside the connector's catalog that this source
    /// exposes. Exactly one database is resolved per source, so a connector
    /// serving several databases is onboarded once per database.
    @required
    databaseName: DatabaseName

    /// Include-filter applied to table names within databaseName. Omit to
    /// discover every table.
    tableFilter: FilterPattern

    /// Exclude-filter applied to table names within databaseName, evaluated
    /// after tableFilter.
    tableExcludeFilter: FilterPattern
}

/// Execution engine choice for a Glue/Iceberg source. A deliberately narrow
/// enum (only the engines valid for a Glue source) so the onboarding UI/API
/// cannot select an engine — e.g. direct JDBC — that doesn't apply to Glue.
enum GlueExecutionEngine {
    /// Execute via Amazon Athena (default).
    ATHENA

    /// Execute via Amazon Redshift Serverless `awsdatacatalog` auto-mount.
    REDSHIFT
}

/// Business-facing metadata for a table or column returned in responses:
/// description, synonyms, glossary terms, tags, enrichment source, review
/// status, and AI-inference confidence.
structure BusinessMetadataOutput {
    /// Human-readable description of the table or column.
    description: String

    /// Alternative names or aliases.
    synonyms: StringList

    /// Associated business glossary terms.
    glossaryTerms: StringList

    /// Free-form classification tags.
    tags: StringList

    /// Origin of this metadata (e.g. AI-inferred, steward-specified, catalog-existing).
    enrichmentSource: String

    reviewStatus: ReviewStatus

    /// AI-inference confidence (0.0-1.0) for AI-generated metadata; absent or 0
    /// for steward-specified or catalog-existing metadata.
    confidence: Float
}

/// The primary-key definition reported for a table: its key columns plus the
/// source (e.g. discovered vs. steward-specified) and inference confidence.
structure PrimaryKeyOutput {
    /// Ordered column names composing the primary key.
    columns: StringList

    /// Origin of the key (e.g. discovered vs. steward-specified).
    source: String

    /// Inference confidence (0.0-1.0) for AI-inferred keys.
    confidence: Float
}

/// A foreign-key relationship reported for a table: the local column and the
/// target table/column it references, with the source and inference confidence.
structure ForeignKeyOutput {
    /// Local column holding the foreign-key reference.
    column: String

    /// Referenced table.
    targetTable: String

    /// Referenced column in the target table.
    targetColumn: String

    /// Origin of the key (e.g. discovered vs. steward-specified).
    source: String

    /// Inference confidence (0.0-1.0) for AI-inferred keys.
    confidence: Float
}

list ForeignKeyList {
    member: ForeignKeyOutput
}

/// Metadata describing a single column of a source table: its name, data type,
/// nullability, partition-key flag, description, business metadata, and any
/// sampled distinct values.
structure ColumnMetadata {
    /// Column name.
    @required
    name: String

    /// Column data type (source-native type string).
    @required
    dataType: String

    /// Whether the column permits null values.
    nullable: Boolean

    /// Whether the column is a partition key.
    isPartitionKey: Boolean

    /// Human-readable description of the column.
    description: String

    businessMetadata: BusinessMetadataOutput

    /// Sampled distinct values for low-cardinality categorical columns
    /// (capped, best-effort). Empty for high-cardinality, non-string, or
    /// unsampled columns. Used by the serve NL→SQL layer to hint the LLM
    /// with correct enum literals for WHERE clauses, and shown in the UI.
    distinctValues: StringList

    /// True when a re-scan found this column removed from the source; it is
    /// deleted on approve unless the steward keeps it. Absent/false otherwise.
    pendingDeletion: Boolean
}

list ColumnMetadataList {
    member: ColumnMetadata
}

/// Category of a source-owned field a re-scan found changed. Groups the
/// individual field changes so the review UI can present them by kind.
enum RescanChangeKind {
    /// Physical shape: data type, nullability, partition flag, storage format,
    /// location, or partition keys.
    SCHEMA

    /// Source-derived comment on the table or column.
    DESCRIPTION

    /// Primary key or foreign keys.
    CONSTRAINT

    /// Column distinct-value sample. Reserved: sampled values are data churn
    /// rather than a schema change, so the diff does not emit this today. Kept
    /// for a planned re-induction signal.
    SAMPLED_VALUES
}

/// One source-owned field that a re-scan changed vs. the last approved scan,
/// carrying both the old and new values for the steward's before/after view.
structure RescanFieldChange {
    /// Field name (e.g. "description", "data_type", "primary_key").
    @required
    field: String

    /// Which category of change this is.
    @required
    kind: RescanChangeKind

    /// Value at the last approved scan.
    old: String

    /// Value the latest re-scan discovered.
    new: String
}

list RescanFieldChangeList {
    member: RescanFieldChange
}

/// How a re-scan changed a column, relative to the last approved scan.
/// Wire values are lower-case to match what the diff has always emitted, so
/// naming them here is not a breaking change.
enum RescanColumnChangeStatus {
    /// Present in the fresh scan, absent from the last approved scan.
    ADDED = "added"

    /// Present in the last approved scan, gone from the source. Deleted on
    /// approve unless the steward keeps it.
    REMOVED = "removed"

    /// Present in both, but one or more source-owned fields differ.
    MODIFIED = "modified"
}

/// A column a re-scan added, removed, or modified, with its changed fields.
structure RescanColumnChange {
    /// Column name.
    @required
    name: String

    /// Whether the column was added, removed, or modified.
    @required
    status: RescanColumnChangeStatus

    /// Changed source-owned fields (populated for MODIFIED).
    fields: RescanFieldChangeList
}

list RescanColumnChangeList {
    member: RescanColumnChange
}

/// The old-vs-new breakdown of what a re-scan changed for a table, so a Data
/// Steward can compare against the last approved scan before approving. Present
/// only while the source is in RESCAN_REVIEW and this table actually changed.
structure RescanTableDiff {
    /// Changed table-level source-owned fields (description, keys, format, ...).
    tableFields: RescanFieldChangeList

    /// Per-column added / removed / modified breakdown.
    columns: RescanColumnChangeList
}

/// Technical metadata for a table: column count, partition keys, storage
/// format, and physical location.
structure TechnicalMetadataOutput {
    /// Number of columns in the table.
    columnCount: Integer

    /// Names of the table's partition-key columns.
    partitionKeys: StringList

    /// Storage/serialization format.
    format: String

    /// Physical storage location (e.g. S3 URI).
    location: String
}

/// Steward-supplied overrides applied to discovered metadata: description,
/// synonyms, glossary terms, and tags.
structure MetadataOverrides {
    /// Override description for the table or column.
    description: String

    /// Override synonyms or aliases.
    synonyms: StringList

    /// Override business glossary terms.
    glossaryTerms: StringList

    /// Override classification tags.
    tags: StringList
}

/// Steward-supplied primary key. Replaces the table's primary key; an empty
/// columns list clears it. Stored with source = STEWARD_SPECIFIED.
structure PrimaryKeyInput {
    /// Column names composing the primary key; empty list clears it.
    @required
    columns: StringList
}

/// Steward-supplied foreign key relationship. Stored with
/// source = STEWARD_SPECIFIED.
structure ForeignKeyInput {
    @required
    column: ColumnName

    /// Referenced table.
    @required
    targetTable: String

    /// Referenced column in the target table.
    targetColumn: String
}

list ForeignKeyInputList {
    member: ForeignKeyInput
}

/// A summary record for a source table in list/get responses: identifiers,
/// database, column and approved-column counts, review status, and enrichment
/// source.
structure TableSummary {
    /// Unique table identifier (database.table format).
    @required
    tableId: String

    /// Table name.
    @required
    name: String

    /// Database the table belongs to.
    @required
    database: String

    /// Number of columns in the table.
    columnCount: Integer

    /// Number of columns with reviewStatus=APPROVED. Allows the UI to show
    /// approval progress (columnsApproved / columnCount) without fetching
    /// per-column detail.
    columnsApproved: Integer

    /// Number of columns the re-scan found removed from the source (retained,
    /// pending deletion on approval). Lets the list surface column-level removals
    /// in the Review status column.
    columnsPendingDeletion: Integer

    @required
    reviewStatus: ReviewStatus

    /// Origin of the table's business metadata.
    enrichmentSource: String

    /// True when a re-scan found this table removed from the source; it is
    /// deleted on approve unless the steward keeps it. Absent/false otherwise.
    pendingDeletion: Boolean

    /// True when this table was created by the re-scan under review (net-new
    /// since the last approved scan). Derived from the re-scan backup; not
    /// persisted; absent outside RESCAN_REVIEW.
    added: Boolean
}

list TableSummaryList {
    member: TableSummary
}

list StringList {
    member: String
}

/// Bedrock inference profile or foundation model ARN.
@pattern("^arn:(aws|aws-us-gov):bedrock:[a-z0-9-]*:[0-9]*:(inference-profile|foundation-model)/.+$")
@length(min: 20, max: 2048)
string BedrockModelArn

/// Extraction mode — mirrors ExtractionMode in constants.py.
enum ExtractionMode {
    /// Extract documents in a single continuous pass.
    CONTINUOUS = "continuous"

    /// Extract documents as separated units.
    SEPARATED = "separated"
}

/// Configuration for document extraction behaviour.
structure ExtractionConfig {
    extractionMode: ExtractionMode

    /// Whether to use Bedrock batch inference for extraction.
    useBatchInference: Boolean

    /// Whether to version extracted documents.
    enableVersioning: Boolean

    /// Whether to extract propositions from chunks.
    enablePropositionExtraction: Boolean

    /// Explicit entity-class vocabulary handed to the extraction LLM. When
    /// non-empty, overrides both graphrag's hardcoded news/finance defaults
    /// AND the corpus-inferred fallback — the exact labels supplied here are
    /// the ones the extractor uses to type entities. Case-sensitive, spaced
    /// (e.g. "Policy", "Claim", "Loss Ratio"). Leave empty to fall back to
    /// inferEntityClassifications.
    preferredEntityClassifications: EntityClassificationList

    /// Whether to derive the entity-class vocabulary from this corpus at ingest
    /// start rather than inheriting graphrag's hardcoded news/finance defaults
    /// ("Company", "Sports Team", "Creative Work", …). Ignored when
    /// preferredEntityClassifications is set. Defaults to true.
    inferEntityClassifications: Boolean

    /// Explicit TOPIC vocabulary handed to the extraction LLM. Topics are the
    /// thematic groupings chunks are assigned to — they become ``__Topic__``
    /// nodes and are later induced as ``skos:Concept`` — as distinct from
    /// preferredEntityClassifications, which types the entities themselves
    /// (``__Entity__.class``, induced as ``owl:Class``).
    ///
    /// The extractor prefers a listed topic when one matches the content in
    /// meaning and specificity, and invents a name otherwise, so this is a
    /// steer rather than a closed set. Case-sensitive, spaces allowed
    /// (e.g. "Black Tie Gala", "Everyday Elegance").
    ///
    /// Leave empty (the default) to let the extractor name every topic from the
    /// chunk text. There is deliberately no ``inferTopics`` flag: the toolkit
    /// has no topic equivalent of the entity-classification inference pass, so
    /// an empty list already *is* "let the model decide".
    preferredTopics: TopicList

    /// Whether to route every PDF through Amazon Textract's AnalyzeDocument
    /// with TABLES feature (instead of unstructured.partition_pdf strategy=
    /// "fast", which does not detect tables). Preserves row/column structure
    /// through preprocessing so a statistical table survives as facts, not
    /// prose. Costs materially more per page than DetectDocumentText; leave
    /// off for prose-dominant corpora. Defaults to false.
    enableTableExtraction: Boolean

    /// Chunk size (in tokens) for the SentenceSplitter that splits staged
    /// documents into extraction/embedding units. Larger values keep more
    /// context (a full table row + header, a full policy clause) in one
    /// chunk. **`0` means "use the graphrag-toolkit default (256)"** — the
    /// sentinel that survives the state-machine → ECS env-var pipe without
    /// a special-cased missing-field state. Set a positive value (e.g. 1024
    /// for dense/tabular corpora) to override.
    @range(min: 0, max: 4096)
    chunkSize: Integer

    /// Overlap (in tokens) between consecutive chunks. **`0` means "use the
    /// graphrag-toolkit default (25)"**. Ignored unless chunkSize > 0.
    @range(min: 0, max: 512)
    chunkOverlap: Integer

    /// Whether to delete previous document versions on re-ingest.
    deletePrevVersions: Boolean

    bedrockModelArn: BedrockModelArn
}

/// A user-supplied list of entity classifications. Members must be non-empty
/// after trim; empty strings are dropped by the API.
@length(min: 0, max: 100)
list EntityClassificationList {
    member: EntityClassification
}

@length(min: 1, max: 128)
string EntityClassification

/// A user-supplied list of preferred topic names. Members must be non-empty
/// after trim; empty strings are dropped by the API.
@length(min: 0, max: 100)
list TopicList {
    member: Topic
}

/// A topic name. Longer than EntityClassification because editorial topic names
/// are phrases ("Black Tie Gala Dress Code"), not single labels.
@length(min: 1, max: 256)
string Topic

list S3PrefixList {
    member: S3Prefix
}

/// Type of per-file preprocessing issue.
enum PreprocessingIssueType {
    /// The file was skipped during preprocessing.
    SKIPPED = "skipped"

    /// The file errored during preprocessing.
    ERROR = "error"
}

/// A single file-level preprocessing issue recorded during ingestion.
structure PreprocessingIssue {
    /// Name of the file the issue applies to.
    @required
    filename: String

    @required
    type: PreprocessingIssueType

    /// Human-readable explanation of the issue.
    @required
    reason: String
}

list PreprocessingIssueList {
    member: PreprocessingIssue
}

/// A single file to upload, identified by filename and MIME content type.
structure UploadFileRequest {
    /// Name of the file to upload.
    @required
    filename: String

    /// MIME content type of the file.
    @required
    contentType: String
}

list UploadFileRequestList {
    member: UploadFileRequest
}

/// A pre-signed S3 PUT URL for a single file.
structure UploadUrl {
    /// Name of the file this URL is for.
    @required
    filename: String

    /// Pre-signed S3 PUT URL for uploading the file.
    @required
    uploadUrl: String
}

list UploadUrlList {
    member: UploadUrl
}

// =============================================================================
// Enums
// =============================================================================
/// Top-level category of a unified source.
enum SourceType {
    /// A structured database source.
    DATABASE

    /// An unstructured documents source.
    DOCUMENTS
}

/// Specific connector sub-type within a source category.
enum SourceSubType {
    /// Structured: AWS Glue Data Catalog database
    GLUE_DATABASE

    /// Structured: JDBC-connected relational database
    JDBC_DATABASE

    /// Structured: customer-authored AWS Athena Query Federation SDK connector
    /// (a Lambda in the customer's account), registered as a Lambda-backed
    /// Athena data catalog and queried through Athena
    CUSTOM_CONNECTOR

    /// Unstructured: S3 bucket prefix
    S3

    /// Unstructured: direct file upload
    LOCAL_UPLOAD
}

/// Unified lifecycle status for all source types.
enum SourceStatus {
    /// Source registered, pipeline not yet started
    REGISTERED

    /// Pipeline is running (schema discovery or document ingestion)
    SCANNING

    /// LLM entity extraction in progress (document sources only)
    SCANNING_ENTITY_EXTRACTION

    /// Knowledge graph build in progress (document sources only)
    SCANNING_KG_BUILD

    /// AI metadata enrichment in progress (database sources only)
    ENRICHING

    /// Enrichment complete, awaiting human review (database sources only)
    PENDING_REVIEW

    /// A re-scan of an already-APPROVED source detected changes that are
    /// awaiting steward review. Distinct from PENDING_REVIEW (initial
    /// enrichment) so the UI can show "approved source, N changes to review"
    /// and so downstream consumers that gate on APPROVED treat the source as
    /// not-currently-approved while its drift is under review. On approve the
    /// source returns to APPROVED; on reject it returns to APPROVED unchanged.
    /// (database sources only)
    RESCAN_REVIEW

    /// Bulk approve in progress — async worker is writing DataZone revisions
    /// (database sources only)
    APPROVING

    /// Async bulk approve worker failed; client may retry by calling
    /// ApproveSource again (database sources only)
    APPROVAL_FAILED

    /// Bulk reject in progress — async worker is writing DataZone revisions
    /// (database sources only)
    REJECTING

    /// Async bulk reject worker failed; client may retry by calling
    /// RejectSource again (database sources only)
    REJECTION_FAILED

    /// Processing completed successfully (document sources)
    COMPLETED

    /// All tables reviewed and approved (database sources only)
    APPROVED

    /// Bulk reject completed — the source and all its tables/columns are
    /// rejected. Terminal: no further review transitions are allowed; the
    /// steward must re-onboard a new source (database sources only)
    REJECTED

    /// Processing failed
    SCAN_FAILED

    /// Deletion in progress
    DELETING

    /// Fully deleted
    DELETED

    /// Deletion failed
    DELETE_FAILED
}

/// Status of a database scan job execution.
enum SourceScanJobStatus {
    /// Scan job is running.
    IN_PROGRESS

    /// Scan job is discovering tables and schema.
    DISCOVERING

    /// Scan job is enriching metadata.
    ENRICHING

    /// Scan job completed successfully.
    COMPLETED

    /// Scan job failed.
    FAILED

    /// Scan job was cancelled.
    CANCELLED
}

/// Kind of entry in a source's scan history. A row with no explicit type is a
/// SCAN (the scan-job rows written before review events existed carry none).
enum ScanJobEventType {
    /// A discovery/enrichment scan of the source.
    SCAN

    /// A steward approve/reject decision (including re-scan approve/reject).
    REVIEW
}

// =============================================================================
// Core structures — shared across multiple operations
// =============================================================================
/// Summary representation of a unified source (used in list responses).
/// Contains only the fields common to all source types.
structure SourceSummary {
    /// Unique source identifier.
    @required
    sourceId: String

    @required
    namespaceId: Uuid

    /// Display name of the source.
    @required
    name: String

    @required
    sourceType: SourceType

    @required
    sourceSubType: SourceSubType

    @required
    status: SourceStatus

    /// Timestamp when the source was created.
    @required
    createdAt: Timestamp

    /// Timestamp when the source was last updated.
    updatedAt: Timestamp

    /// Number of tables discovered in the most recent scan (DATABASE sources only).
    tablesDiscovered: Integer
}

list SourceSummaryList {
    member: SourceSummary
}

// ── Create structures ─────────────────────────────────────────────────────────
/// Top-level create request. Exactly one of databaseSource or documentSource
/// must be set, matching the sourceType discriminator.
structure CreateSourceInput {
    @required
    sourceType: SourceType

    /// Required when sourceType = DATABASE.
    databaseSource: CreateDatabaseSourceInput

    /// Required when sourceType = DOCUMENTS.
    documentSource: CreateDocumentSourceInput
}

/// Database source creation fields.
/// Exactly one of glueConfiguration, jdbcConfiguration or customConnectorConfiguration
/// must be provided — it selects the sub-type.
structure CreateDatabaseSourceInput {
    @required
    name: DisplayName

    /// Glue Data Catalog config. Required for GLUE_DATABASE sub-type.
    glueConfiguration: GlueConfiguration

    /// JDBC connection config. Required for JDBC_DATABASE sub-type.
    jdbcConfiguration: JdbcConfiguration

    /// Custom Athena federation connector config. Required for the
    /// CUSTOM_CONNECTOR sub-type.
    customConnectorConfiguration: CustomConnectorConfiguration

    /// Enable AI-powered business metadata enrichment (descriptions,
    /// synonyms, glossary terms, tags). Defaults to true. When false the
    /// scan pipeline skips the enrichment step and the source transitions
    /// directly from SCANNING to PENDING_REVIEW with discovered technical
    /// metadata only.
    metadataEnrichmentEnabled: Boolean
}

/// Document source creation fields.
/// For S3 sources: provide sourceBucketArn (and optionally s3Prefixes, roleArn).
/// For upload sources: provide uploadId from GetSourceUploadUrls; the ingest
/// prefix is derived server-side as {namespaceId}/raw/{uploadId}/ and any
/// caller-supplied s3Prefixes is ignored.
structure CreateDocumentSourceInput {
    @required
    name: DisplayName

    /// S3 key prefixes to ingest. Used only for the S3 sub-type, where they
    /// point into the caller-owned sourceBucketArn. Ignored for upload sources,
    /// whose prefix is derived server-side from uploadId.
    s3Prefixes: S3PrefixList

    /// Server-issued upload session identifier returned by GetSourceUploadUrls.
    /// Required for the upload sub-type; the ingest prefix is reconstructed as
    /// {namespaceId}/raw/{uploadId}/ so a caller cannot redirect ingestion at
    /// another namespace's objects.
    uploadId: Uuid

    /// ARN of the S3 bucket. Required for S3 sub-type.
    sourceBucketArn: S3BucketArn

    /// IAM role for cross-account S3 access.
    roleArn: IamRoleArn

    extractionConfig: ExtractionConfig
}

/// Create source response.
structure CreateSourceOutput {
    /// Unique identifier of the newly created source.
    @required
    sourceId: String

    @required
    status: SourceStatus

    /// Identifier of the scan job started for the source, if any.
    scanJobId: String

    /// Timestamp when the source was created.
    @required
    createdAt: Timestamp
}

// ── Get structures ────────────────────────────────────────────────────────────
/// Full source record returned by GetSource.
/// Uses sourceType as a discriminator — branch on it to access type-specific detail:
///   DATABASE  → databaseDetails is populated; documentDetails is absent.
///   DOCUMENTS → documentDetails is populated; databaseDetails is absent.
structure GetSourceOutput {
    /// Unique source identifier.
    @required
    sourceId: String

    @required
    namespaceId: Uuid

    /// Display name of the source.
    @required
    name: String

    @required
    sourceType: SourceType

    @required
    sourceSubType: SourceSubType

    @required
    status: SourceStatus

    /// Timestamp when the source was created.
    @required
    createdAt: Timestamp

    /// Timestamp when the source was last updated.
    updatedAt: Timestamp

    /// Populated when sourceType = DATABASE. Absent for DOCUMENTS sources.
    databaseDetails: DatabaseSourceDetail

    /// Populated when sourceType = DOCUMENTS. Absent for DATABASE sources.
    documentDetails: DocumentSourceDetail
}

/// Type-specific detail for DATABASE sources.
structure DatabaseSourceDetail {
    glueConfiguration: GlueConfiguration

    jdbcConfiguration: JdbcConfiguration

    /// Populated for the CUSTOM_CONNECTOR sub-type; absent otherwise.
    customConnectorConfiguration: CustomConnectorConfiguration

    /// Whether AI-powered business metadata enrichment runs for this source.
    /// Set at creation time and persisted with the source record. When false
    /// the scan pipeline skips the enrichment step and the source transitions
    /// from SCANNING straight to PENDING_REVIEW.
    metadataEnrichmentEnabled: Boolean

    /// Preferred execution engine for single-source queries against this source.
    /// Read-only and system-set at creation: `JDBC` for direct (low-latency)
    /// execution, set only for engines with an implemented direct dialect
    /// (PostgreSQL/Redshift today); otherwise `ATHENA` (native Glue, and JDBC
    /// engines without a direct path yet). The consumption layer overrides this
    /// to Athena for any query spanning multiple sources.
    queryEngine: QueryEngine

    /// Number of tables discovered in the most recent scan.
    tablesDiscovered: Integer

    /// Number of tables with reviewStatus=APPROVED.
    tablesApproved: Integer

    /// Identifier of the most recent scan job.
    lastScanJobId: String

    /// Timestamp of the most recent scan.
    lastScanAt: Timestamp

    /// Name of the Glue Connection that backs Athena federated queries
    /// for this data source. Read-only and system-managed: populated
    /// only for JDBC sub-types (PostgreSQL, MySQL, Redshift, Oracle, SQL
    /// Server, Snowflake) on first scan and removed when the source is
    /// deleted. Null for S3/Iceberg-backed Glue databases — those are
    /// queried via Athena's native AwsDataCatalog.
    ///
    /// To query the source via Athena, pair this with the namespace's
    /// `athenaWorkgroupName` and the `athenaDataCatalogName` below. See
    /// the Athena Queryability section of `packages/sources/README.md`
    /// for examples.
    glueConnectionName: String

    /// Name of the Athena data catalog registered for this source. Read-only
    /// and system-managed, derived from `{resource-prefix}ds_{sourceId}`. Null
    /// for S3/Iceberg-backed Glue databases, which are queried through Athena's
    /// native `AwsDataCatalog`.
    ///
    /// The addressing differs by sub-type, because the two catalogs are not the
    /// same kind of object:
    ///
    /// - JDBC sub-types: a managed Glue federated catalog, NESTED under
    ///   `AwsDataCatalog`, and equal to `glueConnectionName`. Query it as
    ///   `AwsDataCatalog.<athenaDataCatalogName>.<schema>.<table>`.
    /// - CUSTOM_CONNECTOR: a LAMBDA-type Athena catalog bound to the customer's
    ///   connector Lambda. It is a TOP-LEVEL catalog, not nested, so query it as
    ///   `<athenaDataCatalogName>.<databaseName>.<table>`.
    ///
    /// Run the query inside the namespace's Athena workgroup (see
    /// NamespaceDetail.athenaWorkgroupName).
    athenaDataCatalogName: String
}

/// Type-specific detail for DOCUMENTS sources.
structure DocumentSourceDetail {
    /// S3 key prefixes ingested for this source.
    s3Prefixes: S3PrefixList

    sourceBucketArn: S3BucketArn

    roleArn: IamRoleArn

    extractionConfig: ExtractionConfig

    /// Total number of files considered for ingestion.
    filesTotal: Integer

    /// Number of files skipped during preprocessing.
    filesSkipped: Integer

    /// Number of files that errored during preprocessing.
    filesErrored: Integer

    /// Error message if processing failed.
    errorMessage: String

    /// Per-file preprocessing issues recorded during ingestion.
    preprocessingIssues: PreprocessingIssueList

    /// Distinct source documents opened for chunk extraction by the KG build,
    /// read from the per-source metrics record (PK=METRICS#{ns}, SK=KGBUILD#{ds}).
    /// Null until the KG build has run and written metrics.
    documentsProcessed: Integer

    /// Count of LLM extraction prompts run on chunks (topic + proposition each
    /// fire once per chunk, so this is ~= extractors x chunks; NOT distinct).
    /// Null until the KG build has run.
    chunksLLM: Integer

    /// Distinct chunks whose embeddings were written to the OSS chunk vector
    /// index. Bounded by the true chunk count. Null until the KG build has run.
    chunksEmbed: Integer

    /// Distinct chunk nodes inserted into the Neptune graph. Bounded by the true
    /// chunk count. Null until the KG build has run.
    chunksGraph: Integer
}

// =============================================================================
// Operations
// =============================================================================
// ── Source CRUD ───────────────────────────────────────────────────────────────
/// List all sources in a namespace across all source types.
@http(method: "GET", uri: "/namespaces/{namespaceId}/sources")
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "items")
operation ListSources {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Filter by source type (DATABASE or DOCUMENTS).
        @httpQuery("sourceType")
        sourceType: SourceType

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: String

        /// Maximum number of items to return per page.
        @httpQuery("maxResults")
        maxResults: Integer
    }

    output := with [PaginatedOutput] {
        /// Page of source summaries.
        @required
        items: SourceSummaryList
    }
}

/// Create a new source.
/// The body must contain sourceType plus exactly one of:
///   databaseSource  — when sourceType = DATABASE
///   documentSource  — when sourceType = DOCUMENTS
@http(method: "POST", uri: "/namespaces/{namespaceId}/sources")
operation CreateSource {
    input: CreateSourceOperationInput
    output: CreateSourceOperationOutput
}

@input
structure CreateSourceOperationInput {
    @required
    @httpLabel
    namespaceId: Uuid

    @required
    @httpPayload
    body: CreateSourceInput
}

@output
structure CreateSourceOperationOutput {
    @required
    @httpPayload
    body: CreateSourceOutput
}

/// Get a single source by ID.
@http(method: "GET", uri: "/namespaces/{namespaceId}/sources/{sourceId}")
@readonly
operation GetSource {
    input: GetSourceOperationInput
    output: GetSourceOperationOutput
}

@input
structure GetSourceOperationInput {
    @required
    @httpLabel
    namespaceId: Uuid

    /// Identifier of the source to fetch.
    @required
    @httpLabel
    sourceId: String
}

@output
structure GetSourceOperationOutput {
    @required
    @httpPayload
    body: GetSourceOutput
}

/// Delete a source and all associated data.
@http(method: "DELETE", uri: "/namespaces/{namespaceId}/sources/{sourceId}")
operation DeleteSource {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source to delete.
        @required
        @httpLabel
        sourceId: String
    }

    output := {
        /// Identifier of the deleted source.
        sourceId: String

        status: SourceStatus
    }
}

/// Re-trigger the scan/ingestion pipeline for an existing source.
@http(method: "POST", uri: "/namespaces/{namespaceId}/sources/{sourceId}/rescan")
operation RescanSource {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source to rescan.
        @required
        @httpLabel
        sourceId: String

        /// Acknowledge that starting this re-scan discards an open re-scan
        /// review. Required only when the source is in RESCAN_REVIEW: a fresh
        /// re-scan re-diffs against the last APPROVED state, so any review
        /// decisions or edits made inside the open review window are lost.
        /// Omitting it in that state returns 409 rather than silently
        /// discarding the review. Ignored from every other status, where a
        /// re-scan discards nothing.
        confirmDiscardOpenReview: Boolean
    }

    output := {
        /// Identifier of the rescanned source.
        sourceId: String

        status: SourceStatus
    }
}

/// Update mutable metadata fields on an existing source (DATABASE only).
@http(method: "PUT", uri: "/namespaces/{namespaceId}/sources/{sourceId}/metadata")
operation UpdateSourceMetadata {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source to update.
        @required
        @httpLabel
        sourceId: String

        name: DisplayName

        glueConfiguration: GlueConfiguration

        jdbcConfiguration: JdbcConfiguration

        customConnectorConfiguration: CustomConnectorConfiguration

        /// Whether AI-powered metadata enrichment runs for this source.
        metadataEnrichmentEnabled: Boolean
    }

    output := {
        /// Identifier of the updated source.
        @required
        sourceId: String

        @required
        status: SourceStatus

        /// Timestamp when the source was last updated.
        @required
        updatedAt: Timestamp
    }
}

/// Generate pre-signed S3 upload URLs for DOCUMENTS sources (upload flow).
@http(method: "POST", uri: "/namespaces/{namespaceId}/sources/upload-urls", code: 200)
operation GetSourceUploadUrls {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Files to generate pre-signed upload URLs for.
        @required
        files: UploadFileRequestList
    }

    output := {
        @required
        uploadId: Uuid

        @required
        s3Prefix: S3Prefix

        /// Pre-signed S3 PUT URLs, one per requested file.
        @required
        uploadUrls: UploadUrlList

        /// Seconds until the generated URLs expire.
        @required
        expiresIn: Integer
    }
}

// ── Table operations (DATABASE sources) ──────────────────────────────────────
/// List tables discovered for a DATABASE source with enrichment/review summary.
@http(method: "GET", uri: "/namespaces/{namespaceId}/sources/{sourceId}/tables")
@readonly
@paginated(inputToken: "nextToken", outputToken: "nextToken", pageSize: "maxResults", items: "items")
operation ListSourceTables {
    input := with [PaginatedInput] {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source whose tables to list.
        @required
        @httpLabel
        sourceId: String

        /// Pagination token from a previous response.
        @httpQuery("nextToken")
        nextToken: String

        /// Maximum number of items to return per page.
        @httpQuery("maxResults")
        maxResults: Integer

        @httpQuery("reviewStatus")
        reviewStatus: ReviewStatus
    }

    output := with [PaginatedOutput] {
        /// Page of table summaries.
        @required
        items: TableSummaryList

        /// Number of assets skipped due to retrieval or parsing failures.
        /// Present only when one or more assets could not be loaded, indicating
        /// a degraded (partial) response.
        skippedAssets: Integer
    }
}

/// Get full table metadata including all columns with business metadata.
@http(method: "GET", uri: "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}")
@readonly
operation GetSourceTable {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source containing the table.
        @required
        @httpLabel
        sourceId: String

        /// Identifier of the table to fetch.
        @required
        @httpLabel
        tableId: String
    }

    output: GetSourceTableOutput
}

@output
structure GetSourceTableOutput {
    /// Unique table identifier (database.table format).
    @required
    tableId: String

    /// Table name.
    @required
    name: String

    /// Database the table belongs to.
    @required
    database: String

    @required
    reviewStatus: ReviewStatus

    /// Metadata for every column in the table.
    @required
    columns: ColumnMetadataList

    businessMetadata: BusinessMetadataOutput

    primaryKey: PrimaryKeyOutput

    /// Foreign-key relationships reported for the table.
    foreignKeys: ForeignKeyList

    technicalMetadata: TechnicalMetadataOutput

    /// True when a re-scan found this table removed from the source; it is
    /// deleted on approve unless the steward keeps it. Absent/false otherwise.
    pendingDeletion: Boolean

    /// Old-vs-new breakdown of what the current re-scan changed for this table,
    /// for the steward's before/after review. Present only while the source is
    /// in RESCAN_REVIEW and this table changed; absent otherwise.
    rescanDiff: RescanTableDiff

    /// True when the current re-scan discovered this table for the first time
    /// (it did not exist in the last approved scan). It therefore has no
    /// old-vs-new diff — the whole table is new. Absent/false otherwise.
    added: Boolean
}

// ── Resource-oriented review endpoints ────────────────────────────────────────
//
// Replaces the deprecated ReviewSourceMetadata "god endpoint" with six
// single-purpose operations. Bulk approve/reject is async (202) to avoid the
// API Gateway 29s timeout for sources with 75+ tables. Per-table and
// per-column review/edit are synchronous and fast (~400ms) because they
// operate on a single asset.
//
// Cascade semantics:
//   ApproveSource cascades APPROVED to all PENDING tables and PENDING columns.
//   ReviewSourceTable APPROVED cascades to PENDING columns on that table only.
//   Neither cascade overrides REJECTED status — explicit re-approval required.
//
// Idempotency:
//   All review endpoints are idempotent. Approving an already-approved
//   resource is a 200 no-op. The async bulk endpoints transition status
//   PENDING_REVIEW → APPROVING via a conditional DynamoDB update so duplicate
//   SQS messages cannot start two workers.
//
// Edit vs. review separation:
//   UpdateSourceTableMetadata / UpdateSourceColumnMetadata sets
//   enrichmentSource = STEWARD_EDITED and does NOT change reviewStatus.
//   "Edit + Approve" UX is two calls: PATCH /metadata, then PUT /review.
/// Approve all PENDING tables and columns of a DATABASE source asynchronously.
///
/// Returns 202 immediately and transitions the source's status to APPROVING.
/// A background worker writes DataZone asset revisions for any asset whose
/// review status actually changes. On completion the source's status becomes
/// APPROVED; on failure it becomes APPROVAL_FAILED. Clients poll the source's
/// status via GetSource until it leaves the APPROVING state.
///
/// Returns 409 if the source is not in PENDING_REVIEW or APPROVAL_FAILED.
@http(method: "POST", uri: "/namespaces/{namespaceId}/sources/{sourceId}/approve", code: 202)
operation ApproveSource {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source to approve.
        @required
        @httpLabel
        sourceId: String
    }

    output := {
        /// Identifier of the source being approved.
        @required
        sourceId: String

        @required
        status: SourceStatus
    }
}

/// Reject all PENDING tables and columns of a DATABASE source asynchronously.
///
/// Returns 202 immediately and transitions the source's status to REJECTING.
/// A background worker writes DataZone asset revisions, marking each
/// non-REJECTED column as REJECTED. On success the source returns to
/// PENDING_REVIEW (rejection does not approve the source overall — the
/// steward can re-review). On failure it becomes REJECTION_FAILED. Clients
/// poll the source's status via GetSource until it leaves the REJECTING state.
///
/// Returns 409 if the source is not in PENDING_REVIEW or REJECTION_FAILED.
@http(method: "POST", uri: "/namespaces/{namespaceId}/sources/{sourceId}/reject", code: 202)
operation RejectSource {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source to reject.
        @required
        @httpLabel
        sourceId: String
    }

    output := {
        /// Identifier of the source being rejected.
        @required
        sourceId: String

        @required
        status: SourceStatus
    }
}

/// Review a single table — set reviewStatus to APPROVED or REJECTED.
///
/// Cascades to PENDING columns on that table only (REJECTED columns are not
/// touched). DataZone asset revision is only written when the resulting
/// status differs from the existing one.
@http(method: "PUT", uri: "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/review")
@idempotent
operation ReviewSourceTable {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source containing the table.
        @required
        @httpLabel
        sourceId: String

        @required
        @httpLabel
        tableId: TableId

        @required
        decision: ReviewDecision
    }

    output := {
        /// Identifier of the reviewed table.
        @required
        tableId: String

        @required
        reviewStatus: ReviewStatus
    }
}

/// Edit a single table's business metadata (description, synonyms, glossary
/// terms, tags). Sets enrichmentSource = STEWARD_EDITED.
///
/// Does NOT change reviewStatus. Use ReviewSourceTable to approve/reject.
@http(method: "PATCH", uri: "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/metadata")
operation UpdateSourceTableMetadata {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source containing the table.
        @required
        @httpLabel
        sourceId: String

        @required
        @httpLabel
        tableId: TableId

        @required
        overrides: MetadataOverrides
    }

    output := {
        /// Identifier of the updated table.
        @required
        tableId: String

        @required
        reviewStatus: ReviewStatus
    }
}

/// Review a single column on a table — set reviewStatus to APPROVED or REJECTED.
///
/// DataZone asset revision is only written when the resulting status differs
/// from the existing one.
@http(method: "PUT", uri: "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/review")
@idempotent
operation ReviewSourceColumn {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source containing the column.
        @required
        @httpLabel
        sourceId: String

        @required
        @httpLabel
        tableId: TableId

        @required
        @httpLabel
        columnName: ColumnName

        @required
        decision: ReviewDecision
    }

    output := {
        /// Identifier of the table containing the column.
        @required
        tableId: String

        /// Name of the reviewed column.
        @required
        columnName: String

        @required
        reviewStatus: ReviewStatus
    }
}

/// Decline a re-scan-flagged removal so approving the re-scan will NOT delete
/// it. Drops the table (or one column of it) from the re-scan removal set in the
/// backup blob; the surfaced pendingDeletion marker clears on the next read.
///
/// "Removed from the source" does not always mean "deleted" — a partial scan, a
/// permission change, or a narrowed include/exclude filter can drop a table or
/// column that should be kept. This lets the steward veto a specific removal.
///
/// Only valid while the source is in RESCAN_REVIEW. Omit columnName to keep the
/// whole table; set it to keep one removed column. Idempotent — keeping an item
/// that is not in the removal set is a no-op.
@http(method: "PUT", uri: "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keep")
@idempotent
operation KeepRescanRemoval {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source containing the flagged table.
        @required
        @httpLabel
        sourceId: String

        @required
        @httpLabel
        tableId: TableId

        /// If set, keep only this removed column of the table. Omit to keep the
        /// whole removed table.
        columnName: ColumnName
    }

    output := {
        /// Identifier of the table the kept item belongs to.
        @required
        tableId: String

        /// The kept column, echoed back only when keeping a single column.
        columnName: String

        /// The item's removal state after the keep. Always false — the item is
        /// no longer pending deletion.
        @required
        pendingDeletion: Boolean
    }
}

/// Edit a single column's business metadata. Sets
/// enrichmentSource = STEWARD_EDITED. Does NOT change reviewStatus.
@http(
    method: "PATCH"
    uri: "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/metadata"
)
operation UpdateSourceColumnMetadata {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source containing the column.
        @required
        @httpLabel
        sourceId: String

        @required
        @httpLabel
        tableId: TableId

        @required
        @httpLabel
        columnName: ColumnName

        @required
        overrides: MetadataOverrides
    }

    output := {
        /// Identifier of the table containing the column.
        @required
        tableId: String

        /// Name of the updated column.
        @required
        columnName: String

        @required
        reviewStatus: ReviewStatus
    }
}

/// Edit a table's primary key and foreign key relationships. Steward intent is
/// authoritative: affected keys are stored with source = STEWARD_SPECIFIED,
/// overriding deterministic or AI-inferred keys. Provide `primaryKey` and/or
/// `foreignKeys` — an omitted field is left unchanged; an empty list clears it.
/// Key columns are validated against the table's columns. Does NOT change
/// reviewStatus.
@http(method: "PATCH", uri: "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keys")
operation UpdateSourceTableKeys {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source containing the table.
        @required
        @httpLabel
        sourceId: String

        @required
        @httpLabel
        tableId: TableId

        primaryKey: PrimaryKeyInput

        /// Foreign-key relationships to set for the table.
        foreignKeys: ForeignKeyInputList
    }

    output := {
        /// Identifier of the updated table.
        @required
        tableId: String

        @required
        reviewStatus: ReviewStatus
    }
}

// ── Scan job (DATABASE sources) ───────────────────────────────────────────────
/// Get the status and results of a scan job for a DATABASE source.
@http(method: "GET", uri: "/namespaces/{namespaceId}/sources/{sourceId}/scan/{jobId}")
@readonly
operation GetSourceScanJob {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source the scan job belongs to.
        @required
        @httpLabel
        sourceId: String

        /// Identifier of the scan job to fetch.
        @required
        @httpLabel
        jobId: String
    }

    output: GetSourceScanJobOutput
}

@output
structure GetSourceScanJobOutput {
    /// Unique scan job identifier.
    @required
    jobId: String

    /// Identifier of the source scanned.
    @required
    sourceId: String

    @required
    status: SourceScanJobStatus

    /// Number of tables discovered by the scan.
    tablesDiscovered: Integer

    /// Number of tables added since the previous scan.
    tablesAdded: Integer

    /// Number of tables removed since the previous scan.
    tablesRemoved: Integer

    /// Number of tables modified since the previous scan.
    tablesModified: Integer

    /// Timestamp when the scan job started.
    @required
    startedAt: Timestamp

    /// Timestamp when the scan job completed.
    completedAt: Timestamp

    /// Error message if the scan job failed.
    errorMessage: String

    /// Number of tables the scan listed but could not read.
    ///
    /// Absent when none failed, rather than zero, so it can be used directly to
    /// filter for degraded scans. A non-zero value means the scan SUCCEEDED but
    /// is incomplete: those tables carry no columns, no comments, and no declared
    /// keys, and AI enrichment will have generated descriptions over the gap — so
    /// a reviewer must not read the result as complete. Only connectors that read
    /// each table separately can report this; one that returns a table's schema in
    /// the same call that lists it cannot partially fail.
    tablesFailed: Integer

    /// The unreadable tables as `database.table`, capped in length.
    ///
    /// A signal for diagnosis, not an inventory — `tablesFailed` is the exact
    /// figure and this list may be shorter than it.
    failedTables: FailedTableList
}

/// Qualified `database.table` names of tables a scan listed but could not read.
list FailedTableList {
    member: String
}

// ── Scan history (DATABASE sources) ───────────────────────────────────────────
/// List the scan and review history for a DATABASE source, newest first.
/// Combines discovery/enrichment scan rows with steward approve/reject events
/// so the console can show a real audit trail instead of a derived guess.
@http(method: "GET", uri: "/namespaces/{namespaceId}/sources/{sourceId}/scan")
@readonly
operation ListSourceScanJobs {
    input := {
        @required
        @httpLabel
        namespaceId: Uuid

        /// Identifier of the source whose history to list.
        @required
        @httpLabel
        sourceId: String
    }

    output: ListSourceScanJobsOutput
}

@output
structure ListSourceScanJobsOutput {
    /// History entries, newest first.
    @required
    items: ScanJobEntryList
}

/// A single scan-history entry: either a scan of the source or a steward
/// review decision.
list ScanJobEntryList {
    member: ScanJobEntry
}

/// One row in a source's scan history.
structure ScanJobEntry {
    /// When the entry occurred (scan start, or the moment the review resolved).
    @required
    at: Timestamp

    /// Whether this row is a scan or a review event. Absent on legacy scan-job
    /// rows written before review events existed — treat a missing value as SCAN.
    eventType: ScanJobEventType

    /// Status of the row. For a SCAN this is the scan-job status
    /// (IN_PROGRESS/DISCOVERING/ENRICHING/COMPLETED/FAILED/CANCELLED); for a
    /// REVIEW it is the terminal source status the decision produced
    /// (e.g. APPROVED, REJECTED, APPROVAL_FAILED). Free-form because the two
    /// event kinds draw from different status vocabularies.
    status: String

    /// For a SCAN row: whether it was a full or incremental (re-scan) run.
    scanType: String

    /// Number of tables discovered by the scan (SCAN rows).
    tablesDiscovered: Integer

    /// Number of tables approved as of this review (REVIEW rows).
    tablesApproved: Integer

    /// Timestamp when the scan job completed (SCAN rows).
    completedAt: Timestamp

    /// Error message if the scan job failed (SCAN rows).
    errorMessage: String

    /// The steward's decision for a REVIEW row.
    decision: ReviewDecision

    /// True when this event resolved a re-scan review (rather than an initial
    /// approve/reject). REVIEW rows only.
    isRescan: Boolean
}
