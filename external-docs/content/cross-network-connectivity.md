# Cross-Network Connectivity

How to make an existing relational database reachable from your Context Ontology
Accelerator (COA) deployment when the database lives in a **different VPC** than
COA — whether that VPC is in the same account or another account, the same
Region or another Region.

This is the network half of onboarding a JDBC source. It is engine-agnostic:
the same peering / Transit Gateway / PrivateLink setup applies to
**PostgreSQL, Aurora (PostgreSQL and MySQL), MySQL, MariaDB, and SQL Server**.
Only the database port differs between engines.

For account-boundary concerns beyond the network path (sharing the credential
secret, cross-account catalog authorization), see
[Cross-Account Data Sources](cross-account-sources.md). This page is referenced
from there as the authoritative network walkthrough.

!!! note "When you do NOT need this page"
    If your database is already in the **same VPC** as COA, there is no routing
    to set up. Skip to [Open the database security group](#3-open-the-database-security-group)
    and add one inbound rule.

---

## Scenarios

| Situation | Connectivity | Notes |
|---|---|---|
| DB in COA's VPC | None needed | Deploy with the DB in COA's VPC; its SG allows the VPC CIDR |
| DB in a separate VPC, same account | VPC peering or Transit Gateway | Add routes both ways and open the DB SG (this page) |
| DB in a separate account | Peering / TGW + cross-account role & secret share | This page for the network path; [Cross-Account Data Sources](cross-account-sources.md) for credentials and catalog authorization |
| Overlapping CIDRs, or expose one endpoint only | PrivateLink | No cross-VPC routes needed |

---

## Why connectivity fails by default

COA runs its database work inside its **own VPC**:

- **Discovery / connection test** — the `{prefix}-sources-db-connector` Lambda,
  attached to COA's private subnets and the `{prefix}-lambda-sg` security group.
- **Federated query** — the Glue managed connector, using the
  `{prefix}-connector-sg` security group.
- **Direct-JDBC fast path** — the serve runtime role, from the same VPC.

When you create a datasource, the connector first does a raw TCP connect to
`host:port` with a **10-second timeout**. If COA's VPC has no route to the
database's VPC, that connect never completes and you see:

```json
{"check": "network", "status": "failed",
 "message": "Network: cannot reach <host>:<port> — timed out"}
```

A timeout here is almost always one of:

1. **No route** between COA's subnets and the database's subnets (different VPCs).
2. **The database security group** does not allow inbound from COA's security group.
3. The database is in a **subnet/AZ** the chosen path does not reach.

A database wizard (for example, the RDS "create database" console flow) places
the instance in the account's **default VPC** unless you choose otherwise. COA
deploys into a **separate VPC** it creates. Those two VPCs have no connectivity
until you add it — which is what this page covers.

---

## Engine ports

The network path is identical for every engine; only the destination port
changes. Use the port your instance actually listens on (a custom port
overrides the default).

| Engine | `engine` value | Default port |
|---|---|---|
| PostgreSQL | `POSTGRESQL` | 5432 |
| Aurora PostgreSQL | `POSTGRESQL` | 5432 |
| MySQL | `MYSQL` | 3306 |
| MariaDB | `MYSQL` | 3306 |
| Aurora MySQL | `MYSQL` | 3306 |
| SQL Server | `SQLSERVER` | 1433 |

!!! note "MariaDB and Aurora use an existing engine value"
    COA has no separate `MARIADB` or `AURORA` engine. MariaDB speaks the MySQL
    wire protocol, so set `engine=MYSQL` and port `3306`. Aurora is
    wire-compatible with its underlying engine, so an Aurora PostgreSQL cluster
    is `engine=POSTGRESQL` (5432) and an Aurora MySQL cluster is `engine=MYSQL`
    (3306). Point `host` at the **cluster endpoint** (writer) for Aurora.

---

## Choose a connectivity path

| Path | Use when | Cost / effort |
|---|---|---|
| **VPC peering** | Both VPCs in the same Region; a handful of VPCs; CIDRs do not overlap | Lowest. No hourly charge; pay only for cross-AZ data |
| **Transit Gateway (TGW)** | Many VPCs, a hub-and-spoke network, or cross-Region | Hourly attachment + data processing charges |
| **PrivateLink** | You want to expose only the one database endpoint, not the whole VPC; overlapping CIDRs are acceptable | NLB + endpoint hourly + data charges |

!!! warning "Overlapping CIDRs block peering and TGW"
    VPC peering and Transit Gateway both require **non-overlapping** CIDR
    ranges between COA's VPC and the database VPC. If they overlap (two default
    VPCs are both `172.31.0.0/16`, for example), you must use **PrivateLink**,
    recreate one VPC with a different CIDR, or place the database behind an NLB.

The rest of this page walks through **VPC peering** end to end (the common
case), then summarizes TGW and PrivateLink. COA can also manage any of these
for you declaratively — see
[Let COA manage connectivity](#let-coa-manage-the-connectivity-cdk).

---

## Manual setup: VPC peering

You need four things in place. Gather these identifiers first:

| Value | How to find it |
|---|---|
| COA VPC id + CIDR | `aws ec2 describe-vpcs` filtered to COA's VPC, or the `{prefix}` stack outputs |
| COA connector subnets + route tables | the subnets the `{prefix}-sources-db-connector` Lambda and the Glue connector use |
| COA security groups | `{prefix}-lambda-sg` (discovery) and `{prefix}-connector-sg` (federated query) |
| Database VPC id + CIDR, subnets, route table(s), security group | from the RDS/Aurora instance's networking details |

!!! tip "Resolve COA's VPC and subnets from SSM"
    COA publishes them under `/{prefix}/{envName}/network/…` (for example
    `.../network/vpc-id`, `.../network/private-subnet-ids`). Note the prefix is
    spelled as two path parts: a deployment prefixed `accelerator-dev` publishes
    `/accelerator/dev/network/vpc-id`.

### 1. Create and accept the peering connection

```bash
PCX=$(aws ec2 create-vpc-peering-connection \
  --vpc-id <coa-vpc-id> \
  --peer-vpc-id <db-vpc-id> \
  --peer-region <db-region> \
  --tag-specifications 'ResourceType=vpc-peering-connection,Tags=[{Key=Name,Value=coa-to-db}]' \
  --query 'VpcPeeringConnection.VpcPeeringConnectionId' --output text)

# Accept (run in the DB's account/Region if cross-account or cross-Region)
aws ec2 accept-vpc-peering-connection --vpc-peering-connection-id "$PCX"
```

Omit `--peer-region` when both VPCs are in the same Region. For a
**cross-account** peering, add `--peer-owner-id <db-account>`; the owner then
accepts it. Wait until the connection reports `Status.Code = active`.

### 2. Add routes on both sides

Peering carries traffic only where a route sends it. Add a route **in both
directions**, on every route table that serves the relevant subnets.

```bash
# COA side: for each route table serving COA's connector subnets,
# send the DB CIDR to the peering connection.
aws ec2 create-route --route-table-id <coa-connector-rtb> \
  --destination-cidr-block <db-vpc-cidr> \
  --vpc-peering-connection-id "$PCX"

# DB side: for each route table serving the DB subnets,
# send COA's CIDR back to the peering connection.
aws ec2 create-route --route-table-id <db-rtb> \
  --destination-cidr-block <coa-vpc-cidr> \
  --vpc-peering-connection-id "$PCX"
```

!!! warning "Cover every connector subnet"
    COA's connector subnets usually span two AZs, often with **different route
    tables**. Add the COA-side route to each. A missing route on one AZ's table
    produces an intermittent timeout that depends on which AZ the connector ENI
    landed in.

### 3. Open the database security group

Add one inbound rule on the **database's** security group allowing the engine
port **from COA's security group** (security-group references work across a
peering connection in the same Region):

```bash
aws ec2 authorize-security-group-ingress \
  --group-id <db-security-group> \
  --ip-permissions "IpProtocol=tcp,FromPort=<port>,ToPort=<port>,\
UserIdGroupPairs=[{GroupId=<coa-lambda-sg>,Description=COA discovery}]"
```

- Reference `{prefix}-lambda-sg` for discovery / connection test.
- Also reference `{prefix}-connector-sg` for federated (Athena) queries.
- Use the port from the [engine table](#engine-ports): `5432`, `3306`, or `1433`.

!!! note "SG references do not cross Regions"
    A security-group reference only works when both SGs are in the **same
    Region** (same-account or cross-account). For a **cross-Region** path,
    allow COA's VPC **CIDR** on the DB SG instead of referencing the SG.

COA's own security groups already permit **egress** on the standard database
ports (`5432, 3306, 1433, 5439, 1521`), so you do not normally touch COA's
egress rules.

### 4. Verify

Enable **DNS resolution** on the peering connection so the database's endpoint
hostname resolves from COA's VPC (otherwise the host resolves to nothing and
the connect fails even with routes in place):

```bash
aws ec2 modify-vpc-peering-connection-options \
  --vpc-peering-connection-id "$PCX" \
  --requester-peering-connection-options AllowDnsResolutionFromRemoteVpc=true \
  --accepter-peering-connection-options AllowDnsResolutionFromRemoteVpc=true
```

Recreate the datasource (or re-run its connection test). The `network` check
should now pass and move on to credentials and the database handshake. From a
host inside the database VPC you can also confirm the listener directly:

```bash
nc -vz <db-host> <port>      # expect "succeeded"
```

If the `network` check still times out, work through
[Troubleshooting](#troubleshooting).

---

## Transit Gateway (summary)

Use TGW for many VPCs or a cross-Region path. The security-group rule in
[step 3](#3-open-the-database-security-group) still applies (use a CIDR, not an
SG reference, across Regions).

1. Attach both COA's VPC and the database's VPC to the Transit Gateway
   (`aws ec2 create-transit-gateway-vpc-attachment` for each).
2. In the TGW route table, ensure each attachment can reach the other's CIDR
   (propagation or static routes).
3. In each VPC's **subnet** route tables, send the remote CIDR to the TGW
   (`--transit-gateway-id tgw-...` instead of `--vpc-peering-connection-id`).
4. Open the database security group on the engine port (step 3).

## PrivateLink (summary)

Use PrivateLink when CIDRs overlap or you want to expose only the one database
endpoint rather than route between whole VPCs.

1. In the **database** VPC, put a Network Load Balancer in front of the database
   (target the instance IP on the engine port) and create a **VPC endpoint
   service** pointing at that NLB.
2. In **COA's** VPC, create an **interface VPC endpoint** to that service in
   COA's connector subnets, with a security group that allows the engine port.
3. Point the datasource `host` at the **endpoint's DNS name** (enable private
   DNS, or use the generated `vpce-*.amazonaws.com` name).
4. The NLB security group / target health, not a peering route, governs
   reachability — no cross-VPC route table changes are needed.

---

## Let COA manage the connectivity (CDK)

If COA **creates** its own VPC (the default — you did not pass a `vpc_id`
context key), it can provision the peering / TGW / PrivateLink **and the
routes** for you from CDK context. This keeps the network path in
infrastructure-as-code so a redeploy does not lose it.

Pass the context keys at deploy time (`-c key=value`) or in `cdk.json`:

=== "VPC peering"

    ```bash
    cdk deploy -c jdbc_peer_vpc_id=<db-vpc-id> \
               -c jdbc_peer_cidrs=<db-vpc-cidr>[,<cidr2>] \
               [-c jdbc_peer_owner_id=<db-account>] \
               [-c jdbc_peer_region=<db-region>]
    ```
    `jdbc_peer_vpc_id` and `jdbc_peer_cidrs` are **required together**;
    `jdbc_peer_cidrs` is comma-separated. Add `jdbc_peer_owner_id` for
    cross-account and `jdbc_peer_region` for cross-Region.

=== "Transit Gateway"

    ```bash
    cdk deploy -c jdbc_tgw_id=<tgw-id> \
               -c jdbc_tgw_cidrs=<remote-cidr>[,<cidr2>]
    ```

=== "PrivateLink"

    ```bash
    cdk deploy -c jdbc_privatelink_service=com.amazonaws.vpce.<region>.vpce-svc-xxxx \
               -c jdbc_privatelink_port=<port> \
               [-c jdbc_privatelink_private_dns=<name>]
    ```

COA wires the resulting routes and SG rules to **both** connector security
groups (`{prefix}-lambda-sg` and `{prefix}-connector-sg`). You still add the
inbound rule on the **database** security group (step 3) — that lives in the
database's account/VPC, which COA does not manage.

!!! warning "Imported VPCs manage their own connectivity"
    If you deploy COA into an **existing** VPC (`-c vpc_id=<id>`), COA does
    **not** provision any peering/TGW/PrivateLink or routes — it assumes the
    imported VPC already brings its own. Set up the network path manually as in
    the sections above.

!!! danger "Manual routes are lost on VPC replacement"
    Routes and peering you add by hand (the manual sections above) are **not**
    tracked by COA's CDK. If a later `cdk deploy` replaces COA's VPC or subnets,
    those routes are gone and the datasource times out again. Prefer the context
    keys above for a COA-created VPC so the path is part of the stack.

!!! note "Integration-test automation"
    The maintainers' CI wires a cross-network path to companion test databases
    automatically as part of their internal pipeline. That test fixture is not
    part of the public mirror; the manual steps above are the supported path
    for your own databases.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `network ... cannot reach <host>:<port> — timed out` | No route, or DB SG does not allow COA's SG | Verify both-direction routes ([step 2](#2-add-routes-on-both-sides)) and the DB SG inbound rule ([step 3](#3-open-the-database-security-group)) |
| Network check passes intermittently | A route is missing on one of COA's AZ route tables | Add the DB-CIDR route to **every** connector-subnet route table |
| Peering stuck `pending-acceptance` | Not accepted (cross-account/Region) | Accept in the DB's account/Region |
| `create-route` fails `RouteAlreadyExists` | A route for that CIDR already exists | Confirm it targets the right peering/TGW; replace with `replace-route` if wrong |
| Timeout only cross-Region, SG reference rejected | SG references do not cross Regions | Allow COA's VPC **CIDR** on the DB SG instead of the SG id |
| Network passes, DB handshake fails | Credentials or TLS, not network | See [Sources](sources.md) and [Cross-Account](cross-account-sources.md) credential setup |
| Still timing out after routes + SG | Overlapping CIDRs silently misroute | Confirm COA and DB CIDRs do not overlap; if they do, use PrivateLink |

### Quick checklist

- [ ] COA VPC CIDR and database VPC CIDR **do not overlap**
- [ ] Peering/TGW/endpoint is **active**
- [ ] Route from COA connector subnets → DB CIDR, on **every** such route table
- [ ] Route from DB subnets → COA CIDR, on **every** such route table
- [ ] DB security group allows the engine port from `{prefix}-lambda-sg` (and `{prefix}-connector-sg` for federated queries)
- [ ] Datasource `host`/`port`/`engine` match the table in [Engine ports](#engine-ports) (Aurora → cluster endpoint; MariaDB → `MYSQL`)

---

## Related

- [Sources](sources.md) — creating a JDBC datasource, credentials, discovery
- [Cross-Account Data Sources](cross-account-sources.md) — secret sharing and catalog authorization across accounts
