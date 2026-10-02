# Databricks SQL Warehouse connector — design notes

Why this connector is built the way it is, and what was measured rather than assumed. None of it is
needed to deploy or operate the connector — [`README.md`](README.md) is the runbook, and it is
self-contained. Read this when you are changing the connector, reviewing it, or asking why some
decision went the way it did.

## Contents

- [Why one deployment is one endpoint](#why-one-deployment-is-one-endpoint)
- [One jar, two modes, and where the seam had to go](#one-jar-two-modes-and-where-the-seam-had-to-go)
- [Why nullability is `@notnull` and not `@nullable`](#why-nullability-is-notnull-and-not-nullable)
- [Why identifiers must be bare and lower-case](#why-identifiers-must-be-bare-and-lower-case)
- [Driver licence](#driver-licence)
- [Push-down: what is and is not advertised](#push-down-what-is-and-is-not-advertised)
- [Measured performance and cost](#measured-performance-and-cost)
- [What it does under the hood](#what-it-does-under-the-hood)

## Why one deployment is one endpoint

The UC catalog cannot travel in a request, and that is structural rather than a simplification. An
Athena federated catalog has exactly **one** namespace level below the registered catalog name, and
this connector spends it on the UC *schema* — so `catalog.schema.table` addressing has nowhere left to
put the UC catalog. Flattening it into a `catalog__schema` schema name was rejected: that name appears
in neither Databricks nor the ontology, so nobody could look it up.

`ConnectionConfigProvider` is nevertheless an interface. It resolves the configuration for the Athena
catalog name a request arrived under, and the mode this section describes reads the environment once
and ignores the argument, because one deployment is one endpoint. The seam exists because Athena passes
the registered catalog name to a connector verbatim on every call path, and that name is the
discriminator a multiplexed deployment resolves on.

**The multiplexed deployment now exists**, and the prediction the seam was built on held: it is a second
implementation of that interface plus a second credential path, not a reshaping of every call site. What
it did cost is described below — and the UC-catalog-cannot-travel argument above is untouched by it,
because in that mode the UC catalog comes from the source's own parameter rather than from a request.

## One jar, two modes, and where the seam had to go

One artifact serves both the deployment shape [`README.md`](README.md) documents and the one COA
operates: one build, one test matrix, one release, and a deployed single-endpoint stack that pulls a
newer jar changes behaviour in no way at all. Two modes and not four, because only two combinations are
valid. Parameter Store facts with a direct secret read would mean COA holding a durable read on every
customer's credential, which is the option credential custody rejected, and environment facts with an
assume has no caller.

### A deploy picks its mode from `DATABRICKS_CONFIG_SOURCE`

One CDK app, one entry point, two stack classes. `bin/app.ts` reads `DATABRICKS_CONFIG_SOURCE` once at
synth and builds either the single-endpoint stack or the multiplexed one, both consuming the same shared
connector factory and the same shared alarms, so the branch costs no duplicated resource code. The same
variable the app then sets on the Lambda, so synth and runtime cannot disagree about the mode, and it is
the same name the operator already has to reason about rather than a second selector beside it.

Unset means `environment`, as in the jar, so a stage-1 deployment is unchanged. An unrecognised value is
refused rather than defaulted: the two modes require different variables, and defaulting would report the
typo as a missing coordinate.

Neither branch can express the other's shape — the single-endpoint one sets no `COA_*` variable and the
managed one reads no endpoint coordinate — so the CDK layer still cross-validates nothing. The jar keeps
its own reader of the same variable, because the environment a *deployed* function carries is a separate
question from what a synth was asked for.

### The dangerous environment is the inverse, and the jar is what refuses it

A COA-operated connector left in `environment` mode with the single-target variables present ignores the
catalog name entirely, so every namespace's catalog resolves the one workspace and one credential that
function was given and namespace A's query returns namespace B's rows with nothing erroring.

**The per-request catalog re-check is no defence against that**, and the reason is worth stating
plainly: in `environment` mode nothing is bound to a catalog to re-check against. Which is why the check
reads the function's own environment rather than the request path, refuses at initialisation rather than
per request, and runs in both directions with two different messages. It is the guard that survives a
variable edited onto a deployed function by hand or written by a deployment tool other than this app,
which is the only route left once each branch reads only its own mode's variables.

One synth-time assertion stays in the single-endpoint branch, and it guards a *name* rather than a mode: a
`FUNCTION_NAME_PREFIX` ending in the reserved `-managed-` segment is refused, because such a deploy
would update COA's own managed stack in place and keep its function ARN, carrying no `COA_*` variable for
anything else to notice.

### The cache is the one place a defect returns the wrong tenant's rows

A shared connector serving several tenants from one container is a multi-tenant request processor, and
everything else it does is either per-request or immutable. So the configuration cache carries the
Athena catalog name it was resolved for **inside the cached value**, and re-checks it on every use
rather than only at insert; a mismatch fails the request rather than serving it. Keying the map is not
accepted as sufficient, because a keying defect is exactly what the check is for.

That is also why `ConfigCache.put` takes the key separately from the value. Production always files an
entry under the catalog it was built for, so the check can never fire — and a check that cannot fire is
indistinguishable from a check that is not there. The two-argument signature is what lets a test file a
mismatched entry and prove the refusal is real.

### The credential path holds no durable grant, and the ExternalId is a shared derivation

In `coa-managed` mode the connector's role grants `sts:AssumeRole` and **nothing on Secrets Manager or
KMS**. It assumes a customer-owned role and reads the secret as that session, so whether the secret sits
beside the role, in a third account, or anywhere else is settled entirely by the customer's two
policies and there is no branch here either way. The customer can revoke without asking COA, and COA
mutates nothing — no resource policy to read-modify-write on an API with no ETag.

Every customer's role trusts the same connector role and a role ARN is not a secret, so the
`sts:ExternalId` is mandatory rather than optional: without it a steward in namespace A could register a
source naming namespace B's role and secret, and COA — holding the permission and told to use it —
would read B's credential and open a session to the warehouse host in A's own configuration.

**That value is one contract expressed in two languages.** COA's UI shows the customer what to put in
their trust policy and computes it in Python; the connector computes it again in Java. It is
`RESOURCE_PREFIX` concatenated with the namespace id, **both verbatim**, and nothing else — the prefix's
trailing hyphen is part of the value, no case is folded, no separator is inserted, and neither operand is
trimmed. Each of those is a normalisation a reader might think is tidying up, and any one of them fails
every onboarding with a plain `AccessDenied` and nothing in it to point at. The Java side spells the
Python function out in a comment and the test asserts against a literal rather than against the
derivation, so the two cannot drift together.

**Trimming was there, and removing it is not the same as leaving whitespace alone.** The Java trimmed
both operands while Python trims neither, so a `RESOURCE_PREFIX` carrying stray whitespace produced two
different strings — and, because the value the customer pastes into their trust policy is the *Python*
one, the mismatch surfaced as `AccessDenied` on every query rather than as a bad deployment. Trimming is
the tempting fix and it is the wrong direction: it makes the connector *succeed* on an input the
authority got wrong. So the connector **refuses** whitespace instead, at initialisation for the prefix
and at the request for the namespace, with a message that says the value must match Python's derivation
byte for byte. Being stricter than the authority is safe; being looser is not.

**One property of the derivation is weaker than it reads, and is worth stating rather than relying on.**
The concatenation carries no separator, so `prefixA + namespaceX` can equal `prefixB + namespaceY`
whenever one prefix is a strict prefix of the other — `coa-dev-` + `x` and `coa-` + `dev-x` are the same
eleven characters. So carrying the deployment's prefix makes replaying one deployment's ExternalId
against another's trust policy **improbable rather than impossible**, and it is not what bounds it. What
bounds it is that the namespace id is **server-minted and UUID-shaped**: no caller chooses one, and no
realistic pair of deployment prefixes collides with a UUID on the other side. It is deliberately not
fixed by inserting a separator, because this derivation is shared with Python and cannot change on one
side; a test pins the collision so that nobody "fixes" it in the Java alone.

### The seam the inherited read loop forced

`athena-jdbc`'s `JdbcRecordHandler` takes a connection factory as a constructor argument, stores it in a
private final field, and calls `jdbcConnectionFactory.getConnection(getCredentialProvider(...))` inside
`readWithConstraint` — verified in the 2026.33.1 bytecode, and *before* it calls `buildSplitSql`, which
is the only per-request hook the class offers. So the factory is fixed for the container's life, is
consulted per request, and receives nothing that identifies the request. A multiplexed connector needs
all three of those to be otherwise.

Overriding `readWithConstraint` outright means reimplementing the read loop and its typed extractors for
eleven Arrow types, which is the whole reason this connector extends `athena-jdbc`. One handler per
catalog means the entry point can no longer be a single class name in the Lambda's configuration. So the
request's catalog travels across the `super` call in a **`ThreadLocal`**, bound in the override and
released in the same statement's `finally`.

A field would not do, and the difference is not stylistic: a ThreadLocal is confined to the invocation's
own thread, whereas a field is the hidden mutable handler state that is safe only by accident of the
Lambda runtime serialising invocations per container — the same thing the toolkit's `catalog` parameter
was added to eliminate. The binding's lifetime is the dynamic extent of one `super` call on one thread,
and every hop between the bind and the read is a plain synchronous method call. An unbound
`getConnection` fails rather than choosing an endpoint, because with several sources behind one function
the choice would be another tenant's warehouse.

Two other things that were fixed at construction had easier answers. The query builder is held per
**Unity Catalog** catalog rather than per Athena catalog — that is all a builder depends on, and two
Athena catalogs resolving the same UC catalog want the same one. And the `DatabaseConnectionConfig` the
base class demands turns out to be read exactly twice in that release: `getEngine()`, at construction,
and `getSecret()` from `getCredentialProvider()`, which returns null because the config names no secret. Its
catalog and JDBC URL are read by nothing, so they are inert placeholders spelled
`resolved-per-request` rather than a plausible-looking value that would be a lie the first time
something did read it.

## Why nullability is `@notnull` and not `@nullable`

Athena's `Column` type has no nullability field and `DESCRIBE` returns name, type and comment, both
confirmed live, so the protocol cannot carry the fact. The comment channel can, and the connector
already has the answer: `is_nullable` is a column of the `information_schema.columns` row it reads for
types and comments anyway, so carrying it costs no extra query.

Whether it is worth carrying turns on who reads it, and the answer is not "nobody". Nothing in the
ontology engine reads nullability — only a declared primary key drives `NOT_NULL`, and this connector
delivers declared primary keys — but **the review UI renders a Nullable column per column**, and both
sub-types that cannot discover the fact hardcode nullable. So a steward reviewing such a source sees
"Yes" against every column whether or not it is true: a uniform inaccuracy shown to the person whose job
is to catch inaccuracies.

**Tagging the exception rather than the rule is the whole design.** `@notnull` is operand-free like
`@pk`, and there is deliberately no `@nullable`. Absence has to keep meaning *unknown*, because every
connector deployed before the tag existed emits none and COA defaults a column to nullable — a spelling
that made absence mean "nullable" would silently reinterpret every already-deployed connector's columns
as asserted rather than unstated. `@notnull` leaves the default where it is, matches the SQL keyword,
and makes the parser change purely additive.

Two consequences follow. A column whose `is_nullable` cannot be interpreted emits **nothing**, so it is
indistinguishable from a nullable one downstream — guessing `NOT NULL` would assert a constraint Unity
Catalog never declared, and guessing nullable is indistinguishable from silence anyway. And the tag joins
the channel a customer can write into: `COMMENT ON COLUMN c IS '@notnull'` is exactly as available to
anyone holding `MODIFY` as `'@pk'` is, and marks a nullable column non-nullable for every consumer
downstream. So the connector strips it out of the comment it forwards, alongside `@pk` and `@fk(...)`,
and emits it only from `information_schema` — see
[Comment tags are the connector's channel only](#comment-tags-are-the-connectors-channel-only).

The emitted order is fixed and pinned by a test: prose, `@pk`, `@notnull`, then the `@fk(...)` list. The
operand-free tags come first so the `@fk` list stays last and contiguous, and `@notnull` goes *after*
`@pk` so a comment for a column that is only a primary key is byte-identical to what the encoder emitted
before the tag existed.

## Why identifiers must be bare and lower-case

`DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` must both match `^[a-z_][a-z0-9_]*$` after folding. That
is stricter than Unity Catalog, though less so than it first appears: measured, UC **rejects** a table
name containing a space, period or forward slash outright, and Delta rejects a space in a column name
unless Column Mapping is enabled. What UC does allow, and what therefore has to be quoted in generated
SQL, is a hyphen or a reserved word — Databricks says so itself:
`[INVALID_IDENTIFIER] The unquoted identifier odd-name-table is invalid and must be back quoted`.

**The reason for refusing such a name is Athena rather than Databricks.** Athena's `SHOW`/`DESCRIBE`
parser accepts backticks and rejects double quotes, while its `SELECT` parser does the reverse — so a
name needing quotes is not addressable through both. Refusing it is the only option that cannot
mis-address a table at query time.

The fold is applied because Unity Catalog stores catalog, schema and **table** names lower-cased. It
does **not** lower-case **column** names — see
[Table names are lower-cased; column names are not](#table-names-are-lower-cased-column-names-are-not).
The same fold is applied independently in the CDK app, so `DATABRICKS_SCHEMA=Sales` cannot deploy
cleanly and then fail every query against a connector that only answers to `sales`.

## Driver licence

**This connector bundles `com.databricks:databricks-jdbc:3.4.2`, which is Apache-2.0 licensed and may
be redistributed in a Lambda deployment package.**

Read that version number carefully, because Databricks publishes **two different drivers under the
same `groupId` *and* the same `artifactId`**, distinguishable only by version:

| Versions | Driver | Licence | Redistributable here |
| --- | --- | --- | --- |
| `3.x`, and `0.9.x-oss` / `1.0.x-oss` before it | Open-source Databricks JDBC driver, [github.com/databricks/databricks-jdbc](https://github.com/databricks/databricks-jdbc) | **Apache License 2.0** | **Yes** |
| `2.6.x`, `2.7.x`, `2.8.x` | Simba-derived Databricks JDBC Driver | [Databricks JDBC Driver License](https://databricks.com/jdbc-odbc-driver-license) | Not evaluated — do not use |

`2.8.3` is *numerically lower* than `3.0.1` but is the proprietary line, and a Maven version range or
a well-meaning dependency bump could cross between them without anyone noticing. The version is
pinned in a named property in `pom.xml` with that warning attached.

Verified from the artifacts themselves, not from documentation: the `3.4.2` POM declares
`Apache License, Version 2.0` and points at the GitHub repository; the `2.7.3` POM declares
`Databricks JDBC Driver License`. The `3.4.2` jar contains no Simba code and relocates every bundled
dependency under `com.databricks.internal.*`, so it introduces no class conflicts with the federation
SDK's Arrow.

`athena-jdbc` and the federation SDK are both Apache-2.0. Attribution files (`META-INF/LICENSE`,
`META-INF/NOTICE`) are preserved in the shaded jar.

## Push-down: what is and is not advertised

**This connector always advertises `filter,limit,topn`, which are exactly the three its query builder
implements**, and, measured against a live warehouse, the advertisement changes nothing observable.
Advertising is not what causes push-down; it is what tells Athena it may stop re-applying the same
optimisation itself. This section was rewritten twice: once after the end-to-end pass, because the
premise it previously rested on turned out to be false, and again when what ships went from silence to
these three.

### What was measured

A deployed connector, a registered Athena catalog, and the same six query shapes run twice: once with
an empty capability map and once advertising `filter,limit,topn`. Evidence from two independent
channels — the statements Databricks recorded in `system.query.history`, and the connector's own
`Read N rows` log line.

With **nothing advertised**, the statements that reached the warehouse were:

```
SELECT `region_code`, `order_num` FROM `workspace`.`coa_dbx_test`.`orders` WHERE (`region_code` = ?)
SELECT `order_num`               FROM `workspace`.`coa_dbx_test`.`orders` LIMIT 1
SELECT `order_num`               FROM `workspace`.`coa_dbx_test`.`orders` LIMIT 3
SELECT `order_num`               FROM `workspace`.`coa_dbx_test`.`orders` ORDER BY `order_num` DESC NULLS LAST LIMIT 2
```

The predicate, the `LIMIT` and the top-N `ORDER BY ... NULLS LAST LIMIT` were **all** pushed into the
warehouse with the capability map empty. Advertising `filter,limit,topn` produced **byte-identical
statements** and identical row counts across all six shapes.

| Query shape | Rows off the warehouse, nothing advertised | Advertising `filter,limit,topn` |
| --- | --- | --- |
| `WHERE region_code='EU'` (2 of 5 rows) | 2 | 2 |
| `WHERE region_code='APAC'` (1 of 5) | 1 | 1 |
| `LIMIT 1` | 1 | 1 |
| `LIMIT 3` | 3 | 3 |
| `ORDER BY order_num DESC LIMIT 2` | 2 | 2 |
| `WHERE region_code='EU' LIMIT 1` | 2 | 2 |

### What that means, and the claim it corrects

**Athena populates `Constraints.getSummary()`, `getLimit()` and `getOrderByClause()` regardless of the
capability map.** Those are part of the base `ReadRecords` payload, not something the advertisement
unlocks. Since this connector's query builder reads all three unconditionally, the predicate and the
limit reach the warehouse either way.

> An earlier version of this section — and the design it came from — claimed that *"Athena pushes
> nothing into a connector that advertises nothing: it requests whole tables and applies the predicate
> and the `LIMIT` itself."* **That is wrong**, at least for simple single-table predicates, integer
> limits and top-N. It is the premise the whole feature was justified on, so it is worth stating
> plainly rather than quietly fixing.

One shape behaves differently and is worth knowing: with **both** a predicate and a limit
(`WHERE region_code='EU' LIMIT 1`), Athena pushes the predicate but **not** the limit — 2 rows, in both
configurations. It applies the limit itself after filtering.

### Why these three are advertised anyway

The measurement says advertising buys nothing *today*. It ships on for what happens if today changes:

- **Without it, push-down rests on undocumented behaviour.** The SDK documents the capability map as the
  way to request push-down. An Athena engine release that begins honouring it strictly would leave a
  silent connector reading every predicate-matching row out of customer-billed compute — no error, just
  a cost and latency regression, and then hard failures once a table passes the row ceiling. Athena's
  engine versions independently of this SDK, so pinning the SDK protects nothing here.
- **The guarantee these three carry is honoured by construction.** The real risk of advertising is
  promising an optimisation the record path drops. This path cannot drop them: `JdbcSplitQueryBuilder`
  applies all three with no reference to the capability map, re-verified at 2026.33.1.
- **Athena's re-application was never the safety net it looked like.** It removes rows the connector
  over-returned; it cannot restore rows a wrong `WHERE` clause excluded at the warehouse. The direction
  that would give a wrong answer was never covered.
- **These three are what every AWS-authored JDBC connector declares**, so the advertised path is the
  one that gets exercised across the fleet.

| Name | Advertises | Measured effect on the SQL |
| --- | --- | --- |
| `filter` | Predicates: comparison, range, `IN`, null checks | None — pushed either way |
| `limit` | `LIMIT n` with an integer constant | None — pushed either way |
| `topn` | `ORDER BY ... LIMIT n` | None — pushed either way |

**The advertisement is fixed in the jar, and that is a trade taken deliberately.** `PushdownCapabilities`
builds one immutable map at class initialisation and there is no environment variable that narrows it.
The cost is that backing the decision out needs a jar release rather than an environment edit, so the
scenario above - a future Athena release honouring the map strictly, combined with a translation defect
in the SDK's own query builder - would be a code change on a release cadence rather than a redeploy.

Judged acceptable on two grounds. The push-down SQL is `JdbcSplitQueryBuilder`'s own code rather than
this connector's, so the defect would be in a dependency this repository pins and bumps deliberately.
And the measurement above shows advertising changes nothing observable today, so the variable's only real
use was re-testing a question the measurement has already answered, which a build of the jar can do just
as well. The cold-start log line reports the set the deployed jar advertised, so what is running is
readable without inspecting the artifact.

**What is still missing is a canary.** Nothing asserts that push-down is still reaching the warehouse:
the test that would is skipped in CI for want of a workspace (R-3), and `ConnectorRowsReturned` cannot
tell a push-down regression from an aggregate-heavy workload. A canary is the real protection against
the scenario above. An advertisement only states an intent; it cannot report whether the intent held.

**Complex-expression push-down is not offered, and a setting could not make it work.** The
connector uses `JdbcSplitQueryBuilder`'s single-argument constructor, which installs
`DefaultJdbcFederationExpressionParser` — whose `mapFunctionToDataSourceSyntax` is an unconditional
`throw` as of 2026.33.1, the latest release: *"Subclass does not yet support complex expressions."*
(verified against that release's published source, not only against the pinned version). Offering a switch for it would guarantee a run-time failure the first
time Athena pushed a function expression, so there is no switch. Supporting it means writing a
Databricks-specific `FederationExpressionParser` that maps each `FunctionName` Athena may send to
Spark SQL's spelling — future work, not configuration.

**Aggregation is not on the list either, and cannot be.** See
[The aggregation caveat](README.md#the-aggregation-caveat).

## Measured performance and cost

From the end-to-end pass against a Serverless PRO **Small** warehouse (10-minute auto-stop) and a
5-row `orders` table, so these are floor figures — they measure orchestration, not data volume.

| Measurement | Value | Source |
| --- | --- | --- |
| Lambda init (cold start) | **1.44 – 1.51 s** (4 samples) | Lambda `Init Duration` |
| Lambda duration, metadata call | 2 – 5 ms | Lambda `REPORT` |
| Lambda duration, row read | 0.55 – 1.34 s | Lambda `REPORT` |
| Peak Lambda memory | **281 MB** of 3008 MB | Lambda `REPORT` |
| Athena planning | 0.53 – 0.78 s | `QueryPlanningTimeInMillis` |
| **Athena total, warm Lambda + warm warehouse** | **8.3 – 12.7 s** | `TotalExecutionTimeInMillis` |
| Athena total, cold Lambda | ~18 s wall clock | measured end to end |
| Warehouse statement duration, average | 784 ms | `system.query.history` |

**The warm end-to-end figure is the surprising one, and it does not meet the design target.** The LLD
budgets p50 3 s / p95 6 s for a warm single-source query; the measured floor on a five-row table is
8.3 s, with 12.7 s seen. The time is not in the Lambda (milliseconds for metadata, under 1.4 s for the
read) and not in Athena planning (under 0.8 s) — it is Athena's federation orchestration, which makes
a separate Lambda invocation per protocol step and schedules each one. **Anyone quoting a latency
budget for this route should start from ~8 s, not ~6 s, and treat it as roughly independent of table
size.** Memory is heavily over-provisioned at 3008 MB for peak 281 MB; it is sized for the aggregate
read described in [The aggregation caveat](README.md#the-aggregation-caveat), not for these queries.

### Cost, and why a per-query DBU figure is the wrong unit

No measured DBU figure is available: `system.billing.usage` lags by hours and had no rows for the test
window. `system.billing.list_prices` is readable and gives **$0.70 per DBU** for US East/West serverless
SQL.

What the pass does establish is that **for a sparse query pattern the auto-stop tail dominates, not the
rows scanned.** The whole exercise — 135 statements including a full discovery pass — consumed **105.9 s**
of warehouse statement time. A single query keeps a 10-minute-auto-stop warehouse alive for 600 s. So
the billed unit for one occasional COA question is ten minutes of warehouse uptime, roughly **6× all the
statement time this entire test generated**.

That matters because it inverts the advice for two different usage patterns:

- **Sparse/interactive** — a steward asking occasional questions. Cost is dominated by resumes and idle
  tails. Push-down is irrelevant; a shorter auto-stop, or accepting the resume latency, is the lever.
- **Dense/aggregate** — the case [The aggregation caveat](README.md#the-aggregation-caveat) describes.
  Cost is dominated by rows read out of the warehouse, and the row ceiling is the lever.

The connector's own statement profile, for sizing a discovery pass:

| Statement | Count | Average |
| --- | --- | --- |
| Row reads (`SELECT`) | 100 | 826 ms |
| `SHOW TABLES` | 26 | 230 ms |
| `information_schema.columns` | 1 | 8610 ms (first, cold metastore) |
| `information_schema.tables` | 2 | 1771 ms |
| Constraint reads (PK + FK) | 6 | ~850 ms |

Note the first `information_schema.columns` read cost **8.6 s** against a cold metastore and dropped to
sub-second afterwards — so the first table of a first scan is far slower than the rest, which is worth
knowing before concluding a scan has hung.

## What it does under the hood

### Enumeration uses `SHOW TABLES`, not `information_schema.tables`

Creating a materialized view or a streaming table **also creates internal side tables**, and
`information_schema.tables` lists them as ordinary user tables:

```
__materialization_mat_96ea77da_..._order_counts_mv_1   MANAGED
event_log_96ea77da_...                                 MANAGED
```

Measured on a small fixture schema: `information_schema.tables` returned **sixteen** rows where
`SHOW TABLES` returned **eleven**. Four of the five extra rows are those internal side tables and the
fifth is a shallow clone. Left in, the side tables would be discovered, enriched by an LLM, and
presented to a steward as real tables — a quarter of the ontology being Databricks' own bookkeeping.
**No column of that view distinguishes them**; across all fifteen of its columns they are shaped
exactly like a genuine `MANAGED` table.

`SHOW TABLES IN <catalog>.<schema>` omits them, so it is the authority on what exists.
`information_schema.tables` is still read for each object's `table_type`, and the answer is the
intersection. Filtering on the `__` and `event_log_` name prefixes was rejected: undocumented internal
naming, and a customer table legitimately called `event_log_2026` would vanish.

### `table_type` is never `'BASE TABLE'`

The ANSI spelling does not exist in Unity Catalog. Databricks returns `MANAGED`, `EXTERNAL`, `VIEW`,
`MATERIALIZED_VIEW`, `STREAMING_TABLE`, `FOREIGN`, `MANAGED_SHALLOW_CLONE` and
`EXTERNAL_SHALLOW_CLONE`.

This connector exposes the first five. `FOREIGN` is excluded because it is a table federated into Unity
Catalog from somewhere else, and reading it through two federation layers is slower and less faithful
than onboarding its own source; the two shallow-clone types are excluded because their rows duplicate
another table's, and including them would put the same facts in the ontology twice under two names.

The failure mode of getting this wrong is worth knowing, because it is not the obvious one. A filter of
`table_type IN ('BASE TABLE', 'VIEW')` does not return nothing: `'VIEW'` *is* a Databricks value, so
measured on that fixture schema it returns **one** row. A steward sees a source that scanned
successfully and contains views but no tables, which reads as a permissions problem rather than as a
dialect bug.

### Table names are lower-cased; column names are not

Measured, and neither half is documented:

- `CREATE TABLE MixedCaseTable` → `information_schema.tables.table_name` = `mixedcasetable`
- `CustomerName STRING` → `information_schema.columns.column_name` = `CustomerName`

So catalog, schema and table names are compared against lower-case literals, and a **column name is
carried through exactly as returned**. Lower-casing it would generate SQL naming a column that does not
exist.

### `ordinal_position` counts from a different base in different views

Measured: `information_schema.columns.ordinal_position` is **0-based**;
`information_schema.key_column_usage.ordinal_position` is **1-based**. Both are only used for
`ORDER BY` here, which is base-agnostic. Joining the two views on `ordinal_position` would be off by
one — silently, pairing each key column with its neighbour.

### Constraints use the ANSI join

The obvious join — `key_column_usage` to `constraint_column_usage` on `constraint_name` — produces an
N x N cartesian product for a composite key and pairs child columns with the wrong parents. It is not
obviously wrong, because for a single-column key N x N is 1 x 1 and the answer is right. Measured
against a two-column foreign key it returns **four rows, two of them wrong**.

This connector walks `referential_constraints` to the referenced constraint and matches
`kcu.position_in_unique_constraint = ref.ordinal_position`, which is the ANSI-defined pairing and the
only one correct for a composite key. It was verified live, once, by hand — the record is the connector
LLD's §8.1. **Nothing in the committed test suite asserts it**, because the integration suites that did
were removed with the workspace they needed; see [Tests](README.md#tests). So the reason for the
complexity is now asserted in a comment rather than checked, which is exactly the shape of thing that
gets simplified back into a bug. Rebuilding that assertion is the first thing to do on acquiring a
workspace.

### Comment tags are the connector's channel only

A column comment in Unity Catalog is set with `COMMENT ON COLUMN`, which anyone with `MODIFY` can run.
So a comment reading `"customer surrogate key @pk"` would mint a primary key Unity Catalog never
declared, `"@fk(payroll.ssn)"` would assert a relationship into a table that may not exist, and
`"@notnull"` would mark a nullable column non-nullable. By the
time COA sees it, a hand-written tag is byte-identical to a generated one.

This connector therefore **strips any live `@pk`, `@notnull` or `@fk(...)` out of the comment before
forwarding it**, and emits tags only from `information_schema`. Near misses are left alone, because COA
leaves them alone: `@PK`, `@pkey`, `@pk=x`, `@pk(x)`, `@notnullable` and `owner bob@pk.example.com` are
prose on both sides — removing them here would delete text a customer wrote and COA would have stored.

### A tag can become live after an earlier one is removed, and that is why it is disarmed rather than deleted

Two rules that each look obviously right are incompatible, and the resolution is worth recording because
both of the obvious implementations are wrong.

**COA's parser scans the customer's original string**, so a tag preceded by an identifier character is
never live *for it*: in `@notnull@pk`, the `@pk` is prose, and COA reading the raw comment would store
`"@pk"` as the description. The connector's `strip` agrees with it exactly. **But COA parses the string
the connector forwards, not the customer's** — and in the forwarded `"@pk"` the tag *is* live. So
forwarding `strip`'s output unchanged would mint a primary key out of a field anyone holding `MODIFY` can
write, which is the whole thing this section is about. "Match COA's parser exactly" and "never forward a
live tag" cannot both hold under this encoding.

The first fix deleted the token, which cost three characters of a customer's description. The one shipped
instead uses **COA's own rule as the mechanism**: a tag is live only when *not* preceded by an identifier
character, so prefixing it with `_` makes it prose to COA's parser and to the toolkit's encoder alike
while every character the customer typed survives. `@notnull@pk` forwards as `_@pk`. One character added
rather than three removed, no rule changed on either side of the wire, and a disarmed tag satisfies
neither side's liveness test — so the anti-forgery property is exactly as strong.

The loop terminates because adding an identifier character can only *remove* liveness, never add it: each
pass makes one live match dead and creates none, so the count strictly decreases. That argument depends
on the inserted character being an identifier character, which is why it must not be "improved" into a
space or a zero-width character — either would leave the tag live and spin.

**The structural answer, deliberately not taken here.** All of the above exists because the tag channel
and the customer's prose share one unstructured string, so a parser has to scan text it does not own.
Giving the tags their own delimited region — a sentinel the customer's prose cannot contain, with
everything outside it never scanned — would make forgery unexpressible rather than filtered, and would
retire `strip`, `neutralise` and both liveness patterns along with it. It is a change to a grammar shared
with COA's parser and with every already-deployed `CUSTOM_CONNECTOR` connector, so it is a platform
migration rather than this connector's work. Recorded so the next person reaches it by reading rather
than by re-deriving it.

There is a second step, and it is the one that stops a bad comment taking down a scan. COA's parser
*keeps* a malformed tag — an `@fk(` that never closes — as feedback to whoever wrote it, so the strip
step keeps it too. But the toolkit's encoder refuses **any** `@fk(` in prose, closed or not, and
refuses by throwing: its guard exists to stop a *connector author* hand-writing a tag. Here the prose
is a *customer's*, which COA cannot ask to be corrected and whose author will never see the complaint.
A single Unity Catalog comment of `'Line total @fk(orders.order_id'` was therefore enough to fail that
table's `DESCRIBE` permanently and take the whole schema's scan with it. So the connector neutralises
the surviving token, keeps the prose, and logs it.

### Credentials never touch the JDBC URL

The Databricks JDBC URL is a `;`-delimited property list, so anything concatenated into it can be split
by a `;`: an injected `SSL=0` downgrades TLS, `ProxyHost` redirects an authenticated session, and
`LogPath` with `LogLevel=6` writes connection details — credentials included — to the Lambda's disk.

The URL this connector builds is exactly `jdbc:databricks://<host>:443`. Everything else — HTTP path,
catalog, schema, credential, every hardening property — is set on a `java.util.Properties` object,
which the driver reads as a map and never parses. The configuration patterns are the second layer.

### Five driver defaults are overridden

| Property | Default | Here | Why |
| --- | --- | --- | --- |
| `TemporarilyUnavailableRetry` | `1` | `0` | Retries a stopped warehouse for up to 900 s, outliving even the 600 s invocation timeout |
| `socketTimeout` | 900 s | 90 s | Same: a timeout above the invocation timeout can never fire |
| `EnableTelemetry` | `1` | `0` | The driver reports usage to Databricks by default |
| `LogLevel` | OFF | `0`, pinned | Driver logging is how a connection string containing `PWD=` reaches disk |
| `IgnoreTransactions` | `0` | `1` | Makes the inherited read loop's `setAutoCommit(false)` and `commit()` no-ops instead of two extra warehouse round trips per read |

### Logging

The module ships an SLF4J binding (`slf4j-simple`) with levels pinned in
`src/main/resources/simplelogger.properties`. Without a binding, every log line from the connector
**and from the federation SDK** is discarded — the reference connector prints
`No SLF4J providers were found` on every invocation.

Enabling logging is a data-exposure change, not pure operability, so the levels are pinned rather than
defaulted: the connector at INFO, and the federation SDK and `athena-jdbc` at WARN, because the SDK's
request logging and `JdbcSplitQueryBuilder`'s statement logging carry constraint literals derived from
the user's question. The connector logs one cold-start line naming the endpoint and one row count per
read, and never the credential, the JDBC URL, or a predicate value. Every error message is passed
through a redactor that replaces the value of any credential-bearing property.

The Databricks driver is a separate case: it bundles its **own relocated** SLF4J bound to a
`java.util.logging` provider, so `simplelogger.properties` cannot reach it. `LogLevel=0` on the
connection is the control that works.

`JAVA_TOOL_OPTIONS` cannot be used for any of this — the CDK construct owns it outright and refuses a
stack that sets it.

### What is inherited from `athena-jdbc`

The **record path**: `JdbcRecordHandler`'s read loop and its typed per-column extractors for eleven
Arrow types, and `JdbcSplitQueryBuilder`'s translation of Athena's constraint model into a prepared
statement — projection list, `WHERE` from each column's value set, typed parameter binding,
identifier quoting, `ORDER BY` and `LIMIT`.

The **metadata path is not inherited**, because it builds a table's schema from JDBC result-set
metadata, which carries neither comments nor constraints, and never queries the catalog for them.
Comments are the channel declared keys travel through, so a metadata handler that cannot read them
cannot deliver the feature.

The **multiplexing handlers are not inherited** either. They route on the catalog name exactly as a
multi-endpoint deployment would, but they build their routing table once at construction from
environment variables named per catalog — against Lambda's 4 KB total environment limit and with a hard
ceiling of 100 catalogs.

The published `athena-jdbc` artifact is a 46 MB shaded uber-jar carrying its own copy of the federation
SDK, Arrow, the AWS SDK, Netty, log4j-core, Bouncy Castle and HikariCP. Unfiltered, every one of those
competes by path with the artifact that legitimately provides it and the shade plugin keeps whichever
it saw first. `pom.xml` filters it to the 39 classes this connector actually extends.
