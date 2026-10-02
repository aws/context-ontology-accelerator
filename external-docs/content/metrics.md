# Metrics

Metrics define reusable business calculations with SQL expressions. Once defined, metrics are resolved by the query engine when users ask business questions — ensuring consistent, validated answers across all consumers.

!!! tip "Full request/response schemas"
    For the complete request/response schema for every Metrics endpoint, see
    the **[API Reference](#/api-reference)** (Control Plane API → Metric
    service) — it's generated directly from the API contract and always
    current.

## What is a Metric?

A metric is a named business calculation that includes:

- **Name**: human-readable identifier (e.g. `total_revenue`, `monthly_active_users`)
- **Description**: what the metric measures and any business context
- **Expression**: SQL formula in one or more SQL dialects
- **Data Source**: which database source provides the underlying data
- **Source Table**: the primary table the metric aggregates

## Metric, Ontology, or Source Document?

The three layers are **complementary, not alternatives** — the same business
concept often appears in more than one. A concrete example: should a fixed
calculation such as *sales amount − order amount* be registered as a Tier-1
metric? The answer depends on governance and execution needs, not on whether
the expression contains arithmetic:

| Register as | Use when | Example |
|---|---|---|
| **Metric (Tier 1)** | The calculation is reusable, governed, deterministic, named, and should execute identically for every consumer | `net_sales = SUM(sales_amount) - SUM(order_amount)` as a canonical business definition |
| **Ontology (Tier-2 semantics)** | The information defines concepts, vocabulary, relationships, constraints, or how data fields map to business meaning | `Sale`, `Order`, `hasAmount`, and the relationship between an order and a sale |
| **Source document (Tier-3 evidence)** | The information is prose: policy, rationale, procedure, exception handling, or supporting evidence | The revenue-recognition policy explaining *when* an order counts as a sale |
| **Ad-hoc Tier-2 query** | The calculation is one-off, or should be generated from the structured schema rather than governed as a reusable KPI | A temporary comparison requested for a single analysis |

So for *sales amount − order amount*: register it as a **metric** when it is a
canonical, reusable definition with a stable SQL expression. Model `Sale` and
`Order` in the **ontology** so Tier 2 can answer ad-hoc variations of the same
question. Ingest the policy that explains the business rule as a **document**
so answers can cite the *why*. Each layer strengthens the others.

Current constraints to factor into the decision:

- A metric binds to a **single `dataSourceId` and `sourceTable`**. A calculation
  spanning sources is not a Tier-1 capability today — model it in the ontology
  and let Tier 2 generate the cross-source query.
- Natural-language qualifiers (a time window, a filter, a grouping) make a
  question fall through to Tier 2 unless supplied via `options.dimensions` or an
  explicit `options.tierOverride: 1` — see
  [How Metrics Are Used in Queries](#how-metrics-are-used-in-queries). Your
  definition is still forwarded to Tier 2 in that case, so the fall-through
  builds on it rather than replacing it.
- Registering the ontology or the document does **not** replace the metric: the
  ontology provides shared vocabulary, documents provide explanation and
  evidence; only the metric gives the calculation a governed, named, always-
  identical execution.

## Creating Metrics

### Via the Web App

1. Navigate to your namespace → **Metrics** → **Create Metric**
2. Fill in:
   - **Name**: unique within the namespace
   - **Description**: explain what this metric measures
   - **Data Source**: select an approved source
   - **Source Table**: the table containing the data
   - **Expression**: complete read-only SQL query (e.g. `SELECT SUM(total_amount) FROM orders`)
   - **Dialect**: which SQL engine the expression targets (Trino, PostgreSQL, etc.)
3. Click **Validate** to check syntax and schema references before saving
4. Click **Create**

### Via the API

`POST /namespaces/{namespaceId}/metrics` with `name`, `description`,
`expression.dialects`, `dataSourceId`, and `sourceTable` — see **CreateMetric**
in the [API Reference](#/api-reference) for the full request/response schema.

## Metric Validation

Before saving, metrics are validated through multiple checks. Validation is
split into **hard** checks (reject the request with `400`) and **soft** checks
(publish the metric and return a warning). The guiding rule: a check is hard
only when the metric would be provably broken or unsafe; anything that the
query engine can independently guard at serve time stays soft so onboarding a
metric is never blocked by a false positive.

| Check | What it Verifies | Outcome |
|-------|-----------------|---------|
| Data source exists | `dataSourceId` resolves to a source in the namespace, in an `APPROVED`/`COMPLETED` status | **Hard — `400`** |
| Table existence | `sourceTable` is present in the source's catalog | **Hard — `400`** when absence is provable (see below) |
| SQL safety | Expression contains data-modifying/administrative SQL, locking reads, filesystem/network functions, or delay/lock functions | **Hard — `400`** (security) |
| SQL syntax and shape | Every dialect parses as a complete `SELECT`; the expression Tier 1 selects is also executable as Trino SQL | **Hard — `400`** |
| Column existence | Referenced columns exist in the table | **Soft — INFO** |

### SQL Expression Requirements

The SQL expression is checked for **safety, syntax, and executable shape**.
All three are hard gates because persisting a metric that Tier 1 cannot execute
causes a silent fallback instead of a working metric:

- **DML/DDL is hard-blocked (`400`).** Any data-modifying or administrative
  statement is rejected and never persisted, whether it is standalone, stacked
  (`SELECT 1; TRUNCATE x`), CTE-nested, or an unrecognized verb. Metrics are
  read-only by definition.
- **Side-effecting reads are hard-blocked (`400`).** This includes locking
  clauses such as `FOR UPDATE`, filesystem/network access, sequence mutation,
  advisory locks, and resource-delay functions such as `pg_sleep`,
  `SLEEP`, and `BENCHMARK`.
- **Fragments and parse errors are hard-blocked (`400`).** `COUNT(*)` must be
  written as a complete query such as `SELECT COUNT(*) FROM orders`.
- **Tier 1 compatibility is hard-blocked (`400`).** Each entry is validated in
  its declared dialect. Tier 1 prefers the `TRINO` entry and otherwise uses the
  first entry, so that selected expression must also parse as complete Trino
  SQL. Add an explicit `TRINO` entry when another dialect uses engine-specific
  syntax.

#### Migrating Existing SQL Fragments

SQL fragments accepted by earlier releases are not grandfathered for
execution. They remain stored after an upgrade, but the serve-time SQL Firewall
applies the same complete-query rule and will not execute them. Update each
legacy definition before relying on it for Tier 1:

| Legacy fragment | Complete query |
|-----------------|----------------|
| `COUNT(*)` | `SELECT COUNT(*) FROM orders` |
| `SUM(orders.total_amount)` | `SELECT SUM(orders.total_amount) FROM orders` |
| `SUM(total_amount) / COUNT(*)` | `SELECT SUM(total_amount) / COUNT(*) FROM orders` |

Use `POST /namespaces/{namespaceId}/metrics/validate` on the replacement
definition first, then update the metric with
`PUT /namespaces/{namespaceId}/metrics/{name}`. Create, update, validate, and
OSI import all enforce the complete-query rule, so an import containing a
legacy fragment records an error for that metric instead of persisting it.

#### SQL Safety Reference

Metric SQL is read-only. The onboarding checks and serve-time SQL Firewall
share the same policy and reject side effects even when they are nested in a
CTE, stacked after a semicolon, or exposed through a function:

| Category | Examples that are rejected |
|----------|----------------------------|
| Data and schema changes | `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `DROP`, `TRUNCATE`, `GRANT`, `COPY`, `CALL` |
| Locking reads and table hints | `FOR UPDATE`, `FOR SHARE`, `WITH (UPDLOCK)`, `WITH (TABLOCKX)` |
| Filesystem, network, and external execution | `pg_read_file`, `pg_ls_dir`, `dblink_exec`, `LOAD_FILE`, `OPENROWSET`, `xp_cmdshell` |
| Sequence and session mutation | `nextval`, `setval`, `set_config`, `pg_notify`, `LAST_INSERT_ID` |
| Advisory locks and resource delays | `pg_advisory_lock`, `GET_LOCK`, `pg_sleep`, `SLEEP`, `BENCHMARK`, `SYSTEM$WAIT` |
| Administrative control functions | `pg_terminate_backend`, `pg_reload_conf`, `SYSTEM$ABORT_SESSION`, `SYSTEM$CANCEL_QUERY` |

This list explains the blocked categories and common examples; it is not an
allowlist. Unknown statement types and dangerous functions from an
unrecognized dialect are rejected conservatively. See
[Access Control on Queries](serve.md#access-control-on-queries) for
the query-time enforcement layers.

### Source Table Existence: Provable Absence Only

`sourceTable` is hard-validated, but only when its absence can be **proven**:

- **`400` (provable absence)** — the source's catalog was read successfully,
  it enumerates at least one table for the source, and the declared
  `sourceTable` is not among them. The web app only offers catalog tables, so
  the API enforces the same constraint for direct/bulk callers.
- **`503` (catalog unavailable)** — the catalog lookup was configured but its
  read failed. Enforcement is impossible on a source that should be readable,
  so the request fails closed rather than silently accepting.
- **Soft warning (unprovable absence)** — the namespace has no provisioned
  catalog lookup, or the catalog is readable but empty for the source (e.g. a
  `COMPLETED` source whose assets are not yet steward-approved). Absence
  cannot be proven, so the metric publishes with an `INFO` warning
  (pre-existing behavior).

### Validate Without Saving

`POST /namespaces/{namespaceId}/metrics/validate` accepts the same body as
**CreateMetric** without persisting anything — see **ValidateMetric** in the
[API Reference](#/api-reference).

Response:
```json
{
  "warnings": [
    {"field": "column_reference", "message": "Column 'total_amount' not found", "severity": "INFO"}
  ]
}
```

## Multi-Dialect Expressions

Metrics support multiple SQL dialects so the same business metric works across different engines:

```json
{
  "expression": {
    "dialects": [
      {"dialect": "TRINO", "expression": "SELECT SUM(total_amount) FROM orders"},
      {"dialect": "POSTGRESQL", "expression": "SELECT SUM(total_amount) FROM orders"},
      {"dialect": "REDSHIFT", "expression": "SELECT SUM(total_amount::DECIMAL) FROM orders"}
    ]
  }
}
```

Tier 1 treats the dialect list as follows:

1. If a `TRINO` entry exists, Tier 1 selects it regardless of list order.
2. If no `TRINO` entry exists, Tier 1 selects the first entry. That expression
   must be valid both in its declared dialect and as executable Trino SQL.
3. Every other entry is still validated in its declared dialect even though
   Tier 1 does not select it.

The selected Trino expression is executed directly for Athena-backed sources
and transpiled to the source engine for direct JDBC sources. Add an explicit
`TRINO` entry whenever a PostgreSQL, Redshift, MySQL, SQL Server, or Snowflake
variant uses engine-specific syntax. For example, keep a portable Trino
expression alongside a PostgreSQL expression that uses `::numeric` casts,
rather than putting the PostgreSQL expression first and relying on fallback.

## Bulk Import (OSI Format)

For teams with many metrics, import in bulk using the OSI v1.0 format — the
schema originally published as **Open Semantic Interchange (OSI)**, now
developed at the Apache Software Foundation as
[**Apache Ossie (incubating)**](https://github.com/apache/ossie). The
vendor-agnostic metric/semantic-model spec is the same lineage; Ontology
Accelerator's importer/exporter currently targets OSI v1.0, predating the
Ossie rename:

1. Navigate to **Metrics** → **Import**
2. Upload a YAML/JSON file conforming to the OSI schema
3. Context Ontology Accelerator validates and creates all metrics in batch

Each imported metric must reference a data source in the same namespace whose
status is `APPROVED` or `COMPLETED`. When `source_table` is present and the
approved catalog can enumerate tables, the table must also exist in that data
source. Metrics with missing, unapproved, or provably invalid source references
are recorded as import errors and are not persisted.

### Binding a metric to a data source (`custom_extensions`)

OSI describes *what* a metric is; it does not say which onboarded data source
or table it runs against, or which ontology classes it governs. That
accelerator-specific metadata travels in the OSI-standard `custom_extensions`
list on each metric, under `vendor_name: COA`:

```yaml
osi_spec_version: "1.0"

datasets:
  - name: public.orders
    data_source_id: ds-warehouse-prod

metrics:
  - name: total_revenue
    description: "Total revenue from completed orders"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SELECT SUM(CASE WHEN status = 'completed' THEN amount ELSE 0 END) FROM public.orders"
    ai_context:
      synonyms: ["total sales", "gross revenue"]
      instructions: "Use for questions about revenue or sales totals."
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-warehouse-prod   # required
          source_table: public.orders          # required
          unit: USD
          return_type: decimal
          time_dimension: month
          ontology_concepts:
            - Revenue
            - FinancialMetric
```

| Field               | Required | What it controls                                                                                                   |
| ------------------- | -------- | ------------------------------------------------------------------------------------------------------------------ |
| `data_source_id`    | yes*     | The onboarded data source the metric is bound to. Must be `APPROVED` or `COMPLETED`.                               |
| `source_table`      | yes*     | The table the expression is evaluated against. Checked against the source's catalog when it can enumerate tables.  |
| `unit`              | no       | Display unit shown alongside results (e.g. `USD`, `count`, `percent`).                                             |
| `return_type`       | no       | Scalar type of the result (e.g. `decimal`, `integer`).                                                             |
| `time_dimension`    | no       | Default time grain: one of `day`, `week`, `month`, `quarter`, `year`.                                              |
| `ontology_concepts` | no       | Ontology classes this metric governs (`:governedMetricFor`). Validated against the published ontology by Check 6.  |

\* If the document has exactly one `datasets` entry, `data_source_id` is
inferred from it, and a missing `source_table` defaults to the metric name. Both
fallbacks are reported as warnings in the import response; declare them
explicitly.

Vendor-prefixed top-level keys such as `x_coa:` are **not** part of OSI and are
ignored. The importer emits a warning naming the key when it sees one, and the
metric lands without its COA metadata (in particular, without an ontology
binding, so Check 6 has nothing to validate). If you have OSI files written in
that older shape, move the block under `custom_extensions` as shown above;
`GET /namespaces/{ns}/export-osi` always writes the `custom_extensions` form
and is a convenient way to see the expected layout for existing metrics.

## How Metrics Are Used in Queries

When a user asks a question that names a metric, the query engine:

1. **Tier 1 (Metric Resolution)**: matches the question to the metric via
   semantic similarity over names and synonyms
2. Checks that the metric accounts for the **whole** question
3. Retrieves the metric's SQL expression and executes it **verbatim** through
   the SQL Firewall (enforcing table/column access controls)

Tier 1 does **not** rewrite the metric's SQL from your wording. A question that
names a metric *and* narrows it — *"What was total revenue **last quarter**?"*
— is a partial match: executing the stored expression would return the
unfiltered total as though it answered the narrower question. Tier 1 therefore
declines it and lets **Tier 2 generate the SQL**, which can express the time
filter. The two supported ways to keep such a question on the deterministic
metric path are `options.dimensions` (bind the filter as a parameter) and
`options.tierOverride: 1` (explicit instruction). See
[Questions carrying a qualifier fall through to Tier 2](serve.md#questions-carrying-a-qualifier-fall-through-to-tier-2)
in the Serve guide for the full routing rules.

**Declining is not discarding.** When Tier 1 steps aside, Tier 2 receives your
metric's expression, description and declared dimensions as authoritative
context, along with the part of the question Tier 1 could not apply. Tier 2
extends your definition instead of re-deriving the calculation from the schema,
so a question that falls through still starts from the governed formula — the
point of authoring it centrally. The `t1.metric_match` trace step reports
`governedDefinitionForwarded: true` when this happened.

This is also why the **description** field is worth writing properly: it is the
only place the *meaning* of the metric travels, and Tier 2 reads it alongside the
SQL when extending your definition. An expression alone does not say that
`active_customer` means operational recency rather than account status.

## Managing Metrics

| Operation | API | Description |
|-----------|-----|-------------|
| List | `GET /namespaces/{ns}/metrics` | All metrics in the namespace |
| Get | `GET /namespaces/{ns}/metrics/{name}` | Single metric detail |
| Update | `PUT /namespaces/{ns}/metrics/{name}` | Modify expression, description |
| Delete | `DELETE /namespaces/{ns}/metrics/{name}` | Remove a metric |
| Validate | `POST /namespaces/{ns}/metrics/validate` | Check without saving |

## Best Practices

- **Descriptive names**: use `snake_case` names that clearly state what's measured
- **Rich descriptions**: include units, time granularity, and business context — these help the AI match questions to metrics
- **Validate first**: always validate before creating to catch schema issues early
- **One metric per calculation**: avoid combining multiple business concepts in a single metric expression
