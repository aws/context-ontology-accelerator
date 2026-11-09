# Architecture

Context Ontology Accelerator turns your structured and unstructured data into a governed semantic layer, then uses that layer to answer natural language questions. The platform runs entirely in your AWS account and is organized into four stages: **Scan**, **Model**, **Metrics**, and **Serve**.

The diagram below shows every component and data flow. Each numbered marker (1a, 1b, and so on) matches a step in the sections that follow.

![Context Ontology Accelerator system architecture](architecture.png)

!!! note "Editable diagram"
    The editable source for this diagram is `external-docs/diagrams/architecture.drawio`. Open it with [draw.io](https://www.drawio.com/) or the draw.io extension for VS Code.

## Request Authentication and Authorization

All traffic enters the platform through one of three paths, and every path is authenticated before it reaches a service.

- **Authentication.** Every API, the Playground, and the MCP server validate a JWT from your identity provider. By default that is an Amazon Cognito user pool the deployment creates. You can also federate a SAML 2.0 or OIDC provider through Cognito, or connect your own OIDC provider directly with no Cognito. See [Authentication Setup](authentication-setup.md).
- **Authorization.** Cedar policies, stored per role, decide which namespace actions a caller may perform. For example, `data-steward` can manage sources, ontologies, and metrics, `data-analyst` can query and read, and `namespace-owner` (or the equivalent `namespace-maintainer`) has full access within a namespace. `platform-admin` and `platform-viewer` apply across all namespaces. See [Roles & Permissions](role-permission-management.md) and [Cedar Policies](cedar-policy-authoring.md).

The platform's web UI is served through Amazon CloudFront. Amazon Route 53 alias records are optional and map your own domain name to the distribution.

## Scan: Structured Data Pipeline

Scan discovers your relational and cataloged data, describes it in business terms, and makes it queryable. See [Sources](sources.md) for the user guide.

| Step | What happens |
|------|--------------|
| **1a** | You register a structured source through the Sources API: an AWS Glue database, a JDBC database, a Databricks SQL Warehouse, or a custom Athena connector (see [Sources](sources.md) for the full list). The request is authenticated by your identity provider and authorized by Cedar for a role that can manage sources, such as `data-steward` or `namespace-owner`. |
| **1b** | The API places a scan request on an Amazon SQS queue. A trigger Lambda (not shown) reads it and starts the Structured Scan pipeline, an AWS Step Functions state machine. |
| **1c** | The Discovery Lambda reads the schema from the AWS Glue Data Catalog (Existing Schemas in the diagram) or from the structured source itself, and writes the discovered schema metadata (tables, columns, data types, primary keys, and foreign keys) to the metadata catalog in Amazon DataZone. For JDBC sources, credentials are read from an AWS Secrets Manager secret that you supply (`credentialSecretArn`). Secrets Manager is not shown in the diagram. |
| **1d** | The Federation Provisioner Lambda creates a Glue connection, registers it with AWS Lake Formation, and provisions a managed Amazon Athena federated catalog for JDBC sources. Glue and S3 sources are already queryable through the native Athena catalog and skip this step. |
| **1e** | The Enrichment task, running on Amazon ECS, reads the schema metadata and uses an Amazon Bedrock model to generate business-friendly descriptions, synonyms, glossary terms, and tags. It writes the enriched asset revisions back to the DataZone catalog. |
| **1f** | Data stewards review the enriched tables and columns and approve or reject them. Review status moves from `PENDING_REVIEW` to `APPROVED` or `REJECTED`, and stewards can edit metadata before approving. Once a source reaches `APPROVED` status, its approved tables are available to ontology induction, and through the induced ontology, to Serve. Approval is separate from reachability: federation provisioning (step 1d) is what makes a JDBC source reachable from Athena. |

The pipeline runs in the order Discovery, then Federation, then Enrichment. A failure while provisioning federation for a JDBC source marks the scan as failed, so a source is never reported as scanned while being unqueryable.

## Scan: Unstructured Data Pipeline

This pipeline ingests documents, extracts their text, and builds a knowledge graph and vector index from them.

| Step | What happens |
|------|--------------|
| **2a** | You upload documents to Amazon S3 or submit an S3 bucket configuration through the Sources API, with the same authentication and authorization as step 1a. |
| **2b** | A Trigger Lambda reads the request from an SQS queue and starts the document ingestion Step Functions state machine. |
| **2c** | The Preprocessing Lambda extracts text from the raw files. Supported formats are PDF, Word (`.docx`), Markdown, and plain text. Scanned PDFs are rendered at 300 DPI and sent to Amazon Textract for OCR. Text-based PDFs and Word documents are converted to Markdown with the Unstructured library, and Markdown and text files pass through unchanged. The extracted text is staged in S3 as `.txt` or `.md` files, each with a `.metadata.json` sidecar. |
| **2d** | A Knowledge Graph (KG) Build task on AWS Fargate starts the GraphRAG pipeline. |
| **2e** | An Amazon Bedrock model extracts entities and relationships. An embedding model, Cohere Embed v4 with 1024 dimensions by default, generates the embeddings. The pipeline writes a lexical graph of entities, topics, and statements to Amazon Neptune through openCypher, and writes the embeddings to Amazon OpenSearch Serverless. Both stores are scoped per namespace: graph nodes carry a per-namespace tenant label, and each namespace has its own vector indexes. |

## Model: Ontology Induction

Induction proposes an ontology for your data and publishes it after a steward approves it. See [Ontologies](ontologies.md) for the user guide.

| Step | What happens |
|------|--------------|
| **3a** | You request ontology induction through the Induction API. You choose the data sources and, optionally, grounding ontologies. The request is authenticated by your identity provider and authorized by Cedar for a role that can manage ontologies, such as `data-steward` or `namespace-owner`. |
| **3b** | The Ontology Engine fetches approved table and column metadata from the catalog. Only sources in `APPROVED` status contribute, and within them only approved tables. |
| **3c** | The engine generates concept embeddings with Cohere Embed v4 and searches OpenSearch to match concepts against foundational ontologies, when you provide them. Concepts at or above the confidence threshold (`0.80` by default) are matched to existing classes. Concepts below it become novel classes. |
| **3d** | The engine builds an OWL 2 ontology proposal. The proposal contains the matched classes, the novel classes, and R2RML mappings that let the platform translate SPARQL to SQL through Ontop, the virtual knowledge graph (VKG) engine. |
| **3e** | The proposal is stored for review: the proposal record in Amazon DynamoDB, and its ontology and R2RML artifacts in S3. |
| **3f** | A steward accepts the proposal, which requires a role that can manage ontologies. The ontology is then published to Neptune, the concept embeddings are indexed in OpenSearch, and the R2RML mappings are written to S3 for VKG query translation. |

## Metrics: Authoring and Validation

Metrics are governed, reusable business calculations that the Serve layer can answer from deterministically. See [Metrics](metrics.md) for the user guide.

| Step | What happens |
|------|--------------|
| **4a** | You author a metric through the Metrics API. A metric specifies a name, a description, SQL expressions for one or more SQL dialects, a data source, and a source table, plus optional unit, time grain, AI context, and ontology concepts. The request is authenticated by your identity provider and authorized by Cedar for a role that can manage metrics, such as `data-steward` or `namespace-owner`. |
| **4b** | The Metric Service runs the metric through hard and soft validation checks (see the Metric Validation section of [Metrics](metrics.md)) and publishes it to Neptune as an OWL class. It also indexes an embedding of the metric in OpenSearch, so Tier 2 can retrieve the metric's definition as context for a question. |

## Serve: Query Resolution

Serve answers natural language questions using the semantic layer. See [Serve](serve.md) for the user guide.

| Step | What happens |
|------|--------------|
| **5a** | A natural language query reaches the Context Manager, which runs on Amazon Bedrock AgentCore Runtime, through one of three independent paths. The Playground streams over Server-Sent Events (SSE) directly to AgentCore Runtime. The Data Layer REST API goes through Amazon API Gateway and a Lambda function to AgentCore Runtime. The MCP server connects AI assistants and agents. All three paths authenticate with a JWT from your identity provider. |
| **5b** | The Context Manager resolves the query in tiers. See [Resolution tiers](#resolution-tiers). |
| **5c** | Access control, shown in the diagram as one gate, has two layers and both must pass. Before resolving any question, the Context Manager checks with Cedar that the caller holds a role with the `query` action on the target namespace. This check also covers paths that don't generate SQL, such as document search and graph traversal. Every generated SQL statement then passes the SQL firewall, which applies the table allowlists and column denylists from the caller's grant profile, before it reaches the query executor. |
| **5d** | The query executor runs the SQL against the structured sources, fetching credentials from AWS Secrets Manager (not shown) for JDBC sources. A query goes through direct JDBC when it targets a single data source that supports direct access. Cross-source queries, Glue and S3 sources, and any ambiguous case go through Athena federated query. Lake Formation grants are applied when sources are provisioned, and Athena and Glue enforce them at query time. |

### Resolution tiers

| Tier | Strategy | When it is used |
|------|----------|-----------------|
| **Tier 1** | Metrics resolver. Matches the question to a governed metric by exact name or synonym, with a fuzzy fallback for typos and plurals, and runs the metric's pre-compiled SQL. It makes no LLM call. | The metric accounts for the whole question. If the question also asks for a filter, grouping, or time window that the metric doesn't cover, Tier 1 passes the question to Tier 2 together with the metric definition. |
| **Tier 2** | Structured query generation. By default it generates SQL from your ontology (NL-to-SQL) and falls back to NL-to-SPARQL, which Ontop translates to SQL through the R2RML mappings. Other strategies can run in the opposite order, in parallel, or alone. | The question maps to ontology classes that are backed by a SQL table. |
| **Tier 3** | LLM synthesis. Vector search and graph traversal run in parallel, and a Bedrock model composes the answer. | Questions that are complex, involve several metrics, or are ambiguous. A Tier 2 result with confidence below `0.3` also falls through to Tier 3, unless you pin Tier 2 with `tierOverride` or Tier 2 returns a final "not answerable" result. |

### SQL firewall rules

The SQL firewall always allows only `SELECT` statements and rejects anything it can't parse. When the caller's grant profile includes restrictions, it also enforces them:

- Every table in the query must be in the profile's table allowlist.
- Columns on the profile's column denylist can't be projected.
- `SELECT *` over a table that has denied columns is rejected, because denied columns can't be proven absent. `COUNT(*)` is allowed.

### Graph sources (optional)

A deployment can also register existing RDF stores in Amazon Neptune as graph sources (`RDF_SPARQL`). Questions about a graph source are answered with NL-to-SPARQL run directly on that store, and every generated query passes a read-only SPARQL firewall. Graph sources are off by default and are not shown in the diagram. See the "Registering an RDF/SPARQL Graph Source" section of [Sources](sources.md) and the "Graph sources" section of [Deploying](deploying.md).

## Data Stores

| Store | What it holds |
|-------|---------------|
| Amazon DynamoDB | Namespaces, source records and their status, roles and grants, the ontology registry, and ontology proposals. |
| Amazon DataZone | The metadata catalog: discovered and enriched table and column metadata, with each table's and column's review status. |
| Amazon S3 | Raw and staged documents, proposal artifacts, and the R2RML mapping files for each ontology. |
| Amazon Neptune | Ontologies and metrics as RDF, with one named graph per namespace. Document entities, topics, and statements as an openCypher lexical graph, with nodes labelled per namespace. |
| Amazon OpenSearch Serverless | Vector indexes, with separate indexes per namespace, for ontology concepts, metrics, and document chunks. |
| AWS Glue and Lake Formation | The catalog and access grants for Glue, S3, and federated JDBC sources. |
| AWS Secrets Manager | Credentials for JDBC sources. |
