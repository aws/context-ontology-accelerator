// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as neptune from "aws-cdk-lib/aws-neptune";
import * as opensearchserverless from "aws-cdk-lib/aws-opensearchserverless";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

import { SCLStack } from "../../constructs/scl-stack";
import { AccessLogsBucket } from "../../constructs";
import { VECTOR_COLLECTION_SUFFIX } from "../../constants";
import { resolveContext } from "../../context";
import { NetworkStack } from "./network-stack";

/**
 * Foundation stack: Neptune graph database and OpenSearch Serverless
 * vector store, shared by all SemanticContext services.
 *
 * Neptune uses L1 (Cfn) constructs with IAM authentication enabled.
 * OpenSearch uses Serverless (VECTORSEARCH collection type) for
 * auto-scaling and pay-per-use in dev.
 */

export interface StorageStackProps extends cdk.StackProps {
  /** The NetworkStack that provides VPC, subnets, and security groups. */
  network: NetworkStack;

  /**
   * Browser origin allowed to PUT directly to the ontology-artifacts bucket
   * via presigned URLs (custom-ontology upload). Defaults to "*" in non-prod.
   * Without a CORS rule the browser blocks the presigned PUT with a
   * NetworkError, even though server-side (curl) uploads succeed.
   */
  allowedOrigin?: string;
}

export class StorageStack extends SCLStack {
  /** Neptune cluster writer endpoint (host reachable from VPC). */
  public readonly neptuneClusterEndpoint: string;

  /** Neptune Gremlin/SPARQL port. */
  public readonly neptunePort: number;

  /** Neptune cluster resource ARN (for IAM policies). */
  public readonly neptuneClusterArn: string;

  /** OpenSearch Serverless collection endpoint. */
  public readonly opensearchEndpoint: string;

  /** OpenSearch Serverless collection name. */
  public readonly opensearchCollectionName: string;

  /** OpenSearch Serverless collection ARN (for IAM policies). */
  public readonly opensearchCollectionArn: string;

  /** S3 bucket for Athena query results. */
  public readonly athenaResultsBucket: s3.Bucket;

  /** S3 bucket for Athena federated connector spill data. */
  public readonly athenaSpillBucket: s3.Bucket;

  /** S3 bucket for ontology artifacts (induced ontologies, R2RML mappings, proposal Turtle). */
  public readonly ontologyArtifactsBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);
    this.addComponentTag("foundation");

    const { network } = props;

    // ── Resolve private subnets ──────────────────────────────────────
    const privateSubnets = network.vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    });

    // =====================================================================
    //  Neptune Cluster  (L1 - CfnDBSubnetGroup + CfnDBCluster + CfnDBInstance)
    //
    //  IAM auth enabled - callers must use SigV4 signing.
    //  Automated backups: 7-day retention. Increase to 35 days for prod
    //  if longer point-in-time recovery is needed.
    // =====================================================================

    // TODO: Consider adding custom resource to verify Neptune endpoint is connectable
    // post-deploy for prod reliability.
    const neptuneSubnetGroup = new neptune.CfnDBSubnetGroup(
      this,
      "NeptuneSubnetGroup",
      {
        dbSubnetGroupName: this.prefixed("neptune-subnet-group"),
        dbSubnetGroupDescription:
          "Private subnets for the SemanticContext Neptune cluster",
        subnetIds: privateSubnets.subnetIds,
      },
    );

    const neptuneCluster = new neptune.CfnDBCluster(this, "NeptuneCluster", {
      dbClusterIdentifier: this.prefixed("neptune"),
      engineVersion: "1.4.7.0",
      dbSubnetGroupName: neptuneSubnetGroup.dbSubnetGroupName!,
      vpcSecurityGroupIds: [network.neptuneSecurityGroup.securityGroupId],
      iamAuthEnabled: true, // SigV4 auth - recommended for multi-component access
      storageEncrypted: true,
      deletionProtection: this.envName === "prod",
      backupRetentionPeriod: 7,
    });
    neptuneCluster.addDependency(neptuneSubnetGroup);
    neptuneCluster.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    const neptuneInstance = new neptune.CfnDBInstance(
      this,
      "NeptunePrimaryInstance",
      {
        dbInstanceIdentifier: this.prefixed("neptune-primary"),
        dbInstanceClass: "db.r8g.large",
        dbClusterIdentifier: neptuneCluster.dbClusterIdentifier!,
      },
    );
    neptuneInstance.addDependency(neptuneCluster);
    neptuneInstance.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);

    this.neptunePort = 8182;
    this.neptuneClusterEndpoint = neptuneCluster.attrEndpoint;

    // Neptune IAM data-access ARN uses the cluster *resource* ID
    // (e.g. cluster-ABCD1234...), NOT the cluster identifier.
    // Format: arn:aws:neptune-db:{region}:{account}:{cluster-resource-id}/*
    this.neptuneClusterArn = this.formatArn({
      service: "neptune-db",
      resource: neptuneCluster.attrClusterResourceId,
      resourceName: "*",
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    });

    // =====================================================================
    //  OpenSearch Serverless  (VECTORSEARCH collection)
    //
    //  AOSS requires separate encryption, network, and data-access policies
    //  (not standard IAM resource policies).
    // =====================================================================
    // OpenSearch Serverless — NEXTGEN Collection Group
    //
    // Uses NEXTGEN generation for improved indexing performance, lower latency,
    // and support for larger vector dimensions. The Collection Group provides:
    // - NEXTGEN generation: 2x indexing throughput, sub-100ms p99 search latency
    // - Standby replicas: must be ENABLED for NEXTGEN (AWS rejects DISABLED).
    //   NEXTGEN strict mode rejects knn_vector method blocks — catches regressions.
    // - Capacity limits: min 2 OCU (default, override via `aoss_min_ocu`; set 0
    //   for NextGen scale-to-zero), max 96 OCU (cost cap)
    //
    // BREAKING CHANGE: Collection renamed to {prefix}-vector-store.
    // This creates a new collection — existing vector embeddings must be re-ingested
    // via the Scan pipeline. Vector data is derived (not source-of-truth), so no
    // data migration is needed — re-run ingestion against existing source documents.
    // =====================================================================

    const collectionName = this.prefixed(VECTOR_COLLECTION_SUFFIX);

    // Encryption policy - required before collection creation
    const encryptionPolicy = new opensearchserverless.CfnSecurityPolicy(
      this,
      "OSSEncryptionPolicy",
      {
        name: this.prefixed("encryption"),
        type: "encryption",
        policy: JSON.stringify({
          Rules: [
            {
              ResourceType: "collection",
              Resource: [`collection/${collectionName}`],
            },
          ],
          AWSOwnedKey: true,
        }),
      },
    );

    // Network policy — VPC-only access via standard EC2 Interface Endpoint (NextGen)
    const networkRules = [
      {
        Rules: [
          {
            ResourceType: "collection",
            Resource: [`collection/${collectionName}`],
          },
          {
            ResourceType: "dashboard",
            Resource: [`collection/${collectionName}`],
          },
        ],
        SourceVPCEs: [network.aossVpcEndpointId],
      },
    ];

    // Dev only: allow public access for integration tests run from developer laptops and CICD pipeline.
    // All other environments (tst, demo, prod) are VPC-only.
    if (this.envName === "dev") {
      networkRules.push({
        Rules: [
          {
            ResourceType: "collection",
            Resource: [`collection/${collectionName}`],
          },
        ],
        AllowFromPublic: true,
      } as any);
    }

    const networkPolicy = new opensearchserverless.CfnSecurityPolicy(
      this,
      "OSSNetworkPolicy",
      {
        name: this.prefixed("network"),
        type: "network",
        policy: JSON.stringify(networkRules),
      },
    );

    const maxOcu = Number(this.node.tryGetContext("aoss_max_ocu") ?? 96);
    // NEXTGEN valid min OCU values: 0, 2, 4, 8, 16, or multiples of 16
    const minOcu = Number(this.node.tryGetContext("aoss_min_ocu") ?? 2);

    // NEXTGEN collection groups require standbyReplicas=ENABLED.
    // AWS rejects DISABLED for NEXTGEN generation (replicas managed internally).
    const cfnCollectionGroup = new opensearchserverless.CfnCollectionGroup(
      this,
      "VectorStoreGroup",
      {
        name: this.prefixed("vector-store-group"),
        standbyReplicas: "ENABLED",
        generation: "NEXTGEN",
        capacityLimits: {
          maxIndexingCapacityInOcu: maxOcu,
          maxSearchCapacityInOcu: maxOcu,
          minIndexingCapacityInOcu: minOcu,
          minSearchCapacityInOcu: minOcu,
        },
        description:
          "Group for SemanticContext vector store for embeddings and similarity search",
      },
    );
    // TODO: Consider adding custom resource with SDK waiter that polls collection
    // status until ACTIVE for prod deployments
    const collection = new opensearchserverless.CfnCollection(
      this,
      "VectorStore",
      {
        name: collectionName,
        collectionGroupName: cfnCollectionGroup.name,
        type: "VECTORSEARCH",
        description:
          "SemanticContext vector store for embeddings and similarity search",
      },
    );
    collection.addDependency(cfnCollectionGroup);
    collection.addDependency(encryptionPolicy);
    collection.addDependency(networkPolicy);

    this.opensearchEndpoint = collection.attrCollectionEndpoint;
    this.opensearchCollectionName = collectionName;
    this.opensearchCollectionArn = collection.attrArn;

    // =====================================================================
    //  Athena Query Buckets
    // =====================================================================

    const { ssmPrefix } = resolveContext(this.node);

    // Shared server-access-log target for this stack's buckets.
    const accessLogs = new AccessLogsBucket(this, "AccessLogs", {
      nameSuffix: "storage-logs",
    });

    this.athenaResultsBucket = new s3.Bucket(this, "AthenaResultsBucket", {
      bucketName: this.prefixed(`athena-results-${this.account}`),
      encryption: s3.BucketEncryption.S3_MANAGED,
      serverAccessLogsBucket: accessLogs.bucket,
      serverAccessLogsPrefix: "athena-results/",
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      lifecycleRules: [
        {
          id: "expire-results",
          expiration: cdk.Duration.days(7),
          enabled: true,
        },
      ],
      removalPolicy:
        this.envName === "prod"
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: this.envName !== "prod",
    });

    this.athenaSpillBucket = new s3.Bucket(this, "AthenaSpillBucket", {
      bucketName: this.prefixed(`athena-spill-${this.account}`),
      encryption: s3.BucketEncryption.S3_MANAGED,
      serverAccessLogsBucket: accessLogs.bucket,
      serverAccessLogsPrefix: "athena-spill/",
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      lifecycleRules: [
        { id: "expire-spill", expiration: cdk.Duration.days(1), enabled: true },
      ],
      removalPolicy:
        this.envName === "prod"
          ? cdk.RemovalPolicy.RETAIN
          : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: this.envName !== "prod",
    });

    // Ontology artifacts bucket — accepted proposals (Turtle + R2RML), shared
    // by the ontology-engine (writer) and VKG/Serve stacks (readers). Bucket
    // name pattern: ${prefix}-${env}-ontology-artifacts-${account} matches the
    // hardcoded fallback in proposals.py / dynamo_store.py.
    this.ontologyArtifactsBucket = new s3.Bucket(
      this,
      "OntologyArtifactsBucket",
      {
        bucketName: this.prefixed(`ontology-artifacts-${this.account}`),
        encryption: s3.BucketEncryption.S3_MANAGED,
        serverAccessLogsBucket: accessLogs.bucket,
        serverAccessLogsPrefix: "ontology-artifacts/",
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        enforceSSL: true,
        versioned: true,
        // Allow the web app to talk to S3 directly via presigned URLs:
        //  - PUT: upload custom-ontology files (and, later, large proposal edits)
        //  - GET/HEAD: download large proposal/ontology Turtle that is too big
        //    to inline through the 6 MB API Gateway / Lambda response limit.
        // Without the matching method in allowedMethods, S3 omits the
        // Access-Control-Allow-Origin header and the browser fetch fails.
        cors: [
          {
            allowedOrigins: [props.allowedOrigin ?? "*"],
            allowedMethods: [
              s3.HttpMethods.GET,
              s3.HttpMethods.HEAD,
              s3.HttpMethods.PUT,
            ],
            allowedHeaders: ["Content-Type"],
            maxAge: 3600,
          },
        ],
        removalPolicy:
          this.envName === "prod"
            ? cdk.RemovalPolicy.RETAIN
            : cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: this.envName !== "prod",
      },
    );

    new ssm.StringParameter(this, "SsmAthenaResultsBucket", {
      parameterName: `${ssmPrefix}/query/athena-results-bucket`,
      stringValue: this.athenaResultsBucket.bucketName,
      description: "Athena query results S3 bucket name",
    });

    new ssm.StringParameter(this, "SsmAthenaSpillBucket", {
      parameterName: `${ssmPrefix}/query/athena-spill-bucket`,
      stringValue: this.athenaSpillBucket.bucketName,
      description: "Athena federated connector spill S3 bucket name",
    });

    new ssm.StringParameter(this, "SsmOpensearchEndpoint", {
      parameterName: `${ssmPrefix}/opensearch/endpoint`,
      stringValue: this.opensearchEndpoint,
      description: "OpenSearch Serverless collection endpoint",
    });

    new ssm.StringParameter(this, "SsmOpensearchCollectionName", {
      parameterName: `${ssmPrefix}/opensearch/collection-name`,
      stringValue: this.opensearchCollectionName,
      description: "OpenSearch Serverless collection name",
    });

    new ssm.StringParameter(this, "SsmOpensearchCollectionArn", {
      parameterName: `${ssmPrefix}/opensearch/collection-arn`,
      stringValue: this.opensearchCollectionArn,
      description: "OpenSearch Serverless collection ARN",
    });

    // ── CfnOutputs ──────────────────────────────────────────────────
    new cdk.CfnOutput(this, "NeptuneClusterEndpointOutput", {
      value: this.neptuneClusterEndpoint,
      description: "Neptune cluster writer endpoint",
    });

    new cdk.CfnOutput(this, "NeptunePortOutput", {
      value: String(this.neptunePort),
      description: "Neptune port",
    });

    new cdk.CfnOutput(this, "OpenSearchEndpointOutput", {
      value: this.opensearchEndpoint,
      description: "OpenSearch Serverless collection endpoint",
    });

    new cdk.CfnOutput(this, "OpenSearchCollectionNameOutput", {
      value: this.opensearchCollectionName,
      description: "OpenSearch Serverless collection name",
    });

    new cdk.CfnOutput(this, "AthenaResultsBucketOutput", {
      value: this.athenaResultsBucket.bucketName,
      description: "Athena query results bucket",
    });

    new cdk.CfnOutput(this, "AthenaSpillBucketOutput", {
      value: this.athenaSpillBucket.bucketName,
      description: "Athena federated connector spill bucket",
    });
  }
}
