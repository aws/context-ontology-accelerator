// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as cr from "aws-cdk-lib/custom-resources";
import * as datazone from "aws-cdk-lib/aws-datazone";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaNodejs from "aws-cdk-lib/aws-lambda-nodejs";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as path from "path";
import { Construct } from "constructs";
import { SCLStack } from "../../constructs/scl-stack";
import { DynamoDBTable } from "../../constructs/dynamodb-table";
import { Paths, fromRoot } from "../../paths";
import { resolveContext } from "../../context";
import { bundlePython } from "../../utils/python-bundling";
import { NamespaceDeletionPipeline } from "./namespace-deletion-pipeline";
import { TABLE_NAMES } from "@coa/shared";

export interface NamespaceStackProps extends cdk.StackProps {
  /** VPC for Lambda placement. */
  readonly vpc: ec2.IVpc;
  /** Shared Lambda security group (allows egress + recognized by ECS ingress rules). */
  readonly lambdaSecurityGroup: ec2.ISecurityGroup;
  /** Roles table from AuthnzStack. */
  readonly rolesTable: dynamodb.ITable;
  /** ResourceRoleMappings table from AuthnzStack. */
  readonly resourceRoleMappingsTable: dynamodb.ITable;
  /** CORS allowed origin (e.g. `https://myapp.example.com` or `*`). */
  readonly allowedOrigin?: string;
  /** VKG ECS cluster ARN (for teardown on namespace delete). */
  readonly vkgClusterArn?: string;
  /** Cloud Map namespace ID (for teardown on namespace delete). */
  readonly cloudMapNamespaceId?: string;
  /** Ontology S3 bucket name (for artifact cleanup on namespace delete). */
  readonly ontologyBucketName?: string;
  /**
   * Ontology-engine HTTP endpoint (Cloud Map DNS, e.g.
   * http://ontology-engine.<prefix>-services.local:8001). Used by both
   * namespaceApiFn (to check active induction jobs before deletion) and
   * the deletion pipeline's DeleteOntology step (to purge ontology data:
   * Neptune graphs, AOSS vector index, DynamoDB records).
   */
  readonly ontologyEngineEndpoint?: string;
}

/**
 * Service stack: Namespace management infrastructure.
 *
 * Provisions:
 *   - Namespaces DynamoDB table with NameByIndex GSI
 *   - SageMaker Unified Studio (SMUS) IAM-based domain used as the
 *     metadata store for namespace→project and data-asset lifecycle
 *
 * Outputs the domain ID to SSM at `/{prefix}/smus/domain-id` so that
 * runtime code (control-plane Python package) can discover it.
 */
export class NamespaceStack extends SCLStack {
  /** The SMUS domain ID. */
  public readonly domainId: string;

  /** Namespaces table. */
  public readonly namespacesTable: dynamodb.Table;

  /** Namespace API Lambda — handles create + list. */
  public readonly namespaceApiFn: lambda.IFunction;

  /** Roles API Lambda — handles list/get namespace roles. */
  public readonly rolesApiFn: lambda.IFunction;

  /** Platform Roles API Lambda — handles list platform roles. */
  public readonly platformRolesApiFn: lambda.IFunction;

  /** Grants API Lambda — handles grant CRUD operations. */
  public readonly grantsApiFn: lambda.IFunction;

  constructor(scope: Construct, id: string, props: NamespaceStackProps) {
    super(scope, id, props);
    this.addComponentTag("namespace");

    const { ssmPrefix } = resolveContext(this.node);
    const allowedOrigin = props.allowedOrigin ?? "*";

    const PK = { name: "PK", type: dynamodb.AttributeType.STRING };
    const SK = { name: "SK", type: dynamodb.AttributeType.STRING };

    // ── Namespaces table ─────────────────────────────────────────
    const namespaces = new DynamoDBTable(this, "NamespacesTable", {
      tableName: TABLE_NAMES.NAMESPACES,
      partitionKey: PK,
      sortKey: SK,
      timeToLiveAttribute: "ttl",
    });
    namespaces.addGlobalSecondaryIndex({
      indexName: "NameByIndex",
      partitionKey: { name: "name", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.namespacesTable = namespaces.table;

    // ── Domain execution role ────────────────────────────────────
    // Trust policy per https://docs.aws.amazon.com/sagemaker-unified-studio/latest/adminguide/setup-iam-based-domains.html
    const smusPrincipals = [
      "datazone.amazonaws.com",
      "sagemaker.amazonaws.com",
      "glue.amazonaws.com",
      "bedrock.amazonaws.com",
      "scheduler.amazonaws.com",
      "lakeformation.amazonaws.com",
      "airflow-serverless.amazonaws.com",
      "athena.amazonaws.com",
      "redshift.amazonaws.com",
      "emr-serverless.amazonaws.com",
    ];

    // Custom scoped policy for SMUS domain execution role.
    // Grants only the minimum permissions required for SMUS IAM-based domain operations.
    // Replaces SageMakerStudioAdminIAMPermissiveExecutionPolicy which grants overly broad iam:* and s3:* wildcards.
    const smusExecutionPolicy = new iam.ManagedPolicy(
      this,
      "SMUSExecutionPolicy",
      {
        managedPolicyName: this.prefixed("smus-exec-policy"),
        description: "Scoped permissions for SMUS domain execution role",
        statements: [
          // DataZone domain operations
          new iam.PolicyStatement({
            actions: [
              "datazone:CreateDomain",
              "datazone:GetDomain",
              "datazone:UpdateDomain",
              "datazone:DeleteDomain",
              "datazone:ListDomains",
              "datazone:CreateProject",
              "datazone:GetProject",
              "datazone:UpdateProject",
              "datazone:DeleteProject",
              "datazone:CreateEnvironment",
              "datazone:GetEnvironment",
              "datazone:UpdateEnvironment",
              "datazone:DeleteEnvironment",
            ],
            resources: ["*"],
          }),
          // SageMaker operations for SMUS
          new iam.PolicyStatement({
            actions: [
              "sagemaker:CreateDomain",
              "sagemaker:DescribeDomain",
              "sagemaker:UpdateDomain",
              "sagemaker:DeleteDomain",
              "sagemaker:CreateUserProfile",
              "sagemaker:DescribeUserProfile",
              "sagemaker:UpdateUserProfile",
              "sagemaker:DeleteUserProfile",
              "sagemaker:CreateSpace",
              "sagemaker:DescribeSpace",
              "sagemaker:UpdateSpace",
              "sagemaker:DeleteSpace",
            ],
            resources: [
              `arn:aws:sagemaker:${this.region}:${this.account}:domain/*`,
              `arn:aws:sagemaker:${this.region}:${this.account}:user-profile/*`,
              `arn:aws:sagemaker:${this.region}:${this.account}:space/*`,
            ],
          }),
          // Glue catalog operations
          new iam.PolicyStatement({
            actions: [
              "glue:CreateDatabase",
              "glue:GetDatabase",
              "glue:UpdateDatabase",
              "glue:DeleteDatabase",
              "glue:CreateTable",
              "glue:GetTable",
              "glue:UpdateTable",
              "glue:DeleteTable",
              "glue:GetTables",
            ],
            resources: [
              `arn:aws:glue:${this.region}:${this.account}:catalog`,
              `arn:aws:glue:${this.region}:${this.account}:database/*`,
              `arn:aws:glue:${this.region}:${this.account}:table/*`,
            ],
          }),
          // Lake Formation permissions
          new iam.PolicyStatement({
            actions: [
              "lakeformation:GrantPermissions",
              "lakeformation:RevokePermissions",
              "lakeformation:GetDataAccess",
              "lakeformation:ListPermissions",
            ],
            resources: ["*"],
          }),
          // Athena query operations
          new iam.PolicyStatement({
            actions: [
              "athena:StartQueryExecution",
              "athena:GetQueryExecution",
              "athena:GetQueryResults",
              "athena:StopQueryExecution",
            ],
            resources: [
              `arn:aws:athena:${this.region}:${this.account}:workgroup/*`,
            ],
          }),
          // Redshift operations
          new iam.PolicyStatement({
            actions: [
              "redshift:DescribeClusters",
              "redshift:GetClusterCredentials",
            ],
            resources: [
              `arn:aws:redshift:${this.region}:${this.account}:cluster:*`,
            ],
          }),
          // S3 access scoped to this deployment's resources only
          new iam.PolicyStatement({
            actions: [
              "s3:GetObject",
              "s3:PutObject",
              "s3:DeleteObject",
              "s3:ListBucket",
            ],
            resources: [
              `arn:aws:s3:::${this.prefixed("*")}`,
              `arn:aws:s3:::${this.prefixed("*")}/*`,
            ],
          }),
          // IAM operations scoped to roles/policies created by this stack only
          new iam.PolicyStatement({
            actions: [
              "iam:GetRole",
              "iam:PassRole",
              "iam:ListRolePolicies",
              "iam:GetRolePolicy",
            ],
            resources: [
              `arn:aws:iam::${this.account}:role/${this.prefixed("*")}`,
            ],
          }),
          // EventBridge Scheduler operations
          new iam.PolicyStatement({
            actions: [
              "scheduler:CreateSchedule",
              "scheduler:GetSchedule",
              "scheduler:UpdateSchedule",
              "scheduler:DeleteSchedule",
            ],
            resources: [
              `arn:aws:scheduler:${this.region}:${this.account}:schedule/*`,
            ],
          }),
          // Bedrock operations for agents and knowledge bases
          new iam.PolicyStatement({
            actions: [
              "bedrock:CreateAgent",
              "bedrock:GetAgent",
              "bedrock:UpdateAgent",
              "bedrock:DeleteAgent",
              "bedrock:InvokeAgent",
              "bedrock:CreateKnowledgeBase",
              "bedrock:GetKnowledgeBase",
              "bedrock:UpdateKnowledgeBase",
              "bedrock:DeleteKnowledgeBase",
            ],
            resources: [
              `arn:aws:bedrock:${this.region}:${this.account}:agent/*`,
              `arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/*`,
            ],
          }),
          // EMR Serverless operations
          new iam.PolicyStatement({
            actions: [
              "emr-serverless:CreateApplication",
              "emr-serverless:GetApplication",
              "emr-serverless:UpdateApplication",
              "emr-serverless:DeleteApplication",
              "emr-serverless:StartJobRun",
              "emr-serverless:GetJobRun",
              "emr-serverless:CancelJobRun",
            ],
            resources: [
              `arn:aws:emr-serverless:${this.region}:${this.account}:/applications/*`,
            ],
          }),
          // MWAA (Airflow) operations
          new iam.PolicyStatement({
            actions: [
              "airflow:GetEnvironment",
              "airflow:CreateEnvironment",
              "airflow:UpdateEnvironment",
              "airflow:DeleteEnvironment",
            ],
            resources: [
              `arn:aws:airflow:${this.region}:${this.account}:environment/*`,
            ],
          }),
        ],
      },
    );

    const executionRole = new iam.Role(this, "DomainExecutionRole", {
      roleName: this.prefixed("smus-exec"),
      assumedBy: new iam.ServicePrincipal("datazone.amazonaws.com"),
      managedPolicies: [smusExecutionPolicy],
    });

    // All 10 SMUS service principals with the 4 required STS actions,
    // scoped to the deploying account to prevent cross-account assumption.
    executionRole.assumeRolePolicy!.addStatements(
      new iam.PolicyStatement({
        actions: [
          "sts:AssumeRole",
          "sts:TagSession",
          "sts:SetContext",
          "sts:SetSourceIdentity",
        ],
        principals: smusPrincipals.map((s) => new iam.ServicePrincipal(s)),
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
        },
      }),
    );

    // ── SMUS domain (IAM auth — no singleSignOn property) ────────
    const domain = new datazone.CfnDomain(this, "SMUSDomain", {
      name: this.prefixed("smus-catalog"),
      domainExecutionRole: executionRole.roleArn,
      serviceRole: executionRole.roleArn,
      domainVersion: "V2",
      description:
        "SMUS domain for namespace isolation and metadata management",
    });
    domain.node.addDependency(executionRole);
    // CFN's native DeleteDomain has no force/cascade option (unlike
    // DeleteProject, which CFN always calls with skipDeletionCheck=true
    // under the hood — confirmed via CloudTrail), so it fails outright
    // once any project exists under the domain: "Domain cannot be
    // deleted because there are existing projects under this domain".
    // Namespace-created projects (one per namespace, holding that
    // namespace's scanned-table assets) always exist while any namespace
    // does, so this is not an edge case — RETAIN means CFN never
    // attempts this delete itself; destroy.sh calls DataZone's own
    // delete-domain --skip-deletion-check directly instead, which does
    // support a full cascading force-delete at the domain level.
    domain.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    this.domainId = domain.attrId;

    // ── Shared project access role ─────────────────────────────────
    // A single IAM role registered as PROJECT_OWNER on every namespace's
    // DataZone project at creation time (see service.py's
    // _add_service_role_memberships, which explicitly passes
    // designation="PROJECT_OWNER" — the create_project_membership default
    // is PROJECT_CONTRIBUTOR, which is not sufficient for DeleteProject).
    // Service Lambdas assume this role for DataZone project operations
    // (Search, GetAsset, CreateAssetRevision, DeleteProject) instead of
    // each needing individual project membership. DeleteProject in
    // particular requires the caller to be a project *owner*, not just
    // IAM-permitted on the datazone:DeleteProject action — the deletion
    // pipeline's DeletePlatform step assumes this role for that reason
    // (see cleanup.delete_smu_project).
    const dzProjectAccessRole = new iam.Role(this, "DzProjectAccessRole", {
      roleName: this.prefixed("dz-project-access"),
      assumedBy: new iam.AccountPrincipal(this.account),
      description:
        "Shared role for DataZone project access - assumed by service Lambdas",
      inlinePolicies: {
        DataZoneAccess: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: [
                "datazone:CreateAsset",
                "datazone:CreateAssetRevision",
                "datazone:CreateFormType",
                "datazone:DeleteAsset",
                "datazone:DeleteProject",
                "datazone:GetAsset",
                "datazone:GetFormType",
                "datazone:GetProject",
                "datazone:ListAssets",
                "datazone:Search",
                "datazone:SearchListings",
              ],
              resources: [
                `arn:aws:datazone:${this.region}:${this.account}:domain/${domain.attrId}`,
                `arn:aws:datazone:${this.region}:${this.account}:domain/${domain.attrId}/*`,
              ],
            }),
          ],
        }),
      },
    });

    const dzProjectAccessUserProfile = new datazone.CfnUserProfile(
      this,
      "DzProjectAccessUserProfile",
      {
        domainIdentifier: domain.attrId,
        userIdentifier: dzProjectAccessRole.roleArn,
        userType: "IAM_ROLE",
        status: "ACTIVATED",
      },
    );
    dzProjectAccessUserProfile.node.addDependency(dzProjectAccessRole);
    dzProjectAccessUserProfile.node.addDependency(domain);
    // #660 — same DataZone delete-order class of problem as FormType
    // below (see its comment for detail): the native CfnUserProfile L1
    // has no hook to satisfy DataZone's own deletion preconditions, so
    // RETAIN avoids CFN attempting (and failing) to delete it directly.
    dzProjectAccessUserProfile.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    new ssm.StringParameter(this, "SsmDzProjectAccessRoleArn", {
      parameterName: `${ssmPrefix}/smus/dz-project-access-role-arn`,
      stringValue: dzProjectAccessRole.roleArn,
      description: "New shared DataZone project access role ARN",
    });

    // ── Default project profile (metadata-only, no blueprints) ──
    const projectProfile = new datazone.CfnProjectProfile(
      this,
      "DefaultProjectProfile",
      {
        domainIdentifier: domain.attrId,
        name: this.prefixed("default"),
        description: "Default metadata-only project profile",
        status: "ENABLED",
      },
    );
    projectProfile.node.addDependency(domain);
    projectProfile.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    const projectProfileId = projectProfile.attrId;

    // ── System project (owns shared form/asset types) ────────────
    const createFormTypeGrant = new datazone.CfnPolicyGrant(
      this,
      "CreateFormTypeGrant",
      {
        domainIdentifier: domain.attrId,
        entityType: "DOMAIN_UNIT",
        entityIdentifier: domain.attrRootDomainUnitId,
        policyType: "CREATE_FORM_TYPE",
        principal: {
          project: {
            projectDesignation: "OWNER",
            projectGrantFilter: {
              domainUnitFilter: {
                domainUnit: domain.attrRootDomainUnitId,
                includeChildDomainUnits: true,
              },
            },
          },
        },
        detail: {
          createFormType: { includeChildDomainUnits: true },
        },
      },
    );
    createFormTypeGrant.node.addDependency(domain);
    createFormTypeGrant.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    const createAssetTypeGrant = new datazone.CfnPolicyGrant(
      this,
      "CreateAssetTypeGrant",
      {
        domainIdentifier: domain.attrId,
        entityType: "DOMAIN_UNIT",
        entityIdentifier: domain.attrRootDomainUnitId,
        policyType: "CREATE_ASSET_TYPE",
        principal: {
          project: {
            projectDesignation: "OWNER",
            projectGrantFilter: {
              domainUnitFilter: {
                domainUnit: domain.attrRootDomainUnitId,
                includeChildDomainUnits: true,
              },
            },
          },
        },
        detail: {
          createAssetType: { includeChildDomainUnits: true },
        },
      },
    );
    createAssetTypeGrant.node.addDependency(domain);
    createAssetTypeGrant.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    const systemProject = new datazone.CfnProject(this, "SystemProject", {
      domainIdentifier: domain.attrId,
      name: this.prefixed("system"),
      description: "System project owning shared form and asset types",
      projectProfileId: projectProfileId,
    });
    systemProject.node.addDependency(domain);
    systemProject.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // ── Custom Form Type ─────────────────────────────────────────
    const formType = new datazone.CfnFormType(this, "CoaTableMetadataForm", {
      domainIdentifier: domain.attrId,
      name: "CoaTableMetadata",
      owningProjectIdentifier: systemProject.attrId,
      model: {
        smithy: [
          "structure CoaTableMetadata {",
          "    @amazon.datazone#searchable",
          "    tableName: smithy.api#String",
          "    @amazon.datazone#searchable",
          "    databaseName: smithy.api#String",
          "    @amazon.datazone#searchable",
          "    dataSourceId: smithy.api#String",
          "    @amazon.datazone#searchable",
          "    namespaceId: smithy.api#String",
          "    databaseDescription: smithy.api#String",
          "    @amazon.datazone#searchable",
          "    description: smithy.api#String",
          "    columnCount: smithy.api#Integer",
          "    partitionKeys: smithy.api#String",
          "    format: smithy.api#String",
          "    location: smithy.api#String",
          "    @amazon.datazone#searchable",
          "    synonyms: smithy.api#String",
          "    @amazon.datazone#searchable",
          "    glossaryTerms: smithy.api#String",
          "    tags: smithy.api#String",
          "    enrichmentSource: smithy.api#String",
          "    reviewStatus: smithy.api#String",
          "    primaryKeyColumns: smithy.api#String",
          "    primaryKeySource: smithy.api#String",
          "    primaryKeyConfidence: smithy.api#Float",
          "    foreignKeys: smithy.api#String",
          "    columns: smithy.api#String",
          "}",
        ].join("\n"),
      },
      status: "ENABLED",
      description:
        "Enriched table metadata: technical metadata, business metadata, PKs, FKs, columns",
    });
    formType.node.addDependency(systemProject);
    formType.node.addDependency(createFormTypeGrant);
    // #660 — DataZone requires a FormType to be DISABLED before
    // DeleteFormType succeeds; the native CfnFormType resource has no
    // such delete hook, so cdk destroy fails with "should be disabled to
    // Delete" and leaves the stack in DELETE_FAILED. RETAIN (see
    // SMUSDomain's comment above) instead of a custom resource.
    formType.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // ── Custom Asset Type (no native CF resource — use Custom Resource) ──
    const assetTypeFn = new lambdaNodejs.NodejsFunction(this, "AssetTypeCrFn", {
      functionName: this.prefixed("asset-type-cr"),
      entry: path.join(__dirname, "../../lambdas/asset-type-handler.ts"),
      handler: "handler",
      runtime: lambda.Runtime.NODEJS_22_X,
      timeout: cdk.Duration.seconds(30),
    });

    assetTypeFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["datazone:CreateAssetType", "datazone:DeleteAssetType"],
        resources: ["*"],
      }),
    );

    // Register CR Lambda as a domain user
    const assetTypeCrUserProfile = new datazone.CfnUserProfile(
      this,
      "AssetTypeCrUserProfile",
      {
        domainIdentifier: domain.attrId,
        userIdentifier: assetTypeFn.role!.roleArn,
        userType: "IAM_ROLE",
      },
    );
    assetTypeCrUserProfile.node.addDependency(domain);
    assetTypeCrUserProfile.node.addDependency(assetTypeFn);
    assetTypeCrUserProfile.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // Add CR Lambda as a member of the system project
    const assetTypeCrMembership = new datazone.CfnProjectMembership(
      this,
      "AssetTypeCrMembership",
      {
        domainIdentifier: domain.attrId,
        projectIdentifier: systemProject.attrId,
        member: { userIdentifier: assetTypeFn.role!.roleArn },
        designation: "PROJECT_OWNER",
      },
    );
    assetTypeCrMembership.node.addDependency(assetTypeCrUserProfile);
    assetTypeCrMembership.node.addDependency(systemProject);
    assetTypeCrMembership.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // Grant CR Lambda ownership of root domain unit (required for CREATE_ASSET_TYPE grant)
    const assetTypeCrOwner = new datazone.CfnOwner(
      this,
      "AssetTypeCrDomainOwner",
      {
        domainIdentifier: domain.attrId,
        entityType: "DOMAIN_UNIT",
        entityIdentifier: domain.attrRootDomainUnitId,
        owner: {
          user: { userIdentifier: assetTypeFn.role!.roleArn },
        },
      },
    );
    assetTypeCrOwner.node.addDependency(assetTypeCrUserProfile);
    assetTypeCrOwner.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    const assetTypeProvider = new cr.Provider(this, "AssetTypeProvider", {
      onEventHandler: assetTypeFn,
    });

    const assetTypeCr = new cdk.CustomResource(this, "CoaRelationalTable", {
      serviceToken: assetTypeProvider.serviceToken,
      properties: {
        DomainId: domain.attrId,
        ProjectId: systemProject.attrId,
        Name: "CoaRelationalTable",
        Description: "Relational table asset (Glue or JDBC source)",
        FormName: "CoaTableMetadata",
        FormRevision: formType.attrRevision,
      },
    });
    assetTypeCr.node.addDependency(formType);
    assetTypeCr.node.addDependency(createAssetTypeGrant);
    assetTypeCr.node.addDependency(assetTypeCrMembership);
    assetTypeCr.node.addDependency(assetTypeCrOwner);

    // ── Shared Lambda code bundle ─────────────────────────────────
    const lambdaCode = bundlePython({
      srcDirs: [
        Paths.controlPlaneSrc,
        Paths.commonLib,
        Paths.smithyGeneratedControlPlanePythonServer,
      ],
      requirementsFile: fromRoot("packages/control-plane/requirements.txt"),
    });

    // ── Namespace API Lambda (combined create + list + delete-trigger) ──
    // Defined before the deletion pipeline so its role ARN can be passed in
    // (DeletePlatform assumes it for datazone:DeleteProject — see below and
    // cleanup.delete_smu_project). DELETION_STATE_MACHINE_ARN is a Lazy
    // token since the pipeline is constructed after this Lambda but this
    // Lambda's environment needs the pipeline's ARN; CDK resolves Lazy
    // values at synth time once every construct has been created, so the
    // circular-looking reference is safe.
    const namespaceApiFn: lambda.Function = new lambda.Function(
      this,
      "NamespaceApiFn",
      {
        functionName: this.prefixed("namespace-api"),
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "coa_control_plane.namespace.namespace_api_handler.handler",
        code: lambdaCode,
        timeout: cdk.Duration.seconds(15),
        memorySize: 256,
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [props.lambdaSecurityGroup],
        environment: {
          NAMESPACES_TABLE: namespaces.table.tableName,
          ROLES_TABLE: props.rolesTable.tableName,
          RESOURCE_ROLE_MAPPINGS_TABLE:
            props.resourceRoleMappingsTable.tableName,
          SOURCES_TABLE: this.prefixed(TABLE_NAMES.SOURCES),
          METRIC_IMPORT_JOBS_TABLE: this.prefixed(
            TABLE_NAMES.METRIC_IMPORT_JOBS,
          ),
          DATAZONE_DOMAIN_ID: domain.attrId,
          DATAZONE_PROJECT_PROFILE_ID: projectProfileId,
          ALLOWED_ORIGIN: allowedOrigin,
          PROJECT_ACCESS_ROLE_ARN_SSM: `${ssmPrefix}/smus/dz-project-access-role-arn`,
          ATHENA_RESULTS_BUCKET_SSM: `${ssmPrefix}/query/athena-results-bucket`,
          RESOURCE_PREFIX: this.prefixed(""),
          TAG_PREFIX: resolveContext(this.node).prefix,
          DELETION_STATE_MACHINE_ARN: cdk.Lazy.string({
            produce: (): string =>
              deletionPipeline.stateMachine.stateMachineArn,
          }),
          ...(props.ontologyEngineEndpoint && {
            ONTOLOGY_ENGINE_ENDPOINT: props.ontologyEngineEndpoint,
          }),
          // Lets get_handler resolve VKG service health from ECS
          // (resolve_vkg_health). Absent this var the resolver short-circuits
          // to UNKNOWN for every namespace.
          ...(props.vkgClusterArn && {
            VKG_CLUSTER_ARN: props.vkgClusterArn,
          }),
        },
      },
    );

    namespaces.table.grantReadWriteData(namespaceApiFn);
    props.rolesTable.grantReadWriteData(namespaceApiFn);
    props.resourceRoleMappingsTable.grantReadWriteData(namespaceApiFn);

    // Precondition checks for delete: read-only queries to detect active
    // operations that could race with the deletion pipeline's cleanup.
    // - SOURCES_TABLE ByNamespace GSI: sources actively scanning/enriching.
    // - METRIC_IMPORT_JOBS_TABLE: IN_PROGRESS import jobs (#215 — completed/
    //   failed jobs no longer block deletion).
    // Ontology induction jobs are checked via an HTTP call to the
    // ontology-engine (see delete_handler._check_active_induction_jobs) —
    // no DynamoDB grant needed for that check.
    namespaceApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:Query"],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${this.prefixed(TABLE_NAMES.SOURCES)}/index/ByNamespace`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${this.prefixed(TABLE_NAMES.METRIC_IMPORT_JOBS)}`,
        ],
      }),
    );

    namespaceApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["dynamodb:TransactWriteItems"],
        resources: [
          namespaces.table.tableArn,
          props.rolesTable.tableArn,
          props.resourceRoleMappingsTable.tableArn,
        ],
      }),
    );
    namespaceApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "datazone:CreateProject",
          "datazone:CreateProjectMembership",
          "datazone:DeleteProject",
        ],
        resources: ["*"], // V2 domains require wildcard — project doesn't exist yet to scope to
      }),
    );
    namespaceApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["iam:GetRole"],
        resources: [`arn:aws:iam::${this.account}:role/${this.prefixed("*")}`],
      }),
    );
    namespaceApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter${ssmPrefix}/smus/*`,
          `arn:aws:ssm:${this.region}:${this.account}:parameter${ssmPrefix}/query/*`,
        ],
      }),
    );

    // Athena workgroup creation (deletion moved to the deletion pipeline's
    // DeletePlatform step — see namespace-deletion-pipeline.ts).
    namespaceApiFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "athena:CreateWorkGroup",
          "athena:GetWorkGroup",
          "athena:TagResource",
        ],
        resources: [
          `arn:aws:athena:${this.region}:${this.account}:workgroup/${this.prefixed("*")}`,
        ],
      }),
    );

    // Read-time VKG health resolution (GetNamespace -> resolve_vkg_health)
    // calls ecs:DescribeServices, then ecs:ListTasks + ecs:DescribeTasks to
    // read the container's functional-probe healthStatus (a running task is not
    // necessarily able to translate — #170). All scoped to the VKG cluster via
    // the ecs:cluster condition, mirroring the deletion pipeline grant.
    if (props.vkgClusterArn) {
      namespaceApiFn.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "ecs:DescribeServices",
            "ecs:ListTasks",
            "ecs:DescribeTasks",
          ],
          resources: ["*"],
          conditions: { ArnLike: { "ecs:cluster": props.vkgClusterArn } },
        }),
      );
    }

    this.namespaceApiFn = namespaceApiFn;

    // ── Namespace deletion Step Functions pipeline ──────────────────
    // sources-api / metric-api function names are constructed by naming
    // convention (this.prefixed(...)), not a CDK cross-stack reference,
    // because SourcesStack/MetricServiceStack deploy *after* NamespaceStack
    // (see bin/app.ts) — the same pattern OntologyStack already uses to
    // invoke sources-api (see ontology-stack.ts).
    // GraphRAG doc-KG index teardown needs the AOSS collection. Both params are
    // written by the storage stack; the read requires an explicit
    // addDependency() in bin/app.ts because CDK cannot infer ordering from
    // valueForStringParameter.
    const opensearchEndpoint = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/opensearch/endpoint`,
    );
    const opensearchCollectionName =
      ssm.StringParameter.valueForStringParameter(
        this,
        `${ssmPrefix}/opensearch/collection-name`,
      );

    const deletionPipeline: NamespaceDeletionPipeline =
      new NamespaceDeletionPipeline(this, "DeletionPipeline", {
        prefixed: (name: string) => this.prefixed(name),
        lambdaCode,
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [props.lambdaSecurityGroup],
        namespacesTable: namespaces.table,
        resourceRoleMappingsTable: props.resourceRoleMappingsTable,
        rolesTable: props.rolesTable,
        sourcesApiFnName: this.prefixed("sources-api"),
        metricApiFnName: this.prefixed("metric-api"),
        datazoneDomainId: domain.attrId,
        // DeletePlatform assumes namespaceApiFn's own execution role for
        // datazone:DeleteProject — DataZone records the CreateProject
        // caller as the project's owner, and namespaceApiFn is the role
        // that actually calls CreateProject (see service.py's
        // NamespaceService.create -> SMUSClient.create_project). Assuming
        // DzProjectAccessRole instead would require also granting it
        // PROJECT_OWNER membership, which is a broader, separate change —
        // this reuses the ownership DataZone already grants for free.
        namespaceApiFnRoleArn: namespaceApiFn.role!.roleArn,
        vkgClusterArn: props.vkgClusterArn,
        cloudMapNamespaceId: props.cloudMapNamespaceId,
        ontologyBucketName: props.ontologyBucketName,
        ontologyEngineEndpoint: props.ontologyEngineEndpoint,
        // For dropping the namespace's GraphRAG doc-KG indexes on teardown.
        // Read from SSM (the storage stack writes both) — see bin/app.ts for
        // the explicit addDependency this read requires, since CDK cannot infer
        // ordering from valueForStringParameter.
        opensearchEndpoint,
        opensearchCollectionName,
        resourcePrefix: this.prefixed(""),
      });

    // Start the deletion pipeline's Step Functions execution.
    deletionPipeline.stateMachine.grantStartExecution(namespaceApiFn);

    // Allow DeletePlatform to assume this Lambda's own execution role.
    // grantAssumeRole only adds an identity policy on the *grantee* — it
    // does NOT modify the target role's trust policy.  We need both sides:
    //  1. Identity policy on DeletePlatformFn (already in namespace-deletion-pipeline.ts)
    //  2. Trust policy on namespaceApiFn's role (added here)
    (namespaceApiFn.role! as iam.Role).assumeRolePolicy!.addStatements(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        principals: [
          new iam.ArnPrincipal(deletionPipeline.deletePlatformFn.role!.roleArn),
        ],
      }),
    );

    // ── Roles API Lambda (list platform roles + list/get namespace roles) ──
    const rolesApiFn = new lambda.Function(this, "RolesApiFn", {
      functionName: this.prefixed("roles-api"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "coa_control_plane.roles.namespace_roles_handler.handler",
      code: lambdaCode,
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      environment: {
        ROLES_TABLE: props.rolesTable.tableName,
        ALLOWED_ORIGIN: allowedOrigin,
      },
    });
    props.rolesTable.grantReadData(rolesApiFn);

    const platformRolesApiFn = new lambda.Function(this, "PlatformRolesApiFn", {
      functionName: this.prefixed("platform-roles-api"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "coa_control_plane.roles.list_platform_roles_handler.handler",
      code: lambdaCode,
      timeout: cdk.Duration.seconds(5),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      environment: {
        ROLES_TABLE: props.rolesTable.tableName,
        ALLOWED_ORIGIN: allowedOrigin,
      },
    });
    props.rolesTable.grantReadData(platformRolesApiFn);

    this.rolesApiFn = rolesApiFn;
    this.platformRolesApiFn = platformRolesApiFn;

    // ── Grants API Lambda (CRUD for grants) ──────────────────────────
    const grantsApiFn = new lambda.Function(this, "GrantsApiFn", {
      functionName: this.prefixed("grants-api"),
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "coa_control_plane.grants.grants_handler.handler",
      code: lambdaCode,
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      environment: {
        ROLES_TABLE: props.rolesTable.tableName,
        RESOURCE_ROLE_MAPPINGS_TABLE: props.resourceRoleMappingsTable.tableName,
        ALLOWED_ORIGIN: allowedOrigin,
      },
    });
    props.rolesTable.grantReadData(grantsApiFn);
    props.resourceRoleMappingsTable.grantReadWriteData(grantsApiFn);

    this.grantsApiFn = grantsApiFn;

    // ── Register Lambda role as a domain user profile ──────────
    const lambdaUserProfile = new datazone.CfnUserProfile(
      this,
      "LambdaUserProfile",
      {
        domainIdentifier: domain.attrId,
        userIdentifier: namespaceApiFn.role!.roleArn,
        userType: "IAM_ROLE",
      },
    );
    lambdaUserProfile.node.addDependency(domain);
    lambdaUserProfile.node.addDependency(namespaceApiFn);
    lambdaUserProfile.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // ── Grant Lambda role ownership of root domain unit ─────────
    const lambdaOwner = new datazone.CfnOwner(this, "LambdaDomainOwner", {
      domainIdentifier: domain.attrId,
      entityType: "DOMAIN_UNIT",
      entityIdentifier: domain.attrRootDomainUnitId,
      owner: {
        user: { userIdentifier: namespaceApiFn.role!.roleArn },
      },
    });
    lambdaOwner.node.addDependency(lambdaUserProfile);
    lambdaOwner.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // ── Admin Login role (assume via console to access SMUS UI) ─
    // Trust only the specific role(s) that human admins federate into — NOT
    // the whole account root (AccountRootPrincipal), which would let any
    // in-account principal holding sts:AssumeRole escalate to this admin role.
    // Defaults to the account's `Admin` role; override per account with the
    // comma-separated `smus_admin_principal_arns` context key (deploy.sh
    // checks the default actually exists before deploying and fails fast
    // with a clear message otherwise — see the SCL_SMUS_ADMIN_ARNS preflight).
    const defaultAdminArn = `arn:aws:iam::${this.account}:role/Admin`;
    const parsedAdminArns = (
      (this.node.tryGetContext("smus_admin_principal_arns") as
        | string
        | undefined) ?? ""
    )
      .split(",")
      .map((arn) => arn.trim())
      .filter((arn) => arn.length > 0);
    // Validate caller-supplied ARNs: must be well-formed IAM role/user ARNs
    // (any partition). Rejects wildcards / bare "*" / malformed strings that
    // would otherwise create an over-broad or invalid trust policy. The default
    // ARN below is not validated (its account is a CDK token, not a literal).
    const iamArnRegex =
      /^arn:aws[a-z-]*:iam::\d{12}:(role|user)\/[\w+=,.@/-]+$/;
    const invalidArns = parsedAdminArns.filter((arn) => !iamArnRegex.test(arn));
    if (invalidArns.length > 0) {
      throw new Error(
        `smus_admin_principal_arns contains invalid IAM role/user ARNs: ${invalidArns.join(", ")}`,
      );
    }
    // Fall back to the default Admin role when the context is unset, empty, or
    // whitespace/commas only — never an empty principal set (which would crash
    // CompositePrincipal at synth).
    const adminPrincipalArns =
      parsedAdminArns.length > 0 ? parsedAdminArns : [defaultAdminArn];

    const loginRole = new iam.Role(this, "DomainLoginRole", {
      roleName: this.prefixed("smus-login"),
      assumedBy: new iam.CompositePrincipal(
        ...adminPrincipalArns.map((arn) => new iam.ArnPrincipal(arn)),
      ),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "SageMakerStudioAdminIAMConsolePolicy",
        ),
      ],
      inlinePolicies: {
        PassExecutionRole: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              sid: "IAMPassRoleStatement",
              actions: ["iam:PassRole"],
              resources: [executionRole.roleArn],
              conditions: {
                StringEquals: {
                  "iam:PassedToService": "datazone.amazonaws.com",
                },
              },
            }),
          ],
        }),
      },
    });

    // Optional MFA enforcement on assume-role
    const requireMfa = this.node.tryGetContext("smus_require_mfa") === "true";
    if (requireMfa) {
      loginRole.assumeRolePolicy!.addStatements(
        new iam.PolicyStatement({
          sid: "DenyAssumeWithoutMFA",
          effect: iam.Effect.DENY,
          principals: [new iam.AnyPrincipal()],
          actions: ["sts:AssumeRole"],
          conditions: {
            BoolIfExists: { "aws:MultiFactorAuthPresent": "false" },
          },
        }),
      );
    }

    // ── Publish domain ID to SSM ─────────────────────────────────
    new ssm.StringParameter(this, "SsmDomainId", {
      parameterName: `${ssmPrefix}/smus/domain-id`,
      stringValue: domain.attrId,
      description: "SageMaker Unified Studio domain ID",
    });

    new ssm.StringParameter(this, "SsmNamespacesTableName", {
      parameterName: `${ssmPrefix}/namespace/namespaces-table-name`,
      stringValue: this.namespacesTable.tableName,
      description: "Namespaces DynamoDB table name",
    });

    new ssm.StringParameter(this, "SsmLoginRoleArn", {
      parameterName: `${ssmPrefix}/smus/login-role-arn`,
      stringValue: loginRole.roleArn,
      description: "SMUS admin login role ARN",
    });

    new ssm.StringParameter(this, "SsmNamespaceApiFnArn", {
      parameterName: `${ssmPrefix}/namespace/api-fn-arn`,
      stringValue: namespaceApiFn.functionArn,
      description: "Namespace API Lambda ARN",
    });

    new ssm.StringParameter(this, "SsmRolesApiFnArn", {
      parameterName: `${ssmPrefix}/namespace/roles-api-fn-arn`,
      stringValue: rolesApiFn.functionArn,
      description: "Roles API Lambda ARN",
    });

    new ssm.StringParameter(this, "SsmPlatformRolesApiFnArn", {
      parameterName: `${ssmPrefix}/namespace/platform-roles-api-fn-arn`,
      stringValue: platformRolesApiFn.functionArn,
      description: "Platform Roles API Lambda ARN",
    });

    new ssm.StringParameter(this, "SsmGrantsApiFnArn", {
      parameterName: `${ssmPrefix}/namespace/grants-api-fn-arn`,
      stringValue: grantsApiFn.functionArn,
      description: "Grants API Lambda ARN",
    });

    new cdk.CfnOutput(this, "DomainId", { value: domain.attrId });
    new cdk.CfnOutput(this, "DomainArn", { value: domain.attrArn });
    new cdk.CfnOutput(this, "LoginRoleArn", { value: loginRole.roleArn });
  }
}
