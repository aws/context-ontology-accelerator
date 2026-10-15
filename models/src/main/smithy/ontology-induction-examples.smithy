// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// @examples for com.amazon.semanticcontext.ontologyinduction operations.
// Extracted from the service definition to keep operation/shape
// definitions readable. `apply` merges the @examples trait back onto
// each operation, so the doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.ontologyinduction

apply StartInduction @examples([
    {
        title: "Induce an ontology from two sales data sources"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            datasourceIds: ["orders_db", "customers_db"]
            ontologyUriPrefix: "https://ontology.example.com/sales/"
            label: "Sales catalog induction"
            confidenceThreshold: 0.75
            strategy: "table_to_ontology"
            groundingOntologyIds: ["schema-org-core"]
            groundingMode: "ENHANCED"
        }
        output: { jobId: "3f8a1c2d-9b4e-4a6f-8c1d-2e5f7a9b0c3d" }
    }
])

apply GetInductionJob @examples([
    {
        title: "Get a completed induction job with its report"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", jobId: "3f8a1c2d-9b4e-4a6f-8c1d-2e5f7a9b0c3d" }
        output: {
            jobId: "3f8a1c2d-9b4e-4a6f-8c1d-2e5f7a9b0c3d"
            status: "completed"
            createdAt: "2026-07-23T14:30:00Z"
            report: {
                tablesProcessed: 12
                columnsProcessed: 84
                novelClassesCreated: 7
                matches: [
                    {
                        sourceTable: "orders"
                        sourceColumn: "customer_id"
                        matchedClassUri: "https://schema.org/Customer"
                        matchedOntologyId: "schema-org-core"
                        similarity: 0.92
                        matchType: "high_confidence"
                    }
                ]
                groundingOntologiesUsed: ["schema-org-core"]
                inducedOntologyId: "ont-sales-2026"
            }
        }
    }
])

apply ListInductionJobs @examples([
    {
        title: "List completed induction jobs"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", status: "completed", maxResults: 20 }
        output: {
            jobs: [
                {
                    jobId: "3f8a1c2d-9b4e-4a6f-8c1d-2e5f7a9b0c3d"
                    status: "completed"
                    createdAt: "2026-07-23T14:30:00Z"
                    sourceType: "STRUCTURED"
                    tablesProcessed: 12
                    novelClassesCreated: 7
                }
            ]
            nextToken: "eyJvZmZzZXQiOiAyMH0="
        }
    }
])

apply ListInducedDatasources @examples([
    {
        title: "List inducted data sources"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" }
        output: {
            datasources: [
                {
                    datasourceId: "orders_db"
                    proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
                    ontologyId: "ont-sales-2026"
                    label: "Sales catalog induction"
                    tablesProcessed: 12
                    columnsProcessed: 84
                    novelClassesCreated: 7
                }
            ]
        }
    }
])

apply ListProposals @examples([
    {
        title: "List pending proposals"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", status: "pending", maxResults: 20 }
        output: {
            proposals: [
                {
                    proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
                    status: "pending"
                    ontologyId: "ont-sales-2026"
                    sourceType: "STRUCTURED"
                    metadata: {
                        datasourceIds: ["orders_db", "customers_db"]
                        label: "Sales catalog induction"
                        tablesProcessed: 12
                        columnsProcessed: 84
                        novelClassesCreated: 7
                    }
                }
            ]
            nextToken: "eyJvZmZzZXQiOiAyMH0="
        }
    }
])

apply GetProposal @examples([
    {
        title: "Get a pending proposal with inlined Turtle"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e" }
        output: {
            proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
            status: "pending"
            ontologyId: "ont-sales-2026"
            ontologyTurtle: "@prefix : <https://ontology.example.com/sales/> .\n:Order a owl:Class ."
            r2rmlTurtle: "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n:OrderMap a rr:TriplesMap ."
            metadata: {
                datasourceIds: ["orders_db", "customers_db"]
                label: "Sales catalog induction"
                tablesProcessed: 12
                columnsProcessed: 84
                novelClassesCreated: 7
            }
        }
    }
])

apply AcceptProposal @examples([
    {
        title: "Accept a proposal into its target ontology"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", ontologyId: "ont-sales-2026" }
        output: { proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", status: "accepting" }
    }
])

apply RejectProposals @examples([
    {
        title: "Reject two proposals with a reason"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            proposalIds: ["7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"]
            reason: "Duplicate of the accepted sales ontology"
        }
        output: { rejected: 2 }
    }
])

apply CancelProposal @examples([
    {
        title: "Cancel a pending proposal"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e" }
        output: { proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", status: "cancelled" }
    }
])

apply UpdateProposal @examples([
    {
        title: "Revise a proposal's OWL Turtle inline"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", ontologyTurtle: "@prefix : <https://ontology.example.com/sales/> .\n:Order a owl:Class ;\n    rdfs:label \"Order\" .", ontologyUploaded: false, r2rmlUploaded: false }
        output: { proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", status: "updated" }
    }
])

apply RepairProposalDatatypes @examples([
    {
        title: "Repair malformed datatype tokens in a proposal"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e" }
        output: {
            proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
            repairedCount: 1
            changes: [
                {
                    found: "xsd:integar"
                    canonical: "http://www.w3.org/2001/XMLSchema#integer"
                }
            ]
            dryRun: false
        }
    }
    {
        title: "Preview datatype repairs without persisting (dry run)"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", dryRun: true }
        output: {
            proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
            repairedCount: 1
            changes: [
                {
                    found: "xsd:integar"
                    canonical: "http://www.w3.org/2001/XMLSchema#integer"
                }
            ]
            dryRun: true
        }
    }
])

apply GetProposalUploadUrls @examples([
    {
        title: "Get presigned PUT URLs for a large proposal revision"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e" }
        output: { revisionId: "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8", ontologyPutUrl: "https://coa-proposals.s3.amazonaws.com/ont-sales-2026/ontology.ttl?X-Amz-Signature=abc123", r2rmlPutUrl: "https://coa-proposals.s3.amazonaws.com/ont-sales-2026/mappings.ttl?X-Amz-Signature=def456" }
    }
])

apply InferConstraints @examples([
    {
        title: "Start constraint inference for a proposal"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e" }
        output: { jobId: "b4d7e1a2-6c3f-4e9b-8a2d-5f0c7b1e3d9a", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", status: "pending" }
    }
])

apply GetInferConstraintsJob @examples([
    {
        title: "Poll a completed constraint-inference job"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", jobId: "b4d7e1a2-6c3f-4e9b-8a2d-5f0c7b1e3d9a" }
        output: {
            jobId: "b4d7e1a2-6c3f-4e9b-8a2d-5f0c7b1e3d9a"
            proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
            status: "completed"
            inferred_constraints: {
                classes: [
                    {
                        uri: "https://ontology.example.com/sales/Order"
                        properties: [
                            {
                                path: "orderTotal"
                                minCount: 1
                                datatype: "decimal"
                            }
                        ]
                    }
                ]
            }
        }
    }
])

apply CompileConstraints @examples([
    {
        title: "Compile a reviewed constraint config into SHACL"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
            constraint_config: {
                classes: [
                    {
                        uri: "https://ontology.example.com/sales/Order"
                        properties: [
                            {
                                path: "orderTotal"
                                minCount: 1
                                datatype: "decimal"
                            }
                        ]
                    }
                ]
            }
        }
        output: { proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", shapes_turtle: "@prefix sh: <http://www.w3.org/ns/shacl#> .\n:OrderShape a sh:NodeShape ;\n    sh:targetClass :Order ." }
    }
])

apply ValidateProposal @examples([
    {
        title: "Validate a proposal across all tiers"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
            tiers: ["tier1_blocking", "tier2_scoring", "tier3_review"]
            competencyQuestions: ["Which customers placed orders in Q3?"]
        }
        output: { jobId: "c5e8f2b3-7d4a-4f0c-9b3e-6a1d8c2f4e0b", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", status: "pending", createdAt: "2026-07-23T14:30:00Z" }
    }
])

apply GetProposalValidationJob @examples([
    {
        title: "Get a completed validation job with its report"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e", jobId: "c5e8f2b3-7d4a-4f0c-9b3e-6a1d8c2f4e0b" }
        output: {
            jobId: "c5e8f2b3-7d4a-4f0c-9b3e-6a1d8c2f4e0b"
            proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
            status: "completed"
            createdAt: "2026-07-23T14:30:00Z"
            report: {
                jobId: "c5e8f2b3-7d4a-4f0c-9b3e-6a1d8c2f4e0b"
                proposalId: "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
                passed: false
                findings: [
                    {
                        validator: "hermit"
                        tier: "tier1_blocking"
                        severity: "warning"
                        code: "UNSAT_CLASS_RISK"
                        message: "Class Order has a disjointness that may cause unsatisfiability"
                        subject: "https://ontology.example.com/sales/Order"
                    }
                ]
                tiersRun: ["tier1_blocking", "tier2_scoring", "tier3_review"]
                createdAt: "2026-07-23T14:30:00Z"
            }
        }
    }
])

apply ListFoundationalOntologies @examples([
    {
        title: "List available foundational ontologies"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" }
        output: {
            items: [
                {
                    key: "schema-org-core"
                    title: "Schema.org Core"
                    description: "Common vocabulary for structured web data"
                    uri: "https://schema.org/"
                    domainTags: ["general", "ecommerce"]
                }
            ]
        }
    }
])

apply LoadFoundationalOntology @examples([
    {
        title: "Load a foundational ontology into a namespace"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", key: "schema-org-core" }
        output: { status: "loading", ontologyId: "schema-org-core", classCount: 843, embeddingCount: 843 }
    }
])
