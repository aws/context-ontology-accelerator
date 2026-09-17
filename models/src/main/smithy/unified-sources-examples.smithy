// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// @examples for com.amazon.semanticcontext.unifiedsources operations.
// Extracted from the service definition to keep operation/shape
// definitions readable. `apply` merges the @examples trait back onto
// each operation, so the doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.unifiedsources

apply ListSources @examples([
    {
        title: "List the database sources in a namespace"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceType: "DATABASE", maxResults: 20 }
        output: {
            items: [
                {
                    sourceId: "src-7f3a9c1e"
                    namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
                    name: "Sales Warehouse"
                    sourceType: "DATABASE"
                    sourceSubType: "GLUE_DATABASE"
                    status: "PENDING_REVIEW"
                    createdAt: "2026-07-23T14:30:00Z"
                    updatedAt: "2026-07-23T14:35:00Z"
                    tablesDiscovered: 12
                }
            ]
            nextToken: "eyJvZmZzZXQiOiAyMH0="
        }
    }
])

apply CreateSource @examples([
    {
        title: "Register a Glue Data Catalog database source"
        input: {
            namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
            body: {
                sourceType: "DATABASE"
                databaseSource: {
                    name: "Sales Warehouse"
                    glueConfiguration: { catalogId: "123456789012", region: "us-east-1", databaseName: "sales", tableFilter: "orders.*" }
                    metadataEnrichmentEnabled: true
                }
            }
        }
        output: {
            body: { sourceId: "src-7f3a9c1e", status: "SCANNING", scanJobId: "job-8b21d4f0", createdAt: "2026-07-23T14:30:00Z" }
        }
    }
])

apply GetSource @examples([
    {
        title: "Get a Glue database source"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e" }
        output: {
            body: {
                sourceId: "src-7f3a9c1e"
                namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
                name: "Sales Warehouse"
                sourceType: "DATABASE"
                sourceSubType: "GLUE_DATABASE"
                status: "PENDING_REVIEW"
                createdAt: "2026-07-23T14:30:00Z"
                updatedAt: "2026-07-23T14:35:00Z"
                databaseDetails: {
                    glueConfiguration: { catalogId: "123456789012", region: "us-east-1", databaseName: "sales" }
                    metadataEnrichmentEnabled: true
                    queryEngine: "ATHENA"
                    tablesDiscovered: 12
                    tablesApproved: 3
                    lastScanJobId: "job-8b21d4f0"
                    lastScanAt: "2026-07-23T14:35:00Z"
                }
            }
        }
    }
])

apply DeleteSource @examples([
    {
        title: "Delete a source"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e" }
        output: { sourceId: "src-7f3a9c1e", status: "DELETING" }
    }
])

apply RescanSource @examples([
    {
        title: "Re-scan an existing source"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e" }
        output: { sourceId: "src-7f3a9c1e", status: "SCANNING" }
    }
])

apply UpdateSourceMetadata @examples([
    {
        title: "Rename a source and enable metadata enrichment"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", name: "Sales Warehouse (Prod)", metadataEnrichmentEnabled: true }
        output: { sourceId: "src-7f3a9c1e", status: "PENDING_REVIEW", updatedAt: "2026-07-23T15:00:00Z" }
    }
])

apply GetSourceUploadUrls @examples([
    {
        title: "Request an upload URL for a PDF document"
        input: {
            namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
            files: [
                {
                    filename: "q3-revenue-report.pdf"
                    contentType: "application/pdf"
                }
            ]
        }
        output: {
            uploadId: "b7e2c4a0-1f3d-4e6b-8a2c-9d0e1f2a3b4c"
            s3Prefix: "uploads/b7e2c4a0-1f3d-4e6b-8a2c-9d0e1f2a3b4c/"
            uploadUrls: [
                {
                    filename: "q3-revenue-report.pdf"
                    uploadUrl: "https://example-bucket.s3.amazonaws.com/uploads/b7e2c4a0/q3-revenue-report.pdf?X-Amz-Signature=abc123"
                }
            ]
            expiresIn: 3600
        }
    }
])

apply ListSourceTables @examples([
    {
        title: "List tables awaiting review for a source"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", reviewStatus: "PENDING_REVIEW", maxResults: 20 }
        output: {
            items: [
                {
                    tableId: "sales.orders"
                    name: "orders"
                    database: "sales"
                    columnCount: 8
                    columnsApproved: 0
                    reviewStatus: "PENDING_REVIEW"
                    enrichmentSource: "AI_GENERATED"
                }
            ]
            nextToken: "eyJvZmZzZXQiOiAyMH0="
        }
    }
])

apply GetSourceTable @examples([
    {
        title: "Get full metadata for the orders table"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", tableId: "sales.orders" }
        output: {
            tableId: "sales.orders"
            name: "orders"
            database: "sales"
            reviewStatus: "PENDING_REVIEW"
            columns: [
                {
                    name: "order_id"
                    dataType: "bigint"
                    nullable: false
                    isPartitionKey: false
                    description: "Unique order identifier"
                    businessMetadata: {
                        description: "Primary key for an order"
                        synonyms: ["order number"]
                        glossaryTerms: ["Order"]
                        tags: ["pii-none"]
                        enrichmentSource: "AI_GENERATED"
                        reviewStatus: "PENDING_REVIEW"
                        confidence: 0.92
                    }
                    distinctValues: ["1001", "1002", "1003"]
                }
                {
                    name: "status"
                    dataType: "varchar"
                    nullable: false
                    isPartitionKey: false
                    distinctValues: ["PLACED", "SHIPPED", "DELIVERED"]
                }
            ]
            businessMetadata: {
                description: "Customer orders fact table"
                synonyms: ["sales orders"]
                glossaryTerms: ["Order"]
                tags: ["sales"]
                enrichmentSource: "AI_GENERATED"
                reviewStatus: "PENDING_REVIEW"
                confidence: 0.88
            }
            primaryKey: {
                columns: ["order_id"]
                source: "DISCOVERED"
                confidence: 1.0
            }
            foreignKeys: [
                {
                    column: "customer_id"
                    targetTable: "sales.customers"
                    targetColumn: "customer_id"
                    source: "AI_INFERRED"
                    confidence: 0.81
                }
            ]
            technicalMetadata: {
                columnCount: 8
                partitionKeys: ["order_date"]
                format: "PARQUET"
                location: "s3://example-bucket/sales/orders/"
            }
        }
    }
])

apply ApproveSource @examples([
    {
        title: "Bulk-approve all pending tables and columns of a source"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e" }
        output: { sourceId: "src-7f3a9c1e", status: "APPROVING" }
    }
])

apply RejectSource @examples([
    {
        title: "Bulk-reject all pending tables and columns of a source"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e" }
        output: { sourceId: "src-7f3a9c1e", status: "REJECTING" }
    }
])

apply ReviewSourceTable @examples([
    {
        title: "Approve a single table"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", tableId: "sales.orders", decision: "APPROVED" }
        output: { tableId: "sales.orders", reviewStatus: "APPROVED" }
    }
])

apply UpdateSourceTableMetadata @examples([
    {
        title: "Edit a table's business metadata"
        input: {
            namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
            sourceId: "src-7f3a9c1e"
            tableId: "sales.orders"
            overrides: {
                description: "Customer purchase orders, one row per order"
                synonyms: ["purchase orders", "sales orders"]
                glossaryTerms: ["Order"]
                tags: ["sales", "reviewed"]
            }
        }
        output: { tableId: "sales.orders", reviewStatus: "PENDING_REVIEW" }
    }
])

apply ReviewSourceColumn @examples([
    {
        title: "Reject a single column"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", tableId: "sales.orders", columnName: "internal_notes", decision: "REJECTED" }
        output: { tableId: "sales.orders", columnName: "internal_notes", reviewStatus: "REJECTED" }
    }
])

apply UpdateSourceColumnMetadata @examples([
    {
        title: "Edit a column's business metadata"
        input: {
            namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
            sourceId: "src-7f3a9c1e"
            tableId: "sales.orders"
            columnName: "order_total"
            overrides: {
                description: "Order grand total in USD, including tax"
                synonyms: ["order amount", "grand total"]
                glossaryTerms: ["Revenue"]
                tags: ["currency-usd"]
            }
        }
        output: { tableId: "sales.orders", columnName: "order_total", reviewStatus: "PENDING_REVIEW" }
    }
])

apply UpdateSourceTableKeys @examples([
    {
        title: "Set the primary key and a foreign key on a table"
        input: {
            namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
            sourceId: "src-7f3a9c1e"
            tableId: "sales.orders"
            primaryKey: {
                columns: ["order_id"]
            }
            foreignKeys: [
                {
                    column: "customer_id"
                    targetTable: "sales.customers"
                    targetColumn: "customer_id"
                }
            ]
        }
        output: { tableId: "sales.orders", reviewStatus: "PENDING_REVIEW" }
    }
])

apply KeepRescanRemoval @examples([
    {
        title: "Keep a table the re-scan flagged as removed"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", tableId: "sales.legacy_orders" }
        output: { tableId: "sales.legacy_orders", pendingDeletion: false }
    }
    {
        title: "Keep a single column the re-scan flagged as removed"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", tableId: "sales.orders", columnName: "legacy_region_code" }
        output: { tableId: "sales.orders", columnName: "legacy_region_code", pendingDeletion: false }
    }
])

apply GetSourceScanJob @examples([
    {
        title: "Get the status of a completed scan job"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e", jobId: "job-8b21d4f0" }
        output: { jobId: "job-8b21d4f0", sourceId: "src-7f3a9c1e", status: "COMPLETED", tablesDiscovered: 12, tablesAdded: 2, tablesRemoved: 0, tablesModified: 1, startedAt: "2026-07-23T14:30:00Z", completedAt: "2026-07-23T14:35:00Z" }
    }
])

apply ListSourceScanJobs @examples([
    {
        title: "List a source's scan and review history, newest first"
        input: { namespaceId: "3f2504e0-4f89-41d3-9a0c-0305e82c3301", sourceId: "src-7f3a9c1e" }
        output: {
            items: [
                {
                    at: "2026-07-23T15:02:00Z"
                    eventType: "REVIEW"
                    status: "APPROVED"
                    decision: "APPROVED"
                    isRescan: false
                    tablesApproved: 12
                }
                {
                    at: "2026-07-23T14:30:00Z"
                    eventType: "SCAN"
                    status: "COMPLETED"
                    scanType: "full"
                    tablesDiscovered: 12
                    completedAt: "2026-07-23T14:35:00Z"
                }
            ]
        }
    }
])
