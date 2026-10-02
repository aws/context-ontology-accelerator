# `coa_sources.database`

Registration, discovery and teardown for **database** sources. This file covers the one
thing in the module that is easy to get wrong when adding a source sub-type: the sub-type
enum and the three sets the create, delete and scan paths branch on. Everything else is
documented where it lives.

The sets are in `sub_types.py`; the enum they draw from is generated from Smithy.

## Two spellings of the sub-type, and why both exist

| Where | What it is | Who may import it |
| --- | --- | --- |
| `SourceSubType`, in the generated control-plane models | The contract. Covers **every** source category, database and document alike: `GLUE_DATABASE`, `JDBC_DATABASE`, `CUSTOM_CONNECTOR`, `DATABRICKS_SQL_WAREHOUSE`, `S3`, `LOCAL_UPLOAD` | Anything that can import the generated server package |
| `DatabaseSubType`, in `coa_common.constants` | A hand-maintained mirror of the enum's **DATABASE members only** | Serve and ontology-engine, which cannot import the generated control-plane models |

The mirror exists because two components outside this package branch on the same concept
and cannot reach the generated enum. It is a mirror rather than a second source of truth:
`packages/sources/tests/unit/database/test_sub_types.py` compares it against the generated
enum and fails when a DATABASE member is missing. That tripwire matters because the
failure it catches is silent. A sub-type absent from the mirror drops out of induction on a
bare `continue`.

`DATABASE_SUB_TYPES` is derived from the mirror rather than re-listed, for the same
reason.

## The three sets

All three are `frozenset`s of sub-type **values**, and all three are tested as
**membership**. Never as equality against one member: that shape fails open for every
other member of the same set, which is exactly how the `queryable` write and the federated
teardown broke when a second connector-backed sub-type arrived.

### `CONNECTOR_BACKED_SUB_TYPES`

Members: `CUSTOM_CONNECTOR`, `DATABRICKS_SQL_WAREHOUSE`. Lives in
`coa_common.constants` and is re-exported here.

A member's Athena catalog is a top-level `LAMBDA` catalog bound to a connector function,
so the source resolves its own database instead of a Glue-crawled schema. Four behaviours
follow, and each has a branch keyed on this set:

- discovery reads the source through Athena SQL, so the metadata-connector registry maps
  every member to `CustomConnector`;
- the federation step has nothing to provision and only flips `queryable`;
- teardown is a plain `athena:DeleteDataCatalog`;
- serve's crawled-table-name rewrite must **not** apply. It strips a `{schema}_` prefix a
  Glue crawler added, so running it here turns `sales_orders` in schema `sales` into
  `orders`, a table the connector has never heard of.

### `FEDERATED_TEARDOWN_SUB_TYPES`

Members: `GLUE_DATABASE`, `JDBC_DATABASE`.

A member's delete tears resources down through the federation provisioner's Lake
Formation admin role: `glue.delete_catalog`, `lf.deregister_resource`,
`glue.delete_connection`.

`GLUE_DATABASE` is in the set even though a native Glue source is not federated, because a
legacy row can carry a provisioned `glueConnectionName` or `athenaDataCatalogName` and
this teardown is the only thing that removes them.

### `PLATFORM_CATALOG_CLAIM_SUB_TYPES`

Members: `JDBC_DATABASE`, `CUSTOM_CONNECTOR`, `DATABRICKS_SQL_WAREHOUSE`.

Create derives a platform catalog name for these and claims it in the ownership record;
delete must release that claim. The set has to stay in step with the create path, which
claims for every sub-type that derives a name. A member missing here leaves its derived
name permanently owned by a source that no longer exists, which is what happened when the
branch covered two of the three.

`GLUE_DATABASE` is deliberately absent: it is given no derived name, so adding it would
make every Glue delete try to release a claim that never existed.

## Deciding which sets a new sub-type joins

Answer these in order. The tripwire tests enforce the invariants behind them, so a wrong
answer fails the suite rather than a deploy.

1. **Is the source's Athena catalog a top-level `LAMBDA` catalog bound to a connector
   function?** If yes, it joins `CONNECTOR_BACKED_SUB_TYPES`. If its catalog is a Glue
   object the federation provisioner created, it joins `FEDERATED_TEARDOWN_SUB_TYPES`.
   **Exactly one of the two.** They are the two teardown paths, they must not overlap, and
   between them they must cover every DATABASE sub-type. A sub-type in neither has its
   catalog torn down by nothing, and a sub-type in both gets `athena:DeleteDataCatalog`
   followed by a Lake Formation teardown against a catalog that is already gone.
2. **Does create derive a catalog name and claim it?** If yes, it joins
   `PLATFORM_CATALOG_CLAIM_SUB_TYPES`. Every connector-backed sub-type does, and that
   containment is asserted; a federated sub-type may or may not, which is why the set is
   separate rather than derived.
3. **Add the member to `DatabaseSubType` in `coa_common.constants`** if it is a DATABASE
   sub-type. Document sub-types belong in neither the mirror nor any of these sets.
4. **Check the delete handler.** Adding a set membership changes which arms of
   `sources_handler`'s delete path run. Read them rather than assuming.

Memberships are deliberately **positive**, so a branch nobody has considered for a new
sub-type fails closed. A `!= X` guard admits every future sub-type by default, and that is
how delete's federated teardown came to run a Glue teardown against a Lambda-backed
catalog, report success, and leak the registration.

## What an absent `sourceSubType` means, per set

`sub_type` is read as `item.get("sourceSubType", "")`, and rows predating the attribute
exist, so `""` is a live row shape. It satisfied every old exclusion and satisfies no new
membership. What it **must** mean differs per set, which is why no set contains `""`:

| Set | Does `""` take this arm? | Why |
| --- | --- | --- |
| `FEDERATED_TEARDOWN_SUB_TYPES` | **Yes**, and the guard tests for it separately (`sub_type in ... or not sub_type`) | A legacy JDBC row whose federation step provisioned a Glue catalog and connection has those stored names, and this teardown is the only thing that removes them |
| `CONNECTOR_BACKED_SUB_TYPES` | No | Both members arrived after `sourceSubType` became mandatory, so no attribute-less row can be one. Treating `""` as connector-backed would point a legacy row's delete at `athena:DeleteDataCatalog` and at the parameter and tag-verification steps only a Databricks source has |
| `PLATFORM_CATALOG_CLAIM_SUB_TYPES` | No | A row without a sub-type never claimed a name, so there is none to release |

`""` is tested for at the call site rather than added to a set, so it does not become a
recognised sub-type value everywhere else that set is used.
