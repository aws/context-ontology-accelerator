// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// @examples for com.amazon.semanticcontext.namespace operations.
// Extracted from the service definition to keep operation/shape
// definitions readable. `apply` merges the @examples trait back onto
// each operation, so the doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.namespace

apply CreateNamespace @examples([
    {
        title: "Create a sales analytics namespace"
        input: { name: "sales-analytics", displayName: "Sales Analytics", description: "Q3 revenue data and dashboards", owner: "data-team@example.com", language: "ja" }
        output: {
            namespace: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", name: "sales-analytics", displayName: "Sales Analytics", description: "Q3 revenue data and dashboards", owner: "data-team@example.com", status: "ACTIVE", language: "ja", createdAt: "2026-07-23T14:30:00Z" }
        }
    }
])

apply GetNamespace @examples([
    {
        title: "Get a namespace by ID"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" }
        output: {
            namespace: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", name: "sales-analytics", displayName: "Sales Analytics", description: "Q3 revenue data and dashboards", owner: "data-team@example.com", status: "ACTIVE", language: "en", sourceCount: 3, createdAt: "2026-07-23T14:30:00Z", updatedAt: "2026-07-23T15:00:00Z" }
        }
    }
])

apply ListNamespaces @examples([
    {
        title: "List namespaces (first page)"
        input: { maxResults: 20 }
        output: {
            namespaces: [
                {
                    namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
                    name: "sales-analytics"
                    displayName: "Sales Analytics"
                    status: "ACTIVE"
                    language: "en"
                    sourceCount: 3
                    createdAt: "2026-07-23T14:30:00Z"
                    updatedAt: "2026-07-23T15:00:00Z"
                }
            ]
            nextToken: "eyJvZmZzZXQiOiAyMH0="
        }
    }
])

apply UpdateNamespace @examples([
    {
        title: "Update a namespace's display name and description"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", displayName: "Sales Analytics (2026)", description: "Full-year revenue data and dashboards" }
        output: {
            namespace: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", name: "sales-analytics", displayName: "Sales Analytics (2026)", description: "Full-year revenue data and dashboards", owner: "data-team@example.com", status: "ACTIVE", language: "en", createdAt: "2026-07-23T14:30:00Z", updatedAt: "2026-07-23T16:45:00Z" }
        }
    }
])

apply DeleteNamespace @examples([
    {
        title: "Delete a namespace (deletion accepted)"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" }
        output: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", status: "DELETING" }
    }
])

apply UpdateNamespaceStatus @examples([
    {
        title: "Archive a namespace"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", status: "ARCHIVED" }
        output: {
            namespace: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", name: "sales-analytics", displayName: "Sales Analytics", owner: "data-team@example.com", status: "ARCHIVED", language: "en", createdAt: "2026-07-23T14:30:00Z", updatedAt: "2026-07-23T17:10:00Z" }
        }
    }
])
