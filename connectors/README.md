# Athena federation connectors

Connectors that bring COA a data source it has no native support for — SAP,
mainframes, internal REST APIs, proprietary SaaS. A customer or partner deploys one as a
Lambda in their own account, and onboards it in COA by the connector Lambda's ARN. This folder holds
two toolkits, a reference connector to copy, a real connector for Databricks SQL Warehouse, and a
CDK app for each.

> These are **Java/Maven** and **CDK** projects in a workspace of their own, deliberately
> outside the repository's pnpm and uv workspaces and the `models/` Gradle build. They build
> and deploy independently of COA.

## Layout

```
connectors/
  pom.xml            Maven aggregator: Java level, SDK version, fat-jar config
  pnpm-workspace.yaml  its own workspace, so N CDK apps share one aws-cdk-lib
  .env.example       deployment facts: serve role ARN, region
  toolkit/           dev.coa:coa-connector-toolkit - CoaMetadataHandler, CoaTable, CoaColumn,
                     and the two classes underneath: ColumnComment, TableSchema
  cdk-toolkit/       coa-connector-cdk - the AthenaFederationConnector construct and env helpers
  example/           the reference connector; copy it to start your own
    pom.xml
    src/
    cdk/             the example's OWN CDK app - bin/app.ts, lib/example-connector-stack.ts
  databricks/        a real connector: one Databricks SQL Warehouse over JDBC, with declared
                     PK/FK read from Unity Catalog's information_schema. Its own README and
                     DESIGN, its own CDK app
```

**[`databricks/README.md`](databricks/README.md)** is the one to read after this file if you want to
see the contract met against a real external system rather than a fabricated one: a bundled JDBC
driver and its licence trap, a credential in Secrets Manager with two auth modes, `information_schema`
overrides, and measured latency and cost. `example/` is still the one to copy.

Each connector is a folder whose **name is its id**: its stack and Lambda derive from it, so
two connectors deployed into one account cannot collide.

**Each connector owns its CDK app.** The toolkits are libraries, not a framework: you compose
`AthenaFederationConnector` inside a stack you control, and add whatever else your source needs — an
RDS proxy, a secret, a KMS key — without asking permission from any code here. A VPC is the one thing
the construct takes itself, as its `network` prop, because the function has to be attached to it. A shared app
driven by a config file could only ever deploy the resources somebody anticipated.

## What a connector is

An Athena federated connector is a Lambda implementing two handlers:

- **`MetadataHandler`** — schemas, tables, table schemas, splits.
- **`RecordHandler`** — reads rows from the source and returns them as Apache Arrow.

A `CompositeHandler` wires both into one Lambda. Athena invokes it at query time; there
is no connector code in COA, which only *consumes* the resulting Athena catalog.

## Quick start

The whole path, for someone who wants to start rather than read. Each step has a section below.

```bash
cd connectors
cp -r example my-source                       # 1. your connector's folder name is its id
$EDITOR my-source/pom.xml                     #    change <artifactId>, and add the module to pom.xml
$EDITOR my-source/cdk/package.json            #    change "name", and -pl example -> -pl my-source
```

2. Implement three methods in your metadata handler, subclassing `CoaMetadataHandler`:
   `listDatabases(catalog)`, `listTables(catalog, database)`,
   `describeTable(catalog, database, tableName)` — the last returns a `CoaTable` of `CoaColumn`s. All
   three are `abstract`, and all three take the Athena catalog name; a single-source connector ignores
   it. Then a `RecordHandler` that writes rows, and a `CompositeHandler` wiring the two together.
   `example/src/main/java/dev/coa/example/` is all three, working.

3. Put the two COA role ARNs in `.env` and export your region:

```bash
cp .env.example .env       # SERVE_ROLE_ARN and DISCOVERY_ROLE_ARN, read from SSM — see below
export AWS_REGION=<region>
pnpm install
```

4. Check, then deploy:

```bash
mvn -B test -pl my-source -am
cd my-source/cdk && pnpm test && pnpm exec cdk synth
pnpm run deploy
```

5. **Onboard it in COA by the connector Lambda's ARN**, as a `CUSTOM_CONNECTOR` source — COA registers
   the Athena data catalog itself, in its own account, under a name it derives. You do not create that
   catalog and you cannot choose its name. See
   [Custom Connector Sources](../external-docs/content/custom-connector-sources.md) for the request
   body.

6. Optionally register your *own* Athena catalog, in your own account, to run step 7's self-check
   there — see
   [Register a catalog to check the connector yourself](#register-a-catalog-to-check-the-connector-yourself).

7. Confirm the keys arrived, which is the one thing that fails silently:

```sql
DESCRIBE my_source.my_db.my_table;   -- the comment column must show your prose plus @pk / @fk(...)
```

The rest of this document is the reference: what each class does, the eleven non-obvious
constraints, spill, and what to check when something returns the wrong answer without an error.

## The two toolkits

**`toolkit/`** — Java, `dev.coa:coa-connector-toolkit`. Use it as a Maven dependency or copy the
files outright:

- **`CoaMetadataHandler`** — subclass this instead of the SDK's `MetadataHandler`. Three methods
  instead of five, none of them mentioning an Athena type.
- **`CoaTable` / `CoaColumn`** — describe your source: names, Arrow types, prose, which columns take
  part in a key, and which are declared `NOT NULL`. Declared as *intent*, not as tag syntax.
- **`ColumnComment`** — encodes the `@pk` / `@notnull` / `@fk` tags. Dependency-free. The base class
  calls it for you; you need it only if you are not using the base class.
- **`TableSchema`** — puts a column comment where Athena actually reads it. Likewise.

The bottom two exist because the thing they encapsulate fails silently when hand-written; the top
two exist so that you never have to know they are there. They form one pipeline, each stage owning
one decision:

```
CoaColumn      declared intent   .describedAs("...").primaryKey().foreignKey("orders", "order_id")
   |
CoaTable       assembly          columns in declaration order — which is the key's column order
   |
ColumnComment  encoding          prose + "@pk @notnull @fk(orders.order_id)", quoting rules applied
   |
TableSchema    placement         that string into the SCHEMA's metadata, keyed by column name
   |
Arrow Schema   what Athena reads
```

Two of those stages are where connectors written by hand go wrong: the encoding, and above all the
placement — a comment on the Arrow *field* is delivered and ignored. Subclass
`CoaMetadataHandler` and the bottom two stages are not yours to get wrong.

**`cdk-toolkit/`** — TypeScript, `coa-connector-cdk`:

- **`AthenaFederationConnector`** — the Lambda, its own spill bucket, and the resource policies the
  COA needs to invoke it and read its spill.
- **`env`** — `.env` loading and the deployment facts read from the environment.

Neither toolkit owns your stack. Both are things you call.

## `example/` — the reference connector

A self-contained connector serving a small fabricated database, `example_source`. No external
system: its purpose is to demonstrate the contract and to be a deterministic
integration-test fixture. Proven end to end — `list_databases`, `list_table_metadata`,
`SELECT` with projection, `JOIN`/`GROUP BY`/`SUM`, predicate push-down, and a
cross-catalog join with `AwsDataCatalog` all return correct rows.

### The fabricated dataset

| Table            | Rows          | Primary key                | Foreign keys                                                   |
| ---------------- | ------------- | -------------------------- | -------------------------------------------------------------- |
| `customers`      | 50            | `customer_id`              | —                                                              |
| `orders`         | 200           | `order_id`                 | `customer_id` → `customers.customer_id`                        |
| `order_lines`    | 600           | `(order_id, line_no)`      | `order_id` → `orders.order_id`                                 |
| `shipment_lines` | 600           | `shipment_line_id`         | `(order_id, line_no)` → `order_lines.(order_id, line_no)`      |
| `bulk_rows`      | env-driven    | `row_id`                   | —                                                              |

`order_lines` and `shipment_lines` exist to exercise the two subtlest shapes of the key
format: a **composite primary key**, and a **composite foreign key** onto it.
`order_lines.order_id` is both a primary-key member and a foreign key, in one comment.
`bulk_rows` exists to make the spill path reachable — see [Spill](#spill).

The tables' shapes — columns, prose and declared keys — are in
`example/src/main/java/dev/coa/example/ExampleMetadataHandler.java`. The rows they serve are in
`ExampleCatalog.java`, which keeps the metadata handler about the contract and nothing else.

### Declared keys travel in column comments

The federation protocol has **no field anywhere** for primary or foreign keys:
`GetTableResponse` carries an Arrow `Schema`, and an Arrow schema describes names and
types only. So declared keys and nullability travel inside the column comments the connector
already emits, as three tags COA parses out and strips:

```
@pk                             this column is a member of the table's primary key
@notnull                        this column is declared NOT NULL
@fk(parent_table.parent_column) this column references that parent column
```

COA's parser is the other side of this format, and nothing here can validate a tag you write
by hand. That is the reason not to write one: the failure mode for a malformed tag is that COA
leaves it in the stored description verbatim, because its warning goes to *its own* logs, not
yours.

Which is why you should not hand-write them. `ColumnComment`
(`toolkit/src/main/java/dev/coa/connector/constraints/ColumnComment.java`) is a
dependency-free, single-file fluent builder — no Athena SDK, no Arrow, no logging — meant
to be copied into your own connector:

```java
ColumnComment.of("Order this line belongs to")
             .primaryKey()
             .foreignKey("orders", "order_id")
             .build();
// -> "Order this line belongs to @pk @fk(orders.order_id)"
```

It applies the rules you would otherwise have to remember:

- **Composite primary key** — `.primaryKey()` on *every* member column. COA collects
  the `@pk` columns of a table in `DESCRIBE` order into one key. There is no ordinal.
- **Composite foreign key** — one `.foreignKey(...)` on *each* participating child column,
  each naming its own parent column. A two-column FK is two tags on two columns, never one
  tag listing two columns.
- **Quoting is automatic and per segment.** `foreignKey("my orders", "customer id")` emits
  `@fk("my orders"."customer id")`; a parent table named `he said "hi"` emits
  `@fk("he said ""hi""".x)` — the embedded quote doubled per SQL, and a three-quote run where it
  meets the closing one. Pass identifiers exactly
  as the source spells them and never pre-quote — a half-quoted segment is a malformed tag.
- **Extra qualification** via `Reference.of("analytics", "public", "orders", "order_id")`.
  COA reads the last two segments as `TABLE.COLUMN` and discards the rest.
- **Duplicate targets collapse**, matching COA, which dedups on the decoded pair.
- **Prose already containing a live tag is refused** at build time. COA would strip it
  and declare a key nobody asked for. Prose that merely *resembles* a tag is fine and is
  left alone by both sides: `@PK`, `@pkey`, `owner bob@pk.example.com`, `@pk=x`, `@pk(x)`,
  `@notnullable`.
- **`.notNull()` tags the exception, and there is no `@nullable` to pair with it.** Call it only for a
  column your source declares `NOT NULL`. Not calling it means *unknown*, which is what COA assumes for
  every connector that emits no such tag — so a column whose nullability you cannot determine gets
  nothing, and that is the correct answer rather than a gap. There is no way to assert "nullable",
  because making absence mean that would reinterpret every already-deployed connector's columns.
- **The emitted order is fixed**: prose, `@pk`, `@notnull`, then one `@fk(...)` per declared reference in
  declaration order. It does not depend on the order you call the intent methods in.

### Subclass `CoaMetadataHandler`

The SDK asks for five methods and all five speak in Athena request and response objects. The
toolkit's base class implements them and asks for three that speak about your source:

```java
public class YourMetadataHandler extends CoaMetadataHandler {
    public YourMetadataHandler(Map<String, String> configOptions) {
        super("your_source", configOptions);
    }

    // Every method takes the Athena catalog name of the request being served. Ignore it in a
    // single-source connector, as this one and the example do; it is there because a MULTIPLEXED
    // connector has no other way to learn which endpoint, credential and configuration a request
    // wants. Without the parameter the only route would be overriding a doXxx method to stash
    // request.getCatalogName() in a field — mutable handler state, correct only because the Lambda
    // runtime serialises invocations per container, and invisible in the signature that needs it.

    @Override
    protected List<String> listDatabases(String catalog) { return List.of("your_db"); }

    @Override
    protected List<String> listTables(String catalog, String database) { ... }

    @Override
    protected CoaTable describeTable(String catalog, String database, String tableName) {
        // No table-level description: no Athena read path surfaces one, so CoaTable
        // deliberately offers no setter for it. Column descriptions do arrive.
        return CoaTable.named(tableName)
            .column(CoaColumn.of("order_id", Types.MinorType.BIGINT.getType())
                .describedAs("Surrogate key for the order")
                .primaryKey())
            .column(CoaColumn.of("customer_id", Types.MinorType.BIGINT.getType())
                .describedAs("Customer that placed the order")
                .foreignKey("customers", "customer_id"))
            .build();
    }
}
```

That is the whole metadata half. Keys are declared as **intent** — `.primaryKey()`,
`.foreignKey(...)` — with no tag syntax, no quoting rules, and no need to know that comments are how
keys travel. `getPartitions` and `doGetSplits` come from the base class's unpartitioned defaults:
one split covering the whole table, overridable for a source that can be read in parallel.

Arrow types stay visible on purpose. Arrow is a type vocabulary, not part of the Athena SDK, and a
parallel COA enum would cost a mapping table while losing decimals and every type added later.

`example/src/main/java/dev/coa/example/ExampleMetadataHandler.java` is that class for real, and
every table it serves is declared inline in it — five tables covering a single-column key, a
composite key, a composite foreign key and a column that is both. It is the file to read when
drafting a connector.

### How intent becomes a comment

`CoaTable.toTableSchema()` is the conversion, and it is worth reading once even if you never call
it — it is two steps, each in one place:

1. `CoaColumn` renders prose plus key intent into a comment via `ColumnComment`, which applies the
   tag syntax, the per-segment quoting and the deduplication.
2. `TableSchema` puts that comment where Athena reads it.

If you write a connector without `CoaMetadataHandler`, that method is the example to copy.

### Assemble the schema with `TableSchema`

A finished comment still has to reach Athena, and Arrow offers two places to put it of which
Athena reads exactly one. `TableSchema`
(`toolkit/src/main/java/dev/coa/connector/schema/TableSchema.java`) is the second file to copy.
It owns a table's columns, types and comments, and renders the Arrow schema:

```java
TableSchema orders = TableSchema.named("orders")
        .column("order_id", Types.MinorType.BIGINT.getType(),
                ColumnComment.of("Surrogate key for the order").primaryKey())
        .column("customer_id", Types.MinorType.BIGINT.getType(),
                ColumnComment.of("Customer that placed the order")
                             .foreignKey("customers", "customer_id"))
        .build();

// in MetadataHandler.doGetTable:
return new GetTableResponse(request.getCatalogName(), request.getTableName(),
        orders.toArrowSchema(), Collections.emptySet());
```

Why it exists rather than assembling a `Schema` by hand: **Athena reads column comments from
the Arrow schema's metadata map, keyed by column name, and ignores metadata attached to an
Arrow `Field`.** Both are valid Arrow, both cross the wire intact, and picking the wrong one
fails silently — see constraint 6. `TableSchema` makes the working placement the only one a caller
can express: a column is named and typed, never handed over as a pre-built `Field`, so the field's
metadata map is built here and is always empty. There is no way to express the mistake.

Only scalar columns, therefore. A `STRUCT`, `LIST` or `MAP` column cannot be declared through the
toolkit — and could not be served either, since `BlockUtils.setValue` has no case for those vectors
(see *What a record handler must write*).

## Build

Java is one Maven reactor; the aggregator pins the Java level, the SDK version and the shade
configuration once, so a connector's own pom is two dependencies.

```bash
cd connectors
mvn -q -B package                     # toolkit + every connector
mvn -q -B package -pl example -am        # the toolkit and one connector
pnpm install                          # the CDK toolkit and every connector's app
```

## Upgrading a connector written against an earlier release

Three changes break a connector built against the previous release. Two are compile-time and one is a
deployment-package change. None of them alters what a working connector does once it is through them.

### The metadata SPI takes a leading catalog parameter

`CoaMetadataHandler`'s three abstract methods now receive the Athena catalog name of the request being
served. Add the parameter to your three overrides; a single-source connector then ignores the value.

Before:

```java
@Override
protected List<String> listDatabases() { ... }

@Override
protected List<String> listTables(String database) { ... }

@Override
protected CoaTable describeTable(String database, String tableName) { ... }
```

After:

```java
@Override
protected List<String> listDatabases(String catalog) { ... }

@Override
protected List<String> listTables(String catalog, String database) { ... }

@Override
protected CoaTable describeTable(String catalog, String database, String tableName) { ... }
```

No no-catalog overload is kept, so every stale override is caught by the compiler: it no longer
implements the abstract method and the class does not build. That is deliberate. Keeping one would let
a connector compile against a signature the base class has stopped calling.

If your handler reached the catalog name by overriding one of the `doXxx` methods purely to stash
`request.getCatalogName()` in a field, delete the field and those overrides with it. The parameter is
what replaces them, and the field was mutable handler state that happened to be correct only because
the Lambda runtime serialises invocations per container.

### The federation SDK moves to 2026.33.1, from 2025.15.1

In order, because step 3's failure is invisible until the function is deployed:

1. **Bump `athena.federation.sdk.version` in `connectors/pom.xml`.** The toolkit and every connector
   inherit it from there, so it is one edit.
2. **If your connector depends on `athena-jdbc`, keep it at the same version as the SDK.** That
   artifact is published as a shaded uber jar carrying its own copy of the SDK and Arrow, so a skew
   puts two SDK builds in one fat jar and the shade plugin keeps whichever it saw first per path.
   `databricks/pom.xml` holds it equal with a property rather than a literal.
3. **Move every direct declaration of the AWS SDK, Jackson and SLF4J to what the new SDK brings**:
   `software.amazon.awssdk:*` to **2.46.17**, `com.fasterxml.jackson.core:jackson-databind` to
   **2.22.1**, `org.slf4j:*` to **2.0.18**. A direct declaration wins over the SDK's transitive one,
   so a stale pin is not overridden for you. It also builds green and passes its unit suite, and the
   deployed function then fails *every* invocation with a `NoClassDefFoundError` on an AWS SDK core
   class the newer service modules expect. Verify one version per artifact before you deploy:

   ```bash
   mvn dependency:tree -pl <id> -Dincludes=software.amazon.awssdk
   ```

   If you declare none of the three, there is nothing to do here.
4. **Fix the three API signature changes.** Most call sites are in tests.

   | What changed | What to write |
   | ------------ | ------------- |
   | `FederatedIdentity` takes a fifth `configOptions` argument | `new FederatedIdentity(arn, account, Collections.emptyMap(), Collections.emptyList(), Collections.emptyMap())` |
   | `Constraints` takes `configOptions` and a `QueryPlan` after the row limit | `new Constraints(summary, expressions, orderBy, limit, Collections.emptyMap(), null)` |
   | `FederationRequestHandler.getCredentialProvider()` became a `public default` delegating to an overload that consults `getDatabaseConnectionSecret()` | Delete a `protected` override of it. It no longer compiles, and for a `DatabaseConnectionConfig` naming no secret the default already returns null, which is what such an override was for |

5. **If your connector declares its own shade `<filters>`, restate the aggregator's global excludes
   inside it.** Maven replaces a list rather than appending to it, so a child block silently drops
   the parent's, and the parent's is what keeps signature files out of the fat jar.
6. **Re-measure the jar.** The bump takes the Databricks connector from 88.8 MB to 139.5 MB. The
   failure mode for an oversized package is a deploy CloudFormation rejects rather than a build that
   fails, so the number is worth knowing before the deploy. Build with `clean` first: a stale toolkit
   jar left in the reactor makes it read low.

### Your jar now ships nested inside the deployment package

`AthenaFederationConnector` places the fat jar at `lib/<jar>` inside the Lambda deployment package,
where the package used to be the jar itself. Lambda's 250 MB limit counts *extracted* bytes and it does
not extract a nested jar, so what binds is the jar's own size. For the Databricks connector that is
139.5 MB against the 357.8 MB its 84,643 files expand to, which is what makes a current federation SDK
deployable at all.

The Java runtime's classpath already covers every jar in `lib/`, so handler classes, resources and
`META-INF/services` files resolve exactly as before. What changes is the filesystem: **your jar's
entries are no longer unpacked under `/var/task`.** A handler that opens a bundled file by path finds
nothing there:

```java
// Silently broken by the new layout: nothing is unpacked at this path any more.
new File("/var/task/reference-data.json");
```

Read bundled files from the classpath instead, which works in either layout:

```java
try (InputStream in = getClass().getResourceAsStream("/reference-data.json")) { ... }
```

`/var/task/lib/<jar>` does exist, so a handler that genuinely needs the jar as a file can find it
there. Prefer the classpath read: the jar's filename carries your connector's version and changes on
every release.

## Deploy

Each connector's CDK app is standalone: its own `cdk.json`, its own `bin/app.ts`, its own
stack. It is deliberately not a stack inside COA's `infra/` app, because
`make deploy-dev` runs `cdk deploy --all` over that app and a customer-facing sample has no
business being swept into it.

Prerequisites: JDK 11 or later, Maven, **Node 20.12 or later** (the CDK toolkit uses Node's
built-in `process.loadEnvFile`, so it needs no dependency of its own), pnpm, AWS credentials,
and a target
account and region that have been **CDK-bootstrapped** — the connector JAR is uploaded as an
S3 asset, so `npx cdk bootstrap` must have been run once for that account and region.

```bash
cp .env.example .env          # SERVE_ROLE_ARN and DISCOVERY_ROLE_ARN
export AWS_REGION=<region>    # in your SHELL: the file's copy is ignored, see below
pnpm install                  # once, at connectors/

cd example/cdk && pnpm run deploy
```

`deploy` builds the jar first via a `predeploy` script, so there is no separate build step to
forget. The stack and the Lambda are both named `example-coa-connector` — the connector's folder
name plus a conventional suffix — so deploying a second connector cannot reshape this one.
Deploying the *same* connector twice into one account needs `FUNCTION_NAME_PREFIX` to tell them
apart; it prefixes both names.

**The `-managed-` segment is reserved: never put it in a `FUNCTION_NAME_PREFIX` you set.** COA's
own managed deployment of the Databricks connector uses `${prefix}-${envName}-managed-`, and a
name collision there is not a retryable failure — every Athena data catalog embeds the handler ARN
it was created with, so the two deployments cannot be untangled after the fact: the same stack
name means COA's deploy reshapes *your* stack, and a different stack name with the same function
name fails COA's deploy outright. Leaving the prefix unset is the normal case and collides with
nothing.

Then onboard it in COA by the function's ARN, and optionally
[register a catalog of your own](#register-a-catalog-to-check-the-connector-yourself) in the account
you want to query from.

### Deployment facts come from the environment

What a connector *is* — its handler class, its jar, its timeout — lives in its stack, in
TypeScript, committed. Where it is *going* differs per deployment and some of it must never be
committed, so it comes from the environment. `connectors/.env` is git-ignored; a pipeline just
exports the variables instead, and **exported values win over the file**.

| Variable                        | Required | Purpose                                            |
| ------------------------------- | -------- | -------------------------------------------------- |
| `SERVE_ROLE_ARN`                | yes      | Accelerator role that runs queries; comma-separate |
| `DISCOVERY_ROLE_ARN`            | yes      | Accelerator role that runs `DESCRIBE`              |
| `AWS_REGION`                    | yes      | Target region; **export it** — see the note below   |
| `FUNCTION_NAME_PREFIX`          | no       | Prefixes Lambda and stack names; resolves conflicts. Must not contain `-managed-` — reserved for COA's own deployment |
| `EXAMPLE_BULK_ROWS`                | no       | Example only: size its spill fixture               |
| `EXAMPLE_BULK_ROW_BYTES`           | no       | Example only: payload width                        |

**Put `AWS_REGION` in your shell, not only in `.env`.** The CDK CLI sets `CDK_DEFAULT_REGION`
from the active profile on every invocation, and that wins over the file — so a `.env` saying
`eu-central-1` with a profile defaulting to `us-east-1` deploys to `us-east-1`, succeeds, and
leaves a connector nothing will ever query. So export it — `AWS_REGION=<region> pnpm run deploy`
— and check the region in the synth output before deploying.

Two roles, because two COA components reach a connector: **serve** runs the queries,
and **discovery** runs `DESCRIBE` to read column comments — which is how the `@pk` / `@fk`
constraint tags get out of a connector at all. Grant serve but not discovery and the connector
answers `SELECT` perfectly while no declared key is ever found.

Read both from SSM in COA's own account, where they are authoritative — the role names are
generated, so do not try to guess them:

```bash
aws ssm get-parameter --name /{prefix}/{envName}/serve/runtime-role-arn        --query Parameter.Value --output text
aws ssm get-parameter --name /{prefix}/{envName}/sources/db-connector-role-arn --query Parameter.Value --output text
```

`{prefix}` is the resource prefix COA was deployed with and `{envName}` is the COA environment those
roles belong to, so a `dev` and a `prod` deployment under one prefix publish their own pair:
`/coa/dev/serve/runtime-role-arn` and `/coa/prod/serve/runtime-role-arn`. Paste the results into `.env`, or
export them: SSM parameters are not readable across accounts, and a connector usually runs in a
different account from COA.

Both are in COA's account and the connector usually is not, and a cross-account
principal needs an allow **on both sides** — so the stack puts the connector's half in place
for each:

- `lambda:InvokeFunction` on the connector, as a Lambda resource policy;
- `s3:GetObject` under `connectors/{id}/spills/`, as a bucket policy on the connector's own spill
  bucket. Spill objects are read with the *querying* role's credentials, not the connector's,
  which is why the bucket needs its own grant;
- `kms:Decrypt` on the spill key, conditioned on `kms:ViaService = s3.<region>.amazonaws.com`.

The other half — the identity policies on those roles — lives in COA and is not this app's to
write. What it matches, and what this stack emits so that it matches:

| COA allows              | Scoped by                                                      | This stack emits          |
| ----------------------- | -------------------------------------------------------------- | ------------------------- |
| `lambda:InvokeFunction` | tag `coa:connector = true`, wildcard account and function name  | that tag, on the function |
| `s3:GetObject`          | key glob `connectors/*/spills/*`                                | that spill prefix         |
| `kms:Decrypt`           | tag `coa:connector-spill = true` on the key                     | that tag, on the CMK      |

Invoke is scoped on the **tag**, not the function name. The `-coa-connector` suffix is a naming
convention that grants nothing; an earlier COA release matched on the name, and any comment
claiming the name carries the grant is out of date.

The spill grant is matchable per connector — `connectors/example/spills/*` — so onboarding one
can be an explicit grant rather than something a wildcard already permitted. It is bounded by that
key prefix and by nothing else, so it spans every account including COA's own — see constraint 11.

Your own connector's environment variables go through the same mechanism: read them in your
stack with the `optionalEnv` / `requiredEnv` helpers. For **credentials, do not pass the value
at all** — a Lambda environment variable is readable by anyone with
`lambda:GetFunctionConfiguration` and lands in the CloudFormation template. Pass a Secrets
Manager reference, call `resolveSecrets()` in the handler, and grant the connector read access
in your own stack:

```typescript
const secret = secretsmanager.Secret.fromSecretNameV2(this, "Creds", requiredEnv("DB_SECRET"));
secret.grantRead(this.connector.connectorFunction);
```

### Register a catalog to check the connector yourself

**This is not how COA reaches your connector.** COA registers its own `LAMBDA` data catalog, in its
own account, under a name it derives from the source — you hand it the function ARN and it does the
rest, so nothing in this section is a prerequisite for onboarding. What it is for is querying your
connector from **your own** account, which is how you confirm the `@pk` / `@fk` tags arrived before
handing anything to COA.

The stack deliberately does **not** create an `AWS::Athena::DataCatalog`, for the same reason: the
catalog belongs to whichever account runs the queries. Register one yourself, naming the connector's
function ARN from the `ConnectorFunctionArn` output:

```bash
aws athena create-data-catalog --name example --type LAMBDA \
  --parameters function=arn:aws:lambda:<region>:<acct>:function:example-coa-connector
```

Catalog names are account-global, so a second deployment of the same connector needs a different
name from the first.

Then, in Athena:

```sql
SELECT * FROM example.example_source.customers LIMIT 5;
```

Catalog names are account-global and cannot contain hyphens, so pick one per connector and
per querying account.

### `AthenaFederationConnector` properties

Set in your stack, not by configuration. `connectorId`, `handler` and `jarPath` are required;
the rest default.

| Property                      | Default                   | Purpose                                        |
| ----------------------------- | ------------------------- | ---------------------------------------------- |
| `connectorId`                 | — (required)              | Names the Lambda and anything else unique      |
| `handler`                     | — (required)              | Fully-qualified handler class                  |
| `jarPath`                     | — (required)              | The fat jar; missing is a synth-time error     |
| `queryRoleArns`               | `[]`                      | Principals granted invoke and spill read       |
| `functionNamePrefix`          | none                      | Resolves name conflicts; keeps the suffix      |
| `runtime`                     | `JAVA_21`                 | Any Java runtime                               |
| `architecture`                | `ARM_64`                  | `X86_64` if your jar has an amd64-only native  |
| `network`                     | none (no VPC)             | VPC, subnets, optional security groups         |
| `timeout`                     | 10 minutes                | Invocation timeout                             |
| `memorySize`                  | 1024 MB                   | Enough to buffer a block before it spills      |
| `spill`                       | `"create"`                | `"none"` deploys no bucket — see Spill         |
| `environment`                 | `{}`                      | Connector-specific variables — see below       |
| `description`                 | derived                   | Lambda description                             |


Four names in `environment` are **refused, not merged**: `JAVA_TOOL_OPTIONS`, `spill_bucket`,
`spill_prefix` and `disable_spill_encryption`. The construct owns all four, and either merging
or overwriting would read as "the spill configuration is ignored". The consequence worth knowing
before you choose this construct: because `JAVA_TOOL_OPTIONS` is owned outright, there is no
supported way to add a further JVM flag — another `--add-opens`, a heap setting, a truststore.
If you need one, call `connectorFunction.addEnvironment("JAVA_TOOL_OPTIONS", ...)` after
construction — and note it **replaces**, so your value must still contain
`--add-opens=java.base/java.nio=ALL-UNNAMED` or every read fails (constraint 1).

### What a record handler must write

`BlockUtils.setValue` infers nothing: the Java value you hand it has to match the Arrow type you
declared, or the query fails at read time with `Unsupported Arrow Type` or an NPE — after
deploy, since constraint 9 rules out constructing a handler in a unit test.

| Arrow type (`Types.MinorType`) | Java value                                       |
| ------------------------------ | ------------------------------------------------ |
| `BIGINT`                       | `Long`                                           |
| `INT`, `SMALLINT`, `TINYINT`   | `Integer`                                        |
| `VARCHAR`                      | `String`                                         |
| `BIT`                          | `Boolean`                                        |
| `FLOAT8` / `FLOAT4`            | `Double` / `Float`                               |
| `DECIMAL`                      | `BigDecimal`                                     |
| `DATEDAY`                      | `Integer` — epoch **day**, not millis            |
| `DATEMILLI`                    | `Long` — epoch millis, UTC; see constraint 4     |
| `VARBINARY`                    | `byte[]`                                         |

That is the whole list. `setValue`'s switch covers scalars only — hand it a `STRUCT`, `LIST` or
`MAP` vector and it throws `Unknown type Struct` — which is why the toolkit declares scalar columns
and nothing else. Flatten a nested source into scalar columns in your metadata handler.

### Deploy by hand

If you would rather not use CDK. You take on **two tags and three resource policies per querying
role**, not just the invoke one. Miss the function's tag (step 3) and nothing can invoke the
connector at all, because COA matches the tag, not the name. Miss either spill policy (steps 6, 7)
and everything works until a response crosses 6 MB. `JAVA_TOOL_OPTIONS` is not optional either.

```bash
# 1. Upload the jar (too large for direct Lambda upload)
aws s3 cp example/target/example-connector-1.0.0.jar s3://<your-bucket>/connector.jar

# 2. Create the Lambda
aws lambda create-function \
  --function-name example-coa-connector --runtime java21 \
  --role <lambda-exec-role-arn> \
  --handler dev.coa.example.ExampleCompositeHandler \
  --code S3Bucket=<your-bucket>,S3Key=connector.jar \
  --timeout 90 --memory-size 1024 \
  --environment 'Variables={spill_bucket=<your-bucket>,spill_prefix=connectors/example/spills,disable_spill_encryption=false,JAVA_TOOL_OPTIONS=--add-opens=java.base/java.nio=ALL-UNNAMED}'

# 3. Tag it — this, not the function name, is what COA's invoke policy matches on.
#    Without the tag nothing can invoke the connector and the first scan is denied.
aws lambda tag-resource \
  --resource arn:aws:lambda:<region>:<acct>:function:example-coa-connector \
  --tags coa:connector=true

# 4. Let COA invoke it
#    A statement id is unique per function, so these are two calls with two ids. Reusing one id
#    fails with ResourceConflictException, which reads as "already granted" — and leaves discovery
#    without invoke, so SELECT works while no declared key ever reaches COA.
aws lambda add-permission --function-name example-coa-connector \
  --statement-id COA-serve-invoke --action lambda:InvokeFunction \
  --principal <COA serve role arn>
aws lambda add-permission --function-name example-coa-connector \
  --statement-id COA-discovery-invoke --action lambda:InvokeFunction \
  --principal <COA discovery role arn>

# 5. Let the connector WRITE its spill (an identity policy on <lambda-exec-role-arn>).
#    Without this the connector cannot spill at all, and a query over 6 MB has been observed
#    returning SUCCEEDED with zero rows rather than failing.
#      s3:PutObject + s3:AbortMultipartUpload on
#        arn:aws:s3:::<your-bucket>/connectors/example/spills/*
#      s3:ListBucket + s3:GetBucketLocation on arn:aws:s3:::<your-bucket> (the bucket itself, not
#        the prefix): the SDK's SpillLocationVerifier calls HeadBucket before returning splits, so
#        without this EVERY query fails in GetSplits — not only the ones large enough to spill
#      kms:GenerateDataKey on the spill key (the bucket must be SSE-KMS with your own CMK)

# 6. Let COA READ the spill — a BUCKET policy, because spill objects are fetched with the
#    QUERYING role's credentials, not the connector's. Omit it and the first query to cross
#    6 MB returns AccessDenied.
#      Principal <COA serve role arn> (and the discovery role), s3:GetObject on
#        arn:aws:s3:::<your-bucket>/connectors/example/spills/*

# 7. Let COA DECRYPT it — a key policy on the spill CMK, same two principals, kms:Decrypt,
#    conditioned on kms:ViaService = s3.<region>.amazonaws.com. Tag the key
#    coa:connector-spill=true, which is what COA's key policy matches on. It must be a key you
#    create: aws/s3 can neither be tagged nor have its policy edited.

# 8. Register as an Athena data catalog, in the querying account
aws athena create-data-catalog \
  --name example --type LAMBDA \
  --parameters function=arn:aws:lambda:<region>:<acct>:function:example-coa-connector
```

## Adding your own connector

Five steps, and nothing outside your own folder needs to change except two one-line
registrations.

1. **`connectors/<id>/`** — the folder name is the connector's id, and the stack and Lambda
   names derive from it. Start with a letter, and use hyphens rather than underscores: the same
   string names a CloudFormation stack, which allows neither underscores nor a leading digit.
   `connectorFunctionName` enforces this at synth, so an illegal id fails before anything is
   built.

2. **`pom.xml`** — copy `example/pom.xml` and change `<artifactId>`; it decides the jar filename
   that step 4's `DEFAULT_JAR_PATH` has to match. The aggregator is the parent, so the only
   dependencies are the toolkit and JUnit, and the shade plugin needs no configuration of its
   own. Add the module to `connectors/pom.xml`.

   **Add an SLF4J binding if you want logs.** `example/pom.xml` ships without one, so nothing
   in the example — or in the federation SDK, which logs through SLF4J — writes anything: every
   `logger.*` call is discarded and SLF4J prints its "no providers were found" notice on each
   cold start. Nothing fails, which is why it is easy to miss until the first invocation you need
   to debug. `connectors/databricks/pom.xml` shows the fix: `org.slf4j:slf4j-simple`, plus a shade
   filter excluding the driver's own bundled binding so two providers cannot both be on the
   classpath.

3. **Handlers** — extend `CoaMetadataHandler` and the SDK's `RecordHandler`, wire them into a
   `CompositeHandler`. Describe your tables with `CoaTable` / `CoaColumn`; the toolkit turns the
   declared keys into comments and places them for you.

4. **`<id>/cdk/`** — copy `example/cdk`. Four of its eight files need editing, not just the
   stack: `package.json` (its `name`, and `-pl example` → `-pl <id>` in the `package` script —
   miss this and you build the *example's* jar), `bin/app.ts` (the import), the stack file
   itself, and its test. `cdk.json`, `tsconfig.json`, `jest.config.js` and `.gitignore` copy
   across unchanged. Delete `CONNECTOR_DATABASE` / `CONNECTOR_TABLES` and
   `publishIntegTestOutputs()` — those are this repository's test scaffolding, not part of a
   connector. The stack is the interesting file:

   ```typescript
   this.connector = new AthenaFederationConnector(this, "Connector", {
     connectorId: "yoursource",
     handler: "dev.coa.yoursource.YourCompositeHandler",
     jarPath: path.join(__dirname, "..", "..", "target", "your-connector-1.0.0.jar"),
     queryRoleArns: queryRoleArns(),
     environment: { your_setting: requiredEnv("YOUR_SETTING") },
   });
   ```

   It is an ordinary stack, so **add whatever else your source needs right here** — an RDS
   proxy, a secret, a KMS key, a cache table — and grant the connector access to it. For a VPC,
   pass `network: connectorNetworkFromEnv(this)` and the connector reads `CONNECTOR_VPC_ID`,
   `CONNECTOR_SUBNET_IDS` and `CONNECTOR_SECURITY_GROUP_IDS`, as the Databricks connector does. The
   `pnpm-workspace.yaml` glob `*/cdk` picks the app up with no edit — but only on the next
   install, so run `pnpm install` again at `connectors/`, or your new app has no `node_modules`
   and `pnpm test` fails with `jest: command not found`.

5. **`.env.example`** — document any variable your connector needs, so the next person knows
   what to set.

Then, from `connectors/`:

```bash
mvn -B test -pl <id> -am                  # your handlers
cd <id>/cdk && pnpm test && pnpm run synth   # your stack, and the jar path/id/region
pnpm run deploy
```

`synth` catches an illegal connector id, a reserved environment key, a missing jar and the wrong
region before anything is created. Note `pnpm run synth` builds the jar first via `presynth`; for
the template alone use `pnpm exec cdk synth`. Then
[register a catalog of your own](#register-a-catalog-to-check-the-connector-yourself) in the account
you want to query from, and confirm the tags actually arrived:

```sql
DESCRIBE <id>.<your_db>.<your_table>;
-- the comment column must show your prose plus @pk / @fk(...) where you declared them
```

If the comments are missing, they went onto the Arrow *field* instead of the schema — see
constraint 6.

## Spill

Athena caps a connector's Lambda response at **6 MB**. Above that the SDK writes the Arrow
block to S3 — the bucket named by the connector's `spill_bucket` environment variable,
under `spill_prefix` — and returns a `SpillLocation` instead of inline data. Athena, acting
for the querying principal, then fetches it. Spill is automatic and connector-side; the
only real choices are which bucket and which prefix.

### A connector's bucket is its own, or absent

There is no option to share one. Sharing would mean one connector's read grant covers another
connector's spilled rows, and spilled rows are query results — so the isolation is a
data-access boundary, not tidiness. It also keeps the bucket policy legible: exactly the
principals allowed to query *this* connector appear on it.

`spill: Provisioning.NONE` opts out entirely: no bucket, no `spill_bucket`. Legitimate for a source whose
responses cannot exceed 6 MB, and **it fails in a shape worse than an error.** Metadata calls
and every inline response keep working; a response that needs to spill has been observed coming
back as a `SUCCEEDED` query with **zero rows** — no exception, nothing in the connector's log.
A query that silently answers "no rows" is harder to notice than one that fails. It depends on your
connector rather than your data volume, but not on its capability map: Athena populates
`Constraints.getLimit()` whether or not a connector advertises `LIMIT` push-down, so a connector that
*ignores* it requests whole tables and even `SELECT ... LIMIT 1000` can spill. Read the limit and apply
it; advertising it only tells Athena it need not re-apply the limit itself. Measured — see the Databricks
connector's [push-down section](databricks/DESIGN.md#push-down-what-is-and-is-not-advertised).

The bucket has block-public-access, enforced SSL, SSE-KMS with its own customer-managed key, no versioning, and a
one-day expiry rule. One day because spill data is scratch — anything older than the query
that wrote it is garbage.

### Two layers of encryption, both required

The bucket is **SSE-KMS with a customer-managed key this stack creates**, tagged
`coa:connector-spill = true`. Not optional, and not the AWS-managed `aws/s3` key: COA's key
policy is scoped to that tag, and `aws/s3` can neither be tagged nor have its policy edited. The
bucket sets `BucketKeyEnabled`, so S3 serves reads without a KMS call per spilled object.

The key policy carries two statements — `kms:Decrypt` for COA's querying roles, conditioned on
`kms:ViaService = s3.<region>.amazonaws.com`, and `kms:GenerateDataKey` for the connector's own
execution role, which is what lets it write.

> The condition is `kms:ViaService` and not `aws:CalledVia`. Under bucket-level SSE-KMS the
> immediate KMS caller is S3, not Athena, so `aws:CalledVia` would depend on whether S3 appends
> itself to the call chain — undocumented, and it would fail closed on every spilled query.

On top of that, `disable_spill_encryption=false` is set rather than left to the SDK's default. The block is
encrypted with a key the SDK generates per query and ships to Athena on the `Split`, so it
costs no KMS calls and needs no KMS grant — and a spill object is useless to anyone who reads
the bucket without the key. This is client-side encryption of the Arrow block, independent of
the bucket's own SSE-KMS.

### Reading spill is the querying role's job, not the connector's

The connector writes the block; Athena reads it back **as the principal that ran the query**.
So the connector's spill bucket needs a resource policy naming that principal, which is what
`SERVE_ROLE_ARN` produces. Get it wrong and you get the worst available failure mode:
every result under 6 MB is returned inline and works, and the first query to cross the limit
returns `AccessDenied`.

`spill_prefix` is derived, not a property: **`connectors/{connectorId}/spills`**. COA's
spill-read grant matches that layout, and this stack grants the connector write access to the
same prefix, so the two cannot drift. That matters: when they disagreed during testing, the
spilling query returned `SUCCEEDED` with zero rows rather than an error.

The shape is deliberate. A single conventional segment like `athena-federation-spill` is what
every published Athena federation connector uses, so granting COA `s3:GetObject` on
`arn:aws:s3:::*/athena-federation-spill/*` would reach spill data in buckets that have nothing
to do with COA. Naming the path after COA's own layout makes any match deliberate, and putting
the connector id in it lets the grant be written per connector —
`arn:aws:s3:::*/connectors/example/spills/*` — instead of one wildcard covering every connector
anyone will ever deploy.

### Driving a response over 6 MB

The fixture's four business tables are tiny, so no response ever spills and the path cannot
be tested. `bulk_rows` fixes that: its row count and row width come from the environment.

One knob, two layers, and the casing is what tells them apart:

| Where you set it              | Read by                    | Default | Meaning                    |
| ----------------------------- | -------------------------- | ------- | -------------------------- |
| `EXAMPLE_BULK_ROWS`           | the CDK app, at deploy     | unset   | Rows the table serves      |
| `EXAMPLE_BULK_ROW_BYTES`      | the CDK app, at deploy     | unset   | Width of each `payload`    |
| `example_bulk_rows`           | the Lambda, at run time    | 64      | What the stack sets from the above |
| `example_bulk_row_bytes`      | the Lambda, at run time    | 64      | Same                       |

You set the **UPPER CASE** pair; the stack passes them down as the lower-case pair. Setting the
lower-case name on a `pnpm run deploy` does nothing at all — the deploy succeeds, the Lambda
keeps its defaults, and the query you then run never spills while looking like it tested the
spill path. The Lambda side reads its names case-insensitively and falls back to 64 on anything
unusable, so a typo cannot take the connector down; the deploy side now rejects a non-integer
instead, which is what makes the silent version above impossible.

`doGetSplits` returns **one split for the whole table**, so the whole table is one response
and `example_bulk_rows × example_bulk_row_bytes` is that response's payload size.

The example's stack reads both from the environment, so sizing them needs no code change:

```bash
cd example/cdk

# 8 MiB of payload in one response — over the 6 MB limit
EXAMPLE_BULK_ROWS=4096 EXAMPLE_BULK_ROW_BYTES=2048 pnpm run deploy

# or, to be comfortably past whichever limit binds first, 32 MiB
EXAMPLE_BULK_ROWS=16384 EXAMPLE_BULK_ROW_BYTES=2048 pnpm run deploy
```

```sql
-- Must project `payload`: that is where the bytes are. `SELECT row_id` or
-- `SELECT count(*)` returns a few KB and never spills, because the RecordHandler
-- writes only the columns the query asked for.
SELECT row_id, payload FROM example.example_source.bulk_rows;
```

Confirm it spilled by looking for objects under
`s3://<spill-bucket>/connectors/example/spills/` after the query, or by the connector's
CloudWatch logs. Note Athena — not the connector — supplies both size limits
(`maxInlineBlockSize`, `maxBlockSize`) in each `ReadRecords` request, and the inline limit
sits below the 6 MB response cap; 32 MiB is over any value Athena has been observed to send.

## Non-obvious constraints (none of these are in the AWS docs)

1. **Arrow needs a JVM flag on Java 17.** Without
   `JAVA_TOOL_OPTIONS=--add-opens=java.base/java.nio=ALL-UNNAMED`, metadata calls succeed
   and every *read* fails with `Failed to initialize MemoryUtil` — i.e. "discovery works,
   queries are broken." The CDK construct sets it unconditionally.
2. **Arrow is not a transitive SDK dependency.** Use the `with-arrow` artifact classifier —
   the aggregator `pom.xml` pins it, and the toolkit depends on it, so a connector inherits it
   transitively. It produces the fat jar, which is why it ships as an S3 asset rather than a direct
   Lambda upload. The construct nests that jar at `lib/<jar>` inside the deployment package, because
   Lambda's 250 MB limit counts *extracted* bytes and it does not extract a nested jar. Classes and
   resources load from the classpath as usual; only a handler that read its own files from
   `/var/task` directly would notice.
3. **Athena projects the schema.** `RecordHandler.readWithConstraint` must write *only* the
   columns present in `request.getSchema()`. Writing an absent column throws
   `NullPointerException` inside `BlockUtils.setValue` ("vector is null"). Unprojected
   queries pass, so this surfaces late. `ExampleRecordHandler` drives its write loop off the
   request's own field list, which makes the mistake impossible rather than merely guarded.
4. **Timestamps must be timezone-aware.** `Types.MinorType.DATEMILLI` written from a
   UTC-based epoch-milli works; a naive timestamp is rejected with `Unsupported Arrow Type`.
5. **Column comments do not survive `GetTableMetadata`.** Athena drops the Arrow schema's
   metadata when converting to its table-metadata API, and the protocol has no field for
   PK/FK at all. Comments *are* recoverable via `DESCRIBE <catalog>.<db>.<table>` (a
   different Athena code path). So a connector source gives COA names and types via the
   metadata API, comments — and therefore the constraint tags — via `DESCRIBE`, and never
   PK/FK directly.
6. **Per-column comments go in the schema's metadata keyed by column name, not on the Arrow
   `Field`.** The single highest-value thing on this page, because the wrong form fails
   silently: a `comment` entry on a `Field`'s `FieldType` is serialised, reaches Athena intact,
   and is then ignored — `DESCRIBE` returns two columns, name and type, with no comment column
   at all, and no tag ever reaches COA. Nothing errors, and the API points the
   wrong way: `FieldType` takes a metadata map that looks purpose-built, while `SchemaBuilder`
   offers no field-metadata method at all. The correct form is
   `SchemaBuilder.addMetadata(columnName, comment)` — one schema-level entry per column, which
   is exactly what the SDK's own `GlueMetadataHandler` does with a Glue column comment. Use
   `TableSchema` and this is unreachable; the note is here to explain why that class exists.
7. **There is no way to publish a table-level comment.** Athena's convention has a slot for
   one — a `comment` entry in the same schema-metadata map — and nothing reads it for a
   `LAMBDA` catalog. Six paths were tried, all negative: `DESCRIBE`; `GetTableMetadata` and
   `ListTableMetadata`, whose `TableMetadata.Parameters` field comes back absent;
   `SHOW TBLPROPERTIES` and `SHOW CREATE TABLE`, both rejected as *Unsupported DDL for Lambda
   catalogs*; and `information_schema.tables`, which exposes only `table_catalog`,
   `table_schema`, `table_name`, `table_type`. `TableSchema` therefore offers no setter for it,
   rather than accepting a value it cannot deliver. Document your tables somewhere a reader
   will actually see. One upside: with that key unused, a source column genuinely named
   `comment` is an ordinary column with nothing to collide with.
8. **`information_schema` is a second reader of the same metadata.**
   `SELECT column_name, comment FROM <catalog>.information_schema.columns` returns the column
   comments, tags included, for a Lambda catalog. Useful when debugging a connector, and a
   reminder that the schema-metadata convention is not private to `DESCRIBE`.
9. **Unit tests must not construct the handlers.** `MetadataHandler` and `RecordHandler`
   build S3, Athena and Secrets Manager clients in their constructors, so instantiating one
   needs credentials and a region. Everything worth asserting lives in `ColumnComment`,
   `TableSchema`, `ExampleCatalog` and `ExampleTable` for exactly that reason.
10. **`coa-contract.ts` states one side of a contract whose other side is elsewhere.** The tag
    keys and the spill glob are what COA's IAM policies match on, restated in the toolkit rather
    than imported, because `connectors/` is a workspace of its own that a customer copies out and
    a dependency reaching back into COA's internals would break that. **Nothing verifies that the
    two sides agree**, so a change to either has to be matched by hand. A changed tag key is loud,
    since every scan fails at once; a changed spill glob is not, since it only breaks responses
    over 6 MB.
11. **Deploying into COA's own account is supported, and neither grant excludes it.** Both are
    scoped by something the connector *carries* rather than by where it lives: invoke on the
    `coa:connector` tag, spill on the `connectors/*/spills/*` key glob. So the function may be
    named anything — COA's own resource prefix included — and its spill bucket is readable in
    COA's account on the same terms as in yours.

    Earlier releases differed in both respects, and notes to that effect are out of date. The
    serve role's `AthenaFederationSpillRead` carried a
    `StringNotEquals aws:ResourceAccount = <COA's account>` exclusion, and the Athena-invoke
    `Deny` was scoped to `<COA's prefix>-*` by name, which silently caught any same-account
    connector named that way. Neither holds now: the spill statement is bounded by the key glob
    alone, and the `Deny` — `DenyAthenaInvokeOfUntaggedFunctions` — is scoped by the *absence* of
    the connector tag, so a tagged connector is exempt wherever it sits.

    What is still true, and the reason to prefer cross-account: a same-account request is
    authorised by an allow from *either* the identity or the resource policy rather than needing
    both, so the second independent barrier cross-account gives you is not there.

## When it goes wrong

Almost every failure in this system is silent: the query succeeds and the answer is wrong or
empty. Start from the symptom.

| Symptom | Look at |
| ------- | ------- |
| `DESCRIBE` shows no comments at all | The comment went on the Arrow **field** instead of the schema's metadata (constraint 6). Use `TableSchema`, which refuses a field carrying metadata. |
| Comments are there, but COA finds no keys | The **discovery** role was not granted invoke, so nothing ever ran `DESCRIBE`. `SELECT` works throughout. See `DISCOVERY_ROLE_ARN`. |
| Query returns **zero rows, status SUCCEEDED** | The connector could not write its spill. Check `spill_bucket` is set on the function, that `spill_prefix` is `connectors/<id>/spills`, that objects appear under it, and that the execution role can `PutObject` there. |
| `AccessDenied`, but only on large results | Spill is being *read* by the querying role, not the connector. Check the bucket policy and the key policy, both scoped to the spill prefix. |
| Every invocation fails once rows are read, metadata calls fine | `JAVA_TOOL_OPTIONS=--add-opens=java.base/java.nio=ALL-UNNAMED` is missing (constraint 1). |
| `Unsupported Arrow Type`, or an NPE in `BlockUtils.setValue` | The Java value does not match the declared Arrow type — see *What a record handler must write*. |
| Catalog resolves, but no table ever returns rows | The connector is deployed in a different region from the catalog. |
| A tag reaches COA verbatim, e.g. `@fk(orders.order_id)` shown as prose | The tag was malformed, and COA logged that to its own logs. Build tags with `ColumnComment`, never by hand. |
| CloudWatch has no connector logs, only a "no SLF4J providers were found" line | No SLF4J binding on the classpath, which is how `example/pom.xml` ships. Add `slf4j-simple` — see *Adding your own connector*, step 2. |
