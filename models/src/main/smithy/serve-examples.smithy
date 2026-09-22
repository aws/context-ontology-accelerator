// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// @examples for com.amazon.semanticcontext.datalayer operations.
// Extracted from the service definition to keep operation/shape
// definitions readable. `apply` merges the @examples trait back onto
// each operation, so the doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.datalayer

apply Query @examples([
    {
        title: "Ask a natural-language question"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", query: "What was total revenue in Q3 2025?", execute: true }
        output: {
            result: {
                tier: 1
                confidence: { score: 0.94, rationale: "Exact metric match on 'total revenue'" }
                resultRows: [
                    {
                        period: "Q3 2025"
                        revenue: "12450000"
                    }
                ]
                synthesizedAnswer: "Total revenue in Q3 2025 was $12.45M."
                trace: [
                    {
                        step: "metric-match"
                        status: "ok"
                        durationMs: 42
                    }
                ]
            }
            requestId: "req-01HZY8K3M4N5P6Q7R8S9T0V1W2"
        }
    }
])

apply TranslateSPARQL @examples([
    {
        title: "Preview the SPARQL for a question"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", query: "List all customers in the enterprise segment" }
        output: {
            sparqlQuery: "SELECT ?customer WHERE { ?customer a :Customer ; :segment \"enterprise\" }"
            confidence: { score: 0.88, rationale: "Class and property matched from ontology" }
            trace: [
                {
                    step: "nl-to-sparql"
                    status: "ok"
                    durationMs: 310
                }
            ]
            ontologyVersion: "2025-09-01"
        }
    }
])

apply KBSearch @examples([
    {
        title: "Search the knowledge base"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", query: "refund policy for enterprise contracts", topK: 3, minScore: 0.5 }
        output: {
            chunks: [
                {
                    chunkId: "doc-42#p7"
                    text: "Enterprise contracts allow refunds within 30 days of invoice."
                    sourceDocumentId: "src-6f8b4f12"
                    sourceDocumentName: "Enterprise Terms.pdf"
                    relevanceScore: 0.82
                }
            ]
            trace: [
                {
                    step: "kb-search"
                    status: "ok"
                    durationMs: 128
                }
            ]
            queryEmbeddingModel: "amazon.titan-embed-text-v2"
        }
    }
])

apply GraphTraverse @examples([
    {
        title: "Traverse from an entity"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", startUri: "urn:coa:customer:acme-corp", maxDepth: 2, direction: "outgoing" }
        output: {
            entities: [
                {
                    uri: "urn:coa:customer:acme-corp"
                    label: "Acme Corp"
                    type: "Customer"
                }
                {
                    uri: "urn:coa:order:o-1001"
                    label: "Order 1001"
                    type: "Order"
                }
            ]
            relationships: [
                {
                    sourceUri: "urn:coa:customer:acme-corp"
                    predicateUri: "urn:coa:rel:placed"
                    targetUri: "urn:coa:order:o-1001"
                    predicateLabel: "placed"
                }
            ]
            trace: [
                {
                    step: "graph-traverse"
                    status: "ok"
                    durationMs: 66
                }
            ]
        }
    }
])

apply DescribeSchema @examples([
    {
        title: "Describe the queryable schema"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", includeProperties: true }
        output: {
            classes: [
                {
                    uri: "urn:coa:class:Customer"
                    label: "Customer"
                    description: "A purchasing organization or individual"
                    properties: [
                        {
                            uri: "urn:coa:prop:segment"
                            label: "segment"
                            range: "xsd:string"
                        }
                    ]
                }
            ]
            ontologyVersion: "2025-09-01"
        }
    }
])
