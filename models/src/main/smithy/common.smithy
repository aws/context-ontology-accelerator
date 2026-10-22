// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Common Smithy shapes for Context Ontology Accelerator
$version: "2"

namespace com.amazon.semanticcontext.common

/// Standard error structure
@error("client")
structure ValidationError {
    /// Human-readable description of the validation failure.
    @required
    message: String
}

/// Standard pagination input
@mixin
structure PaginatedInput {
    /// Opaque token from a prior response; pass it to fetch the next page.
    nextToken: PaginationToken

    /// Maximum number of items to return in a single page. Each operation
    /// narrows this to its own server page-size cap.
    @range(min: 1, max: 1000)
    maxResults: Integer
}

/// Standard pagination output
@mixin
structure PaginatedOutput {
    /// Opaque token for fetching the next page; absent when there are no more results.
    nextToken: PaginationToken
}

// ── Standard error responses ─────────────────────────────────────────
/// Returned with HTTP 404 when the requested resource does not exist.
@error("client")
@httpError(404)
structure NotFoundError {
    /// Human-readable description of what was not found.
    @required
    message: String
}

/// Returned with HTTP 409 when a request conflicts with the current state of the resource, such as a duplicate.
@error("client")
@httpError(409)
structure ConflictError {
    /// Human-readable description of the conflict.
    @required
    message: String
}

/// Returned with HTTP 403 when the caller lacks permission to perform the requested operation.
@error("client")
@httpError(403)
structure AccessDeniedError {
    /// Human-readable description of the authorization failure.
    @required
    message: String
}

/// Returned with HTTP 500 when the service fails unexpectedly. The message is a
/// fixed, generic description; the underlying cause is logged, never returned.
@error("server")
@httpError(500)
structure InternalServerError {
    /// Generic description of the failure; carries no internal detail.
    @required
    message: String
}

/// Returned with HTTP 503 when the service, or a required dependency (including
/// one it needs to handle the request safely), is temporarily unavailable. For
/// example, a create that
/// carries a `clientToken` fails closed with this error when the retry-safety
/// store cannot be reached, rather than risk running the create twice. Retry
/// the request (with the same `clientToken`) after a backoff.
@error("server")
@httpError(503)
@retryable
structure ServiceUnavailableError {
    /// Description of the unavailable dependency or operation; carries no
    /// internal detail.
    @required
    message: String
}

/// UUID v4 identifier.
@pattern("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
@length(min: 36, max: 36)
string Uuid

/// ULID identifier (26-char Crockford base32, lexicographically sortable by creation time).
/// Used for resources where time-ordered listing is beneficial (e.g., docSourceId).
/// Benefits: sortable by creation timestamp, more compact than UUID (26 vs 36 chars).
/// Alphabet excludes I, L, O, U to avoid visual ambiguity (Crockford base32 spec).
/// See: https://github.com/ulid/spec
@pattern("^[0-9A-HJKMNP-TV-Z]{26}$")
@length(min: 26, max: 26)
string Ulid

// ── Reusable constrained primitives ─────────────────────────────────
/// Human-readable display name. Allows spaces and punctuation. 1–128 chars.
@length(min: 1, max: 128)
string DisplayName

/// Relative S3 key prefix — no leading slash, no ".." traversal, no s3:// URI.
/// Examples: "documents/", "data/reports/2024/"
@pattern("^(?!/)(?!.*\\.\\.)[A-Za-z0-9_./-]+$")
@length(min: 1, max: 1024)
string S3Prefix

/// S3 bucket ARN. Format: arn:aws:s3:::{bucket-name}
@pattern("^arn:aws:s3:::[a-z0-9][a-z0-9.\\-]{1,61}[a-z0-9]$")
@length(min: 20, max: 128)
string S3BucketArn

/// IAM role ARN for cross-account access.
@pattern("^arn:(aws|aws-us-gov):iam::\\d{12}:role(/|/[\\u0021-\\u007F]+/)[\\w+=,.@\\-]+$")
@length(min: 20, max: 2048)
string IamRoleArn

/// Opaque pagination cursor returned by a list operation as `nextToken`. Pass
/// it back unchanged to fetch the next page. Sized for the largest cursor any
/// list operation passes through from an upstream catalog search (8,192
/// characters); cursors minted from an encoded table key are far shorter.
@length(min: 1, max: 8192)
string PaginationToken

/// Caller-chosen token that makes a create request safe to retry. Printable
/// ASCII, 1-64 characters; a UUID is a good choice.
@length(min: 1, max: 64)
@pattern("^[\\x21-\\x7E]+$")
string ClientToken

/// Ontology identifier: an IRI or URN, e.g. `https://schema.org/` or
/// `urn:ontology:upload-1a2b3c4d`.
@length(min: 1, max: 2048)
string OntologyId

/// Presigned S3 URL. Anyone holding it can read or write the object until it
/// expires, so it is a bearer credential: generated clients keep it out of logs.
@sensitive
string PresignedUrl
