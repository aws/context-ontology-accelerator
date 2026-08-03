// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
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
import { Construct } from "constructs";

import { SCLStack } from "../../constructs/scl-stack";
import { AccessLogsBucket } from "../../constructs";
import { DynamoDBTable } from "../../constructs/dynamodb-table";
import { LakeFormationAdmin } from "../../constructs/lakeformation-admin";
import { SclMonitoring } from "../../constructs";
import { resolveContext } from "../../context";
import { Paths, fromRoot } from "../../paths";
import { bundlePython } from "../../utils/python-bundling";
import { NetworkStack } from "../foundation/network-stack";
import { StorageStack } from "../foundation/storage-stack";
import { TABLE_NAMES } from "@coa/shared";
import { SourceStatus, DEFAULT_MAX_FILE_SIZE_MB } from "../../constants";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";

export interface SourcesStackProps extends cdk.StackProps {
  readonly network: NetworkStack;
  readonly storage: StorageStack;
  readonly allowedOrigin?: string;
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
    const allowedOrigin = props.allowedOrigin ?? "*";
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
    // role. Allow cross-account secrets (a customer's secret in their own
    // account) — cross-account access is still gated by the secret's resource
    // policy, which the customer must grant to this role (see docs). Hence no
    // aws:ResourceAccount restriction here.
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadCredentialSecret",
        actions: ["secretsmanager:GetSecretValue"],
        resources: ["arn:aws:secretsmanager:*:*:secret:*"],
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
    // Mutating ENI actions — Glue's managed connector provisioner validates the
    // connection by creating ENIs and may access subnets beyond the explicitly
    // configured connector subnet (e.g. VPC endpoint subnets for service
    // communication). Scoping to specific subnets causes "Unable to access VPC"
    // failures. Use wildcard for subnet resources while keeping SG and ENI scoped
    // to the account.
    federatedCatalogRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Ec2NetworkInterfaceManagement",
        actions: [
          "ec2:CreateNetworkInterface",
          "ec2:DeleteNetworkInterface",
          "ec2:CreateTags",
        ],
        resources: [
          `arn:aws:ec2:${this.region}:${this.account}:network-interface/*`,
          `arn:aws:ec2:${this.region}:${this.account}:subnet/*`,
          `arn:aws:ec2:${this.region}:${this.account}:security-group/*`,
        ],
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
      securityGroups: [lambdaSecurityGroup],
      deadLetterQueue: dbConnectorDlq,
      environment: {
        SOURCES_TABLE: this.sourcesTable.tableName,
        SOURCE_SCAN_JOBS_TABLE: this.sourceScanJobsTable.tableName,
        NAMESPACES_TABLE: namespacesTableName,
        SMUS_DOMAIN_ID: domainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
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
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
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
    // Athena reads the table data during sampling; Glue sources backed by the
    // sources-data bucket need read (GetObject + ListBucket) on it.
    sourcesBucket.grantRead(dbConnectorFn);
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
        },
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
    // See LakeFormationAdmin for the bootstrap caveat (the CR Lambda role must
    // itself be an LF admin in accounts that already have admins).
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
    // Secrets Manager: Context Ontology Accelerator-managed secrets + customer-provided credential secrets.
    // Customer secrets are arbitrary ARNs provided at source creation time.
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "SecretsManagerCoaManaged",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:${this.prefixed("datasource-")}*`,
        ],
      }),
    );
    dbConnectorFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "SecretsManagerCustomerProvided",
        actions: ["secretsmanager:GetSecretValue"],
        resources: [`arn:aws:secretsmanager:*:*:secret:*`],
        conditions: {
          StringEquals: {
            "aws:ResourceAccount": this.account,
          },
        },
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
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:${this.prefixed("datasource-")}*`,
        ],
      }),
    );
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        resources: [
          `arn:aws:iam::*:role/${this.prefixed("datasource-access-")}*`,
        ],
      }),
    );
    dbEnrichmentTaskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "glue:GetTable",
          "glue:GetTables",
          "glue:GetDatabase",
          "glue:GetConnection",
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
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
          ],
        },
      ],
      resultPath: "$.enrichmentResult",
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
        timeout: cdk.Duration.hours(1),
      },
    );

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
        NAMESPACES_TABLE: namespacesTableName,
        SMUS_DOMAIN_ID: domainId,
        PROJECT_ACCESS_ROLE_ARN: projectAccessRoleArn,
        // Parallel revision writes per worker invocation. Tunable per env.
        BULK_REVIEW_PARALLELISM: "10",
        // Self-continuation: a source larger than one invocation can process
        // is paged by re-enqueuing to this same queue with a nextToken. Every
        // table is eventually approved with no silent drop (#853).
        REVIEW_QUEUE_URL: bulkReviewQueue.queueUrl,
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
    namespacesTable.grantReadData(bulkReviewWorkerFn);
    projectAccessRole.grantAssumeRole(bulkReviewWorkerFn.role!);
    // Self-continuation: the worker re-enqueues to its own queue to page a
    // large source across invocations (#853).
    bulkReviewQueue.grantSendMessages(bulkReviewWorkerFn);

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
        reservedConcurrentExecutions: 5,
        environment: {
          BUCKET_NAME: sourcesBucket.bucketName,
          DOC_SOURCES_TABLE: this.sourcesTable.tableName,
          MAX_FILE_SIZE_MB: DEFAULT_MAX_FILE_SIZE_MB.toString(),
          CROSS_ACCOUNT_ROLE_PREFIX: resolveContext(this.node).prefix,
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
        actions: ["s3:GetObject", "s3:GetObjectTagging", "s3:ListBucket"],
        // Wildcard required: customers provide their own bucket names.
        resources: ["*"],
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
        actions: ["textract:DetectDocumentText"],
        resources: ["*"],
      }),
    );

    // ── KgBuild ECS Task ─────────────────────────────────────────────
    const kgBuildCluster = new ecs.Cluster(this, "SourcesKgBuildCluster", {
      clusterName: this.prefixed("sources-doc-kg-build-cluster"),
      vpc,
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
        // Only pass the summary fields needed by downstream states.
        resultSelector: {
          "status.$": "$.Payload.status",
          "files_total.$": "$.Payload.files_total",
          "files_preprocessed.$": "$.Payload.files_preprocessed",
          "files_skipped.$": "$.Payload.files_skipped",
          "files_errored.$": "$.Payload.files_errored",
          "staging_prefix.$": "$.Payload.staging_prefix",
          "issues.$": "$.Payload.issues",
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
          "SET #s = :s, #ft = :ft, #fp = :fp, #fs = :fs, #fe = :fe, #pi = :pi, #u = :u",
        expressionAttributeNames: {
          "#s": "status",
          "#ft": "filesTotal",
          "#fp": "filesPreprocessed",
          "#fs": "filesSkipped",
          "#fe": "filesErrored",
          "#pi": "preprocessingIssues",
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
              "States.JsonToString($.preprocessResult.issues)",
            ),
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
          BEDROCK_MODEL_ARN: `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0`,
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
      },
    });

    this.sourcesTable.grantReadWriteData(sourcesApiFn);
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
    // catalog permissions of its own.

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
