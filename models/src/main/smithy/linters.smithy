// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Documentation-coverage and input-constraint gates for the public API.
//
// EmitEachSelector emits a DANGER event for every operation matched by the
// selector; unsuppressed DANGER events make the model invalid and fail the
// Smithy build (`gradlew build` / `make generate`). This enforces that every
// public operation is documented and carries examples — satisfying ORR DOC-A-3
// at generation time, locally and in CI, with no custom script.
//
// `///` doc comments compile to the `smithy.api#documentation` trait, so the
// documentation selector matches operations that have no leading `///` comment.
//
// The structure gate covers hand-authored domain shapes (including error
// shapes) but deliberately excludes the input/output structures Smithy
// synthesizes from inline `input :=` / `output :=` blocks — those carry the
// `@input` / `@output` traits and are machine-generated, so requiring prose on
// them would add noise, not documentation.
//
// The member gate requires a description for every structure member that would
// otherwise render blank in the API reference — i.e. a member with no `///` of
// its own whose *target shape* is also undocumented. Members that target a
// documented type (e.g. `namespaceId: Uuid`, where `Uuid` carries a `///`)
// inherit that description in the generated OpenAPI and are correctly exempt.
//
// The enum gates require a description on every enum shape AND every enum value
// (member). Both are scoped to the `com.amazon.semanticcontext` namespace so
// that transitively-referenced AWS enums (aws.apigateway#*, aws.protocols#*),
// which we do not own and cannot annotate, are not gated.
//
// The input-constraint gates require every input-reachable value to carry a
// bound, so no free-form string, number, list or map reaches a handler
// unconstrained. "Input-reachable" means a member of an operation's input
// structure, or any member of a structure, list, map or union reachable from
// one (including list `member` and map `key`/`value`). A bound on either the
// member or its target shape satisfies the gate. Enums (including legacy
// @enum strings) are already closed sets and are exempt; @pattern alone does
// not bound length, so strings need @length. Blobs need @length too. Numbers
// need BOTH `min` and `max`: a one-sided @range still admits arbitrarily large
// (or small) values. A member-level @range replaces the target's, so a
// one-sided member @range fails even when its target is fully bounded.
//
// Documented exceptions are listed by shape id in the selector itself, each
// with its reason here, so adding one is a reviewed model change:
//   - KBSearchInput$minScore (numeric, min-only): the relevance-score scale
//     depends on the index's similarity metric, so no fixed max is correct.
//
// The four input-constraint selectors (string, number, collection, blob) apply
// to every operation; any future operation exclusion must be added to all four.
//
// The document gate flags input-reachable `document` members. A document is
// free-form JSON that no model constraint can bound, so each one must be
// explicitly allowlisted by tagging the member `@tags(["bounded-at-runtime"])`
// once its handler enforces a size cap; its doc comment should say so.
//
// The authentication gate forbids opting out of the service's bearer-token
// authorizer: no operation or service may carry @optionalAuth or an empty
// @auth([]), except the HealthCheck liveness probe (GET /health). Either trait
// makes the OpenAPI conversion emit `security: []`, which the API deploy step
// preserves, so the route would be served without the authorizer.
$version: "2"

metadata validators = [
    {
        name: "EmitEachSelector"
        id: "OperationMissingDocumentation"
        message: "Every public operation must have documentation (a /// doc comment)."
        configuration: { selector: "operation :not([trait|documentation])" }
    }
    {
        name: "EmitEachSelector"
        id: "OperationMissingExamples"
        message: "Every public operation must have an @examples trait."
        configuration: { selector: "operation :not([trait|examples])" }
    }
    {
        name: "EmitEachSelector"
        id: "StructureMissingDocumentation"
        message: "Every domain structure must have documentation (a /// doc comment). Synthesized @input/@output wrappers are exempt."
        configuration: { selector: "structure :not([trait|documentation]) :not([trait|input]) :not([trait|output])" }
    }
    {
        name: "EmitEachSelector"
        id: "MemberMissingDocumentation"
        message: "Every structure member must have documentation (a /// doc comment) unless its target type is itself documented. Otherwise it renders blank in the API reference."
        configuration: {
            selector: "structure > member :not([trait|documentation]) :not(:test(> [trait|documentation]))"
        }
    }
    {
        name: "EmitEachSelector"
        id: "EnumMissingDocumentation"
        message: "Every enum must have documentation (a /// doc comment)."
        configuration: { selector: "[id|namespace^=com.amazon.semanticcontext] :is(enum) :not([trait|documentation])" }
    }
    {
        name: "EmitEachSelector"
        id: "EnumValueMissingDocumentation"
        message: "Every enum value must have documentation (a /// doc comment) so it is described in the API reference."
        configuration: {
            selector: "[id|namespace^=com.amazon.semanticcontext] :is(enum) > member :not([trait|documentation])"
        }
    }
    {
        name: "EmitEachSelector"
        id: "InputStringMissingLength"
        message: "Every input-reachable string member must have @length on the member or its target (enums excepted; @pattern alone is not enough)."
        configuration: {
            selector: "operation[id|namespace^=com.amazon.semanticcontext] -[input]-> structure ~> member :test(> string :not(enum) :not([trait|enum]) :not([trait|length])) :not([trait|length])"
        }
    }
    {
        name: "EmitEachSelector"
        id: "InputNumberMissingRange"
        message: "Every input-reachable numeric member must have @range with both min and max on the member or its target (a member-level @range replaces the target's)."
        configuration: {
            selector: "operation[id|namespace^=com.amazon.semanticcontext] -[input]-> structure ~> member :not([id='com.amazon.semanticcontext.datalayer#KBSearchInput$minScore']) :test(> number :not(intEnum)) :not([trait|range|min] [trait|range|max]) :not(:not([trait|range]) :test(> [trait|range|min] [trait|range|max]))"
        }
    }
    {
        name: "EmitEachSelector"
        id: "InputCollectionMissingLength"
        message: "Every input-reachable list or map member must have @length on the member or its target."
        configuration: {
            selector: "operation[id|namespace^=com.amazon.semanticcontext] -[input]-> structure ~> member :test(> :is(list, map) :not([trait|length])) :not([trait|length])"
        }
    }
    {
        name: "EmitEachSelector"
        id: "InputBlobMissingLength"
        message: "Every input-reachable blob member must have @length on the member or its target."
        configuration: {
            selector: "operation[id|namespace^=com.amazon.semanticcontext] -[input]-> structure ~> member :test(> blob :not([trait|length])) :not([trait|length])"
        }
    }
    {
        name: "EmitEachSelector"
        id: "InputDocumentNotAllowlisted"
        message: "Input-reachable document members are free-form JSON that no model constraint bounds. Tag the member @tags([\"bounded-at-runtime\"]) only once its handler enforces a size cap, and say so in its doc comment."
        configuration: {
            selector: "operation[id|namespace^=com.amazon.semanticcontext] -[input]-> structure ~> member :test(> document) :not([trait|tags|(values)='bounded-at-runtime'])"
        }
    }
    {
        name: "EmitEachSelector"
        id: "UnauthenticatedOperation"
        message: "Only the HealthCheck liveness probe may opt out of authentication; remove @optionalAuth / @auth([]) from this shape."
        configuration: {
            selector: "[id|namespace^=com.amazon.semanticcontext] :is(operation, service) :not([id=com.amazon.semanticcontext#HealthCheck]) :is([trait|optionalAuth], [trait|auth|(length)=0])"
        }
    }
]
