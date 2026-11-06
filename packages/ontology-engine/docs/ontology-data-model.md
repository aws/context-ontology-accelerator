# Ontology Data Model

How a relational schema becomes an OWL ontology in COA, where the metadata
that describes it lives, and what that design costs.

Audience: a developer new to this package who knows SQL and general
engineering but has not worked with RDF/OWL before. Part 1 is vocabulary you
need to read the rest; skip it if RDF is already familiar.

Primary sources for everything below:

- `src/coa_ontology/inducer/strategies/table_to_ontology.py` — T-Box builder (`_build_proposal_ontology`)
- `src/coa_ontology/inducer/strategies/base.py` — R2RML builder (`InductionStrategy.build_r2rml`) and the shared naming/FK helpers
- `src/coa_ontology/catalog/ingest.py` — accept-time persistence and the derived-marker/embedding-text pipeline
- `src/coa_ontology/stores/neptune_db_graph.py`, `src/coa_ontology/stores/na_graph.py` — the two graph backends
- `packages/context-manager/src/coa_serve/tier2/ontop/tbox_context.py` — the serve-side reader

---

## 1. Terminology

### Triple, IRI, literal

RDF represents everything as **triples**: `(subject, predicate, object)`. A
subject or predicate is always an **IRI** (a globally unique identifier that
looks like a URL); an object is either an IRI or a **literal** (a typed value
like `"42"^^xsd:integer`). A set of triples is a graph.

```turtle
ind:Customer  rdf:type   owl:Class .
ind:Customer  rdfs:label "customers" .
```

### T-Box vs A-Box

- **T-Box** — the schema: which classes exist, which properties they have, how
  they relate. "A `Customer` has a `customerId` of type string."
- **A-Box** — the instance data: "customer #17 has `customerId` `'C-17'`."

COA's ontology engine produces a **T-Box only**. The A-Box is never
materialized — it stays in the source SQL tables and is reached at query time
through the virtual knowledge graph (see *R2RML* below).

### Blank node

An anonymous node with no IRI, used when RDF needs a structural placeholder.
OWL requires them for constructs like cardinality restrictions and key lists.
They are not a modeling choice; they are syntax.

### Named graph / quad

A triple plus a fourth component naming which graph it belongs to. Lets one
triple store hold many logically separate graphs and query them in isolation.
COA gives each ontology its own named graph.

### OWL, and the OWL 2 QL profile

**OWL** is a vocabulary layered on RDF for describing schemas with formal
semantics — subclassing, property domains and ranges, cardinality, equivalence.
A reasoner can derive new facts from OWL axioms.

OWL has **profiles**: subsets chosen so that a specific reasoning task stays
computationally tractable. **OWL 2 QL** is the profile designed for query
rewriting over databases, and it is what Ontop (COA's VKG engine) reasons over.
Axioms outside QL are not errors; they are simply not used during query
rewriting.

### R2RML

**R2RML** is a W3C standard for declaring how relational tables map to RDF: a
`rr:TriplesMap` names a table, a `rr:subjectMap` says how to mint a subject IRI
from its primary key, and each `rr:predicateObjectMap` maps a column to a
predicate. R2RML is itself written as RDF triples.

A **Referencing Object Map** (R2RML §7.5) is the mechanism for joins:
`rr:parentTriplesMap` points at another TriplesMap, and one or more
`rr:joinCondition` nodes carry the `rr:child` / `rr:parent` column pairs.

### VKG (Virtual Knowledge Graph)

A query engine — Ontop, here — that reads an ontology plus an R2RML mapping and
answers SPARQL by **rewriting it into SQL** against the live tables. No RDF
instance data is ever loaded. This is why the T-Box/A-Box split above matters:
the graph is virtual.

### Reification

**Making a statement itself addressable, so you can attach statements about
it.** RDF triples have no identity, so `(A, subClassOf, B)` cannot be the
subject of another triple. Reification works around this by minting a node that
*stands for* the statement:

```turtle
_:s1  rdf:type     rdf:Statement ;
      rdf:subject   ind:Customer ;
      rdf:predicate rdfs:subClassOf ;
      rdf:object    schema:Person ;
      coa:confidence "0.91"^^xsd:float .   # a statement about the statement
```

Cost: four bookkeeping triples per reified statement, and the reified node is
not semantically connected to the original triple — a reasoner does not treat
`_:s1` as asserting anything. Related patterns solving the same problem:
**singleton properties** (mint a unique predicate IRI per statement and annotate
that) and **named-graph-per-statement** (put each triple in its own graph and
annotate the graph).

### RDF-star (RDF 1.2)

A newer syntax that makes a triple directly usable as a subject, replacing the
four-triple reification dance with one nested term:

```turtle
<< ind:Customer rdfs:subClassOf schema:Person >>  coa:confidence "0.91"^^xsd:float .
```

Same intent as reification, far less overhead, and standardized (SPARQL-star
provides matching query syntax). Requires engine support on both the store and
the query layer.

### Reified out vs. on the fast path

Two ways to make derived or descriptive metadata available to a reader:

- **Reified out** — store it once in normalized form and reconstruct what the
  reader needs at query time (traverse the graph, join, aggregate). Always
  consistent with the source; costs latency per read.
- **On the fast path** — precompute and denormalize it into whatever shape the
  reader consumes, at write time. Fast reads; the copy is a cache and can go
  stale.

This distinction is the subject of section 2.5, and COA chooses the fast path
nearly everywhere.

---

## 2. How COA handles it

### 2.1 The schema is asserted directly, not reified

There is no reification, no `rdf:Statement`, no singleton properties, and no
RDF-star anywhere in the package (verified by search across
`src/coa_ontology/`). The T-Box is asserted plainly, in
`table_to_ontology.py:_build_proposal_ontology`:

| Source | Emitted |
|---|---|
| table | one `owl:Class`, `rdfs:label` = verbatim table name |
| table description | `rdfs:comment` |
| table/column synonyms | `skos:altLabel` |
| column | one property on the class, `rdfs:domain` = table class |
| non-FK column | `owl:DatatypeProperty`, `rdfs:range` = XSD type |
| FK column | `owl:ObjectProperty`, `rdfs:range` = parent class |
| primary key | `owl:hasKey` over the key columns' properties |
| NOT NULL | `rdfs:subClassOf [ owl:minCardinality 1 ]` |
| UNIQUE | `rdfs:subClassOf [ owl:maxCardinality 1 ]` |
| both | `owl:cardinality 1` |
| grounding match | `rdfs:subClassOf` + `skos:exactMatch` (exact) or `skos:closeMatch` (high-confidence) |
| ambiguous match | `skos:relatedMatch` |
| column-level match | `owl:equivalentProperty` |
| sampled enum values | `coa:distinctValues`, one literal per value |

Blank nodes appear only where OWL requires them: the `owl:Restriction` nodes
above and the RDF collection behind `owl:hasKey`.

Two deliberate omissions:

- **`owl:imports` is never emitted.** Ontop network-resolves imports at VKG load
  and fails every query in the namespace when a foundational IRI is not
  dereferenceable. Grounding is carried entirely by `rdfs:subClassOf` plus the
  SKOS axioms.
- **`coa:groundedTo` was dropped** as redundant with those same axioms.

The engine's own vocabulary lives at
`http://coa.amazon.com/vocab/coa#` — `groundedTo`, `fkProvenance`,
`subClassProvenance`, `suggestedSubClassOf`, `isMapped`, `distinctValues`,
`datasourceId`, `sourceSchema`, `matchConfidence`.

### 2.2 Statement-level metadata is attached to the subject

Because nothing is reified, annotations that logically describe an *axiom* are
attached to that axiom's **subject** instead:

- `coa:matchConfidence` on the class, alongside the `skos:closeMatch` whose
  similarity score it reports
- `coa:fkProvenance` on the object property, describing the FK it represents
- `coa:subClassProvenance "PK_SHARING"` on the class, explaining why a
  `rdfs:subClassOf` edge exists

This is unambiguous **only because there is at most one such axiom per subject
per induction run** — `_build_proposal_ontology` reads one grounding match per
`(table, "")` and one per `(table, column)`. It is not a general guarantee: the
proposal-accept path calls `ingest_ontology(allow_append=True)`, which merges
into an existing named graph, so a class IRI re-grounded by a later run
accumulates multiple `matchConfidence` literals on one subject with no way to
say which belongs to which match axiom.

This is the one place in the model where RDF-star would pay for itself. It is
not available on any backend this package targets:

- **Neptune Database** — the live deployment (`WORKBENCH_BACKEND=opensearch_neptune`),
  pinned to engine **1.4.7.0** in
  `infra/lib/stacks/foundation/storage-stack.ts`. Its SPARQL engine implements
  SPARQL 1.1; there is no RDF-star/SPARQL-star support to store or match a
  quoted triple. This constraint is **version-bound, not architectural** —
  re-check it against the pinned engine version before concluding RDF-star is
  unavailable.
- **Ontop 5.5.0** (pinned in `packages/vkg/Dockerfile`) — no RDF-star notion at
  all. The VKG synthesizes triples from SQL rows through R2RML, and R2RML has no
  vocabulary for emitting a quoted triple, so the mapped half of the graph could
  not carry one even if the store accepted it.
- **Neptune Analytics** (`na_only`) — a property graph reached over openCypher,
  so the question does not arise; there are no triples to quote. Edge properties
  are the analogous mechanism and nothing uses them.

If per-axiom attribution becomes a requirement before an engine upgrade, the
available options are classical reification or named-graph-per-axiom. Both work
on engine 1.4.7.0 and both are worse than RDF-star — see §3.1.

### 2.3 Join paths are expressed twice

A foreign key is emitted into **two artifacts**, and keeping them identical is
the single largest source of complexity in this package.

**Semantic side** (T-Box, `table_to_ontology.py`) — existence and typing of the
relationship:

```turtle
ind:order_customerId  a owl:ObjectProperty ;
    rdfs:domain ind:Order ;
    rdfs:range  ind:Customer ;
    coa:fkProvenance "MANY_TO_ONE" ;
    rdfs:comment "Foreign key: orders.customer_id references customers.id" .
```

**Physical side** (R2RML, `base.py:build_r2rml`) — the mechanics:

```turtle
ind:TriplesMap_Order/POM_CustomerId
    rr:predicate ind:order_customerId ;
    rr:objectMap [ rr:parentTriplesMap ind:TriplesMap_Customer ;
                   rr:joinCondition [ rr:child  "customer_id" ;
                                      rr:parent "id" ] ] .
```

The **join keys live only in the R2RML.** OWL has no vocabulary for "join on
these columns," so the T-Box carries what the relationship *means* and the
R2RML carries how to compute it. Ontop turns a SPARQL triple pattern over
`ind:order_customerId` into a SQL `JOIN` using the join conditions.

Divergence between the two artifacts is the recurring bug class, which is why
both builders consume the **same** helpers from `base.py` rather than deriving
anything independently:

| Helper | Prevents |
|---|---|
| `table_identity`, `pascal_names_for` | Two tables (`public.customers`, `analytics.customers`, or `order_item` vs `order-item`) fusing onto one class/TriplesMap IRI. A fused TriplesMap carries two `rr:tableName` values, which is invalid R2RML — Ontop rejects the mapping or silently picks one table. |
| `reference_index`, `ambiguous_target_names` | Resolving an unqualified FK target to the wrong parent. An ambiguous target is degraded to a datatype property + `rr:datatype` literal in **both** artifacts, because a wrong join is worse than a missing one. |
| `composite_fk_anchors`, `composite_fk_columns` | The two builders picking different anchor columns for a multi-column FK. |
| `composite_fk_is_usable` | Declaring `rdfs:range <Class>` for a column the mapping exposes as a literal. |
| `parse_referred_column` | Splitting a dotted FK target under two incompatible conventions — it was previously reimplemented at six call sites, `parts[0]` in some and `parts[-2]` in others. |

**Composite FKs are the notable compromise.** R2RML expresses one naturally
(one Referencing Object Map, one `rr:joinCondition` per column pair), but OWL
cannot say "these three properties jointly reference that class." So the
relationship is **pinned to the constraint's first column**: that column gets
the object property and the POM, and the remaining columns become plain datatype
properties carrying an `rdfs:comment` that names the anchor. Emitting an object
property per participating column instead would mint properties with no mapping
behind them — visible in the T-Box and in the NL→SPARQL prompt, unresolvable by
Ontop, so queries using them return nothing silently.

**One special case skips the object property entirely.** When a child table's
primary key is also its foreign key (`services/subtype_detection.py`), the
relationship is emitted as `rdfs:subClassOf` + `coa:subClassProvenance
"PK_SHARING"` — inheritance, not a relation. The property is still declared,
because that column appears in the child's `owl:hasKey` and an undeclared
property there is an OWL-DL structural inconsistency.

**Serve side is 1-hop only.** `tbox_context.py:_fetch_object_properties`
surfaces object properties as a literal "Join paths" section in the NL→SPARQL
prompt, gated on **both** ends carrying `coa:isMapped` — an edge to an unmapped
class cannot become a SQL join. Nothing materializes, ranks, or caches multi-hop
paths; composing them is left to the LLM and to Ontop's SQL planner.

### 2.4 What the model does not express

| Not expressed | Why |
|---|---|
| Join keys, join arity | No OWL vocabulary; R2RML only |
| Per-axiom attribution | No reification/RDF-star — see 2.2 |
| Datatype precision | `CatalogColumn` carries `dataLength`/`precision`/`scale`; `base.py:xsd_for` discards all three. `DECIMAL(10,2)` → bare `xsd:decimal`, no XSD facets |
| Cardinality beyond 0/1 | Only NOT NULL/UNIQUE are read; nothing derives "at most 5" or the many-side of a relation |
| Datasource routing | `coa:datasourceId` / `coa:sourceSchema` annotations on TriplesMaps (`base.py:_annotate_triples_map`) — ignored by Ontop, parsed only by the VKG translation layer |
| Class hierarchy on the unstructured path | The `entailment` stage is a pass-through in v0; no `owl:equivalentClass` / `owl:disjointWith` |

One deliberate inaccuracy worth knowing about: `base.py:xsd_for_column` types a
`VARCHAR` column as `xsd:decimal` when its **name** implies a number
(`total_spent`, `amount`, `consumption` — see `_NUMERIC_COLUMN_PATTERNS`), so
Ontop can `SUM`/`AVG` it. The declared SQL type is overridden by a name
heuristic.

Also note that `owl:hasKey` and the cardinality restrictions fall **outside OWL
2 QL**, the fragment Ontop rewrites over. The T-Box is genuinely loaded — the
VKG launches `ontop endpoint --ontology=<file>` (`packages/vkg/entrypoint.sh`) on
Ontop **5.5.0** (`packages/vkg/Dockerfile`, `ARG ONTOP_VERSION=5.5.0`) — but
rewriting uses the QL-expressible axioms: `rdfs:subClassOf`,
`rdfs:subPropertyOf`, `rdfs:domain`, `rdfs:range`, and equivalences. Axioms
outside the fragment are not errors and do not fail the load; they simply do not
participate in query rewriting.

So `owl:hasKey` and `owl:min/max/cardinality` serve the **validation tiers**
(HermiT, OoPS!) and the class browser, not Tier-2 answerability. They do not
extend what Tier 2 can answer, and removing them would not shrink it.

### 2.5 Where metadata lives — the fast path

Metadata is **denormalized into four places at once**. Nothing requires a graph
traversal to reconstruct a table's description at query time.

| Location | Contents | Read by |
|---|---|---|
| Ontology named graph (Neptune) | The T-Box: axioms plus `rdfs:label`/`comment`, `skos:altLabel`, `coa:matchConfidence`, `coa:fkProvenance`, `coa:distinctValues`, `coa:isMapped`, and a `definedBy` back-pointer in the workbench vocab (`https://ontology-workbench.local/vocab#`, see `neptune_db_graph.py:WB`) | serve T-Box context; graph browser (`GET /graph/class`) |
| R2RML Turtle in S3 — **never loaded into the graph** | `rr:tableName`, subject templates, join conditions, `coa:datasourceId` / `coa:sourceSchema` | Ontop at VKG load; scanned once at ingest to derive `isMapped` |
| DynamoDB registry row | class/property/axiom/embedding counts, lifecycle status, `source_proposals`, parse status | ontologies list UI; accept flow |
| AOSS vector doc, one per class and property | embedding vector plus two pre-rendered strings and `data_source_id` | serve retrieval; NL→SQL prompt |

Proposal artifacts are keyed
`proposals/{namespace}/{proposal_id}/{version}/{artifact}.ttl`
(`dynamo_store.py:_proposal_s3_key`). Large payloads are offloaded to S3 to stay
under the DynamoDB item cap and the 6 MB API Gateway response cap; the browser
reads Turtle via presigned GET.

**The AOSS documents are the most aggressively denormalized part.**
`ingest.py:_class_text_for` builds two strings per class:

- **`embed_text`** — name, description, synonyms, relationships, bare column
  names. This is what gets embedded. Types and sampled values are deliberately
  excluded as noise for recall.
- **`context_text`** — the same, plus `column:type` and
  `allowed values: [...]` per column. Stored but **not** embedded, handed
  straight to the NL→SQL LLM so it writes correct casts and `WHERE` literals.

So a class's entire description — columns, types, relationships, enum values —
is flattened into a string at accept time. Serve reads one document instead of
walking `rdfs:domain` edges.

**`coa:isMapped` is a derived marker, not stored input.** It is computed
server-side from R2RML `rr:class` membership
(`ingest.py:_class_datasource_map_from_r2rml`) and any caller-supplied
`coa:isMapped` triple is **stripped** at ingest (`_strip_server_markers`, which
also strips `coa:queryBackend` and `coa:dataSourceId`) — it is a trust boundary,
since forging it would force an unbacked class into the structured-query
prompt. Note the deliberate decoupling: **membership** decides `isMapped` (Ontop
can answer a mapped class even with no datasource id), while the **id value**
feeds AOSS `data_source_id` for NL→SQL routing. A mapped class with no id is
still `isMapped` but is dropped by the NL→SQL presence filter. Only `"true"` is
ever emitted — absent means unmapped.

**Graph-backed classes (RDF_SPARQL sources).** A graph source has no R2RML, so
an imported class names its source with the author directive
`coa_import:dataSourceName "<source name>"`
(`ingest.py:_class_graphsource_map_from_payload`). Import resolves the name to
exactly one registered `RDF_SPARQL` source in the namespace
(`catalog/graph_source_resolver.py`) and emits, server-side:

```turtle
ex:SupportTicket coa:isMapped true ;
    coa:queryBackend "RDF_SPARQL" ;    # second serve gate signal; absent == SQL
    coa:dataSourceId "src-abc123" .    # routes the query to that source's endpoint
```

The class's AOSS document gets `query_backend: "RDF_SPARQL"` and the source id
as `data_source_id`. The import is rejected (422, one class-attributed finding
per problem) when a name is unknown, ambiguous or not a graph source, when a
class names more than one source, or when a class is also the `rr:class` of an
R2RML TriplesMap. The `coa_import:` directives are removed before the bulk load,
so they never appear in the queryable graph.

### 2.6 The four internal representations

```
CatalogTable (Pydantic)          wire-shaped, camelCase, mirrors OpenMetadata/Glue
        │                        inducer/services/data_catalog.py
        ▼
two rdflib.Graph objects         proposal T-Box  +  R2RML mapping
        │                        built independently from the same table list
        ▼
Turtle in S3 + DynamoDB row      versioned proposal artifacts, review workflow
        │
        ▼ (on accept)
Neptune named graph (NDB)        quads, one graph per ontology
   or property graph (NA)   +    AOSS vector docs
```

The `CatalogTable` layer stays deliberately close to the catalog wire shape —
camelCase fields and all — with dotted FK targets parsed by
`parse_referred_column` rather than normalized on arrival.

**Two backends, two data models.** `WORKBENCH_BACKEND` selects:

- `opensearch_neptune` (NDB) — real quads via SPARQL `INSERT DATA` into a
  per-ontology named graph, plus OpenSearch Serverless for vectors. This is the
  implemented and E2E-verified path.
- `na_only` (Neptune Analytics) — a **property graph**: `Class` / `Property` /
  `Ontology` nodes with `DEFINES`, `SUBCLASS_OF`, `HAS_DOMAIN`, `HAS_RANGE`
  edges, written via openCypher (`catalog/na_store.py`), with HNSW vector search
  in the same engine.

The two are not at parity. `na_graph.py:store_class` explicitly **no-ops
`is_mapped`** — persisting it needs an NA-specific write and a matching
serve-side read filter — so every class on an NA-backed namespace reads as
unmapped and all Tier-2 structured queries fall back to Tier-3.

---

## 3. Tradeoffs

### 3.1 Direct assertion over reification — accepted cost: no per-axiom metadata

**Chosen because** reification costs four triples per statement and produces a
node a reasoner ignores; RDF-star is unavailable on Neptune Database and
meaningless to Ontop; and the current emission is 1:1 (one grounding match per
class per run), so subject-level annotation is unambiguous in practice.

**Cost:** the 1:1 property is an emission accident, not an invariant. Append-mode
accepts can accumulate several `matchConfidence` literals on one class with no
way to pair them with their match axioms. If multi-match grounding, competing
alignment candidates, or per-axiom audit trails become requirements, this model
has no room for them and the workaround (named-graph-per-axiom, or singleton
properties) is worse than either standard option.

### 3.2 Two artifacts for one concept — accepted cost: they can diverge

The T-Box and the R2RML both describe every table and every foreign key. This is
unavoidable: OWL cannot express join keys, and R2RML cannot express semantics.

**Cost:** every FK decision must be made identically twice. The failure mode is
silent and expensive — a mismatched `rdfs:range` and `rr:datatype` makes Ontop
drop triples or fail a type filter; a property declared in the T-Box with no POM
behind it is offered to the NL→SPARQL LLM and returns empty results with no
mapping-gap signal in the logs.

**Mitigation in place:** every naming and FK decision is centralized in
`base.py` and consumed by both builders. The extensive comments there are a log
of past divergences, and `tests/unit/test_r2rml_spec_compliance.py` (~142 KB)
plus `test_datatype_layer_consistency.py` guard the agreement. The structural
fix — deriving both artifacts from one intermediate representation — has not
been taken.

### 3.3 Fast path over reified-out — accepted cost: caches go stale

**Chosen because** serve latency is on the critical path for every agent query,
and reconstructing a table's description by traversing `rdfs:domain` edges per
class per query is expensive. Pre-rendering `context_text` at accept time turns
that into a single document read.

**Cost:**
- `embed_text` / `context_text` are caches with no invalidation hook. Editing
  the ontology, or re-inducing, requires regenerating them or the NL→SQL prompt
  silently describes the old schema.
- `coa:isMapped` (and the AOSS `data_source_id`) is derived at ingest, so a
  namespace accepted before the marker existed is wholly invisible to Tier 2.
  A temporary shim that dropped the gate for zero-marker namespaces was removed:
  it also exposed document-induced classes as tables whenever a
  structured proposal was not yet accepted. Migration is a fresh structured
  induction, or a new namespace.
- Metadata lives in four stores, so "what does this class look like" has four
  possible answers during any window where they disagree.

**Mitigation in place:** ingest logs `mapped_class_count` and escalates to a
**warning** when R2RML was supplied yet zero classes came out mapped — the canary
for "structured namespace accepted but Tier 2 returns nothing." Serve logs
`tbox_context_no_mapped_classes` when a namespace is dark.

### 3.4 In-memory rdflib — accepted cost: the ontology must fit in the task

**Cost:** graph construction is single-threaded and fully resident. Batching
(`INDUCER_TABLE_BATCH_SIZE`, default 500 tables) bounds only the **embedding
vectors** — 1024-dim Cohere vectors for ~886k columns is roughly 28 GB and
OOM-killed a 32 GB task. The graph itself is still built once from the full
table list, and `detect_pk_sharing_subtypes` needs the full list by design.

**Mitigation in place:** `MemoryError` is re-raised rather than swallowed, so an
OOM fails the job loudly instead of degrading to an all-novel ontology.

### 3.5 Two graph backends — accepted cost: unequal fidelity

**Cost:** `na_only` trades RDF semantics for a single-engine deployment (graph +
HNSW vectors in Neptune Analytics), but the property-graph model means every
RDF-shaped feature needs a second implementation. `coa:isMapped` has only the
NDB one, so **NA-backed namespaces have no working Tier-2 structured querying at
all.** The fail-safe direction is right (absent marker → hidden, not wrongly
exposed) but the capability gap is total, and it is documented in the package
README rather than enforced by config validation.

### 3.6 Degrade-to-literal on ambiguity — accepted cost: silent loss of joins

When an FK target is ambiguous across the run, or a composite FK is malformed,
both builders drop the relationship and emit a plain datatype property instead.

**Chosen because** the bare-`to_pascal` fallback IRI is exactly the IRI
`pascal_names_for` hands the `min()` keeper of a colliding set — so "unresolved"
would land on one arbitrary same-named table and join real data to the wrong
parent. A missing join beats a wrong join.

**Cost:** a real relationship disappears from both artifacts. It is logged
(`fk_target_ambiguous_degraded_to_literal`, `table_name_collides_after_pascal_case`)
and recorded as an `rdfs:comment` on the degraded property, but it is not
surfaced in the proposal review UI, so a reviewer will not see it unless they
read the logs.

### 3.7 One datasource per class — accepted cost: multi-source classes pin to one

`_class_datasource_map_from_r2rml` returns `dict[str, str]` and keeps the last
non-empty id, so a class that is the `rr:class` of TriplesMaps from two
datasources routes NL→SQL to one of them. `isMapped` is unaffected (it keys on
membership). Flagged in-code as a follow-up: widen to `dict[str, set[str]]` and
federate.

### 3.8 Precision loss in datatypes — accepted cost: no round-trip

Length, precision, and scale are captured in `CatalogColumn` and then discarded,
and the `VARCHAR`→`xsd:decimal` name heuristic overrides the declared SQL type.
Aggregation works, which is the point; but the ontology is not a faithful
description of the source schema and cannot be used to regenerate DDL.
