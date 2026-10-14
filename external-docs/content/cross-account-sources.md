# Cross-Account Data Sources

How to onboard a **JDBC database** or **Glue Data Catalog** that lives in a
*different AWS account* than your Context Ontology Accelerator deployment. The data owner (the account
holding the database/catalog) grants Context Ontology Accelerator narrowly-scoped access; Context Ontology Accelerator never
requires admin in the owner's account.

> `{prefix}` is the deployment prefix `{project}-{env}` (e.g. `accelerator-dev`).
> `<accelerator-account>` is the account where Context Ontology Accelerator is deployed; `<owner-account>`
> holds the data source.
>
> **SSM parameter paths spell the two parts separately**, as `/{prefix}/{envName}/…`,
> where `{prefix}` there is the project token alone and `{envName}` is the
> environment: the deployment prefixed `accelerator-dev` publishes
> `/accelerator/dev/serve/runtime-role-arn`. A lookup that joins them into one
> segment returns nothing.

There are three independent concerns. Set up only the ones your source needs:

| Concern | JDBC | Glue |
|---|---|---|
| **Network** — reach the DB host | ✅ Required | — (Glue/Athena APIs are regional, no VPC path) |
| **Credentials** — read the Secrets Manager secret | ✅ Required | — |
| **Catalog authorization** — read table metadata | — | ✅ Required (IAM and/or Lake Formation) |

---

## Cross-Account JDBC

### 1. Network connectivity

The discovery connector and federated-query connector run inside Context Ontology Accelerator's VPC and
must reach the database host. Establish a private path (VPC peering, Transit
Gateway, or PrivateLink) and open the DB security group to Context Ontology Accelerator's connector
security group on the engine port. See
[Cross-Network Connectivity](cross-network-connectivity.md) for the full
peering / Transit Gateway / PrivateLink walkthrough — it is the same regardless
of account boundary.

### 2. Share the credential secret

The secret holding `username`/`password` lives in the owner account. **Three**
roles in the Context Ontology Accelerator account must be able to read it:

- `{prefix}-sources-db-connector` — discovery
- `{prefix}-federated-catalog-role` — the managed Athena connector (federated query)
- The **serve runtime role** — the Context Manager's own role, used by the
  direct-JDBC fast path (a single-source query routes to a direct `asyncpg`
  connection instead of Athena federation for lower latency; see
  [Serve](serve.md#resolution-tiers)). This path fetches the secret directly
  with the serve runtime's own credentials, bypassing Athena and the
  federated-catalog-role entirely. **Missing this grant doesn't break the
  source outright** — discovery and Athena-routed queries still work — it
  only fails the subset of queries the composite executor routes to the
  direct-JDBC path, which looks like an intermittent bug rather than a clear
  permissions error. Resolve its ARN from SSM in the Context Ontology Accelerator
  account:

  ```bash
  aws ssm get-parameter --name "/{prefix}/{envName}/serve/runtime-role-arn" \
    --query 'Parameter.Value' --output text
  ```

**a. Secret resource policy** (in the owner account):

```bash
aws secretsmanager put-resource-policy \
  --secret-id "arn:aws:secretsmanager:<region>:<owner-account>:secret:db/prod-XXXX" \
  --resource-policy '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": { "AWS": [
        "arn:aws:iam::<accelerator-account>:role/{prefix}-sources-db-connector",
        "arn:aws:iam::<accelerator-account>:role/{prefix}-federated-catalog-role",
        "<serve-runtime-role-arn-from-ssm-above>"
      ]},
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "*"
    }]
  }'
```

**b. KMS key policy** — if the secret is encrypted with a customer-managed key,
add all three role ARNs to the key policy with `kms:Decrypt` (Context Ontology Accelerator's roles
already scope their KMS use to `kms:ViaService = secretsmanager.*`).

!!! note "No `<prefix>:namespace` tag needed cross-account"
    The `<prefix>:namespace` tag that in-account credential secrets must carry (see
    [Data Sources](sources.md)) does **not** apply to a cross-account secret —
    the accelerator does not own the owner account and cannot tag its secrets.
    Authorization here comes entirely from the secret's resource policy above
    (and the KMS key policy). The registration-time namespace-binding check
    detects that the secret ARN is in a different account and skips the tag
    requirement accordingly.

### 3. Register the source

Pass the owner-account secret ARN. No `crossAccountRoleArn` is needed when the
secret resource policy grants Context Ontology Accelerator's roles directly (recommended). Use
`crossAccountRoleArn` only if your security model requires Context Ontology Accelerator to assume a role
in the owner account to reach the secret.

```json
{
  "sourceType": "DATABASE",
  "name": "partner-postgres",
  "databaseSource": {
    "jdbcConfiguration": {
      "engine": "POSTGRESQL",
      "host": "db.cluster-xyz.<region>.rds.amazonaws.com",
      "port": 5432,
      "databaseName": "analytics",
      "credentialSecretArn": "arn:aws:secretsmanager:<region>:<owner-account>:secret:db/prod-XXXX",
      "crossAccountRoleArn": "arn:aws:iam::<owner-account>:role/{prefix}-datasource-access-secret-role"
    }
  }
}
```

If you use `crossAccountRoleArn`, that role's trust policy must allow the
`{prefix}-sources-db-connector` role to assume it, and its permissions must allow
`secretsmanager:GetSecretValue` (and `kms:Decrypt` if applicable) on the secret.

!!! important "The trust policy must require an ExternalId"
    Every cross-account assume presents an **ExternalId derived from the namespace
    the source belongs to**: `{prefix}-{namespaceId}`, where `{prefix}` is this
    page's `{project}-{env}` deployment prefix — so a dev deployment's value can
    never satisfy a prod trust policy. The value is computed server-side and is
    **not** accepted from the API request — that is what stops a caller who can
    create sources in one namespace from pointing a source at a role onboarded for
    a different namespace and reading its data.

    **Copy the value rather than assembling it.** The Connect Source form displays
    it beside the cross-account role field, and `GET /namespaces/{namespaceId}`
    returns it as `datasourceExternalId`. Both are the platform's own derivation;
    a hand-built value that differs by one character fails every assume with
    `AccessDenied` and nothing to point at.

    Condition your trust policy on that exact value. An assume with no ExternalId
    is denied by the platform's own IAM policy, so a role whose trust policy omits
    the condition is assumable by any namespace in the deployment — the condition
    is what makes the trust policy an authorization list.

    ```json
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": [
          "arn:aws:iam::<coa-account>:role/{prefix}-sources-db-connector",
          "arn:aws:iam::<coa-account>:role/{prefix}-sources-db-enrichment-agent"
        ]
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "sts:ExternalId": "<datasourceExternalId>" }
      }
    }
    ```

    **The principals above are the JDBC and Glue ones.** A
    `DATABRICKS_SQL_WAREHOUSE` source is reached by neither, and needs a different
    **pair**: the **sources API** role, which validates the wiring once at
    registration, and the **connector's** execution role, which reads the
    credential on every request. Naming only one of those fails — omit the sources
    API role and the create itself is refused. See
    [Databricks SQL Warehouse Sources](databricks-sources.md#resolve-the-two-principals-your-trust-policy-must-name)
    for how to resolve both ARNs, and both policies in full.

    **Existing sources:** a source onboarded before this control keeps using the
    `externalId` stored on its record, so nothing breaks. New sources ignore any
    `externalId` in the request. To migrate one, update the trust policy to the
    namespace's `datasourceExternalId` and re-register the source.

!!! important "The role naming convention is a real control, not just a form check"
    The example above sends `crossAccountRoleArn`. The role name must **start
    with** `{prefix}-datasource-access-` (e.g.
    `{prefix}-datasource-access-{customer}`), because the platform's own
    `sts:AssumeRole` policy is scoped to that name prefix and matches nothing else. A correctly-configured role under any other
    name is denied by that policy. The Connect Source form checks the name so the
    failure is legible at submit rather than arriving as an opaque `AccessDenied`
    at the first scan — the form is the earlier warning, not the control.

    The role's **account** is not constrained, and the deployment account is
    allowed: the platform is deployed into your account, so for a single-account
    deployment the role naturally lives there.

    The role's **partition** is constrained to `aws`, the commercial one. The same
    `sts:AssumeRole` policy is written `arn:aws:iam::*:role/{prefix}-datasource-access-*`,
    so an `aws-cn` or `aws-us-gov` role ARN can only be denied by it. A
    `DATABRICKS_SQL_WAREHOUSE` source refuses such an ARN at submit, with the
    partition named, rather than registering it and failing every query afterwards.

---

## Cross-Account Glue Data Catalog

Glue access has no network component — it's authorized by IAM and, when enabled,
Lake Formation.

!!! important "`crossAccountRoleArn` covers discovery, not querying"
    Cross-account Glue access happens at **two distinct stages**, governed
    differently:

>   - **Discovery (metadata)** — the discovery connector
      (`{prefix}-sources-db-connector`) **assumes `crossAccountRoleArn`** and
      calls `glue:GetDatabase`/`GetTables` as a principal in the owner account.
      Supplying the role is sufficient to scan a source successfully (tables and
      columns appear).
>   - **Query (data)** — Amazon Athena runs as the Context Ontology Accelerator **serve runtime role**
      and does **not** assume `crossAccountRoleArn`. Cross-account querying
      requires native AWS sharing: the owner shares the catalog
      (Glue resource policy / AWS RAM) and, if LF-governed, grants the serve
      runtime role `SELECT`/`DESCRIBE` (LF grants flow through RAM).

>   A source can therefore scan and show metadata while still being
    **non-queryable** until the catalog is shared with — and LF-granted to — the
    serve runtime role. Complete both the discovery role *and* the
    query-time sharing below for end-to-end access.

Determine which mode the owner's catalog uses:

```bash
# Run in the OWNER account. Empty CreateDatabaseDefaultPermissions ⇒ strict-LF.
aws lakeformation get-data-lake-settings \
  --query 'DataLakeSettings.CreateDatabaseDefaultPermissions'
```

- Non-empty (contains `ALL` for `IAM_ALLOWED_PRINCIPALS`) → **IAM-mode**
- Empty → **strict Lake Formation mode**

### 1. Register the source

```json
{
  "sourceType": "DATABASE",
  "name": "partner-lake",
  "databaseSource": {
    "glueConfiguration": {
      "databaseName": "hcp360",
      "region": "<region>",
      "catalogId": "<owner-account>"
    }
  }
}
```

`catalogId` set to the owner account ID tells Context Ontology Accelerator the catalog is cross-account.

### 2. Grant Context Ontology Accelerator catalog access (owner account)

Two Context Ontology Accelerator-side roles must be authorized — the **discovery connector**
(`{prefix}-sources-db-connector`, reads metadata) and the **serve runtime**
(queries data via Athena). Grant both for end-to-end access.

**IAM-mode** — a cross-account Glue catalog resource policy authorizes the
metadata reads. Include **both** roles so discovery *and* Athena query work:

```bash
# In the OWNER account
aws glue put-resource-policy --policy-in-json '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": [
      "arn:aws:iam::<accelerator-account>:role/{prefix}-sources-db-connector",
      "arn:aws:iam::<accelerator-account>:role/<serve-runtime-role>"
    ]},
    "Action": ["glue:GetDatabase","glue:GetDatabases","glue:GetTable","glue:GetTables","glue:GetPartitions"],
    "Resource": [
      "arn:aws:glue:<region>:<owner-account>:catalog",
      "arn:aws:glue:<region>:<owner-account>:database/hcp360",
      "arn:aws:glue:<region>:<owner-account>:table/hcp360/*"
    ]
  }]
}'
```

!!! note "Nested / federated catalogs (`catalogId` = `account:catalogName`)"
    The policy above covers the owner account's **default** Glue catalog
    (`catalogId` = the owner account id). A **federated** catalog — a Databricks
    Unity Catalog or Snowflake/Redshift Iceberg REST federation, addressed as
    `catalogId: "<owner-account>:<catalogName>"` — is a nested resource, and Glue
    authorizes `GetDatabase`/`GetTables` against the nested-catalog resource
    itself, not just its databases and tables. For that case add `glue:GetCatalog`
    to the `Action` array and the nested-catalog ARN to `Resource`:

    ```
    arn:aws:glue:<region>:<owner-account>:catalog/<catalogName>
    ```

    Without it the read is denied on `catalog/<catalogName>` even though the
    `database/`  and `table/` grants are present.

For **query-time** in IAM-mode, Athena also needs the catalog shared via AWS RAM
(and the underlying S3 data readable by the serve runtime role). A Glue resource
policy alone authorizes the API calls but Athena's cross-account catalog
resolution relies on the RAM share:

```bash
# In the OWNER account — share the database (and its tables) with the Context Ontology Accelerator account
aws ram create-resource-share --name accelerator-hcp360 \
  --resource-arns arn:aws:glue:<region>:<owner-account>:database/hcp360 \
  --principals <accelerator-account>
```

**Strict-LF mode** — additionally grant Lake Formation permissions. Context Ontology Accelerator
**cannot** self-grant across accounts (it is not an LF admin in the owner
account), so these grants are a required prerequisite. The first two grants
enable **discovery**; the third enables **querying**:

```bash
# In the OWNER account, as a Lake Formation admin
# Discovery connector — DESCRIBE on database + tables
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::<accelerator-account>:role/{prefix}-sources-db-connector \
  --resource '{"Database":{"Name":"hcp360"}}' --permissions DESCRIBE
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::<accelerator-account>:role/{prefix}-sources-db-connector \
  --resource '{"Table":{"DatabaseName":"hcp360","TableWildcard":{}}}' --permissions DESCRIBE

# Serve runtime — SELECT/DESCRIBE so Athena can query the data (query-time)
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::<accelerator-account>:role/<serve-runtime-role> \
  --resource '{"Table":{"DatabaseName":"hcp360","TableWildcard":{}}}' --permissions SELECT DESCRIBE
```

Resolve the serve runtime role ARN from SSM in the Context Ontology Accelerator account:

```bash
aws ssm get-parameter --name "/{prefix}/{envName}/serve/runtime-role-arn" \
  --query 'Parameter.Value' --output text
```

> If you onboard and the connection fails with a Lake Formation authorization
> error, the scan-job `errorMessage` returns the exact `grant-permissions`
> commands to run (including the resolved role ARNs). Run them in the owner
> account, then re-scan.

### 3. Enable cross-account querying (resource link)

The owner-side grants above let the source **scan** (discovery succeeds), but
Athena in the Context Ontology Accelerator account cannot query a database that only exists in the
owner's catalog. Cross-account Athena querying requires a **resource link** — a
local Glue database in the Context Ontology Accelerator account that points at the shared owner
database. Athena queries the resource-link name, not the original database.

Do this once per cross-account database, in the **Context Ontology Accelerator account**, as a Lake
Formation admin:

```bash
# 1. Accept the RAM share from the owner (automatic if both accounts are in the
#    same AWS Organization with resource sharing enabled; otherwise accept the
#    invitation explicitly).
aws ram get-resource-share-invitations \
  --query "resourceShareInvitations[?senderAccountId=='<owner-account>']"
aws ram accept-resource-share-invitation --resource-share-invitation-arn <invitation-arn>

# 2. Create a resource link pointing at the shared owner database.
aws glue create-database --catalog-id <accelerator-account> --database-input '{
  "Name": "hcp360_link",
  "TargetDatabase": { "CatalogId": "<owner-account>", "DatabaseName": "hcp360" }
}'

# 3. Grant the serve runtime role DESCRIBE on the LOCAL resource link
#    (in addition to the cross-account SELECT/DESCRIBE granted by the owner above).
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::<accelerator-account>:role/<serve-runtime-role> \
  --resource '{"Database":{"CatalogId":"<accelerator-account>","Name":"hcp360_link"}}' \
  --permissions DESCRIBE
```

Athena then queries the resource link:

```sql
SELECT * FROM "AwsDataCatalog"."hcp360_link"."patients" LIMIT 100;
```

!!! warning "Context Ontology Accelerator does not automate cross-account query setup"
    Context Ontology Accelerator's automatic serve-role grant
    (`grant_consumer_select_native`) targets **only the Context Ontology Accelerator account's own
    catalog** (`CatalogId = <accelerator-account>`) and does **not** create resource
    links. It self-heals same-account strict-LF sources but performs no
    cross-account grant or resource-link creation. For cross-account Glue, steps
    1–3 above are a manual prerequisite (owner-side LF grant + RAM accept +
    resource link + local DESCRIBE). This is a known limitation; cross-account
    discovery works via `crossAccountRoleArn`, but cross-account *querying* must
    be wired up manually.

---

## IAM-mode vs Lake Formation mode (same-account note)

For **same-account** catalogs Context Ontology Accelerator self-heals: in strict-LF mode the discovery
connector transiently assumes the LF-admin grantor role
(`{prefix}-sources-federation-provisioner`) and grants itself `DESCRIBE`, so
no manual grant is needed. This self-heal only works same-account — the grantor
is not an admin in other accounts, which is why cross-account requires the
explicit owner-side grants above.

---

## Quick checklist

**Cross-account JDBC**

- [ ] Private network path established; DB SG opens engine port to `{prefix}-connector-sg`
- [ ] Secret resource policy grants `{prefix}-sources-db-connector` + `{prefix}-federated-catalog-role` + the serve runtime role (`/{prefix}/{envName}/serve/runtime-role-arn` in SSM)
- [ ] KMS key policy grants `kms:Decrypt` to those three roles (CMK-encrypted secrets only)
- [ ] Source registered with owner-account `credentialSecretArn`

**Cross-account Glue**

- [ ] `catalogId` set to the owner account ID
- [ ] **Discovery:** Glue resource policy grants `glue:Get*` to `{prefix}-sources-db-connector` (+ strict-LF: LF `DESCRIBE` to the connector)
- [ ] **Query-time (owner account):** catalog shared via AWS RAM; serve runtime role can read the underlying S3 data (+ strict-LF: LF `SELECT`/`DESCRIBE` to the serve runtime role)
- [ ] **Query-time (Context Ontology Accelerator account):** RAM share accepted; resource link created pointing at the owner DB; serve runtime role granted `DESCRIBE` on the resource link

> `crossAccountRoleArn` only covers discovery. A source can scan and show
> metadata yet remain non-queryable until the query-time rows above are
> complete. Context Ontology Accelerator does not automate cross-account query setup — it is a manual
> prerequisite.

See [Structured Data Source Guide](index.md) for registration, scanning, review,
and querying once access is in place.

---

## Network connectivity

Reaching a database in a different VPC or account (VPC peering, Transit Gateway,
or PrivateLink; routes; security-group ingress; the `jdbc_peer_*` / `jdbc_tgw_*`
/ `jdbc_privatelink_*` CDK context keys) is covered in one place:
[Cross-Network Connectivity](cross-network-connectivity.md). The network path is
identical whether or not an account boundary is involved — this page adds only
the credential-sharing and catalog-authorization steps on top of it.

