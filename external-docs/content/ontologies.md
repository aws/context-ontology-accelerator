# Ontologies

Ontologies are formal knowledge models that describe the concepts, relationships, and rules in your data domain. Context Ontology Accelerator uses ontologies to understand your data semantics — enabling accurate natural language to SQL translation and knowledge graph traversal.

!!! tip "Full request/response schemas"
    For the complete request/response schema for every ontology and induction
    endpoint, see the **[API Reference](#/api-reference)** (Control Plane API
    → Ontology graph + catalog, Ontology induction) — it's generated directly
    from the API contract and always current.

## What is an Ontology?

An ontology defines:

- **Classes**: concepts in your domain (e.g. `Customer`, `Order`, `Product`)
- **Properties**: attributes of classes (e.g. `Customer.email`, `Order.total_amount`)
- **Relationships**: how classes connect (e.g. `Customer places Order`, `Order contains Product`)
- **Constraints**: rules and cardinality (e.g. "every Order must have exactly one Customer")

Context Ontology Accelerator stores ontologies in OWL/Turtle format and materializes them in the Neptune knowledge graph.

## Creating Ontologies

### Automatic Induction

Context Ontology Accelerator can induce an ontology from your approved data sources:

1. Navigate to your namespace → **Ontology** → **Induction**
2. Click **Start induction**
3. Select the data sources to analyze
4. Context Ontology Accelerator examines table structures, column relationships, and metadata to generate an ontology
5. Review and refine the generated model

Induction uses Bedrock (Claude) to analyze table schemas and infer semantic relationships between entities.

#### Grounding

During induction, Context Ontology Accelerator can **ground** new classes against existing ontologies — aligning them to concepts in a loaded foundational ontology (e.g. FIBO) or to your namespace's previously accepted induced ontologies. Grounding emits `rdfs:subClassOf` / `skos:*Match` links so related concepts converge instead of proliferating as duplicates.

The **`groundingMode`** parameter controls how aggressively this runs:

| Mode | Behavior |
|------|----------|
| `NONE` | Grounding is disabled — every class is treated as novel. |
| `STANDARD` | Auto-grounds exact-name matches only; no LLM involved. Fast, conservative. |
| `ENHANCED` *(default)* | Always defers to the LLM reranker to verify domain alignment before grounding — catches semantic matches that names miss, and rejects same-name-different-meaning false matches (e.g. Dublin Core "Policy" ≠ insurance "policy"). |

`ENHANCED` is the default and the right choice for most cases. Use `STANDARD` when you want speed and only trust exact-name alignment, or `NONE` to keep an induced ontology fully independent.

Grounding targets come from `groundingOntologyIds` (foundational/uploaded ontologies you explicitly select) plus the namespace's accepted induced ontologies, which are always grounded against.

In `ENHANCED` mode the LLM reranker returns a small JSON verdict per class or table. Reasoning models spend part of their output budget on internal thinking before that JSON, so on a very long or complex source the answer can be truncated. When that happens the induction job **fails with a clear error** (rather than silently marking everything novel) and the error tells you to raise **`rerankMaxTokens`**. This optional `StartInduction` parameter caps the reranker's output-token budget; it defaults to `1000` (ample for the short verdict on all current models) and accepts `1`–`8192`. You only need to set it in the rare case that error names it — raise it (e.g. to `2000`) and re-run. It has no effect in `STANDARD` or `NONE` mode, which don't call the LLM.

### Manual Upload

Upload an existing ontology file:

1. Navigate to your namespace → **Ontology** → **Induction**, then use the ontology upload on that page
2. Select your file (formats: Turtle `.ttl`, RDF/XML `.xml`, OWL/XML, JSON-LD)
3. The ontology is ingested into the knowledge graph

### Via the API

Ontologies are managed under `/namespaces/{namespaceId}/ontologies` —
creating an entry, uploading a Turtle/RDF file, and triggering induction (with
optional `groundingOntologyIds` / `groundingMode`, defaulting to `ENHANCED`)
are each separate calls. See **CreateOntology**, **UploadOntology**, and
**StartInduction** in the [API Reference](#/api-reference) for the full
request/response schemas.

## Ontology Lifecycle

| Action | Description |
|--------|-------------|
| **Create** | Register an ontology entry (metadata only) |
| **Induce** | Auto-generate from data source schemas |
| **Upload** | Import from OWL/Turtle/RDF file |
| **Fetch** | Pull from an external URL |
| **Update** | Modify metadata or re-upload |
| **Validate** | Run consistency + quality checks on a proposal before accepting, including datatype checks (see "Validating and repairing datatype issues" below) |
| **Delete** | Remove an ontology's graph triples, vector embeddings, and registry entry from the namespace (see "Deleting ontologies" below) |

## Induction Job Status and Troubleshooting

Induction runs **asynchronously**. When you trigger it (via **Start induction** or the **StartInduction** operation), Context Ontology Accelerator returns a job that moves through several in-progress stages before reaching one of two terminal states:

| Status | Meaning |
|--------|---------|
| `pending` | Queued, not yet started. |
| *in-progress* (`fetching_metadata`, `matching`, `building_ontology`, `storing`, …) | The job is running through its stages. |
| `completed` | Success — a proposal is ready to review and accept. |
| `failed` | No proposal was produced; check the job's `error` field. |

### Why a job fails

- **Grounding can't reach OpenSearch (the vector search backend).** The most common cause: the grounding step hits a transport error against OpenSearch — throttling (`429`), a `5xx`, or a connection/timeout — that persists after automatic retries. Transient OpenSearch errors are retried automatically (6 attempts with increasing backoff), so most blips never surface as a failure.
- **Data or configuration problems** — for example, no tables were found in the selected data sources.

### Fail-loud reliability guarantee

A failed job means the ontology was **not** silently degraded. If grounding cannot complete, the system refuses to hand back an ontology with missing grounding — which would look like a legitimately all-novel schema, with no signal that anything went wrong — and fails the job instead. A `failed` status is a clear, honest signal, not a misleading result.

### Checking status and re-running

Poll the job returned by **StartInduction** to see its current status; on failure, the `error` field names the failure type, so you can tell a transient outage from a configuration problem. See **StartInduction** in the [API Reference](#/api-reference) for the request/response schema.

- **Failed on a transient OpenSearch error?** Just re-run the induction — the error is often gone on retry.
- **Failed on a data or configuration problem?** Fix the source selection or configuration first (e.g. make sure the selected sources actually contain tables), then re-run.

## Deleting ontologies

Deleting an ontology removes its named graph triples from Neptune, its vector
embeddings from the search index, and its registry entry. The delete runs
asynchronously: the ontology stays listed as **Delete in progress** until the
graph and embeddings are actually clean, then the row disappears. Deleting an
ontology also removes the proposals tied to it (those that fed into it and those
grounded against it).

**Reversibility depends on the ontology type:**

- **Foundational** (curated references such as FIBO or Schema.org, loaded from
  the catalog): removal is **reversible** — you can re-load the same ontology
  from the catalog afterward if you need it again.
- **Uploaded** (your own OWL/Turtle/RDF file) and **induced** (auto-generated
  from your data): removal is **permanent** — this is your own content and
  cannot be recovered. Re-creating it means uploading the file again or
  re-running induction.

**Dependency guard.** While any **induced** ontology exists in the namespace,
you cannot delete a **non-induced** (foundational or uploaded) ontology — the
induced ontology is derived from and grounded against those references, so
removing one out from under it would leave it dangling. The API rejects this
with `409 Conflict`, and the UI disables the **Delete** action on those rows
(with an explanation) rather than letting you click into an error. Deleting an
**induced** ontology itself is always allowed (it excludes itself from the
check). To resolve a blocked delete: first delete the induced ontology (or
ontologies) that depend on the reference, then delete the reference.

**Where each Delete lives in the web app:**

- **Foundational / uploaded** — **Induction** page → **Reference ontologies**
  tab → the row's **Delete** action.
- **Induced** — **Explorer** → **Ontologies** tab → open the ontology → **Delete
  ontology**. (Induced ontologies aren't listed on the Induction page, so this is
  the path to use when the guard above tells you to remove one first.)

## Validating and repairing datatype issues

Before you accept a proposal, **Validate** runs consistency and quality checks on
it. One of those checks looks at the **datatype names** on your properties.

Some datatype tokens are *malformed* — a mis-cased XSD type (`xsd:datetime` with a
lowercase "t" instead of the correct `xsd:dateTime`) or a SQL-style alias that
isn't a real XSD type (`xsd:varchar`, which should be `xsd:string`). These usually
come from hand-editing a proposal's Turtle or uploading an ontology authored
elsewhere — induction itself always emits valid types.

**Editing a proposal keeps your Turtle exactly as you wrote it.** The system no
longer silently rewrites datatype tokens when you save — your ontology is your
data. Instead, when malformed tokens are present, Validate surfaces them as a
`MALFORMED_DATATYPE_TOKENS` finding (a non-blocking warning) that lists each one
and the correctly-spelled type it would become.

**Repairing is a one-click, opt-in action.** The finding shows a **Repair** button;
clicking it previews the exact changes (e.g. `datetime → dateTime`, and how many
properties are affected) and, on confirm, rewrites the tokens in your saved
ontology. Nothing changes until you consent.

**Repair is recommended but optional.** Your queries work either way — when a
proposal is accepted, the copy served to the query engine is automatically
normalized, so malformed tokens never break querying. Repairing matters for
*consistency*: it keeps your **saved** ontology (and anything you download from it)
spelled the same as what actually gets served. If you accept a proposal that still
has unrepaired tokens, you'll get a confirmation prompt first — you can repair then,
or accept as-is.

## Mapped classes and Tier-2 answerability

Not every class in an ontology can answer a **structured** query. Only classes
backed by a real SQL table can — Context Ontology Accelerator marks these as **mapped**.

- **What makes a class "mapped"?** A class is mapped when it is the target of an
  R2RML mapping (the `rr:class` of a TriplesMap) — i.e. it corresponds to a real
  table/view in a data source and is therefore SQL-queryable. Classes from
  **document (unstructured) induction** or from a **loaded foundational ontology**
  (e.g. FIBO, Schema.org) have no table behind them and are **unmapped**.
- **Why it matters:** the structured-query path (Tier 2 — NL→SPARQL via the VKG,
  and NL→SQL) is filtered to **mapped** classes only. This stops the system from
  authoring a query against a class that has no data behind it (which would
  return nothing). Unmapped classes still power Tier-3 knowledge retrieval and
  graph traversal — they're only excluded from structured queries.
- **How to tell:** `GET /graph/class` returns an **`is_mapped`** boolean per
  class. `true` means R2RML-backed and eligible for Tier 2; `false`/absent means
  it is not.
- **Trust boundary:** `is_mapped` is **server-derived** from the R2RML mapping at
  ingest. It cannot be set by uploading an ontology that asserts the marker
  itself — any caller-supplied `coa:isMapped` triple is stripped on ingest, so
  the flag always reflects a genuine SQL mapping.

## How Ontologies Power Queries

Once an ontology is ingested:

1. **Tier 2 (structured queries — NL-to-SPARQL / NL-to-SQL)**: The ontology provides semantic context for translating natural language into SPARQL/SQL. The VKG (Virtual Knowledge Graph) uses the ontology to map between the conceptual model and the physical database schema. **Only mapped classes** (see above) are exposed to this path.

2. **Tier 3 (Knowledge Retrieval)**: The Neptune knowledge graph stores ontology entities as nodes. Semantic search and graph traversal use these to find relevant context for answers.

3. **Embeddings**: Ontology classes and properties are embedded in OpenSearch for vector similarity search, enabling the system to match natural language queries to the right concepts.

## Viewing Ontologies

The web app separates *starting* ontology work from *browsing* what already exists:

- The **Induction** page is where you start an induction run, review and accept
  proposals, and manage the reference ontologies (foundational + uploaded) used
  for grounding.
- The **Explorer** page (reachable from Induction via **Open Explorer**) is where
  you browse what exists in the namespace. It has three tabs:
  - **Classes** — the class hierarchy, relationships, and properties, with an
    interactive graph view of the knowledge graph.
  - **Ontologies** — an inventory of every ontology in the namespace (induced,
    foundational, and uploaded), filterable by type.
  - **Inducted sources** — the data sources that have fed an induction run.

The Classes tab's graph view seeds with every class in the namespace on first
open (across all its ontologies) **plus each ontology's object-property
relationships**, so the initial picture matches the graph shown on an accepted
proposal's detail page rather than a class-only skeleton. A relationship is
drawn as an edge when the property declares an `rdfs:domain` and `rdfs:range`
that are both classes in the view — which is what the induction engine emits
for foreign-key and inferred relationships. (Some imported foundational
vocabularies describe their properties differently, so they may show as a
class hierarchy with few or no relationship edges; their classes still render
in full.)

Very large ontologies are bounded so layout and rendering stay responsive, and
the limit applies **per ontology** rather than to the namespace total:

- An ontology with **500 classes or fewer** renders in full, with its
  relationships.
- One above that is reduced to a bounded set (its largest-subtree root classes
  plus a capped number of descendants).
- Across the whole view, at most **1500** classes and **1000** relationship
  edges are drawn up front; ontologies are filled in induced-first, then
  smallest-first.

Because the limit is per ontology, loading a large reference ontology for
grounding (Schema.org, FIBO) reduces only *that* ontology — your induced
ontology keeps every class and relationship beside it. Anything not drawn up
front is still reachable through search and by selecting a node, which upgrades
it to the full class detail including all its relationships and attributes.

## Best Practices

- **Start with induction**: let Context Ontology Accelerator generate a baseline ontology from your sources, then refine
- **One ontology per domain**: keep ontologies focused on a single business domain. Spread ontologies across logically separated namespaces as needed.
