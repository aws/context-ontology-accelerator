# Ontology Engine

- **Owner**: TBD
- **LLD**: TBD
- **Status**: In development

Automated induction of OWL 2 ontologies from relational schemas and
unstructured documents. Grounds against foundational ontologies
(Schema.org, Dublin Core, PROV-O, FOAF, FIBO) via vector similarity
(Amazon Bedrock Cohere Embed v4). Uses large language models
(Amazon Bedrock Claude) for concept extraction, alignment, and
rigor-style generate/judge induction. Persists ontologies and
embeddings in Amazon Neptune Analytics (graph + HNSW vector index) or
Neptune Database + OpenSearch Serverless.

## Layout

    packages/ontology-engine/
    ├── src/coa_ontology/
    │   ├── main.py                      FastAPI service entry
    │   ├── induce_catalog.py            induction driven by data-catalog datasources
    │   ├── proposals.py                 proposal review workflow
    │   ├── dynamo_store.py              DynamoDB single-table store
    │   ├── bedrock_embeddings.py        direct Bedrock embedding client
    │   ├── catalog/                     ontology and embedding CRUD
    │   ├── inducer/                     induction pipelines
    │   │   ├── routers/                 /induce and /induce/document endpoints
    │   │   ├── services/                pipeline, LLM, embedding clients
    │   │   └── strategies/              pluggable induction strategies
    │   │       ├── table_to_ontology.py embedding-based matching
    │   │       └── rigor_ontology.py    RIGOR Gen-LLM + Judge-LLM
    │   └── validation/                  three-tier validation (HermiT, OntoQA, OoPS!)
    ├── scripts/
    │   └── live_induction_run.py        direct pipeline invocation without HTTP
    ├── demo/
    │   ├── setup.py                     foundational ontology loading
    │   ├── demo.py                      interactive induction walkthrough
    │   ├── parse_ddl_to_config.py       SQL DDL → YAML parser (sqlglot)
    │   ├── stage_fixtures.py            stage parsed tables into the mock catalog
    │   ├── pc-insurance-ddl.sql         OMG P&C benchmark schema (13 tables, 65 columns)
    │   └── pc-insurance-tables.yaml     parser output ready for staging
    ├── neptune-config/
    │   └── p8-config-vector.json        Neptune Analytics graph config
    └── tests/
        ├── unit/                        mocked, no live AWS (pytest.mark.unit)
        ├── integ/                       require live AWS (pytest.mark.integ)
        └── fixtures/
            └── data-catalog/            OpenMetadata-compatible mock catalog service

## Induction strategies

`table_to_ontology` (default) — embed each column name, vector-search
against grounding-ontology class and property embeddings, emit matches
at configurable similarity thresholds. One OWL class per table; columns
become datatype properties (object properties for foreign keys).

`rigor_ontology` — implements
[Retrieval-Augmented Generation of Ontologies from Relational
Databases](https://arxiv.org/abs/2506.01232) (Nayyeri et al., 2025). For
each table in FK topological order, retrieve related classes and
properties from the growing core ontology via embedding similarity,
call a generator LLM to produce an OWL fragment (Turtle), then call a
judge LLM to review and refine. Merge the refined fragment into the
core ontology. Finishes with a novelty check that separates matched
from novel terms.

## Unstructured induction (v0)

Induces an OWL 2 ontology directly from an unstructured lexical knowledge
graph in Neptune Analytics (entities, facts, and topics produced by the
document-ingestion pipeline) rather than from a relational schema. The
backend services live under `inducer/unstructured/`; the API + orchestration
wiring exposes them as a job-based HTTP endpoint that mirrors the structured
`/induce` surface.

### Calling the API

Start a job (returns `202 Accepted` + a `job_id`):

    POST /induce/unstructured/
    {
        "namespace": "default",
        "name": "my-ontology",
        "graph_arn": "arn:aws:neptune-graph:us-east-1:<account>:graph/<graph-id>",
        "ontology_uri_prefix": "http://coa.amazon.com/namespace/default/ontology/my-ontology",
        "description_mode": "schema",
        "include_individuals": false,
        "entailment_enabled": false
    }

Poll and list jobs:

    GET /induce/unstructured/jobs            # list jobs in a namespace
    GET /induce/unstructured/jobs/{job_id}   # poll a single job

On success the worker writes an `OntologyProposals` record tagged
`source_type=UNSTRUCTURED`. Review and accept it through the existing,
source-type-agnostic proposal endpoint:

    POST /ontology/proposals/{id}/accept

### Stage progression

A job moves through these statuses (per Requirement 2.4):

    pending → querying_neptune → sampling → generating_descriptions
        → resolving_iris → applying_rules → entailment → serializing
        → storing → completed → failed

`applying_rules` absorbs the LLD's `aligning` and `proposing_axioms` stages
(the class normalizer's `skos:closeMatch` bands are the alignment surface area
v0 emits). `entailment` is reported for API-contract stability but is a
pass-through in v0 (see below). The LLD's `validating` stage is omitted from
v0. The orchestrator emits stages through `serializing`; the worker thread
owns the terminal `storing` / `completed` / `failed` transitions.

### v0 limitations

v0 is intentionally incomplete in four areas. These are known gaps, not bugs;
the first three are also surfaced at runtime in the job's
`InductionReport.warnings` list so they show up in the API response, not just
here. Each is handed off to a deferred task in the parent spec
(`.kiro/specs/unstructured-ontology-induction/`).

1. **Sparse `owl:DatatypeProperty` output.** ~84% of `__Fact__` nodes on the
   dev graph encode the complement inline in the `value` string with no
   `__OBJECT__` edge, so Rule 4 (`induce_datatype_properties`) sees no SPC
   facts and emits few or zero datatype properties. → parent spec **Task 12B**
   (predicate-type extraction from inline complements).
2. **No cross-run normalizer persistence.** The `ClassNormalizer` rebuilds an
   in-memory class-embedding index on every run, so the LLD's "subsequent
   batches reuse existing IRIs" convergence story does not hold yet. → parent
   spec **Task 10A** (persistent class embedding store).
3. **No hierarchy axioms.** The `entailment` stage is a pass-through and emits
   zero `rdfs:subClassOf` / `owl:equivalentClass` / `owl:disjointWith` axioms.
   The only alignment triples come from the class normalizer's
   `skos:closeMatch` bands. → parent spec **Task 13** (OntoEKG-style entailment
   engine).
4. **No SHACL / HermiT validation.** Tier 1/2/3 validation (HermiT, OntoQA,
   OoPS!) is not invoked, so proposals are stored without conformance checks.
   The validation framework (`validation/`) is its own subsystem, wired in
   separately rather than by a single deferred induction task.

### Deferred work

Pointers to the parent spec's deferred tasks that close these gaps:

| Parent-spec task | Addresses |
|---|---|
| Task 10A | Persistent class embedding store → cross-run IRI convergence |
| Task 12B | Predicate-type extraction → richer `owl:DatatypeProperty` output |
| Task 13  | OntoEKG-style entailment engine → inferred class hierarchy axioms |

See `.kiro/specs/unstructured-ontology-induction/` for the parent spec and
`.kiro/specs/unstructured-ontology-induction-api/` for this API wiring spec.

## Backends

Selected via `WORKBENCH_BACKEND`:

- `na_only` — Neptune Analytics for both graph storage and HNSW vector
  search. Simplest deployment.
- `opensearch_neptune` — Neptune Database for graph, OpenSearch
  Serverless for vector search. Higher throughput, more moving parts.

> **⚠️ Tier-2 answerability depends on the backend.** The `coa:isMapped` marker
> that gates R2RML-mapped classes into the structured-query path (NL→SPARQL /
> NL→SQL) is written **only by the `opensearch_neptune` (NDB) backend** at
> ingest. The `na_only` (Neptune Analytics) backend currently **no-ops** the
> marker write, so **every class on an NA-backed namespace is unmapped → all
> Tier-2 structured queries fall back to Tier-3**. Use `opensearch_neptune` if
> you need structured querying. NA marker persistence is a known limitation
> (planned follow-up).

## Quick start

Copy `demo/.env.example` to `.env` in this package root, fill in your
AWS resource IDs, then from the monorepo root:

    uv run --package coa-ontology uvicorn \
        coa_ontology.main:app --port 8080

Load foundational ontologies and explore interactively:

    cd packages/ontology-engine/demo
    python setup.py
    python demo.py

For non-interactive induction against data-catalog-registered
datasources:

    python scripts/live_induction_run.py

## Tests

Unit tests (no live AWS required):

    uv run pytest packages/ontology-engine -m unit -v

Integration tests (require live AWS + data-catalog fixture running):

    # Terminal 1 — start the mock data-catalog
    cd packages/ontology-engine/tests/fixtures/data-catalog
    uvicorn app.main:app --port 8003

    # Terminal 2 — run integ tests
    uv run pytest packages/ontology-engine -m integ -v

The `tests/fixtures/data-catalog/` directory contains a standalone
OpenMetadata-compatible mock catalog, used by integration tests to
register schemas without touching production catalogs. See its
`README.md` for standalone use.

## Configuration

Runtime environment variables (see `demo/.env.example` for the complete
list):

| Variable | Purpose |
|---|---|
| `WORKBENCH_BACKEND` | `na_only` or `opensearch_neptune` |
| `NA_GRAPH_ID` | Neptune Analytics graph ID (na_only backend) |
| `NDB_ENDPOINT`, `OSS_ENDPOINT` | Neptune DB + OpenSearch (opensearch_neptune backend) |
| `DYNAMODB_TABLE` | Single-table store for jobs and proposals |
| `BEDROCK_REGION`, `BEDROCK_EMBED_MODEL_ID` | Embedding model config |
| `LLM_MODEL_ID`, `LLM_REGION` | LLM config (RIGOR strategy and document induction) |
| `DATA_CATALOG_URL` | URL of the data-catalog service (default `http://localhost:8003`) |

## References

- [OWL 2 Profiles](https://www.w3.org/TR/owl2-profiles/) (QL, RL, EL)
- [R2RML — RDB to RDF Mapping Language](https://www.w3.org/TR/r2rml/)
- [SKOS Simple Knowledge Organization System](https://www.w3.org/TR/skos-reference/)
- [Amazon Neptune Analytics Vector Search](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/vector-search.html)
- [Cohere Embed v4 on Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html)
- Nayyeri et al., [RIGOR: Retrieval-Augmented Generation of Ontologies](https://arxiv.org/abs/2506.01232), 2025
- Sequeda et al., [A Benchmark to Understand the Role of Knowledge Graphs on LLM's Accuracy for QA on Enterprise SQL Databases](https://arxiv.org/abs/2311.07509), 2023
- Tartir et al., OntoQA: Metric-Based Ontology Quality Analysis, 2005
- Poveda-Villalón et al., OOPS! (OntOlogy Pitfall Scanner), 2014
