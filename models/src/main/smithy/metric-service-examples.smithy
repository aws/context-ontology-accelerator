// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// @examples for com.amazon.semanticcontext.metric operations.
// Extracted from the service definition to keep operation/shape
// definitions readable. `apply` merges the @examples trait back onto
// each operation, so the doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.metric

apply CreateMetric @examples([
    {
        title: "Create a revenue metric"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            name: "total_revenue"
            description: "Sum of net revenue across all closed orders."
            expression: {
                dialects: [
                    {
                        dialect: "POSTGRESQL"
                        expression: "SELECT SUM(net_amount) FROM public.orders"
                    }
                ]
            }
            dataSourceId: "warehouse-pg-01"
            sourceTable: "public.orders"
            defaultTimeGrain: "MONTH"
            unit: "USD"
            returnType: "decimal"
        }
        output: {
            metric: {
                metricId: "01HZY8K3M4N5P6Q7R8S9T0V1W2"
                namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
                name: "total_revenue"
                description: "Sum of net revenue across all closed orders."
                expression: {
                    dialects: [
                        {
                            dialect: "POSTGRESQL"
                            expression: "SELECT SUM(net_amount) FROM public.orders"
                        }
                    ]
                }
                dataSourceId: "warehouse-pg-01"
                sourceTable: "public.orders"
                defaultTimeGrain: "MONTH"
                unit: "USD"
                returnType: "decimal"
                definedBy: "user-01HZY8K3M4N5P6Q7R8S9T0V1W2"
                effectiveFrom: "2026-07-23"
            }
            warnings: []
        }
    }
])

apply GetMetric @examples([
    {
        title: "Get a metric by name"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", name: "total_revenue" }
        output: {
            metric: {
                metricId: "01HZY8K3M4N5P6Q7R8S9T0V1W2"
                namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
                name: "total_revenue"
                description: "Sum of net revenue across all closed orders."
                expression: {
                    dialects: [
                        {
                            dialect: "POSTGRESQL"
                            expression: "SELECT SUM(net_amount) FROM public.orders"
                        }
                    ]
                }
                dataSourceId: "warehouse-pg-01"
                sourceTable: "public.orders"
                defaultTimeGrain: "MONTH"
                unit: "USD"
                returnType: "decimal"
                definedBy: "user-01HZY8K3M4N5P6Q7R8S9T0V1W2"
                effectiveFrom: "2026-07-23"
            }
        }
    }
])

apply ListMetrics @examples([
    {
        title: "List metrics filtered by source table"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", maxResults: 20, sourceTable: "public.orders" }
        output: {
            metrics: [
                {
                    metricId: "01HZY8K3M4N5P6Q7R8S9T0V1W2"
                    namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
                    name: "total_revenue"
                    description: "Sum of net revenue across all closed orders."
                    expression: {
                        dialects: [
                            {
                                dialect: "POSTGRESQL"
                                expression: "SELECT SUM(net_amount) FROM public.orders"
                            }
                        ]
                    }
                    dataSourceId: "warehouse-pg-01"
                    sourceTable: "public.orders"
                    defaultTimeGrain: "MONTH"
                    unit: "USD"
                    returnType: "decimal"
                }
            ]
            nextToken: "eyJvZmZzZXQiOjIwfQ"
        }
    }
])

apply UpdateMetric @examples([
    {
        title: "Update a metric's expression and description"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            name: "total_revenue"
            description: "Sum of net revenue across all closed and shipped orders."
            expression: {
                dialects: [
                    {
                        dialect: "POSTGRESQL"
                        expression: "SELECT SUM(net_amount) FROM public.orders"
                    }
                    {
                        dialect: "TRINO"
                        expression: "SELECT SUM(net_amount) FROM public.orders"
                    }
                ]
            }
            dataSourceId: "warehouse-pg-01"
            sourceTable: "public.orders"
            defaultTimeGrain: "MONTH"
            unit: "USD"
            returnType: "decimal"
        }
        output: {
            metric: {
                metricId: "01HZY8K3M4N5P6Q7R8S9T0V1W2"
                namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
                name: "total_revenue"
                description: "Sum of net revenue across all closed and shipped orders."
                expression: {
                    dialects: [
                        {
                            dialect: "POSTGRESQL"
                            expression: "SELECT SUM(net_amount) FROM public.orders"
                        }
                        {
                            dialect: "TRINO"
                            expression: "SELECT SUM(net_amount) FROM public.orders"
                        }
                    ]
                }
                dataSourceId: "warehouse-pg-01"
                sourceTable: "public.orders"
                defaultTimeGrain: "MONTH"
                unit: "USD"
                returnType: "decimal"
                definedBy: "user-01HZY8K3M4N5P6Q7R8S9T0V1W2"
                effectiveFrom: "2026-07-23"
            }
            warnings: []
        }
    }
])

apply DeleteMetric @examples([
    {
        title: "Delete a metric"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", name: "total_revenue" }
        output: {}
    }
])

apply ValidateMetric @examples([
    {
        title: "Validate a metric definition"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            name: "avg_order_value"
            description: "Average net revenue per order."
            expression: {
                dialects: [
                    {
                        dialect: "POSTGRESQL"
                        expression: "SELECT AVG(net_amount) FROM public.orders"
                    }
                ]
            }
            dataSourceId: "warehouse-pg-01"
            sourceTable: "public.orders"
            defaultTimeGrain: "DAY"
            unit: "USD"
            returnType: "decimal"
        }
        output: {
            warnings: [
                {
                    field: "unit"
                    message: "Unit 'USD' is not recognized by the currency registry; using as free-text."
                    severity: "INFO"
                }
            ]
        }
    }
])

apply BulkDeleteMetrics @examples([
    {
        title: "Delete metrics by name"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            names: ["total_revenue", "avg_order_value", "legacy_metric"]
        }
        output: {
            deleted: 2
            notFound: ["legacy_metric"]
        }
    }
])

apply ImportOsi @examples([
    {
        title: "Import metrics from inline OSI YAML"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", content: "osi_spec_version: '1.0'\nmetrics:\n- name: total_revenue\n  description: Total revenue from closed orders\n  expression:\n    dialects:\n    - dialect: ANSI_SQL\n      expression: SELECT SUM(net_amount) FROM public.orders\n  custom_extensions:\n  - vendor_name: COA\n    data:\n      data_source_id: warehouse-pg-01\n      source_table: public.orders\n" }
        output: { datasetsResolved: 0, metricsCreated: 0, metricsUpdated: 0, jobId: "01HZY8K3M4N5P6Q7R8S9T0V1W2", status: "IN_PROGRESS" }
    }
])

apply GetImportJob @examples([
    {
        title: "Poll an in-progress import job"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", jobId: "01HZY8K3M4N5P6Q7R8S9T0V1W2" }
        output: {
            jobId: "01HZY8K3M4N5P6Q7R8S9T0V1W2"
            status: "IN_PROGRESS"
            metricsTotal: 500
            metricsProcessed: 320
            metricsCreated: 300
            metricsUpdated: 20
            errors: []
            warnings: ["Metric 'legacy_gmv' skipped: no matching data source."]
        }
    }
])

apply GetOsiUploadUrl @examples([
    {
        title: "Request a pre-signed upload URL"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", filename: "metrics-export.yaml" }
        output: { uploadUrl: "https://coa-osi-uploads.s3.amazonaws.com/sales-analytics/01HZY8K3M4N5P6Q7R8S9T0V1W2.yaml?X-Amz-Signature=abc123", s3Key: "sales-analytics/01HZY8K3M4N5P6Q7R8S9T0V1W2.yaml", expiresIn: 900 }
    }
])

apply ExportOsi @examples([
    {
        title: "Export selected metrics inline"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            format: "inline"
            names: ["total_revenue"]
        }
        output: { content: "osi_spec_version: '1.0'\ndatasets:\n- name: orders\n  source: public.orders\n  data_source_id: warehouse-pg-01\nmetrics:\n- name: total_revenue\n  description: Total revenue from closed orders\n  expression:\n    dialects:\n    - dialect: ANSI_SQL\n      expression: SELECT SUM(net_amount) FROM public.orders\n  custom_extensions:\n  - vendor_name: COA\n    data:\n      data_source_id: warehouse-pg-01\n      source_table: public.orders\n" }
    }
])
