// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Data Layer service — runtime data operations (query, retrieval, graph traversal).
$version: "2"

namespace com.amazon.semanticcontext.datalayer

use aws.apigateway#authorizer
use aws.apigateway#authorizers
use aws.protocols#restJson1
use com.amazon.semanticcontext.common#AccessDeniedError
use com.amazon.semanticcontext.common#InternalServerError
use com.amazon.semanticcontext.common#ServiceUnavailableError
use com.amazon.semanticcontext.common#ValidationError

// Every data-plane operation is authenticated. These routes are served by the
// same REST API as ControlPlaneService and sit behind the same TokenAuthorizer
// (api-stack merges this spec's paths into the control-plane spec), so the
// declaration below mirrors control-plane.smithy rather than introducing a
// second authorizer. Keep the two in step — in particular resultTtlInSeconds
// must stay 0; see the comment there for why caching is unsafe.
@title("Context Ontology Accelerator - Data Layer (Serve)")
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
        resultTtlInSeconds: 0
    }
)
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
        InternalServerError
        ServiceUnavailableError
    ]
}
