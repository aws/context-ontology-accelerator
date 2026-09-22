// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Data Layer service — runtime data operations (query, retrieval, graph traversal).
$version: "2"

namespace com.amazon.semanticcontext.datalayer

use aws.protocols#restJson1
use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#ValidationError

@title("Context Ontology Accelerator - Data Layer (Serve)")
@restJson1
service DataLayerService {
    version: "2025-05-01"
    operations: [
        Query
        TranslateSPARQL
        KBSearch
        GraphTraverse
        DescribeSchema
    ]
    errors: [
        ValidationError
        AccessDeniedError
        RateLimitExceededError
    ]
}
