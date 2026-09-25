// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as opensearchserverless from "aws-cdk-lib/aws-opensearchserverless";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import { Construct } from "constructs";

import { SCLStack } from "../../constructs/scl-stack";
import { AccessLogsBucket } from "../../constructs";
import { DynamoDBTable } from "../../constructs/dynamodb-table";
import { LakeFormationAdmin } from "../../constructs/lakeformation-admin";
import { SclMonitoring } from "../../constructs";
import {
  namespaceTagKey,
  resolveContext,
  resolveLambdaReservedConcurrency,
} from "../../context";
import { Paths, fromRoot } from "../../paths";
import { bundlePython } from "../../utils/python-bundling";
import { bedrockModelArn } from "../../utils/bedrock-utils";
import { NetworkStack } from "../foundation/network-stack";
import { StorageStack } from "../foundation/storage-stack";
import { TABLE_NAMES } from "@coa/shared";
import {
  SourceStatus,
  CONNECTOR_TAG_KEY,
  CONNECTOR_TAG_VALUE,
  DEFAULT_MAX_FILE_SIZE_MB,
  DEFAULT_BEDROCK_CHAT_MODEL_ID,
  DEFAULT_BEDROCK_MODEL_ID,
} from "../../constants";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";

export interface SourcesStackProps extends cdk.StackProps {
  readonly network: NetworkStack;
  readonly storage: StorageStack;
  readonly allowedOrigin?: string;
  /**
   * Bedrock model IDs, resolved from the SSM deploy config (#94). Omitted →
   * shared defaults, so a deployment configuring nothing is unchanged. Set for
   * non-US deploys, where the default `us.` inference profiles are not
   * invocable.
   */
  readonly bedrockChatModelId?: string;
  readonly bedrockEmbedModelId?: string;
  readonly bedrockEmbedDimensions?: number;
  /** Optional OE alarm action strategy (undefined in round one). */
  readonly alarmAction?: IAlarmActionStrategy;
}

/**
 * Service stack: Unified source registry for the SemanticContext platform.
 *
 * Provisions:
 *   - sources-table DDB: unified registry of all sources (DATABASE + DOCUMENTS)
 *       PK = NS#{namespaceId}, SK = SRC#{sourceId}
 *   - source-scan-jobs DDB: scan job history for database sources
 *       PK = SRC#{sourceId}, SK = <ISO timestamp>
 *   - sources-data S3 bucket: raw uploads and pre-processed staging for document sources
 *   - Database pipeline: ConnectorFn (discovery) + EnrichmentECS + DbScanStateMachine + DbScanQueue
 *   - Documents pipeline: PreprocessingFn + KgBuildECS + DocIngestionStateMachine + DocDeletionStateMachine + DocIngestionQueue
 *   - SourcesApiFn Lambda: unified REST API for all source types
 */
export class SourcesStack extends SCLStack {
  /** Unified sources DynamoDB table. */
  public readonly sourcesTable: dynamodb.Table;

  /** Source scan jobs DynamoDB table. */
  public readonly sourceScanJobsTable: dynamodb.Table;

  /** Sources API Lambda function ARN. */
  public readonly sourcesApiFnArn: string;

  constructor(scope: Construct, id: string, props: SourcesStackProps) {
    super(scope, id, props);
    this.addComponentTag("sources");

    const { ssmPrefix } = resolveContext(this.node);
    // Tag key binding a credential secret to the namespaces entitled to it.
    // Derived once here so every IAM condition below and the runtime env var
    // (RESOURCE_TAG_PREFIX) cannot drift from each other.
    const nsTagKey = namespaceTagKey(this.node);
    const allowedOrigin = props.allowedOrigin ?? "*";

    // ── Bedrock model IDs (#94) ────────────────────────────────────────
    // Resolved once so the doc-ingestion inference-profile ARN, the KG-build
    // container env, and the scan dashboard's ModelId dimension all derive from
    // the same value. Config wins; shared defaults otherwise.
    const chatModelId =
      props.bedrockChatModelId ?? DEFAULT_BEDROCK_CHAT_MODEL_ID;
    const embedModelId = props.bedrockEmbedModelId ?? DEFAULT_BEDROCK_MODEL_ID;
    const embedDimensions = String(props.bedrockEmbedDimensions ?? 1024);

    const vpc = props.network.vpc;
    const lambdaSecurityGroup = props.network.lambdaSecurityGroup;
    const ecsSecurityGroup = props.network.ecsSecurityGroup;
    const neptuneEndpoint = props.storage.neptuneClusterEndpoint;
    const neptuneClusterArn = props.storage.neptuneClusterArn;
    const opensearchEndpoint = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/opensearch/endpoint`,
    );
    const opensearchCollectionArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/opensearch/collection-arn`,
    );
    const opensearchCollectionName =
      ssm.StringParameter.valueForStringParameter(
        this,
        `${ssmPrefix}/opensearch/collection-name`,
      );

    // ── Resolve namespace resources via SSM ──────────────────────────
    const domainId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/smus/domain-id`,
    );
    const projectAccessRoleArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/smus/dz-project-access-role-arn`,
    );
    const namespacesTableName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/namespace/namespaces-table-name`,
    );
    const projectAccessRole = iam.Role.fromRoleArn(
      this,
      "ProjectAccessRole",
      projectAccessRoleArn,
    );
    const namespacesTable = dynamodb.Table.fromTableName(
      this,
      "NamespacesTable",
      namespacesTableName,
    );

    // ================================================================
    // DynamoDB Tables
    // ================================================================

    // ── sources-table ────────────────────────────────────────────────
    // PK = NS#{namespaceId}, SK = SRC#{sourceId}
    const sourcesConstruct = new DynamoDBTable(this, "SourcesTable", {
      tableName: TABLE_NAMES.SOURCES,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
    });
    sourcesConstruct.addGlobalSecondaryIndex({
      indexName: "ByNamespace",
      partitionKey: {
        name: "namespaceId",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: { name: "createdAt", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    sourcesConstruct.addGlobalSecondaryIndex({
      indexName: "BySourceType",
      partitionKey: {
        name: "namespaceId",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "sourceTypeCreatedAt",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    sourcesConstruct.addGlobalSecondaryIndex({
      // Used for O(1) name-uniqueness checks on source creation.
      indexName: "ByName",
      partitionKey: {
        name: "namespaceId",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: { name: "name", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });
    this.sourcesTable = sourcesConstruct.table;

    // ── source-scan-jobs ─────────────────────────────────────────────
    // PK = SRC#{sourceId}, SK = createdAt (ISO timestamp)
    const sourceScanJobsConstruct = new DynamoDBTable(
      this,
      "SourceScanJobsTable",
      {
        tableName: TABLE_NAMES.SOURCE_SCAN_JOBS,
        partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      },
    );
    sourceScanJobsConstruct.addGlobalSecondaryIndex({
      indexName: "ByNamespace",
      partitionKey: {
        name: "namespaceId",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.sourceScanJobsTable = sourceScanJobsConstruct.table;

    // ================================================================
    // S3 Bucket — raw uploads + pre-processed staging (documents)
    // ================================================================
    const sourcesAccessLogs = new AccessLogsBucket(this, "SourcesAccessLogs", {
      nameSuffix: "sources-logs",
    });
    const sourcesBucket = new s3.Bucket(this, "SourcesDataBucket", {
      bucketName: cdk.Lazy.string({
        produce: () => `${this.prefixed("sources-data")}-${this.account}`,
      }),
      versioned: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      serverAccessLogsBucket: sourcesAccessLogs.bucket,
      serverAccessLogsPrefix: "sources-data/",
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy:
        this.envName === "prod"
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: this.envName !== "prod",
      cors: [
        {
          allowedOrigins: [allowedOrigin],
          allowedMethods: [s3.HttpMethods.PUT],
          allowedHeaders: ["Content-Type"],
          maxAge: 3600,
        },
      ],
      lifecycleRules: [
        {
          prefix: "extracted/",
          expiration: cdk.Duration.days(30),
        },
      ],
    });

    // ================================================================
    // ECR repo context (shared between database enrichment + documents)
    // ================================================================
    const ecrRepositoryArn = this.node.tryGetContext("ecr_repository_arn") as
      | string
      | undefined;
    const ecrRepositoryName = this.node.tryGetContext("ecr_repository_name") as
      | string
      | undefined;
    const ecrRepo =
      ecrRepositoryArn && ecrRepositoryName
        ? ecr.Repository.fromRepositoryAttributes(this, "SourcesEcrRepo", {
            repositoryArn: ecrRepositoryArn,
            repositoryName: ecrRepositoryName,
          })
        : undefined;

    // ================================================================
    // Database Pipeline — Connector Lambda + Enrichment ECS + SFN
    // ================================================================

    // ── Connector Service Lambda ─────────────────────────────────────
    const dbConnectorDlq = new sqs.Queue(this, "DbConnectorDLQ", {
      queueName: this.prefixed("sources-db-connector-dlq"),
      retentionPeriod: cdk.Duration.days(14),
    });

    // Federated connections/catalogs are named `{sanitizedPrefix}ds_{hash}` by
    // the provisioner (prefix lowercased, non-alphanumerics stripped), so scope
    // IAM to that exact pattern — `this.prefixed("*")` (e.g. `coa-dev-*`) would
    // NOT match `scldevds_*`.
    const fedResourcePrefix =
      this.prefixed("")
        .toLowerCase()
        .replace(/[^a-z0-9]/g, "") + "ds_";

    // Glue Data Catalog / Lake Formation role for managed federated catalogs.
    // Set as ROLE_ARN on each federated Glue connection and used by Lake
    // Formation to vend credentials for the managed (no-Lambda) connector.
    // Must be assumable by Glue and Lake Formation.
    const federatedCatalogRole = new iam.Role(this, "FederatedCatalogRole", {
      roleName: this.prefixed("federated-catalog-role"),
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal("glue.amazonaws.com"),
        new iam.ServicePrincipal("lakeformation.amazonaws.com"),
      ),
    });
    props.storage.athenaSpillBucket.grantReadWrite(federatedCatalogRole);
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["glue:GetConnection"],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:connection/${fedResourcePrefix}*`,
        ],
      }),
    );
    // glue:ManagedConnector does not support resource-level restrictions.
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "GlueManagedConnectorExecution",
        actions: ["glue:ManagedConnector"],
        resources: ["*"],
      }),
    );
    // The AWS-managed federated connector reads the credential secret AS this
    // role. Two statements, because the tag condition only applies in-account:
    //
    // (1) In-account secrets must carry a `{prefix}:namespace` tag — same
    //     onboarded-only reduction as the discovery role. ponytail: tag-EXISTS,
    //     not an exact match (shared role); exact binding is at registration and
    //     on the serve resource policy.
    // (2) Cross-account secrets (a customer's secret in their own account) can't
    //     carry a tag we control, so they stay unconditioned here — access is
    //     gated by the secret's own resource policy, which the customer grants to
    //     this role (see cross-account docs).
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadCredentialSecretInAccount",
        actions: ["secretsmanager:GetSecretValue"],
        resources: ["arn:aws:secretsmanager:*:*:secret:*"],
        conditions: {
          StringEquals: { "aws:ResourceAccount": this.account },
          Null: { [`secretsmanager:ResourceTag/${nsTagKey}`]: "false" },
        },
      }),
    );
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadCredentialSecretCrossAccount",
        actions: ["secretsmanager:GetSecretValue"],
        resources: ["arn:aws:secretsmanager:*:*:secret:*"],
        conditions: {
          StringNotEquals: { "aws:ResourceAccount": this.account },
          // A negated condition evaluates TRUE when its key is absent, so
          // StringNotEquals alone would make this statement unconditioned in any
          // request context that does not populate aws:ResourceAccount. Require
          // the key to be present, so "not my account" can only be satisfied by
          // an account that was actually resolved.
          Null: { "aws:ResourceAccount": "false" },
        },
      }),
    );
    // Decrypt CMK-encrypted secrets, restricted to Secrets Manager (not
    // arbitrary KMS use). resources:['*'] is intentional — the customer's CMK
    // key id isn't known at synth time, and cross-account CMKs require an
    // explicit key-policy grant from the customer regardless. The
    // kms:ViaService condition confines this to Secrets-Manager-mediated
    // decrypts, so the role can't use it for any other KMS operation.
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DecryptCredentialSecret",
        actions: ["kms:Decrypt"],
        resources: ["*"],
        conditions: {
          StringLike: { "kms:ViaService": "secretsmanager.*.amazonaws.com" },
        },
      }),
    );
    // EC2 ENI permissions — the managed connector validates and runs inside the
    // connection's VPC; without these, connection creation fails with
    // "Unable to access VPC provided in the connection".
    const connectorSgId = props.network.connectorSecurityGroup.securityGroupId;
    // ec2:Describe* actions do not support resource-level permissions and must
    // use "*" (AWS limitation); they are read-only and low risk.
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Ec2NetworkDescribe",
        actions: [
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeVpcs",
          "ec2:DescribeRouteTables",
          "ec2:DescribeAvailabilityZones",
        ],
        resources: ["*"],
      }),
    );
    // Mutating ENI actions — MUST be `*`, not resource-scoped.
    //
    // Glue's managed connector runs a PRE-FLIGHT authorization check before it
    // touches any real ENI: it dry-runs CreateNetworkInterface,
    // DescribeNetworkInterfaces and DeleteNetworkInterface. That check authorizes
    // against the WILDCARD resource `arn:aws:ec2:<region>:<account>:*/*`, which no
    // enumeration of concrete resource types can satisfy. Captured from CloudTrail
    // in a failing account (2026-08-06):
    //
    //   ec2:DeleteNetworkInterface  Client.UnauthorizedOperation
    //   "...is not authorized to perform: ec2:DeleteNetworkInterface on resource:
    //    arn:aws:ec2:us-east-1:<acct>:*/* because no identity-based policy allows
    //    the ec2:DeleteNetworkInterface action"
    //
    // The connection is then reported as
    //   "Unable to access VPC provided in the connection. Please check SubnetId and
    //    SecurityGroup passed in the request and the policies and trust
    //    relationships on the IAM role."
    // — a generic message that names the subnet and SG and never mentions which
    // action was denied, which is why this was repeatedly misdiagnosed as a
    // networking problem. It is not: the SG, subnet, route table and peering are
    // all irrelevant to this failure. Verified by experiment in the failing
    // account: 4 consecutive connections FAILED with the scoped policy, and one
    // created ~10s after adding these actions on `*` reached READY.
    //
    // AWS's own managed policy for Glue (`AWSGlueServiceRole`) grants
    // CreateNetworkInterface / DeleteNetworkInterface / DescribeNetworkInterfaces
    // on `Resource: ["*"]` for exactly this reason, so this matches the documented
    // service contract rather than loosening past it.
    //
    // CAUTION when tightening: Glue CACHES a successful validation per
    // (role, VPC, subnet, security group). After one success, later connections
    // skip the pre-flight and keep succeeding even if the policy is narrowed
    // again — so a narrowing looks safe in a warm account and only breaks the
    // next environment built from scratch. Test any change here in a fresh VPC.
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Ec2NetworkInterfaceManagement",
        actions: [
          "ec2:CreateNetworkInterface",
          "ec2:DeleteNetworkInterface",
          "ec2:CreateTags",
        ],
        resources: ["*"],
      }),
    );

    // ec2:CreateNetworkInterfacePermission is REQUIRED for Glue managed/VPC
    // connections: after creating the ENI, Glue must grant its managed service
    // account permission to attach it. Without it the connection fails at
    // validation with "Unable to access VPC provided in the connection ... check
    // ... the policies and trust relationships on the IAM role" — even when the
    // subnet routes to the source and the SG is correct (confirmed live 2026-07-29:
    // MySQL/SQLServer federated connections FAILED with that message while the
    // connector subnet had an active peering route to the DB VPC and a valid
    // self-referencing SG).
    //
    // This action's grantee is NOT expressible via the resource ARN, so it lives
    // in its own statement with an `ec2:AuthorizedService` condition confining the
    // grant to Glue — least-privilege: the role cannot hand ENI-attach permission
    // to an arbitrary service/account. Glue managed connections are the only
    // consumer, so scoping to glue.amazonaws.com preserves the verified flow.
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Ec2CreateNetworkInterfacePermissionForGlue",
        actions: ["ec2:CreateNetworkInterfacePermission"],
        resources: [
          `arn:aws:ec2:${this.region}:${this.account}:network-interface/*`,
        ],
        conditions: {
          StringEquals: { "ec2:AuthorizedService": "glue.amazonaws.com" },
        },
      }),
    );

    const dbConnectorFn = new lambda.Function(this, "DbConnectorFn", {
      functionName: this.prefixed("sources-db-connector"),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "coa_sources.database.pipeline.discovery_handler.handler",
      code: bundlePython({
        srcDirs: [
          // Full src/ tree needed: handler is in database/pipeline/, not api/
          fromRoot("packages/sources/src"),
          Paths.commonLib,
          Paths.smithyGeneratedControlPlanePythonServer,
        ],
        requirementsFile: fromRoot("packages/sources/requirements.txt"),
        architecture: "arm64",
      }),
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [
        lambdaSecurityGroup,
        props.network.discoveryOcspSecurityGroup,
      ],
      deadLetterQueue: dbConnectorDlq,
      environment: {
        SOURCES_TABLE: this.sourcesTable.tableName,
        SOURCE_SCAN_JOBS_TABLE: this.sourceScanJobsTable.tableName,
        NAMESPACES_TABLE: namespacesTableName,
        SMUS_DOMAIN_ID: domainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
        // BARE prefix — keys the namespace tag this handler re-verifies against the
        // STORED row: the credential secret before the connector reads it, and the
        // Glue database's ownership before it reads the catalog or samples rows.
        // Must equal the prefix in `nsTagKey`, which the IAM conditions are written
        // against: derive the key from a different prefix here and discovery would
        // read a tag nobody writes.
        RESOURCE_TAG_PREFIX: resolveContext(this.node).prefix,
        // Parallel DataZone asset writes per discovery invocation. The scan
        // queue runs one discovery per source at a time, so this is the only
        // concurrency hitting DataZone from discovery.
        DATAZONE_WRITE_PARALLELISM: "10",
        // Guardrail: fail fast above this table count with an actionable
        // message (use schema/table filters) instead of timing out. Sized to
        // what the parallel writer clears within the 15-min Lambda timeout.
        MAX_TABLES_PER_SOURCE: "10000",
        // Enables Athena-based enum-value sampling for Glue-catalog sources
        // (SELECT DISTINCT via Athena → coa:distinctValues). When unset the
        // sampler is a no-op; the JDBC sampling path is unaffected.
        ATHENA_SPILL_BUCKET: props.storage.athenaSpillBucket.bucketName,
        // Two consumers, both needing the `{prefix}-{env}-` form:
        //  - the ExternalId this deployment presents when assuming a customer
        //    datasource-access role (discovery_handler._external_id);
        //  - the Glue namespace-ownership check, which recognises this deployment's
        //    own federated catalogs by the `{sanitizedPrefix}ds_` shape derived from
        //    it. Left unset, the runtime default (`coa-dev-`) would disagree with
        //    `fedResourcePrefix` and our own catalogs would read as third-party.
        RESOURCE_PREFIX: this.prefixed(""),
        // Re-scan backup blob (pre-rescan asset forms + change-set) is written
        // here before a merge overwrites live assets, and read back by the
        // bulk-review worker on approve/reject. Same bucket as the API/worker.
        BUCKET_NAME: sourcesBucket.bucketName,
      },
    });

    this.sourcesTable.grantReadWriteData(dbConnectorFn);
    this.sourceScanJobsTable.grantReadWriteData(dbConnectorFn);
    // Read-only: the db-connector (discovery/scan pipeline) only reads
    // dataZoneProjectId from namespace records. The sourceCount counter is
    // maintained exclusively by the sources API Lambda, which is granted
    // read-write separately.
    namespacesTable.grantReadData(dbConnectorFn);
    projectAccessRole.grantAssumeRole(dbConnectorFn.role!);

    // Glue catalog access: same-account, deployment region only.
    // Glue resources are region-scoped; cross-region access is not required
    // for federated catalogs and would broaden the blast radius unnecessarily.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "GlueCatalogAccess",
        actions: [
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:GetTable",
          "glue:GetTables",
          "glue:GetPartitions",
          "glue:GetConnection",
          // Nested/federated Glue catalogs (catalogId "account:catalogName")
          // authorize GetDatabase/GetTables against the nested-catalog resource
          // itself, not just its children — without these a federated source
          // fails discovery with AccessDenied on `catalog/<name>` (issue 118).
          "glue:GetCatalog",
          "glue:GetCatalogs",
          // Reads the `coa:namespace` tag by which a database owner declares
          // which namespaces may catalog it — the authorization this role's
          // otherwise account-wide `database/*` read is checked against before
          // discovery runs. Without it the check fails closed and every native
          // Glue source is refused.
          "glue:GetTags",
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          // The nested-catalog resource federated reads authorize against.
          // Account-wide because catalog names arrive per-source at runtime and
          // are not knowable at synth; mirrors the federation-provisioner and
          // serve grants. Actions stay read-only.
          `arn:aws:glue:${this.region}:${this.account}:catalog/*`,
          `arn:aws:glue:${this.region}:${this.account}:database/*`,
          `arn:aws:glue:${this.region}:${this.account}:table/*/*`,
          `arn:aws:glue:${this.region}:${this.account}:connection/*`,
        ],
      }),
    );
    // Athena enum-value sampling for Glue sources (SELECT DISTINCT → distinctValues).
    // Non-fatal in code, so a denial degrades to "no sample values", not a failure.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "AthenaEnumSampling",
        actions: [
          "athena:StartQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults",
          "athena:StopQueryExecution",
          "athena:GetWorkGroup",
        ],
        resources: [
          `arn:aws:athena:${this.region}:${this.account}:workgroup/*`,
        ],
      }),
    );
    // Athena data-catalog resolution for CUSTOM_CONNECTOR (custom connector)
    // sources. Discovery runs `SHOW DATABASES` / `SHOW TABLES` / `DESCRIBE`
    // against the Lambda-backed catalog the sources API registered at source
    // create, and Athena resolves the catalog name → connector ARN through
    // GetDataCatalog. Scoped to the same `{sanitizedPrefix}ds_*` names the
    // registrar derives, so this reaches only catalogs this deployment created.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CustomConnectorCatalogRead",
        actions: ["athena:GetDataCatalog"],
        resources: [
          `arn:aws:athena:${this.region}:${this.account}:datacatalog/${fedResourcePrefix}*`,
        ],
      }),
    );
    // Invoke a customer-authored Athena federation connector — but only when
    // Athena is the one doing it. The connector Lambda lives in the CUSTOMER's
    // account and its ARN is unknown at deploy time, so the resource cannot be
    // enumerated; containment is by condition key plus the customer's own
    // Lambda resource policy, which must independently name this role.
    //
    // `aws:CalledVia` is populated on forward access sessions, so this Allow
    // matches only while Athena is executing a statement for this role and
    // never for a direct `lambda:Invoke` from discovery code. It is multi-valued
    // and its order cannot be constrained, hence ForAnyValue — AWS documents
    // "somewhere in the chain" as the intended semantics. Evaluation fails
    // CLOSED: an absent key does not match, so a bug here denies rather than
    // widens.
    //
    // Region-pinned by choice, not by necessity: Athena CAN invoke a connector
    // in another region when given its full ARN, but we do not support that
    // topology, and the control-plane rejects such an ARN at source-create.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "AthenaFederationConnectorInvoke",
        actions: ["lambda:InvokeFunction"],
        // The account MUST stay a wildcard — the connector lives in the customer's
        // — so the ARN cannot scope this. A resource TAG does: Lambda evaluates
        // aws:ResourceTag natively for InvokeFunction, with no per-resource opt-in,
        // so an untagged function is simply unreachable. Preferred over a name
        // convention because a tag cannot be matched by accident.
        resources: [`arn:aws:lambda:${this.region}:*:function:*`],
        conditions: {
          "ForAnyValue:StringEquals": {
            "aws:CalledVia": "athena.amazonaws.com",
          },
          StringEquals: {
            [`aws:ResourceTag/${CONNECTOR_TAG_KEY}`]: CONNECTOR_TAG_VALUE,
          },
        },
      }),
    );
    // The escalation the Allow above would otherwise open, closed explicitly.
    //
    // `aws:CalledVia` is satisfied by an Athena UDF
    // (`USING EXTERNAL FUNCTION ... LAMBDA '<arn>'`), which needs only
    // StartQueryExecution — already granted above — plus lambda:InvokeFunction. A
    // same-account invoke also needs no resource policy, so without this Deny the
    // Allow reaches every in-region Lambda in THIS account, including the
    // federation provisioner that holds Lake Formation admin.
    //
    // A same-account Deny rather than an `aws:ResourceAccount` exclusion on the
    // Allow, because excluding the account would also rule out a connector deployed
    // alongside this stack — which is how the reference connector and its
    // integration test are deployed.
    //
    // Conditioned on `aws:CalledVia` so it cannot affect direct invokes; Athena has
    // no reason to invoke one of ours.
    //
    // NOT scoped to our name prefix. It was, and that made a naming convention
    // load-bearing for security while silently refusing any connector deployed into
    // this account under the prefix — which is what the reference connector's own
    // deploy script does. The tag exemption below expresses the real intent
    // directly, so the prefix is gone and the statement is now account-wide and
    // region-wide. Breadth is the safe direction for a Deny.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "DenyAthenaInvokeOfUntaggedFunctions",
        effect: iam.Effect.DENY,
        actions: ["lambda:InvokeFunction"],
        resources: [`arn:aws:lambda:*:${this.account}:function:*`],
        conditions: {
          "ForAnyValue:StringEquals": {
            "aws:CalledVia": "athena.amazonaws.com",
          },
          // StringNotEquals matches an ABSENT key, so an untagged function stays
          // denied — fail-closed, the direction a Deny needs. Same constants as the
          // Allow above, so the two agree by construction.
          //
          // Not a no-op against that Allow: what this still catches is an Athena UDF
          // (`USING EXTERNAL FUNCTION ... LAMBDA '<arn>'`) pointed at any
          // same-account function lacking the tag, the federation provisioner that
          // holds Lake Formation admin included.
          //
          // The residual is one of our own functions ACQUIRING the tag, which CDK's
          // `Tags.of(scope)` propagation makes the realistic path. Closed at build
          // time by infra/test/app-connector-tag.test.ts, which asserts no
          // synthesised resource in this app carries it.
          StringNotEquals: {
            [`aws:ResourceTag/${CONNECTOR_TAG_KEY}`]: CONNECTOR_TAG_VALUE,
          },
        },
      }),
    );
    // Lake Formation data access for governed Glue tables (ignored in
    // IAM_ALLOWED_PRINCIPALS accounts).
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "LakeFormationSelectForSampling",
        actions: ["lakeformation:GetDataAccess"],
        resources: ["*"],
      }),
    );
    // Athena writes query results to the shared spill bucket.
    props.storage.athenaSpillBucket.grantReadWrite(dbConnectorFn);
    // Read: Athena reads table data during enum sampling for Glue sources
    // backed by the sources-data bucket. Write: a re-scan writes its pre-rescan
    // backup blob here before merging onto live assets (approve/reject reads it).
    sourcesBucket.grantReadWrite(dbConnectorFn);
    // ── Federation provisioner (JDBC-only, dedicated role) ───────────
    // Isolated from discovery so the Lake Formation data-lake-admin privilege
    // required to create managed federated catalogs lives only on this
    // single-purpose role. Invoked as its own scan-pipeline step; the handler
    // self-gates to JDBC sources and is fully non-fatal.
    const federationProvisionerFn = new lambda.Function(
      this,
      "FederationProvisionerFn",
      {
        functionName: this.prefixed("sources-federation-provisioner"),
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.ARM_64,
        handler: "coa_sources.database.pipeline.federation_handler.handler",
        code: bundlePython({
          srcDirs: [
            fromRoot("packages/sources/src"),
            Paths.commonLib,
            Paths.smithyGeneratedControlPlanePythonServer,
          ],
          requirementsFile: fromRoot("packages/sources/requirements.txt"),
          architecture: "arm64",
        }),
        timeout: cdk.Duration.minutes(5),
        memorySize: 512,
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [lambdaSecurityGroup],
        environment: {
          SOURCES_TABLE: this.sourcesTable.tableName,
          // Glue Connections accept one SubnetId, so each federated query lives
          // in a single AZ (multi-AZ resilience is future work).
          CONNECTOR_SECURITY_GROUP_ID:
            props.network.connectorSecurityGroup.securityGroupId,
          CONNECTOR_SUBNET_ID: vpc.selectSubnets({
            subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          }).subnetIds[0],
          RESOURCE_PREFIX: this.prefixed(""),
          ATHENA_SPILL_BUCKET: props.storage.athenaSpillBucket.bucketName,
          FEDERATED_CATALOG_ROLE_ARN: federatedCatalogRole.roleArn,
          // SSM param holding the consumer query principal (serve runtime role)
          // ARN; resolved at runtime to grant LF SELECT on the federated catalog.
          CONSUMER_QUERY_ROLE_SSM_PARAM: `${ssmPrefix}/serve/runtime-role-arn`,
          // BARE prefix — keys the namespace tag this handler conditions the
          // serve-side secret resource policy on. Same value as `nsTagKey`.
          RESOURCE_TAG_PREFIX: resolveContext(this.node).prefix,
        },
      },
    );
    const fedFnRole = federationProvisionerFn.role!;
    this.sourcesTable.grantReadWriteData(federationProvisionerFn);
    // create_connection validation checks the caller can access the spill bucket.
    props.storage.athenaSpillBucket.grantReadWrite(federationProvisionerFn);

    // Before provisioning, the handler assumes the federated-catalog role and
    // tries to read the credential secret — verifying the managed connector can
    // actually read it (rather than creating a connection that fails silently at
    // query time, e.g. a cross-account secret whose resource policy doesn't yet
    // grant the connector role). Read-only precheck; the catalog role grants no
    // mutating access.
    federatedCatalogRole.grantAssumeRole(fedFnRole);
    federatedCatalogRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        principals: [fedFnRole],
      }),
    );

    // Glue Connections — created per source (named {prefix}ds_*).
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "GlueConnectionManagement",
        actions: [
          "glue:CreateConnection",
          "glue:DeleteConnection",
          "glue:GetConnection",
          "glue:UpdateConnection",
          "glue:PassConnection",
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:connection/${fedResourcePrefix}*`,
        ],
      }),
    );
    // Glue federated catalog management — one managed catalog per data source.
    // GetDatabase on nested catalog databases is required for
    // lakeformation:GrantPermissions on provisioned schemas.
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "GlueFederatedCatalogManagement",
        actions: [
          "glue:CreateCatalog",
          "glue:DeleteCatalog",
          "glue:GetCatalog",
          "glue:GetDatabase",
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:catalog/${fedResourcePrefix}*`,
          `arn:aws:glue:${this.region}:${this.account}:database/${fedResourcePrefix}*/*`,
        ],
      }),
    );
    // lakeformation:GrantPermissions validates that the GRANTOR can access the
    // Glue resource it's granting — so granting SELECT/DESCRIBE requires
    // glue:GetDatabase/GetTable(s) on the target resource.
    //
    // Two grant paths exist:
    //  1. Federated catalogs (JDBC): catalog name carries the prefix, so the
    //     prefix pattern scopes these reads to our own federated catalogs.
    //  2. Native Glue databases (GLUE_DATABASE): targets AwsDataCatalog, so
    //     the database/* resource pattern must cover all native databases.
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "GlueFederatedCatalogRead",
        actions: [
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:GetTable",
          "glue:GetTables",
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:catalog/${fedResourcePrefix}*`,
          `arn:aws:glue:${this.region}:${this.account}:database/${fedResourcePrefix}*`,
          `arn:aws:glue:${this.region}:${this.account}:table/${fedResourcePrefix}*`,
        ],
      }),
    );
    // Native Glue database read — required for lakeformation:GrantPermissions
    // on GLUE_DATABASE (S3/Iceberg) sources in strict-LF accounts. The LF
    // grant API validates that the grantor can read the target database/tables.
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "GlueNativeDatabaseRead",
        actions: [
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:GetTable",
          "glue:GetTables",
          // The namespace-ownership tag this role re-checks before granting the
          // SHARED serve runtime role SELECT on a native Glue database. It holds
          // Lake Formation admin, so it verifies rather than assumes.
          "glue:GetTags",
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:database/*`,
          `arn:aws:glue:${this.region}:${this.account}:table/*/*`,
        ],
      }),
    );
    // Lake Formation: register/deregister the connection as a federated
    // resource and grant IAM_ALLOWED_PRINCIPALS on provisioned databases.
    // These do not support resource-level scoping.
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "LakeFormationFederation",
        actions: [
          "lakeformation:RegisterResource",
          "lakeformation:DeregisterResource",
          "lakeformation:DescribeResource",
          // Grant the consumer query principal SELECT/DESCRIBE on the federated
          // catalog at provision time. LF grant APIs don't support resource scoping.
          "lakeformation:GrantPermissions",
        ],
        resources: ["*"],
      }),
    );
    // Read the consumer query role ARN (serve runtime role) at provision time
    // to scope the Lake Formation grant to that principal.
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "ReadConsumerQueryRoleParam",
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter${ssmPrefix}/serve/runtime-role-arn`,
        ],
      }),
    );
    // Grant the consumer query principal (runtime role) read access to JDBC
    // source credential secrets via resource-based policy at provision time.
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "SecretResourcePolicyForConsumer",
        actions: [
          "secretsmanager:GetResourcePolicy",
          "secretsmanager:PutResourcePolicy",
        ],
        resources: ["arn:aws:secretsmanager:*:*:secret:*"],
        conditions: {
          StringEquals: {
            "aws:ResourceAccount": this.account,
          },
          // The provisioner attaches a resource policy to the customer's
          // credential secret. Without this, that PutResourcePolicy is a write
          // primitive over EVERY in-account secret (a caller could get the
          // provisioner to mutate the policy of a secret it has no business
          // touching). Restrict it to secrets already onboarded to a namespace
          // (carrying a `{prefix}:namespace` tag). ponytail: tag-EXISTS, not an exact
          // match — the provisioner role is shared across namespaces; the exact
          // namespace is enforced by the registration check that gates whether
          // provisioning runs at all, and by the condition the provisioner
          // writes INTO the resource policy (federation_handler).
          Null: {
            [`secretsmanager:ResourceTag/${nsTagKey}`]: "false",
          },
        },
      }),
    );
    // Read the credential secret's TAGS to re-verify the namespace binding on the
    // stored row before the provisioner touches the secret. Metadata only — this
    // role deliberately has no GetSecretValue of its own; the readability precheck
    // reads the secret as the federated-catalog role instead.
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "DescribeSecretForNamespaceBinding",
        actions: ["secretsmanager:DescribeSecret"],
        resources: [`arn:aws:secretsmanager:*:${this.account}:secret:*`],
      }),
    );
    // Pass + read the federated-catalog role (Glue connection ROLE_ARN and LF
    // register-resource RoleArn). GetRole is a separate statement — it can't
    // carry the PassedToService condition.
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "PassFederatedCatalogRole",
        actions: ["iam:PassRole"],
        resources: [federatedCatalogRole.roleArn],
        conditions: {
          StringEquals: {
            "iam:PassedToService": [
              "glue.amazonaws.com",
              "lakeformation.amazonaws.com",
            ],
          },
        },
      }),
    );
    fedFnRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "GetFederatedCatalogRole",
        actions: ["iam:GetRole"],
        resources: [federatedCatalogRole.roleArn],
      }),
    );
    // Lake Formation admin requirement: creating a federated catalog from a
    // per-source connection requires DATA_LOCATION_ACCESS on that connection,
    // which only an LF data-lake admin can grant (verified — non-admin
    // self-grant and hybrid-access mode both fail). This dedicated role must be
    // registered as an LF data-lake admin. That is an account-global setting,
    // so it is managed centrally (foundation / LF bootstrap) rather than
    // clobbered from this service stack: add the role ARN below to the Lake
    // Formation DataLakeAdmins.
    const fedRoleArnParam = new ssm.StringParameter(
      this,
      "SsmFederationProvisionerRoleArn",
      {
        parameterName: `${ssmPrefix}/sources/federation-provisioner-role-arn`,
        stringValue: fedFnRole.roleArn,
        description:
          "Role that must be a Lake Formation data-lake admin to provision federated catalogs",
      },
    );
    // Register the federation provisioner role as an LF data-lake admin
    // non-destructively (read current admins → append → write), instead of the
    // declarative CfnDataLakeSettings which would overwrite existing admins.
    // No bootstrap step on any account: PutDataLakeSettings is authorized by the
    // IAM action, which the custom resource's Lambda role is granted, not by that
    // role's own DataLakeAdmins membership. See LakeFormationAdmin.
    const lfAdmin = new LakeFormationAdmin(
      this,
      "FederationProvisionerLfAdmin",
      {
        roleArnSsmParameterName: `${ssmPrefix}/sources/federation-provisioner-role-arn`,
        roleArn: fedFnRole.roleArn,
      },
    );
    lfAdmin.node.addDependency(fedRoleArnParam);

    // ── Strict-LF self-heal for the discovery connector ──────────────
    // The discovery connector is intentionally NOT a Lake Formation admin
    // (least privilege). In strict-LF accounts its Glue reads are denied until
    // it holds an LF DESCRIBE grant. To self-heal without a separate pipeline
    // step, the connector transiently assumes the LF-admin *grantor* (the
    // federation provisioner role) and grants itself DESCRIBE on the target DB,
    // then retries. The assume is scoped to this single role; the connector
    // gains no standing LF-admin privilege. Cross-account catalogs (where the
    // grantor isn't an admin) fall back to an actionable onboarding error.
    dbConnectorFn.addEnvironment("LF_GRANTOR_ROLE_ARN", fedFnRole.roleArn);

    // Serve runtime role ARN — the consumer query principal that needs SELECT
    // grants on LF-governed tables. Read from SSM (written by ServeStack) to
    // avoid a circular CDK cross-stack reference. Requires addDependency(serve)
    // in app.ts so the SSM param exists at deploy time.
    const serveRuntimeRoleArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/serve/runtime-role-arn`,
    );
    dbConnectorFn.addEnvironment(
      "CONSUMER_QUERY_ROLE_ARN",
      serveRuntimeRoleArn,
    );
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "AssumeLfGrantorForSelfHeal",
        actions: ["sts:AssumeRole"],
        resources: [fedFnRole.roleArn],
      }),
    );
    const fedRoleForTrust = fedFnRole as iam.Role;
    fedRoleForTrust.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        principals: [dbConnectorFn.role!],
      }),
    );
    // Secrets Manager: credential secrets provided at source creation time.
    //
    // There used to be a second, UNCONDITIONED statement here granting
    // GetSecretValue on `{prefix}datasource-*` for "accelerator-managed" secrets.
    // It has been removed rather than tag-gated: its resource set is a strict
    // subset of the tag-gated statement below (same account, narrower name), so
    // the only thing it added was an exemption from the namespace-tag requirement
    // — for exactly the naming convention the platform's own credential secrets
    // use. IAM statements are additive, so its presence meant the condition below
    // governed every in-account secret EXCEPT the ones most likely to hold tenant
    // database credentials. Verified against a live account: with that statement
    // attached, an untagged secret and a secret tagged for another namespace were
    // both readable.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "SecretsManagerCustomerProvided",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [`arn:aws:secretsmanager:*:*:secret:*`],
        conditions: {
          StringEquals: {
            "aws:ResourceAccount": this.account,
          },
          // An in-account credential secret must carry a `{prefix}:namespace`
          // tag. This shrinks the discovery role's reach from every in-account
          // secret to only those onboarded to a namespace, so a code path that
          // bypasses the registration check still can't read an arbitrary
          // account secret (e.g. another service's DB master secret).
          // ponytail: this is tag-EXISTS, not an exact namespace match — the
          // discovery Lambda uses one shared execution role with no per-request
          // namespace identity. The exact match is enforced at registration
          // (database_routes) and on the serve-side resource policy. Upgrade
          // path: session-tag the execution identity per scan and switch to
          // `secretsmanager:ResourceTag/{prefix}:namespace == ${aws:PrincipalTag/{prefix}:namespace}`.
          Null: {
            [`secretsmanager:ResourceTag/${nsTagKey}`]: "false",
          },
        },
      }),
    );
    // Read the credential secret's TAGS to re-verify the namespace binding at
    // scan time, before the connector fetches the secret VALUE. Metadata only —
    // deliberately not GetSecretValue, which the statement above governs.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "DescribeSecretForNamespaceBinding",
        actions: ["secretsmanager:DescribeSecret"],
        resources: [`arn:aws:secretsmanager:*:${this.account}:secret:*`],
      }),
    );
    // STS: assume Context Ontology Accelerator-managed roles + customer-provided cross-account roles.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "AssumeRoleCoaManaged",
        actions: ["sts:AssumeRole"],
        resources: [
          `arn:aws:iam::*:role/${this.prefixed("datasource-access-")}*`,
        ],
        // Deny any cross-account assume that presents no ExternalId. The role
        // ARN is caller-supplied, so the ExternalId (derived from the requesting
        // namespace) is what binds the assume to the namespace that asked for it.
        // Belt-and-braces: the connectors always send one, this makes a
        // regression fail closed at IAM instead of silently widening access.
        conditions: {
          Null: { "sts:ExternalId": "false" },
        },
      }),
    );

    // ── Enrichment Agent ECS Task ────────────────────────────────────
    const dbEnrichmentCluster = new ecs.Cluster(this, "DbEnrichmentCluster", {
      clusterName: this.prefixed("sources-db-enrichment-cluster"),
      vpc,
    });

    const dbEnrichmentLogGroup = new logs.LogGroup(
      this,
      "DbEnrichmentLogGroup",
      {
        logGroupName: `/ecs/${this.prefixed("sources-db-enrichment-agent")}`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy:
          this.envName === "prod"
            ? cdk.RemovalPolicy.RETAIN
            : cdk.RemovalPolicy.DESTROY,
      },
    );

    const dbEnrichmentTaskDef = new ecs.FargateTaskDefinition(
      this,
      "DbEnrichmentTaskDef",
      {
        family: this.prefixed("sources-db-enrichment-agent"),
        cpu: 2048,
        memoryLimitMiB: 8192,
      },
    );

    const dbEnrichmentImageUri = this.node.tryGetContext(
      "sources_db_enrichment_image_uri",
    ) as string | undefined;
    const dbEnrichmentImage =
      dbEnrichmentImageUri && ecrRepo
        ? ecs.ContainerImage.fromEcrRepository(
            ecrRepo,
            dbEnrichmentImageUri.split(":").pop() || "latest",
          )
        : ecs.ContainerImage.fromAsset(Paths.root, {
            file: "packages/sources/database/enrichment/Dockerfile",
          });

    dbEnrichmentTaskDef.addContainer("DbEnrichmentContainer", {
      containerName: this.prefixed("sources-db-enrichment-agent"),
      image: dbEnrichmentImage,
      environment: {
        SOURCES_TABLE: this.sourcesTable.tableName,
        SOURCE_SCAN_JOBS_TABLE: this.sourceScanJobsTable.tableName,
        NAMESPACES_TABLE: namespacesTableName,
        SMUS_DOMAIN_ID: domainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
        // ECS does not inject a region into task containers, so without this
        // resolve_region() falls back to us-east-1 and every Bedrock call —
        // including ApplyGuardrail — targets the wrong region. In a non-us-east-1
        // deployment ApplyGuardrail is then DENIED by the region-scoped IAM
        // policy below and screening fails open. Guardrail metrics would also
        // land in the wrong region's namespace.
        AWS_DEFAULT_REGION: cdk.Aws.REGION,
        BEDROCK_REGION: cdk.Aws.REGION,
        // Without this the enrichment task's Bedrock calls run UNGUARDED —
        // table_enricher._resolve_guardrail_id() reads this param name to look
        // up the guardrail id. Same param the ontology task reads (#111 AC5).
        GUARDRAIL_SSM_PARAM: `${ssmPrefix}/bedrock/retrieval-guardrail-id`,
        // Model for table enrichment. The container previously set NO model id,
        // so the shared BedrockClient default (a `us.` profile) always won and
        // enrichment was unreachable from a non-US deploy (#94).
        BEDROCK_CHAT_MODEL_ID: chatModelId,
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "sources-db-enrichment",
        logGroup: dbEnrichmentLogGroup,
      }),
    });

    this.sourcesTable.grantReadWriteData(dbEnrichmentTaskDef.taskRole);
    this.sourceScanJobsTable.grantReadWriteData(dbEnrichmentTaskDef.taskRole);
    namespacesTable.grantReadData(dbEnrichmentTaskDef.taskRole);
    projectAccessRole.grantAssumeRole(dbEnrichmentTaskDef.taskRole);

    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [
          `arn:aws:bedrock:*::foundation-model/*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      }),
    );
    // Guardrail enforcement on enrichment prompts, plus the SSM read that
    // resolves the guardrail id from GUARDRAIL_SSM_PARAM above (#111 AC6).
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:ApplyGuardrail"],
        resources: [
          `arn:aws:bedrock:${this.region}:${this.account}:guardrail/*`,
        ],
      }),
    );
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter${ssmPrefix}/bedrock/retrieval-guardrail-id`,
        ],
      }),
    );
    // Guardrail decision metrics (#111 AC10) go out via PutMetricData, matching
    // how this task publishes its other custom metrics, so it needs the grant.
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "COA/Guardrails" },
        },
      }),
    );
    // Same namespace-tag requirement as the discovery role: an in-account
    // credential secret is only readable once it has been onboarded to a
    // namespace. This role reaches source credentials through the same JDBC
    // connector code, so leaving it unconditioned would have kept an untagged
    // read path open on a role that is easy to overlook.
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "ReadNamespaceBoundCredentialSecret",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:${this.prefixed("datasource-")}*`,
        ],
        conditions: {
          Null: { [`secretsmanager:ResourceTag/${nsTagKey}`]: "false" },
        },
      }),
    );
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "AssumeRoleCustomerProvided",
        actions: ["sts:AssumeRole"],
        resources: [
          `arn:aws:iam::*:role/${this.prefixed("datasource-access-")}*`,
        ],
        // Deny any cross-account assume that presents no ExternalId. The role
        // ARN is caller-supplied, so the ExternalId (derived from the requesting
        // namespace) is what binds the assume to the namespace that asked for it.
        // Belt-and-braces: the connectors always send one, this makes a
        // regression fail closed at IAM instead of silently widening access.
        conditions: {
          Null: { "sts:ExternalId": "false" },
        },
      }),
    );
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: "EnrichmentGlueCatalogAccess",
        actions: [
          "glue:GetTable",
          "glue:GetTables",
          "glue:GetDatabase",
          "glue:GetConnection",
          // Nested/federated catalogs authorize against the catalog resource
          // itself — same gap as discovery (issue 118).
          "glue:GetCatalog",
          "glue:GetCatalogs",
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:catalog/*`,
          `arn:aws:glue:${this.region}:${this.account}:database/*`,
          `arn:aws:glue:${this.region}:${this.account}:table/*/*`,
          `arn:aws:glue:${this.region}:${this.account}:connection/*`,
        ],
      }),
    );

    // ── Database Scan Pipeline Step Functions ────────────────────────
    // Scan job status updates go to source-scan-jobs table
    // PK = SRC#{sourceId}  (passed as scanJobPK from the trigger Lambda)
    // SK = scanJobSK       (ISO timestamp, passed from the trigger Lambda)
    const makeDbStatusUpdate = (
      stepId: string,
      status: string,
    ): tasks.DynamoUpdateItem =>
      new tasks.DynamoUpdateItem(this, stepId, {
        table: this.sourceScanJobsTable,
        key: {
          PK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt("$.scanJobPK"),
          ),
          SK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt("$.scanJobSK"),
          ),
        },
        updateExpression: "SET #s = :s, #u = :u",
        expressionAttributeNames: { "#s": "status", "#u": "updatedAt" },
        expressionAttributeValues: {
          ":s": tasks.DynamoAttributeValue.fromString(status),
          ":u": tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt("$$.State.EnteredTime"),
          ),
        },
        resultPath: sfn.JsonPath.DISCARD,
      });

    // Source record status update goes to sources table
    // PK = NS#{namespaceId}, SK = SRC#{sourceId}
    // datasourceId arrives as "DS#<uuid>" — strip the prefix to get the bare sourceId.
    const dbUpdateSourceScanFailed = new tasks.DynamoUpdateItem(
      this,
      "DbUpdateSourceStatusScanFailed",
      {
        table: this.sourcesTable,
        key: {
          PK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.format(
              "NS#{}",
              sfn.JsonPath.stringAt("$.namespaceId"),
            ),
          ),
          SK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.format("SRC#{}", sfn.JsonPath.stringAt("$.sourceId")),
          ),
        },
        updateExpression: "SET #s = :s, #u = :u, #l = :l",
        expressionAttributeNames: {
          "#s": "status",
          "#u": "updatedAt",
          "#l": "lastScanJobId",
        },
        expressionAttributeValues: {
          ":s": tasks.DynamoAttributeValue.fromString("SCAN_FAILED"),
          ":u": tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt("$$.State.EnteredTime"),
          ),
          ":l": tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt("$.scanJobSK"),
          ),
        },
        resultPath: sfn.JsonPath.DISCARD,
      },
    );

    const dbUpdateDiscovering = makeDbStatusUpdate(
      "DbUpdateStatusDiscovering",
      "DISCOVERING",
    );
    const dbUpdateEnriching = makeDbStatusUpdate(
      "DbUpdateStatusEnriching",
      "ENRICHING",
    );
    const dbUpdateCompleted = makeDbStatusUpdate(
      "DbUpdateStatusCompleted",
      "COMPLETED",
    );
    const dbUpdateFailed = makeDbStatusUpdate("DbUpdateStatusFailed", "FAILED");
    const dbFailState = new sfn.Fail(this, "DbScanFailed", {
      cause: "Scan pipeline failed",
      error: "ScanError",
    });
    // Error chain: mark scan job FAILED + mark source SCAN_FAILED, then enter terminal Fail state.
    // This ensures the source status is always updated even when the ECS task is killed externally
    // (OOM, Fargate preemption, timeout) and the Python exception handler never runs.
    const dbErrorChain = dbUpdateFailed
      .next(dbUpdateSourceScanFailed)
      .next(dbFailState);

    const dbDiscoveryTask = new tasks.LambdaInvoke(this, "DbDiscovery", {
      lambdaFunction: dbConnectorFn,
      resultPath: "$.discoveryResult",
      retryOnServiceExceptions: false,
    });
    dbDiscoveryTask.addRetry({
      // Retry only failures a re-run can fix: our transient classification plus
      // Lambda infra faults. PermanentScanError (bad config, denied auth, missing
      // source, unsupported type) is absent, so it falls straight through to the
      // SCAN_FAILED catch instead of burning 3 retries (~15s). AWS Lambda reports
      // the raised exception's class name as the Step Functions error name.
      errors: [
        "TransientScanError",
        "Lambda.ServiceException",
        "Lambda.AWSLambdaException",
        "Lambda.SdkClientException",
        "Lambda.TooManyRequestsException",
      ],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(5),
    });
    dbDiscoveryTask.addCatch(dbErrorChain, { resultPath: "$.error" });

    // Federation provisioning (JDBC-only, self-gating). A failure marks the
    // scan FAILED and the source SCAN_FAILED via the shared error chain — a
    // source that isn't queryable is not a successful scan, so we fail loudly
    // rather than silently completing. Legitimate no-ops (non-JDBC / incomplete
    // config) return success from the handler and are not treated as failures.
    const dbFederationTask = new tasks.LambdaInvoke(this, "DbFederation", {
      lambdaFunction: federationProvisionerFn,
      resultPath: "$.federationResult",
    });
    dbFederationTask.addRetry({
      maxAttempts: 2,
      backoffRate: 2,
      interval: cdk.Duration.seconds(5),
    });
    dbFederationTask.addCatch(dbErrorChain, { resultPath: "$.error" });

    // Enrichment deadline, configurable via CDK context so a genuinely
    // large source can be given more headroom at deploy time without a code
    // change. `dbScanEnrichmentTimeoutMinutes` sets the CATCHABLE per-task
    // timeout; the state-machine timeout (below) is this + 2 min so the
    // catchable States.Timeout always fires first and routes to SCAN_FAILED.
    // Default 120 min ≈ the measured ~35s/table × ceil(tables/10) throughput
    // (MAX_WORKERS=10) for a ~2,000-table source; NOT sized to any one dataset.
    const enrichmentTimeoutMinutesRaw: unknown = this.node.tryGetContext(
      "dbScanEnrichmentTimeoutMinutes",
    );
    const enrichmentTimeoutMinutes = Number(enrichmentTimeoutMinutesRaw ?? 120);
    if (
      !Number.isFinite(enrichmentTimeoutMinutes) ||
      enrichmentTimeoutMinutes <= 0
    ) {
      throw new Error(
        `dbScanEnrichmentTimeoutMinutes must be a positive number, got: ${String(enrichmentTimeoutMinutesRaw)}`,
      );
    }

    const dbEnrichmentTask = new tasks.EcsRunTask(this, "DbEnrichment", {
      integrationPattern: sfn.IntegrationPattern.RUN_JOB,
      cluster: dbEnrichmentCluster,
      taskDefinition: dbEnrichmentTaskDef,
      launchTarget: new tasks.EcsFargateLaunchTarget({
        platformVersion: ecs.FargatePlatformVersion.LATEST,
      }),
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [ecsSecurityGroup],
      containerOverrides: [
        {
          containerDefinition: dbEnrichmentTaskDef.defaultContainer!,
          environment: [
            {
              name: "DATASOURCE_ID",
              value: sfn.JsonPath.stringAt("$.datasourceId"),
            },
            {
              name: "SCAN_JOB_ID",
              value: sfn.JsonPath.stringAt("$.scanJobId"),
            },
            {
              name: "SCAN_JOB_SK",
              value: sfn.JsonPath.stringAt("$.scanJobSK"),
            },
            {
              name: "NAMESPACE_ID",
              value: sfn.JsonPath.stringAt("$.namespaceId"),
            },
            { name: "SCAN_TYPE", value: sfn.JsonPath.stringAt("$.scanType") },
            // Re-scan marker (string "true"/"false", always present in the
            // execution input via the trigger). Routes the terminal source
            // status to RESCAN_REVIEW when a re-scan of an approved source
            // completes, instead of PENDING_REVIEW.
            { name: "IS_RESCAN", value: sfn.JsonPath.stringAt("$.isRescan") },
            // No-drift signal from discovery (string "true"/"false", always
            // present in its result). When a re-scan reports "false" — no drift
            // and no carried-forward orphaned tables — enrichment returns the
            // source straight to APPROVED instead of parking it in RESCAN_REVIEW.
            {
              name: "RESCAN_REVIEW_NEEDED",
              // DbDiscovery is a LambdaInvoke without payloadResponseOnly, so its
              // result at $.discoveryResult is the full Lambda envelope
              // ({Payload, ExecutedVersion, SdkHttpMetadata, ...}). reviewNeeded
              // lives under .Payload — reading it directly off $.discoveryResult
              // raises a runtime "JSONPath could not be found" and fails the scan.
              value: sfn.JsonPath.stringAt(
                "$.discoveryResult.Payload.reviewNeeded",
              ),
            },
          ],
        },
      ],
      resultPath: "$.enrichmentResult",
      // CATCHABLE per-task deadline. The state-machine `timeout` (below) fires
      // as `ExecutionTimedOut`, which no state can `.addCatch()` — so an
      // enrichment run that outlives it strands the source in ENRICHING with
      // no terminal-status write, leaving it undeletable/un-rescannable.
      // A `taskTimeout` instead raises a catchable
      // `States.Timeout` that the `addCatch(dbErrorChain)` below routes to
      // SCAN_FAILED, and Step Functions stops the ECS task. Kept below the
      // state-machine timeout so this catchable path always fires first.
      taskTimeout: sfn.Timeout.duration(
        cdk.Duration.minutes(enrichmentTimeoutMinutes),
      ),
    });
    dbEnrichmentTask.addRetry({
      errors: ["ECS.ServiceException", "ECS.AmazonECSException"],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(30),
    });
    dbEnrichmentTask.addCatch(dbErrorChain, { resultPath: "$.error" });

    const dbScanStateMachine = new sfn.StateMachine(
      this,
      "DbScanStateMachine",
      {
        stateMachineName: this.prefixed("sources-db-scan-pipeline"),
        definitionBody: sfn.DefinitionBody.fromChainable(
          dbUpdateDiscovering
            // Discovery runs first and validates the connection (fail-fast on a
            // bad source before any federated resource is provisioned). Federation
            // then provisions the catalog + grants LF.
            .next(dbDiscoveryTask)
            .next(dbFederationTask)
            .next(dbUpdateEnriching)
            .next(dbEnrichmentTask)
            .next(dbUpdateCompleted),
        ),
        // 2 min above the enrichment taskTimeout so the catchable
        // States.Timeout on DbEnrichment fires first (→ SCAN_FAILED via
        // dbErrorChain). This outer ceiling only backstops paths the reaper
        // covers (execution-level abort/timeout with no catchable error).
        timeout: cdk.Duration.minutes(enrichmentTimeoutMinutes + 2),
      },
    );

    // ── Db-scan reaper (terminal-status safety net) ────────────
    //
    // The in-machine dbErrorChain → SCAN_FAILED only fires on CATCHABLE task
    // errors. An execution-level abort — the state-machine `timeout` above
    // firing as `ExecutionTimedOut`, an operator `StopExecution` (ABORTED), or
    // a `FAILED` execution no state caught — is NOT catchable in-machine, so it
    // strands the source in an active status (SCANNING/ENRICHING) with no
    // terminal-status write, leaving it undeletable/un-rescannable.
    //
    // This EventBridge rule fires the reaper on those terminal execution
    // statuses and guarantees the terminal-status write. The reaper is
    // idempotent: it no-ops unless the source is still in an active status, so
    // it is safe if the in-machine write already ran (both fired) or on
    // redelivery.
    const dbScanReaperFn = new lambda.Function(this, "DbScanReaperFn", {
      functionName: this.prefixed("sources-db-scan-reaper"),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "coa_sources.database.pipeline.reaper_handler.handler",
      code: bundlePython({
        srcDirs: [
          // Full src/ tree needed: handler is in database/pipeline/, not api/
          fromRoot("packages/sources/src"),
          Paths.commonLib,
          Paths.smithyGeneratedControlPlanePythonServer,
        ],
        requirementsFile: fromRoot("packages/sources/requirements.txt"),
        architecture: "arm64",
      }),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [lambdaSecurityGroup],
      environment: {
        SOURCES_TABLE: this.sourcesTable.tableName,
      },
    });
    this.sourcesTable.grantReadWriteData(dbScanReaperFn);

    new events.Rule(this, "DbScanReaperRule", {
      eventPattern: {
        source: ["aws.states"],
        detailType: ["Step Functions Execution Status Change"],
        detail: {
          stateMachineArn: [dbScanStateMachine.stateMachineArn],
          status: ["TIMED_OUT", "ABORTED", "FAILED"],
        },
      },
      targets: [new targets.LambdaFunction(dbScanReaperFn)],
    });

    // ── Database Scan Queue + Trigger Lambda ─────────────────────────
    const dbScanDlq = new sqs.Queue(this, "DbScanDLQ", {
      queueName: this.prefixed("sources-db-scan-dlq"),
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });
    const dbScanQueue = new sqs.Queue(this, "DbScanQueue", {
      queueName: this.prefixed("sources-db-scan-queue"),
      visibilityTimeout: cdk.Duration.seconds(90),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: { maxReceiveCount: 3, queue: dbScanDlq },
    });

    const dbTriggerFn = new lambda.Function(this, "DbScanTriggerFn", {
      functionName: this.prefixed("sources-db-scan-trigger"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(Paths.sourcesDatabaseTrigger),
      environment: { STATE_MACHINE_ARN: dbScanStateMachine.stateMachineArn },
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [lambdaSecurityGroup],
    });
    dbScanStateMachine.grantStartExecution(dbTriggerFn);
    dbTriggerFn.addEventSource(
      new lambdaEventSources.SqsEventSource(dbScanQueue, {
        batchSize: 1,
        maxConcurrency: 5,
      }),
    );

    // ── Bulk Review Queue + Worker Lambda ────────────────────────────
    //
    // Async pipeline for ApproveSource / RejectSource. The API Lambda
    // does the conditional status transition + SQS enqueue and returns
    // 202; this worker performs the actual DataZone asset revisions for
    // every PENDING table/column on the source.
    //
    // Visibility timeout = 6 minutes — slightly above the worker
    // Lambda's 5-minute timeout to ensure the message is invisible
    // until the worker has either succeeded or failed.
    //
    // DLQ after 3 receives — repeated worker failures land here for
    // operator inspection. The CloudWatch alarm below pages on any
    // message reaching the DLQ.
    const bulkReviewDlq = new sqs.Queue(this, "BulkReviewDLQ", {
      queueName: this.prefixed("sources-bulk-review-dlq"),
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });
    const bulkReviewQueue = new sqs.Queue(this, "BulkReviewQueue", {
      queueName: this.prefixed("sources-bulk-review-queue"),
      visibilityTimeout: cdk.Duration.minutes(6),
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      deadLetterQueue: { maxReceiveCount: 3, queue: bulkReviewDlq },
    });

    const bulkReviewWorkerFn = new lambda.Function(this, "BulkReviewWorkerFn", {
      functionName: this.prefixed("sources-bulk-review-worker"),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "index.handler",
      // The shim lives in packages/sources/database/bulk_review_worker
      // and delegates to the testable logic module under
      // packages/sources/src/coa_sources/database/bulk_review/worker.py.
      // We bundle the full src/ tree + common + smithy generated server
      // because the worker imports both review_logic (common) and the
      // domain models (smithy generated).
      code: bundlePython({
        srcDirs: [
          fromRoot("packages/sources/database/bulk_review_worker"),
          fromRoot("packages/sources/src"),
          Paths.commonLib,
          Paths.smithyGeneratedControlPlanePythonServer,
        ],
        requirementsFile: fromRoot("packages/sources/requirements.txt"),
        architecture: "arm64",
      }),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [lambdaSecurityGroup],
      environment: {
        SOURCES_TABLE: this.sourcesTable.tableName,
        // Scan-history store — the worker appends a REVIEW audit row here on
        // each terminal approve/reject so the console shows real history.
        SOURCE_SCAN_JOBS_TABLE: this.sourceScanJobsTable.tableName,
        NAMESPACES_TABLE: namespacesTableName,
        SMUS_DOMAIN_ID: domainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
        // Parallel revision writes per worker invocation. Tunable per env.
        BULK_REVIEW_PARALLELISM: "10",
        // Self-continuation: a source larger than one invocation can process
        // is paged by re-enqueuing to this same queue with a nextToken. Every
        // table is eventually approved with no silent drop (#853).
        REVIEW_QUEUE_URL: bulkReviewQueue.queueUrl,
        // Re-scan finalize: the worker reads the pre-rescan backup blob to
        // delete removed items (approve) or restore the pre-rescan state
        // (reject). Same bucket discovery wrote the backup to.
        BUCKET_NAME: sourcesBucket.bucketName,
      },
    });

    // SQS event source — one message per invocation. The worker is
    // idempotent (verifies source is still in the matching transient
    // state before doing anything) so SQS redelivery is safe.
    bulkReviewWorkerFn.addEventSource(
      new lambdaEventSources.SqsEventSource(bulkReviewQueue, {
        batchSize: 1,
        // Cap concurrent bulk reviews per environment. Each invocation
        // can fan out to many parallel DataZone calls; capping concurrency
        // here protects DataZone account-wide rate limits.
        maxConcurrency: 5,
      }),
    );

    // Worker IAM — least-privilege:
    // - Read+write the sources table (status transitions + tablesApproved counter)
    // - Read namespaces table (resolve dataZoneProjectId)
    // - Assume the shared project access role for DataZone calls
    this.sourcesTable.grantReadWriteData(bulkReviewWorkerFn);
    // Append a REVIEW audit row to the scan-jobs table on each terminal
    // approve/reject so the console's Scan History tab shows real events.
    this.sourceScanJobsTable.grantReadWriteData(bulkReviewWorkerFn);
    namespacesTable.grantReadData(bulkReviewWorkerFn);
    projectAccessRole.grantAssumeRole(bulkReviewWorkerFn.role!);
    // Self-continuation: the worker re-enqueues to its own queue to page a
    // large source across invocations (#853).
    bulkReviewQueue.grantSendMessages(bulkReviewWorkerFn);
    // Re-scan finalize reads/writes the pre-rescan backup blob (delete removed
    // on approve; restore modified + delete added on reject).
    sourcesBucket.grantReadWrite(bulkReviewWorkerFn);

    // The API Lambda needs to push to the bulk review queue.
    // (Granted later, after sourcesApiFn is constructed.)

    // ================================================================
    // Documents Pipeline — Preprocessing Lambda + KgBuild ECS + SFN
    // ================================================================

    // ── Pre-Processing Lambda ────────────────────────────────────────
    const preprocessingImageUri = this.node.tryGetContext(
      "sources_preprocessing_image_uri",
    ) as string | undefined;
    const preprocessingCode =
      preprocessingImageUri && ecrRepo
        ? lambda.DockerImageCode.fromEcr(ecrRepo, {
            tagOrDigest: preprocessingImageUri.split(":").pop() || "latest",
          })
        : lambda.DockerImageCode.fromImageAsset(Paths.root, {
            file: "packages/sources/documents/preprocessing/Dockerfile",
          });

    const preprocessingFn = new lambda.DockerImageFunction(
      this,
      "SourcesPreProcessingFn",
      {
        functionName: this.prefixed("sources-doc-preprocessing"),
        code: preprocessingCode,
        timeout: cdk.Duration.minutes(15),
        memorySize: 3008,
        // Reserved concurrency caps document-preprocessing fan-out (bounds
        // parallel heavy invocations); it is not a correctness requirement.
        // Configurable via `lambda_reserved_concurrency`; `0`/undefined omits
        // the reservation so the stack deploys on reduced-quota accounts.
        reservedConcurrentExecutions: resolveLambdaReservedConcurrency(
          this.node,
        ),
        environment: {
          BUCKET_NAME: sourcesBucket.bucketName,
          DOC_SOURCES_TABLE: this.sourcesTable.tableName,
          MAX_FILE_SIZE_MB: DEFAULT_MAX_FILE_SIZE_MB.toString(),
          CROSS_ACCOUNT_ROLE_PREFIX: resolveContext(this.node).prefix,
          // BARE prefix — feeds `bucket_namespace_tag_key()`, the tag a bucket
          // owner sets to authorize namespaces to read it. Without it the handler
          // falls back to the bare `coa` default and looks for a tag no customer
          // was told to set. Same variable the secret-binding check uses; a second
          // one for the same value could drift from it.
          RESOURCE_TAG_PREFIX: resolveContext(this.node).prefix,
        },
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [lambdaSecurityGroup],
      },
    );
    sourcesBucket.grantReadWrite(preprocessingFn);
    this.sourcesTable.grantReadWriteData(preprocessingFn);
    preprocessingFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ReadCustomerSourceBuckets",
        actions: [
          "s3:GetObject",
          "s3:GetObjectTagging",
          "s3:ListBucket",
          // Reads the `{prefix}:namespace` tag that authorizes a bucket for a
          // namespace. Must be on the wildcard too: whether we may read a bucket
          // is exactly what this call answers, so it cannot be scoped by the
          // answer. It returns tag metadata, never object data.
          "s3:GetBucketTagging",
        ],
        // Wildcard required: customers provide their own bucket names, which are
        // not knowable at synth time. Authorization for these buckets is the
        // owner-set `{prefix}:namespace` tag, verified at source registration and
        // again in the preprocessing handler.
        resources: ["*"],
      }),
    );
    // The platform's own buckets ARE knowable at synth time, so they are carved
    // out here rather than left to the tag check. An explicit Deny cannot be
    // defeated by a bug in that check, and these buckets hold other namespaces'
    // data: Athena results and spill carry sampled query output, and the ontology
    // bucket carries generated artifacts. The sources data bucket is deliberately
    // absent — uploads and staging depend on the grantReadWrite above, which a
    // Deny would override.
    preprocessingFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "DenyPlatformOwnedBuckets",
        effect: iam.Effect.DENY,
        // Mirrors the Allow above action-for-action, GetBucketTagging included.
        // Reading a platform bucket's tags discloses nothing (they never carry the
        // namespace tag), but a Deny that covers less than the Allow it guards
        // invites the question of why — and the next action added to the Allow
        // would silently escape it.
        actions: [
          "s3:GetObject",
          "s3:GetObjectTagging",
          "s3:ListBucket",
          "s3:GetBucketTagging",
        ],
        resources: [
          props.storage.athenaResultsBucket.bucketArn,
          `${props.storage.athenaResultsBucket.bucketArn}/*`,
          props.storage.athenaSpillBucket.bucketArn,
          `${props.storage.athenaSpillBucket.bucketArn}/*`,
          props.storage.ontologyArtifactsBucket.bucketArn,
          `${props.storage.ontologyArtifactsBucket.bucketArn}/*`,
        ],
      }),
    );
    preprocessingFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        // Scoped to roles following the platform prefix naming convention.
        resources: [
          `arn:aws:iam::*:role/${resolveContext(this.node).prefix}-*`,
        ],
      }),
    );
    preprocessingFn.addToRolePolicy(
      new iam.PolicyStatement({
        // DetectDocumentText: scanned-PDF OCR path (is_scanned_pdf -> process_pdf_textract).
        // AnalyzeDocument: table-extraction path (enable_table_extraction ->
        //   process_pdf_textract_tables calls AnalyzeDocument with FeatureTypes=[TABLES]).
        //   Without this action the tables path fails at runtime with AccessDeniedException.
        actions: ["textract:DetectDocumentText", "textract:AnalyzeDocument"],
        resources: ["*"],
      }),
    );

    // ── KgBuild ECS Task ─────────────────────────────────────────────
    // Container Insights on: kg-build tasks have died with no exit code and no
    // memory data, leaving OOM impossible to confirm or rule out. Task-level
    // memory/CPU metrics are the only way to tell an OOM kill apart from a hang.
    const kgBuildCluster = new ecs.Cluster(this, "SourcesKgBuildCluster", {
      clusterName: this.prefixed("sources-doc-kg-build-cluster"),
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });
    const kgBuildLogGroup = new logs.LogGroup(this, "SourcesKgBuildLogGroup", {
      logGroupName: `/ecs/${this.prefixed("sources-doc-kg-build")}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy:
        this.envName === "prod"
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
    });
    const kgBuildTaskDef = new ecs.FargateTaskDefinition(
      this,
      "SourcesKgBuildTaskDef",
      {
        family: this.prefixed("sources-doc-kg-build"),
        cpu: 4096,
        memoryLimitMiB: 16384,
      },
    );

    const kgBuildImageUri = this.node.tryGetContext(
      "sources_kg_build_image_uri",
    ) as string | undefined;
    const kgBuildImage =
      kgBuildImageUri && ecrRepo
        ? ecs.ContainerImage.fromEcrRepository(
            ecrRepo,
            kgBuildImageUri.split(":").pop() || "latest",
          )
        : ecs.ContainerImage.fromAsset(Paths.root, {
            file: "packages/sources/documents/kg-build/Dockerfile",
          });

    const batchInferenceRole = new iam.Role(this, "SourcesBatchInferenceRole", {
      roleName: this.prefixed("sources-doc-bedrock-batch-inference"),
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: {
            "aws:SourceArn": `arn:aws:bedrock:${this.region}:${this.account}:model-invocation-job/*`,
          },
        },
      }),
    });
    sourcesBucket.grantReadWrite(batchInferenceRole);
    batchInferenceRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [
          `arn:aws:bedrock:*::foundation-model/*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      }),
    );

    kgBuildTaskDef.addContainer("SourcesKgBuildContainer", {
      containerName: this.prefixed("sources-doc-kg-build"),
      image: kgBuildImage,
      environment: {
        BUCKET_NAME: sourcesBucket.bucketName,
        DOC_SOURCES_TABLE: this.sourcesTable.tableName,
        NEPTUNE_ENDPOINT: neptuneEndpoint,
        OPENSEARCH_ENDPOINT: opensearchEndpoint,
        BATCH_INFERENCE_ROLE_ARN: batchInferenceRole.roleArn,
        // ECS does not inject a region into task containers, so without this
        // resolve_region() falls back to us-east-1 and the ingestion-time
        // ApplyGuardrail call below targets the wrong region — DENIED by the
        // region-scoped IAM policy, and graph_build screening fails OPEN
        // (screens nothing). Guardrail metrics would also land in us-east-1.
        AWS_DEFAULT_REGION: cdk.Aws.REGION,
        BEDROCK_REGION: cdk.Aws.REGION,
        // Doc-KG-build embeds chunks. Without these the container fell back to
        // the PYTHON constants (coa_common.constants.DEFAULT_EMBED_MODEL_ID /
        // coa_common.bedrock.DEFAULT_MODEL_ID), so editing only the TypeScript
        // side left this path on the US models (#94). Embedding model MUST match
        // what serve queries with, hence the single resolved value.
        BEDROCK_EMBED_MODEL_ID: embedModelId,
        BEDROCK_EMBED_DIMENSIONS: embedDimensions,
        BEDROCK_CHAT_MODEL_ID: chatModelId,
        // Content screening: retrieval guardrail for ingestion-time ApplyGuardrail
        // (SDO-188). Empty string disables screening gracefully.
        RETRIEVAL_GUARDRAIL_ID: ssm.StringParameter.valueForStringParameter(
          this,
          `${ssmPrefix}/bedrock/retrieval-guardrail-id`,
        ),
        RETRIEVAL_GUARDRAIL_VERSION:
          ssm.StringParameter.valueForStringParameter(
            this,
            `${ssmPrefix}/bedrock/retrieval-guardrail-version`,
          ),
        // Smaller bulk embedding writes so each AOSS _bulk request reserves less
        // JVM heap — avoids tripping the NEXTGEN parent circuit breaker
        // (429 circuit_breaking_exception) on large doc corpora at low OCU.
        // graphrag default is 25; read via os.environ["BUILD_BATCH_WRITE_SIZE"].
        BUILD_BATCH_WRITE_SIZE: "5",
        // GraphRAG toolkit parallelism tuning (read by GraphRAGConfig from env).
        // Task is 4 vCPU / 16 GB; the toolkit caps num_workers at cpu_count(),
        // so 4 workers fully uses the box. Extraction is LLM-I/O-bound, so
        // threads-per-worker matters most there (gated by Bedrock quota) — CPU
        // is not the bottleneck, which is why the box stays at 4 vCPU.
        EXTRACTION_NUM_WORKERS: "4",
        BUILD_NUM_WORKERS: "4",
        // graphrag-toolkit logs via stdlib logging. INFO surfaces the per-batch
        // "Running build pipeline [num_workers, job_sizes, batch_write_size]"
        // line — the only report of effective write parallelism. DEBUG adds the
        // batch-write retry ladder but is very verbose; raise it temporarily when
        // diagnosing write contention, don't leave it on.
        DEPENDENCY_LOG_LEVEL: "INFO",
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "sources-doc-kg-build",
        logGroup: kgBuildLogGroup,
      }),
    });

    sourcesBucket.grantReadWrite(kgBuildTaskDef.taskRole);
    this.sourcesTable.grantReadWriteData(kgBuildTaskDef.taskRole);
    kgBuildTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "neptune-db:ReadDataViaQuery",
          "neptune-db:WriteDataViaQuery",
          "neptune-db:DeleteDataViaQuery",
          "neptune-db:GetQueryStatus",
          "neptune-db:CancelQuery",
        ],
        resources: [neptuneClusterArn],
      }),
    );
    kgBuildTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["aoss:APIAccessAll"],
        resources: [opensearchCollectionArn],
      }),
    );
    kgBuildTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [
          `arn:aws:bedrock:*::foundation-model/*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      }),
    );
    // ApplyGuardrail for ingestion-time content screening (SDO-188).
    kgBuildTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:ApplyGuardrail"],
        resources: [
          `arn:aws:bedrock:${this.region}:${this.account}:guardrail/*`,
        ],
      }),
    );
    // Guardrail decision metrics (#111 AC10) go out via PutMetricData, matching
    // how this task publishes its other custom metrics, so it needs the grant.
    kgBuildTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "COA/Guardrails" },
        },
      }),
    );
    // Bedrock batch job actions scoped to the same model ARN patterns as InvokeModel above.
    // Prevents launching batch jobs against arbitrary models or stopping jobs from other workloads.
    kgBuildTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:CreateModelInvocationJob",
          "bedrock:GetModelInvocationJob",
          "bedrock:ListModelInvocationJobs",
          "bedrock:StopModelInvocationJob",
        ],
        resources: [
          `arn:aws:bedrock:*::foundation-model/*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      }),
    );
    kgBuildTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["iam:PassRole"],
        resources: [batchInferenceRole.roleArn],
        conditions: {
          StringEquals: { "iam:PassedToService": "bedrock.amazonaws.com" },
        },
      }),
    );

    // ── AOSS Data Access Policy ──────────────────────────────────────
    new opensearchserverless.CfnAccessPolicy(
      this,
      "SourcesOSSDataAccessPolicy",
      {
        name: this.prefixed("src-ingestion-access"),
        type: "data",
        policy: JSON.stringify([
          {
            Rules: [
              {
                ResourceType: "index",
                Resource: [`index/${opensearchCollectionName}/*`],
                Permission: [
                  "aoss:CreateIndex",
                  "aoss:UpdateIndex",
                  "aoss:DescribeIndex",
                  "aoss:DeleteIndex",
                  "aoss:ReadDocument",
                  "aoss:WriteDocument",
                ],
              },
              {
                ResourceType: "collection",
                Resource: [`collection/${opensearchCollectionName}`],
                Permission: [
                  "aoss:CreateCollectionItems",
                  "aoss:DescribeCollectionItems",
                  "aoss:UpdateCollectionItems",
                ],
              },
            ],
            Principal: [
              kgBuildTaskDef.taskRole.roleArn,
              `arn:aws:iam::${this.account}:root`,
            ],
          },
        ]),
      },
    );

    // ── Documents Ingestion Step Functions ───────────────────────────
    const makeDocStatusUpdate = (
      id: string,
      status: string,
      opts?: { includeError?: boolean },
    ): tasks.DynamoUpdateItem => {
      const exprNames: Record<string, string> = {
        "#s": "status",
        "#u": "updatedAt",
      };
      const exprValues: Record<string, tasks.DynamoAttributeValue> = {
        ":s": tasks.DynamoAttributeValue.fromString(status),
        ":u": tasks.DynamoAttributeValue.fromString(
          sfn.JsonPath.stringAt("$$.State.EnteredTime"),
        ),
      };
      let updateExpr = "SET #s = :s, #u = :u";
      if (opts?.includeError) {
        exprNames["#e"] = "errorMessage";
        exprValues[":e"] = tasks.DynamoAttributeValue.fromString(
          sfn.JsonPath.stringAt("$.error.Cause"),
        );
        updateExpr += ", #e = :e";
      }
      return new tasks.DynamoUpdateItem(this, id, {
        table: this.sourcesTable,
        key: {
          PK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.format(
              "NS#{}",
              sfn.JsonPath.stringAt("$.namespace_id"),
            ),
          ),
          SK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.format(
              "SRC#{}",
              sfn.JsonPath.stringAt("$.doc_source_id"),
            ),
          ),
        },
        updateExpression: updateExpr,
        expressionAttributeNames: exprNames,
        expressionAttributeValues: exprValues,
        resultPath: sfn.JsonPath.DISCARD,
      });
    };

    const docUpdateIngesting = makeDocStatusUpdate(
      "DocUpdateStatusIngesting",
      SourceStatus.SCANNING,
    );
    const docUpdateCompleted = makeDocStatusUpdate(
      "DocUpdateStatusCompleted",
      SourceStatus.COMPLETED,
    );
    const docUpdateFailed = makeDocStatusUpdate(
      "DocUpdateStatusFailed",
      SourceStatus.SCAN_FAILED,
      { includeError: true },
    );
    const docFailState = new sfn.Fail(this, "DocIngestionFailed", {
      cause: "Ingestion pipeline failed",
      error: "IngestionError",
    });
    const docErrorChain = docUpdateFailed.next(docFailState);

    const preprocessTask = new tasks.LambdaInvoke(
      this,
      "SourcesPreProcessing",
      {
        lambdaFunction: preprocessingFn,
        resultPath: "$.preprocessResult",
        // Trim Lambda output to avoid States.DataLimitExceeded (256KB limit).
        // Only pass the summary fields needed by downstream states. The full
        // per-file issues list is bounded IN THE LAMBDA (issue 104) — the raw
        // Lambda payload is size-checked before this selector runs, so the
        // handler returns a capped preview + an S3 pointer, not the whole array.
        resultSelector: {
          "status.$": "$.Payload.status",
          "files_total.$": "$.Payload.files_total",
          "files_preprocessed.$": "$.Payload.files_preprocessed",
          "files_skipped.$": "$.Payload.files_skipped",
          "files_errored.$": "$.Payload.files_errored",
          "staging_prefix.$": "$.Payload.staging_prefix",
          "issues_preview.$": "$.Payload.issues_preview",
          "issues_truncated.$": "$.Payload.issues_truncated",
          "issues_s3_key.$": "$.Payload.issues_s3_key",
        },
        retryOnServiceExceptions: false,
      },
    );
    preprocessTask.addRetry({
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(2),
    });
    preprocessTask.addCatch(docErrorChain, { resultPath: "$.error" });

    const kgBuildTask = new tasks.EcsRunTask(this, "SourcesKGBuild", {
      integrationPattern: sfn.IntegrationPattern.RUN_JOB,
      cluster: kgBuildCluster,
      taskDefinition: kgBuildTaskDef,
      launchTarget: new tasks.EcsFargateLaunchTarget({
        platformVersion: ecs.FargatePlatformVersion.LATEST,
      }),
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [ecsSecurityGroup],
      containerOverrides: [
        {
          containerDefinition: kgBuildTaskDef.defaultContainer!,
          environment: [
            {
              name: "DOC_SOURCE_ID",
              value: sfn.JsonPath.stringAt("$.doc_source_id"),
            },
            {
              name: "NAMESPACE_ID",
              value: sfn.JsonPath.stringAt("$.namespace_id"),
            },
            { name: "TENANT_ID", value: sfn.JsonPath.stringAt("$.tenant_id") },
            {
              name: "STAGING_PREFIX",
              value: sfn.JsonPath.stringAt("$.preprocessResult.staging_prefix"),
            },
            {
              name: "EXTRACTION_MODE",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.extraction_mode",
              ),
            },
            {
              name: "USE_BATCH_INFERENCE",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.use_batch_inference",
              ),
            },
            {
              name: "ENABLE_VERSIONING",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.enable_versioning",
              ),
            },
            {
              name: "ENABLE_PROPOSITION_EXTRACTION",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.enable_proposition_extraction",
              ),
            },
            {
              // Whether to derive the entity-class vocabulary from the corpus
              // itself instead of inheriting graphrag's hardcoded news/finance
              // defaults. See graph_build.py:_build_indexing_config.
              name: "INFER_ENTITY_CLASSIFICATIONS",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.infer_entity_classifications",
              ),
            },
            {
              // JSON-encoded list of explicit entity-class labels. Trigger
              // Lambda json.dumps()es it so the state-machine input carries
              // a string. Empty JSON array "[]" is the default.
              name: "PREFERRED_ENTITY_CLASSIFICATIONS",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.preferred_entity_classifications",
              ),
            },
            {
              // Preferred TOPIC vocabulary — the thematic groupings chunks are
              // assigned to (__Topic__ nodes), as distinct from the entity-class
              // list above. Same JSON-string transport for the same reason: a
              // container-override JsonPath cannot carry an array. Empty JSON
              // array "[]" is the default and means "let the model name topics",
              // which is the behaviour of every ingest before this field existed.
              // There is no INFER_TOPICS counterpart — graphrag-toolkit has no
              // topic equivalent of the classification inference pass.
              //
              // stringAt() fails the execution if the field is missing, so the
              // trigger Lambda re-merges EXTRACTION_DEFAULTS into every message
              // before StartExecution — that is what keeps sources created before
              // this field shipped working. Both live in this stack, so a change
              // set that lands the state-machine definition before the Lambda code
              // leaves a brief window where an old-format input would fail here;
              // the same is already true of PREFERRED_ENTITY_CLASSIFICATIONS above.
              name: "PREFERRED_TOPICS",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.preferred_topics",
              ),
            },
            {
              // Route PDFs through Textract AnalyzeDocument(TABLES) instead of
              // unstructured strategy="fast" — preserves table structure at
              // materially higher per-page cost.
              name: "ENABLE_TABLE_EXTRACTION",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.enable_table_extraction",
              ),
            },
            {
              // "0" means "use the graphrag-toolkit default (256)".
              name: "CHUNK_SIZE",
              value: sfn.JsonPath.stringAt("$.extraction_config.chunk_size"),
            },
            {
              // "0" means "use the graphrag-toolkit default (25)".
              name: "CHUNK_OVERLAP",
              value: sfn.JsonPath.stringAt("$.extraction_config.chunk_overlap"),
            },
            {
              name: "BEDROCK_MODEL_ARN",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.bedrock_model_arn",
              ),
            },
            {
              name: "DELETE_PREV_VERSIONS",
              value: sfn.JsonPath.stringAt(
                "$.extraction_config.delete_prev_versions",
              ),
            },
          ],
        },
      ],
      resultPath: "$.kgBuildResult",
    });
    kgBuildTask.addRetry({
      errors: ["ECS.ServiceException", "ECS.AmazonECSException"],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(30),
    });
    kgBuildTask.addCatch(docErrorChain, { resultPath: "$.error" });

    const savePreprocessingResults = new tasks.DynamoUpdateItem(
      this,
      "SourcesSavePreprocessingResults",
      {
        table: this.sourcesTable,
        key: {
          PK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.format(
              "NS#{}",
              sfn.JsonPath.stringAt("$.namespace_id"),
            ),
          ),
          SK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.format(
              "SRC#{}",
              sfn.JsonPath.stringAt("$.doc_source_id"),
            ),
          ),
        },
        updateExpression:
          "SET #s = :s, #ft = :ft, #fp = :fp, #fs = :fs, #fe = :fe, #pi = :pi, #pik = :pik, #pit = :pit, #u = :u",
        expressionAttributeNames: {
          "#s": "status",
          "#ft": "filesTotal",
          "#fp": "filesPreprocessed",
          "#fs": "filesSkipped",
          "#fe": "filesErrored",
          "#pi": "preprocessingIssues",
          "#pik": "preprocessingIssuesS3Key",
          "#pit": "preprocessingIssuesTruncated",
          "#u": "updatedAt",
        },
        expressionAttributeValues: {
          ":s": tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt("$.preprocessResult.status"),
          ),
          ":ft": tasks.DynamoAttributeValue.numberFromString(
            sfn.JsonPath.stringAt(
              "States.Format('{}', $.preprocessResult.files_total)",
            ),
          ),
          ":fp": tasks.DynamoAttributeValue.numberFromString(
            sfn.JsonPath.stringAt(
              "States.Format('{}', $.preprocessResult.files_preprocessed)",
            ),
          ),
          ":fs": tasks.DynamoAttributeValue.numberFromString(
            sfn.JsonPath.stringAt(
              "States.Format('{}', $.preprocessResult.files_skipped)",
            ),
          ),
          ":fe": tasks.DynamoAttributeValue.numberFromString(
            sfn.JsonPath.stringAt(
              "States.Format('{}', $.preprocessResult.files_errored)",
            ),
          ),
          ":pi": tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt(
              "States.JsonToString($.preprocessResult.issues_preview)",
            ),
          ),
          ":pik": tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt("$.preprocessResult.issues_s3_key"),
          ),
          ":pit": tasks.DynamoAttributeValue.booleanFromJsonPath(
            // stringAt() is REQUIRED: given a raw "$.x" string this helper emits
            // {"BOOL": "$.x"} with no ".$", so the path is never substituted and
            // CreateStateMachine rejects the literal string as a boolean
            // (aws-cdk-lib 2.260.0). A token value makes the renderer emit
            // "BOOL.$", substituting the path. Pinned by the synth test below.
            sfn.JsonPath.stringAt("$.preprocessResult.issues_truncated"),
          ),
          ":u": tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.stringAt("$$.State.EnteredTime"),
          ),
        },
        resultPath: sfn.JsonPath.DISCARD,
      },
    );
    savePreprocessingResults.addCatch(docErrorChain, { resultPath: "$.error" });

    const checkPreprocessResult = new sfn.Choice(
      this,
      "SourcesPreprocessingSucceeded?",
    )
      .when(
        sfn.Condition.stringEquals(
          "$.preprocessResult.status",
          SourceStatus.SCAN_FAILED,
        ),
        new sfn.Fail(this, "SourcesPreprocessingAllFailed", {
          cause: "All files failed preprocessing",
          error: "PreprocessingError",
        }),
      )
      .otherwise(kgBuildTask.next(docUpdateCompleted));

    const docIngestionStateMachine = new sfn.StateMachine(
      this,
      "SourcesDocIngestionStateMachine",
      {
        stateMachineName: this.prefixed("sources-doc-ingestion-pipeline"),
        definitionBody: sfn.DefinitionBody.fromChainable(
          docUpdateIngesting
            .next(preprocessTask)
            .next(savePreprocessingResults)
            .next(checkPreprocessResult),
        ),
        timeout: cdk.Duration.hours(24),
      },
    );

    // ── Documents Deletion Pipeline ──────────────────────────────────
    const docCleanupFn = new lambda.Function(
      this,
      "SourcesDocDeletionCleanupFn",
      {
        functionName: this.prefixed("sources-doc-deletion-cleanup"),
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.ARM_64,
        handler: "cleanup_handler.handler",
        code: bundlePython({
          srcDirs: [Paths.sourcesDocumentsDeletion, Paths.commonLib],
          requirementsFile: fromRoot(
            "packages/sources/documents/deletion/requirements.txt",
          ),
          architecture: "arm64",
        }),
        environment: { DOC_SOURCES_TABLE: this.sourcesTable.tableName },
        timeout: cdk.Duration.minutes(5),
        memorySize: 256,
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [lambdaSecurityGroup],
      },
    );
    this.sourcesTable.grantReadWriteData(docCleanupFn);
    sourcesBucket.grantDelete(docCleanupFn);
    sourcesBucket.grantRead(docCleanupFn);

    const docGraphCleanupTask = new tasks.EcsRunTask(
      this,
      "SourcesDocGraphCleanup",
      {
        integrationPattern: sfn.IntegrationPattern.RUN_JOB,
        cluster: kgBuildCluster,
        taskDefinition: kgBuildTaskDef,
        launchTarget: new tasks.EcsFargateLaunchTarget({
          platformVersion: ecs.FargatePlatformVersion.LATEST,
        }),
        subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [ecsSecurityGroup],
        containerOverrides: [
          {
            containerDefinition: kgBuildTaskDef.defaultContainer!,
            command: [
              "python",
              "-m",
              "coa_sources.documents.kg_build.graph_cleanup",
            ],
            environment: [
              {
                name: "DOC_SOURCE_ID",
                value: sfn.JsonPath.stringAt("$.doc_source_id"),
              },
              {
                name: "NAMESPACE_ID",
                value: sfn.JsonPath.stringAt("$.namespace_id"),
              },
              {
                name: "TENANT_ID",
                value: sfn.JsonPath.stringAt("$.tenant_id"),
              },
              { name: "NEPTUNE_ENDPOINT", value: neptuneEndpoint },
              { name: "OPENSEARCH_ENDPOINT", value: opensearchEndpoint },
            ],
          },
        ],
        resultPath: "$.graphCleanupResult",
      },
    );
    docGraphCleanupTask.addRetry({
      errors: ["ECS.ServiceException", "ECS.AmazonECSException"],
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(30),
    });

    const docUpdateDeleteFailed = makeDocStatusUpdate(
      "DocUpdateStatusDeleteFailed",
      SourceStatus.DELETE_FAILED,
      { includeError: true },
    );
    const docDeletionFailState = new sfn.Fail(this, "DocDeletionFailed", {
      cause: "Deletion pipeline failed",
      error: "DeletionError",
    });
    const docDeletionErrorChain =
      docUpdateDeleteFailed.next(docDeletionFailState);

    const docCleanupTask = new tasks.LambdaInvoke(this, "SourcesDocCleanupS3", {
      lambdaFunction: docCleanupFn,
      resultPath: "$.cleanupResult",
      resultSelector: {
        "namespace_id.$": "$.Payload.namespace_id",
        "doc_source_id.$": "$.Payload.doc_source_id",
        "tenant_id.$": "$.Payload.tenant_id",
      },
    });
    docCleanupTask.addRetry({
      maxAttempts: 3,
      backoffRate: 2,
      interval: cdk.Duration.seconds(2),
    });
    docCleanupTask.addCatch(docDeletionErrorChain, { resultPath: "$.error" });
    docGraphCleanupTask.addCatch(docDeletionErrorChain, {
      resultPath: "$.error",
    });

    const docDeleteDdbRecord = new tasks.DynamoDeleteItem(
      this,
      "SourcesDocDeleteDdbRecord",
      {
        table: this.sourcesTable,
        key: {
          PK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.format(
              "NS#{}",
              sfn.JsonPath.stringAt("$.namespace_id"),
            ),
          ),
          SK: tasks.DynamoAttributeValue.fromString(
            sfn.JsonPath.format(
              "SRC#{}",
              sfn.JsonPath.stringAt("$.doc_source_id"),
            ),
          ),
        },
        resultPath: sfn.JsonPath.DISCARD,
      },
    );
    docDeleteDdbRecord.addCatch(docDeletionErrorChain, {
      resultPath: "$.error",
    });

    const docDeletionStateMachine = new sfn.StateMachine(
      this,
      "SourcesDocDeletionStateMachine",
      {
        stateMachineName: this.prefixed("sources-doc-deletion-pipeline"),
        definitionBody: sfn.DefinitionBody.fromChainable(
          docCleanupTask.next(docGraphCleanupTask).next(docDeleteDdbRecord),
        ),
        timeout: cdk.Duration.hours(2),
      },
    );

    // ── Documents Ingestion Queue + Trigger Lambda ───────────────────
    const docIngestionDlq = new sqs.Queue(this, "SourcesDocIngestionDLQ", {
      queueName: this.prefixed("sources-doc-ingestion-dlq"),
      retentionPeriod: cdk.Duration.days(14),
    });
    const docIngestionQueue = new sqs.Queue(this, "SourcesDocIngestionQueue", {
      queueName: this.prefixed("sources-doc-ingestion-queue"),
      visibilityTimeout: cdk.Duration.seconds(900),
      deadLetterQueue: { maxReceiveCount: 3, queue: docIngestionDlq },
    });

    const docTriggerFn = new lambda.Function(
      this,
      "SourcesDocIngestionTriggerFn",
      {
        functionName: this.prefixed("sources-doc-ingestion-trigger"),
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.ARM_64,
        handler: "index.handler",
        code: bundlePython({
          srcDirs: [Paths.sourcesDocumentsTrigger, Paths.commonLib],
          requirementsFile: fromRoot(
            "packages/sources/documents/trigger/requirements.txt",
          ),
          architecture: "arm64",
        }),
        environment: {
          STATE_MACHINE_ARN: docIngestionStateMachine.stateMachineArn,
          // Built from the resolved chat model ID (#94) — an inlined `us.`
          // profile assembled an ARN that does not exist outside the US, and the
          // trigger hands this string to the KG-build container via Step
          // Functions state, so the whole ingestion path inherited it. The helper
          // picks the right ARN shape: a bare in-region model id is a
          // foundation-model ARN, not an inference-profile one.
          BEDROCK_MODEL_ARN: bedrockModelArn(
            chatModelId,
            this.region,
            this.account,
          ),
        },
        timeout: cdk.Duration.seconds(30),
        memorySize: 128,
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [lambdaSecurityGroup],
      },
    );
    docIngestionStateMachine.grantStartExecution(docTriggerFn);
    docTriggerFn.addEventSource(
      new lambdaEventSources.SqsEventSource(docIngestionQueue, {
        batchSize: 1,
        maxConcurrency: 5,
      }),
    );

    // ================================================================
    // Sources API Lambda — unified CRUD for all source types
    // ================================================================
    const sourcesApiFn = new lambda.Function(this, "SourcesApiFn", {
      functionName: this.prefixed("sources-api"),
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "coa_sources.api.sources_handler.handler",
      code: bundlePython({
        srcDirs: [
          // Full src/ tree needed: handler delegates to database/ and documents/ submodules
          fromRoot("packages/sources/src"),
          Paths.commonLib,
          Paths.smithyGeneratedControlPlanePythonServer,
        ],
        requirementsFile: fromRoot("packages/sources/requirements.txt"),
        architecture: "arm64",
      }),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [lambdaSecurityGroup],
      environment: {
        SOURCES_TABLE: this.sourcesTable.tableName,
        SOURCE_SCAN_JOBS_TABLE: this.sourceScanJobsTable.tableName,
        NAMESPACES_TABLE: namespacesTableName,
        SCAN_QUEUE_URL: dbScanQueue.queueUrl,
        INGESTION_QUEUE_URL: docIngestionQueue.queueUrl,
        REVIEW_QUEUE_URL: bulkReviewQueue.queueUrl,
        BUCKET_NAME: sourcesBucket.bucketName,
        DELETION_STATE_MACHINE_ARN: docDeletionStateMachine.stateMachineArn,
        ALLOWED_ORIGIN: allowedOrigin,
        SMUS_DOMAIN_ID: domainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
        FEDERATION_PROVISIONER_ROLE_ARN: federationProvisionerFn.role!.roleArn,
        // Feeds `_build_catalog_name`, which derives the per-source Athena
        // data-catalog name registered at source create. Without it the handler
        // falls back to its hard-coded `coa-dev-` default in every environment.
        RESOURCE_PREFIX: this.prefixed(""),
        // BARE prefix (not `{prefix}-{env}-`) — keys the `{prefix}:namespace` tag on
        // every resource this role checks at registration: a JDBC credential secret,
        // a document source bucket, and a Glue database's ownership. Checking at
        // create time is what tells a customer then, rather than by a failed scan.
        // Must equal the prefix in `nsTagKey` above, which the IAM conditions are
        // written against. Inferring it from RESOURCE_PREFIX would put the
        // environment in the key (`coa-prod:namespace`), hiding a dev-tagged
        // resource from prod.
        RESOURCE_TAG_PREFIX: resolveContext(this.node).prefix,
      },
    });

    this.sourcesTable.grantReadWriteData(sourcesApiFn);
    // Registration verifies that a caller-named bucket carries the
    // `{prefix}:namespace` tag authorizing this namespace, so the customer is told
    // at create time instead of discovering it as a failed scan. Tag metadata
    // only here, and sources-api reads no customer object data (nothing under
    // `raw/*`). Its one s3:GetObject is the narrowly-scoped re-scan backup
    // metadata read granted below (`rescan-backup/*`), which the tables API needs
    // to render the old-vs-new diff panel.
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ReadSourceBucketTags",
        actions: ["s3:GetBucketTagging"],
        resources: ["*"],
      }),
    );
    // On delete, sources-api assumes the federation provisioner's role (a Lake
    // Formation admin able to DROP the federated catalog) to run teardown.
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "AssumeFederationProvisionerRole",
        actions: ["sts:AssumeRole"],
        resources: [federationProvisionerFn.role!.roleArn],
      }),
    );
    (federationProvisionerFn.role as iam.Role).assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        principals: [sourcesApiFn.grantPrincipal],
      }),
    );
    this.sourceScanJobsTable.grantReadWriteData(sourcesApiFn);
    namespacesTable.grantReadWriteData(sourcesApiFn);
    dbScanQueue.grantSendMessages(sourcesApiFn);
    docIngestionQueue.grantSendMessages(sourcesApiFn);
    bulkReviewQueue.grantSendMessages(sourcesApiFn);
    docDeletionStateMachine.grantStartExecution(sourcesApiFn);
    projectAccessRole.grantAssumeRole(sourcesApiFn.role!);

    // DataZone Search/Get/Delete for tables tab and source deletion
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "datazone:Search",
          "datazone:GetAsset",
          "datazone:DeleteAsset",
        ],
        resources: [
          `arn:aws:datazone:${this.region}:${this.account}:domain/${domainId}`,
          `arn:aws:datazone:${this.region}:${this.account}:domain/${domainId}/*`,
        ],
      }),
    );

    // Federation teardown on delete-source runs under the federation
    // provisioner's Lake Formation admin role, which sources-api assumes (see
    // above). The sources-api role therefore needs no Glue/Lake Formation
    // catalog permissions of its own — with one exception below.

    // Read-only tag lookup for the Glue namespace-ownership check at
    // source-create. `catalogId`/`databaseName` arrive from the caller and decide
    // what discovery will read, so create refuses a database whose owner has not
    // tagged it for the caller's namespace (see
    // coa_sources.database.glue_ownership).
    //
    // `glue:GetDatabase` is NOT optional here, however much this would prefer to be
    // a tag read alone: Glue authorizes `GetTags` on a database ARN against
    // `glue:GetDatabase` on the CATALOG as well, so GetTags by itself yields
    //   "not authorized to perform: glue:GetDatabase on resource: ...:catalog"
    // and the check — which fails closed — then refuses every legitimate source.
    // Verified end-to-end in a live account; the narrower policy this originally
    // shipped with made every Glue-source create return 403.
    //
    // The residual is that the control plane can read database metadata (name,
    // description, location URI) account-wide. That is strictly more than reading
    // the authorization, and it is the minimum AWS permits for reading it. It is
    // still metadata only: no table schemas, no Lake Formation grant, no data.
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "GlueOwnershipTagRead",
        actions: ["glue:GetTags", "glue:GetDatabase"],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:database/*`,
        ],
      }),
    );

    // Athena data-catalog lifecycle for custom-connector (CUSTOM_CONNECTOR)
    // sources: sources-api registers a `LAMBDA`-type catalog at source-create
    // time and deletes it at teardown, so create and delete stay co-located on
    // the control-plane role. `GetDataCatalog` is required because registration
    // is made idempotent by a get-then-create check — `CreateDataCatalog`
    // declares no `AlreadyExistsException`, so a duplicate name is a 400
    // indistinguishable from a malformed request.
    //
    // The registrar derives the catalog name with
    // `glue_connection_provisioner.build_catalog_name`
    // (`{sanitizedPrefix}ds_{sha256(sourceId)[:16]}`) — the same derivation the
    // federated-JDBC path uses — which is what lets `fedResourcePrefix` scope
    // this to catalogs this deployment created rather than every catalog in the
    // account. The coupling fails closed in both directions: any other
    // derivation, or a `RESOURCE_PREFIX` that disagrees with `prefixed("")`,
    // turns every `CreateDataCatalog` into an AccessDenied that reads as a
    // policy defect rather than a naming one.
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "AthenaDataCatalogLifecycle",
        actions: [
          "athena:CreateDataCatalog",
          "athena:DeleteDataCatalog",
          "athena:GetDataCatalog",
        ],
        resources: [
          `arn:aws:athena:${this.region}:${this.account}:datacatalog/${fedResourcePrefix}*`,
        ],
      }),
    );

    // Explicit TransactWriteItems grant (grantReadWriteData covers it but be explicit for audit)
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:TransactWriteItems"],
        resources: [
          this.sourcesTable.tableArn,
          this.sourceScanJobsTable.tableArn,
        ],
      }),
    );

    // Pre-signed PUT URLs for document uploads
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["s3:PutObject"],
        resources: [`${sourcesBucket.bucketArn}/*/raw/*`],
      }),
    );

    // Namespace-binding check for JDBC credential secrets. At source
    // registration the API reads the secret's TAGS (DescribeSecret — metadata
    // only, never GetSecretValue) to require a `{prefix}:namespace` tag LISTING the
    // registering namespace, so a source can only be registered against a
    // credential secret bound to its own namespace. Scoped to in-account
    // secrets: cross-account credential secrets are gated by the customer's own
    // resource policy + assume-role, not by tags this deployment cannot set.
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "DescribeSecretForNamespaceBinding",
        actions: ["secretsmanager:DescribeSecret"],
        // Region-wildcard, in-account: matches the region-wildcard read grants
        // on the discovery/federated roles, so a same-account secret in another
        // region can still be verified at registration (the binding check calls
        // DescribeSecret in the secret's own region).
        resources: [`arn:aws:secretsmanager:*:${this.account}:secret:*`],
      }),
    );

    // Re-scan backup blob: the tables API reads the pre-rescan pre-image to
    // render the old-vs-new diff panel and the removed-item sets under
    // RESCAN_REVIEW, and rewrites it when a steward keeps a flagged removal.
    // Without GetObject here the diff read fails closed (rescan_diff_backup_read_failed)
    // and the diff panel never renders.
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:PutObject"],
        resources: [`${sourcesBucket.bucketArn}/*/rescan-backup/*`],
      }),
    );

    // S3 reports a missing key as NoSuchKey only to a caller that also holds
    // ListBucket on the bucket; without it, GetObject on an absent key returns
    // AccessDenied instead. An absent backup blob is a NORMAL state — a re-scan
    // that finds no drift writes none, yet still lands the source in
    // RESCAN_REVIEW — so the read helper's absent-key branch has to be able to
    // fire. Lacking this grant, that branch is unreachable in a deployed
    // environment and the tables page 500s for every no-drift re-scan.
    //
    // Scoped to the bucket ARN with no s3:prefix condition on purpose: the
    // condition governs ListObjects calls, and GetObject's 403-vs-404 choice is
    // not guaranteed to honour it, so a prefix-scoped grant risks looking like a
    // fix while leaving the 500 in place. The grant conveys only "may list this
    // bucket", which the two other roles touching this bucket already hold via
    // grantReadWrite.
    sourcesApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["s3:ListBucket"],
        resources: [sourcesBucket.bucketArn],
      }),
    );

    this.sourcesApiFnArn = sourcesApiFn.functionArn;

    // ================================================================
    // OE monitoring (cdk-monitoring-constructs)
    // ================================================================
    const monitoring = new SclMonitoring(this, "Monitoring", {
      alarmNamePrefix: this.prefixed("sources"),
      alarmAction: props.alarmAction,
    });
    monitoring
      .monitorLambda(sourcesApiFn)
      .monitorLambda(dbConnectorFn)
      .monitorLambda(bulkReviewWorkerFn)
      .monitorLambda(preprocessingFn)
      .monitorLambda(dbTriggerFn)
      .monitorLambda(docCleanupFn)
      .monitorLambda(docTriggerFn)
      .monitorLambda(federationProvisionerFn)
      .monitorStateMachine(dbScanStateMachine)
      .monitorStateMachine(docIngestionStateMachine)
      .monitorStateMachine(docDeletionStateMachine)
      .monitorQueueWithDlq(dbScanQueue, dbScanDlq)
      .monitorQueueWithDlq(bulkReviewQueue, bulkReviewDlq)
      .monitorQueueWithDlq(docIngestionQueue, docIngestionDlq);

    // ================================================================
    // CloudWatch Dashboard — Structured Scan & Enrichment Pipeline
    // ================================================================
    // Custom metrics come from the sources Lambdas via EMF-stdout (see
    // packages/sources/.../database/metrics.py), which the Lambda log pipeline
    // auto-extracts into `COA/Sources`. The enrichment ECS task
    // uses a plain awsLogs driver with no PutMetricData grant, so EMF is NOT
    // extracted there — LLM-side visibility therefore comes from the
    // account-wide AWS/Bedrock namespace rather than per-task custom metrics.
    // SourceType is a fixed low-cardinality dimension (GLUE_DATABASE /
    // JDBC_DATABASE...), but new subtypes appear without a stack change, so
    // every custom-metric widget uses a SEARCH() MathExpression that
    // auto-discovers the live dimensions. `usingMetrics` is intentionally
    // empty for SEARCH exprs.
    const sourcesNamespace = "COA/Sources";
    const sourcesPeriod = cdk.Duration.minutes(5);
    const sourcesRegion = cdk.Aws.REGION;

    /** A SEARCH-based series across all values of `dimensions` for one metric. */
    const sourcesSearch = (
      metricName: string,
      stat: string,
      label: string,
      dimensions = "SourceType",
    ): cloudwatch.MathExpression =>
      new cloudwatch.MathExpression({
        expression: `SEARCH('{${sourcesNamespace},${dimensions}} MetricName="${metricName}"', '${stat}', ${sourcesPeriod.toSeconds()})`,
        label,
        usingMetrics: {},
        period: sourcesPeriod,
        searchRegion: sourcesRegion,
      });

    /** AWS/Bedrock per-model metric — account-wide, NOT pipeline-scoped. */
    const bedrockMetric = (
      metricName: string,
      modelId: string,
      stat = "Sum",
      label = `${modelId} ${metricName}`,
    ): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: "AWS/Bedrock",
        metricName,
        dimensionsMap: { ModelId: modelId },
        statistic: stat,
        period: sourcesPeriod,
        region: sourcesRegion,
        label,
      });

    // The model the enrichment task calls. Derived from the SAME resolved chat
    // model ID the stack configures elsewhere (#94), so a configured model and
    // these widgets cannot drift — previously this tracked the Python-side
    // default independently and went dark on any non-default deploy.
    // Chat, not embed: enrichment never calls the embedding model.
    const enrichmentModelId = chatModelId;

    const scanDashboard = new cloudwatch.Dashboard(this, "ScanDashboard", {
      dashboardName: this.prefixed("sources-structured-scan"),
      defaultInterval: cdk.Duration.days(1),
    });

    scanDashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          "# Structured Scan & Enrichment Pipeline",
          "",
          `Custom metrics publish to \`${sourcesNamespace}\` from the sources **Lambdas** via EMF-stdout, dimensioned by \`SourceType\`.`,
          "Widgets use CloudWatch SEARCH() so a new source subtype appears without a stack change.",
          "The enrichment **ECS task** has no PutMetricData grant and a plain awsLogs driver, so it emits no custom metrics —",
          "LLM latency/token/error visibility below comes from the AWS/Bedrock namespace instead.",
        ].join("\n"),
        width: 24,
        height: 4,
      }),
    );

    scanDashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Scan duration by SourceType (p50 / p90 / p99, ms)",
        left: [
          sourcesSearch("ScanDuration", "p50", "ScanDuration p50"),
          sourcesSearch("ScanDuration", "p90", "ScanDuration p90"),
          sourcesSearch("ScanDuration", "p99", "ScanDuration p99"),
        ],
        leftYAxis: { label: "ms", showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Scan stage durations (Average, ms)",
        left: [
          sourcesSearch("ValidationLatency", "Average", "Validation"),
          sourcesSearch("DiscoveryDuration", "Average", "Discovery"),
          sourcesSearch("ScanDuration", "Average", "Scan wallclock"),
        ],
        leftYAxis: { label: "ms", showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    scanDashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Tables discovered by SourceType (Sum)",
        left: [sourcesSearch("TablesDiscovered", "Sum", "TablesDiscovered")],
        leftYAxis: { label: "tables", showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Catalog asset writes — Create / Update (Sum)",
        // No delete path exists in the metadata writer, so this is Create/Update
        // only, NOT full CRUD.
        left: [
          sourcesSearch(
            "CatalogAssetWrites",
            "Sum",
            "Asset writes by Operation",
            "Operation",
          ),
        ],
        leftYAxis: { label: "assets", showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    scanDashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Connection validation — Success / Failure (Sum)",
        left: [
          sourcesSearch(
            "ConnectionValidation",
            "Sum",
            "Validation by SourceType/Result",
            "SourceType,Result",
          ),
        ],
        leftYAxis: { label: "count", showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Enrichment review — approved / rejected + acceptance rate",
        left: [
          sourcesSearch(
            "TablesApprovedByReview",
            "Sum",
            "Approved",
            "ReviewScope",
          ),
          sourcesSearch(
            "TablesRejectedByReview",
            "Sum",
            "Rejected",
            "ReviewScope",
          ),
        ],
        right: [
          new cloudwatch.MathExpression({
            // acceptance rate = approved / (approved + rejected). SUM() collapses
            // each SEARCH array to a single series so the division is valid.
            expression: "approved / (approved + rejected)",
            label: "Acceptance rate",
            usingMetrics: {
              approved: new cloudwatch.MathExpression({
                expression: `SUM(SEARCH('{${sourcesNamespace},ReviewScope} MetricName="TablesApprovedByReview"', 'Sum', ${sourcesPeriod.toSeconds()}))`,
                label: "approved",
                usingMetrics: {},
                period: sourcesPeriod,
                searchRegion: sourcesRegion,
              }),
              rejected: new cloudwatch.MathExpression({
                expression: `SUM(SEARCH('{${sourcesNamespace},ReviewScope} MetricName="TablesRejectedByReview"', 'Sum', ${sourcesPeriod.toSeconds()}))`,
                label: "rejected",
                usingMetrics: {},
                period: sourcesPeriod,
                searchRegion: sourcesRegion,
              }),
            },
            period: sourcesPeriod,
          }),
        ],
        leftYAxis: { label: "tables", showUnits: false },
        rightYAxis: { label: "rate", min: 0, max: 1, showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    scanDashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: [
          "## Bedrock (enrichment LLM) — account-wide, not pipeline-isolated",
          "",
          `\`AWS/Bedrock\` metrics are per **ModelId** across the whole account. Sources enrichment and ontology induction`,
          `both invoke \`${enrichmentModelId}\`, so these series are a **sum of both pipelines**, not sources alone.`,
          "Per-pipeline LLM attribution would require PutMetricData from the enrichment ECS task (an IAM grant it does not have).",
        ].join("\n"),
        width: 24,
        height: 3,
      }),
    );

    scanDashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Bedrock latency + errors (account-wide)",
        left: [
          bedrockMetric(
            "InvocationLatency",
            enrichmentModelId,
            "Average",
            "InvocationLatency avg",
          ),
          bedrockMetric(
            "InvocationLatency",
            enrichmentModelId,
            "p90",
            "InvocationLatency p90",
          ),
        ],
        right: [
          bedrockMetric("InvocationClientErrors", enrichmentModelId),
          bedrockMetric("InvocationServerErrors", enrichmentModelId),
          bedrockMetric("InvocationThrottles", enrichmentModelId),
        ],
        leftYAxis: { label: "ms", showUnits: false },
        rightYAxis: { label: "errors", showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Bedrock token usage (account-wide)",
        left: [
          bedrockMetric("InputTokenCount", enrichmentModelId),
          bedrockMetric("OutputTokenCount", enrichmentModelId),
        ],
        leftYAxis: { label: "tokens", showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    scanDashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Scan state machine — execution outcomes (Sum)",
        left: [
          dbScanStateMachine.metricSucceeded({
            period: sourcesPeriod,
            label: "Succeeded",
          }),
          dbScanStateMachine.metricFailed({
            period: sourcesPeriod,
            label: "Failed",
          }),
          dbScanStateMachine.metricTimedOut({
            period: sourcesPeriod,
            label: "TimedOut",
          }),
          dbScanStateMachine.metricAborted({
            period: sourcesPeriod,
            label: "Aborted",
          }),
        ],
        leftYAxis: { label: "executions", showUnits: false },
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: "Scan state machine — execution time (ms)",
        left: [
          dbScanStateMachine.metricTime({
            period: sourcesPeriod,
            statistic: "Average",
            label: "ExecutionTime avg",
          }),
          dbScanStateMachine.metricTime({
            period: sourcesPeriod,
            statistic: "p90",
            label: "ExecutionTime p90",
          }),
        ],
        leftYAxis: { label: "ms", showUnits: false },
        width: 12,
        height: 6,
      }),
    );

    scanDashboard.addWidgets(
      new cloudwatch.SingleValueWidget({
        title: "Failed executions (drill down via the console link)",
        metrics: [
          dbScanStateMachine.metricFailed({
            period: sourcesPeriod,
            label: "Failed",
          }),
          dbScanStateMachine.metricTimedOut({
            period: sourcesPeriod,
            label: "TimedOut",
          }),
          // botocore's standard retry mode swallows retried throttles; only the
          // terminal ClientError is counted (see glue_connection_provisioner).
          sourcesSearch("GlueApiThrottles", "Sum", "Glue throttles", "Api"),
        ],
        setPeriodToTimeRange: true,
        width: 12,
        height: 6,
      }),
      new cloudwatch.TextWidget({
        markdown: [
          "## Drill-downs",
          "",
          `**Failed scan executions** — [Step Functions console](https://${sourcesRegion}.console.aws.amazon.com/states/home?region=${sourcesRegion}#/statemachines/view/${dbScanStateMachine.stateMachineArn})`,
          "(filter Executions by status = Failed, then open the failing state's input/output).",
          "",
          "**Overlay match rate** — pending #114 (SageMaker Catalog Overlay, M2).",
          "No overlay code exists yet, so there is no metric to chart; this widget is a placeholder, not a broken query.",
        ].join("\n"),
        width: 12,
        height: 6,
      }),
    );

    // ================================================================
    // SSM Parameters
    // ================================================================
    new ssm.StringParameter(this, "SsmSourcesApiFnArn", {
      parameterName: `${ssmPrefix}/sources/api-fn-arn`,
      stringValue: sourcesApiFn.functionArn,
      description: "Sources API Lambda ARN",
    });
    new ssm.StringParameter(this, "SsmSourcesTableName", {
      parameterName: `${ssmPrefix}/sources/sources-table-name`,
      stringValue: this.sourcesTable.tableName,
      description: "Sources DynamoDB table name",
    });
    new ssm.StringParameter(this, "SsmSourceScanJobsTableName", {
      parameterName: `${ssmPrefix}/sources/source-scan-jobs-table-name`,
      stringValue: this.sourceScanJobsTable.tableName,
      description: "Source scan jobs DynamoDB table name",
    });
    new ssm.StringParameter(this, "SsmSourcesDocIngestionQueueUrl", {
      parameterName: `${ssmPrefix}/sources/doc-ingestion-queue-url`,
      stringValue: docIngestionQueue.queueUrl,
      description: "Sources document ingestion SQS queue URL",
    });
    new ssm.StringParameter(this, "SsmSourcesDbScanQueueUrl", {
      parameterName: `${ssmPrefix}/sources/db-scan-queue-url`,
      stringValue: dbScanQueue.queueUrl,
      description: "Sources database scan SQS queue URL",
    });
    new ssm.StringParameter(this, "SsmSourcesDbScanStateMachineArn", {
      parameterName: `${ssmPrefix}/sources/db-scan-state-machine-arn`,
      stringValue: dbScanStateMachine.stateMachineArn,
      description: "Sources database scan Step Functions state machine ARN",
    });
    new ssm.StringParameter(this, "SsmSourcesDocIngestionStateMachineArn", {
      parameterName: `${ssmPrefix}/sources/doc-ingestion-state-machine-arn`,
      stringValue: docIngestionStateMachine.stateMachineArn,
      description:
        "Sources document ingestion Step Functions state machine ARN",
    });
    new ssm.StringParameter(this, "SsmSourcesDbConnectorRoleArn", {
      parameterName: `${ssmPrefix}/sources/db-connector-role-arn`,
      stringValue: dbConnectorFn.role!.roleArn,
      description: "Sources database connector Lambda execution role ARN",
    });
    new ssm.StringParameter(this, "SsmSourcesDbEnrichmentRoleArn", {
      parameterName: `${ssmPrefix}/sources/db-enrichment-role-arn`,
      stringValue: dbEnrichmentTaskDef.taskRole.roleArn,
      description: "Sources database enrichment ECS task role ARN",
    });

    // ================================================================
    // CfnOutputs
    // ================================================================
    new cdk.CfnOutput(this, "SourcesTableName", {
      value: this.sourcesTable.tableName,
    });
    new cdk.CfnOutput(this, "SourceScanJobsTableName", {
      value: this.sourceScanJobsTable.tableName,
    });
    new cdk.CfnOutput(this, "SourcesApiFnArn", {
      value: sourcesApiFn.functionArn,
    });
    new cdk.CfnOutput(this, "SourcesDataBucketName", {
      value: sourcesBucket.bucketName,
    });
    new cdk.CfnOutput(this, "SourcesDocIngestionStateMachineArn", {
      value: docIngestionStateMachine.stateMachineArn,
    });
    new cdk.CfnOutput(this, "SourcesDbScanStateMachineArn", {
      value: dbScanStateMachine.stateMachineArn,
    });
  }
}
