# Lexical Baseline Retriever

Tier 3 retrieval strategy that uses the graphrag-toolkit's built-in retrievers
against the lexical knowledge graph. This is the **default** Tier 3 path
(`TIER3_STRATEGY=lexical-baseline`); the hand-rolled retrieval
(VectorRetriever + GraphTraverser) is the selectable alternative.

## What this is

A retrieval adapter that queries the same Neptune property graph and OpenSearch
indexes that `kg-build` (unstructured document ingestion) writes to. The
graphrag retrieval approach is **selectable** from a named set of strategies
(see [Retriever strategies](#retriever-strategies)) rather than hardcoded; the
default, `topic_beam`, navigates Source → Chunk → Topic → Statement →
Fact → Entity structures.

## What this is NOT

- Does NOT consult the published ontology (urn:coa:{namespace}:published)
- Does NOT use OWL classes, domain/range, or any formal schema knowledge
- Does NOT change Tier 1 or Tier 2 behaviour

This is the floor against which future ontology-aware retrieval will be measured.

## How to enable

The graphrag retriever is **request-selectable on any deployment**. The lexical
retriever is built whenever the Neptune + OpenSearch endpoints are configured,
so a request can engage graphrag on-demand by passing `options.retrieverStrategy`
— regardless of `TIER3_STRATEGY`.

Whether the *default* Tier 3 path (a request with **no** `retrieverStrategy`)
uses graphrag is controlled by `TIER3_STRATEGY`:

| `TIER3_STRATEGY` | no `retrieverStrategy` | `retrieverStrategy` passed |
|---|---|---|
| `lexical-baseline` (default) | graphrag with the deployment default (`LEXICAL_RETRIEVER_STRATEGY`, `topic_beam`) | graphrag with that strategy |
| `hand-rolled` | hand-rolled (VectorRetriever + GraphTraverser) | graphrag with that strategy |

## Retriever strategies

Once `lexical-baseline` is enabled, the graphrag retrieval strategy is selectable
from a named set (single source of truth: `lexical/strategies.py`,
`STRATEGY_REGISTRY`). Each name maps to a specific toolkit engine family +
retriever configuration:

| Strategy | Engine family | Notes |
|----------|---------------|-------|
| `topic_beam` | `for_semantic_guided_search` | **Default.** Strongest single-shot on the SEC-10-Q benchmark (45.13% strict vs 34.36% hand-rolled, 195 q). |
| `chunk_based_semantic` | `for_traversal_based_search` | Fast (~0.48s p50 SEC-10Q); the safety fallback when an invalid strategy is configured. |
| `traversal` | `for_traversal_based_search` | The weighted set (ChunkBasedSearch@1.0 + EntityNetworkSearch@1.0 + TopicBasedSearch@0.5). Slowest + least accurate. |

### Selecting a strategy

The strategy is resolved once per request with precedence
**request > deployment default**, where the deployment default depends on
`TIER3_STRATEGY`:

- **Per-request override** — `options.retrieverStrategy` on the invoke request
  (alongside `tierOverride` / `dataSourceId` / `maxResults`). When present it
  engages graphrag with that strategy on **any** deployment. The value is
  validated at the model layer, so an unknown/unsupported strategy is rejected
  with a **400** (`ValidationError`) rather than silently substituting a
  different strategy. Absence of the key is valid.
- **Deployment default** — `LEXICAL_RETRIEVER_STRATEGY` environment variable,
  surfaced as `ServiceConfig.lexical_retriever_strategy` (defaults to
  `topic_beam`). It only applies when `TIER3_STRATEGY=lexical-baseline` (the
  default); there, a request with no `retrieverStrategy` uses it (an invalid
  value logs `invalid_lexical_retriever_strategy` and falls back to
  `chunk_based_semantic`). Under `TIER3_STRATEGY=hand-rolled` there is **no**
  deployment default, so a request with no `retrieverStrategy` runs the
  hand-rolled path.

The resolved strategy name is recorded in the trace / response metadata so eval
runs and request logs attribute results to the strategy that produced them.

## Store connections

Reuses the same `NEPTUNE_ENDPOINT` and `OPENSEARCH_ENDPOINT` env vars already
configured for context-manager. The URI format matches what kg-build uses and is
**backend-aware**, derived from the `NEPTUNE_ENDPOINT` shape:

- Graph store (Neptune Database / NDB): `neptune-db://{neptune_host}:8182`
- Graph store (Neptune Analytics / NA): `neptune-graph://g-{id}` — used when the
  endpoint is NA-shaped (`g-…`, `neptune-graph://…`, or an NA host)
- Vector store: `aoss://{opensearch_endpoint}`

## Tenant ID

### Namespace → tenant contract (production default)

`_get_engine` derives `tenant_id = to_graphrag_tenant_id(namespace)` (from
`coa_common.constants`) — the same function `kg-build` uses at
ingestion time — and passes it to the toolkit factory. This is the production
multi-tenancy contract: the derived `tenant_id` **must match the value kg-build
wrote**, or retrieval targets a non-existent tenant partition (suffixed
`__Chunk{tenant}__` labels / `chunk_{tenant}` index) and returns zero documents.
Engines are cached per `(effective_tenant, strategy.name)` so a per-request
strategy override does not collide with the cache.

### Default-tenant override (eval-only seam — not request-reachable)

`BaselineLexicalRetriever` accepts an optional constructor parameter
`tenant_id_override` (default `None` = derive from namespace, the production
behaviour):

- `None` → derive the tenant from the namespace (production default).
- `DEFAULT_TENANT_OVERRIDE` (the sentinel `"__graphrag_default_tenant__"`) →
  read under the graphrag-toolkit **default** tenant (`tenant_id=None` → bare
  `__Chunk__` labels / bare `chunk` index).
- any other string → read under that literal tenant.

This seam exists **only** for the eval/integ harness, which reads the
toolkit-built SEC-10Q graph (populated by the graphrag-toolkit example under the
toolkit default tenant, not via kg-build). It is constructed directly by that
harness and is deliberately **NOT** wired into `build_lexical_retriever` or the
request path — there is no way to reach it from an invoke request. The production
namespace→tenant path is validated end-to-end separately (KG-build E2E).
