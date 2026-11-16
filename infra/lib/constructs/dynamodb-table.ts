// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { Construct } from "constructs";
import { prefixed, resolveContext } from "../context";

/** Props for the DynamoDBTable construct. */
export interface DynamoDBTableProps {
  /**
   * Logical table name — will be prefixed automatically via `prefixed()`.
   * Example: `"namespaces"` → `"coa-dev-namespaces"`
   */
  tableName: string;

  /** Partition key definition. */
  partitionKey: dynamodb.Attribute;

  /** Optional sort key definition. */
  sortKey?: dynamodb.Attribute;

  /** Optional DynamoDB stream view type. Defaults to no stream. */
  stream?: dynamodb.StreamViewType;

  /** Optional TTL attribute name. Enables automatic item expiration. */
  timeToLiveAttribute?: string;

  /** Override removal policy. Defaults to RETAIN in prod, DESTROY elsewhere. */
  removalPolicy?: cdk.RemovalPolicy;
}

/**
 * Reusable DynamoDB table construct with secure defaults.
 *
 * Enforces:
 *   - PAY_PER_REQUEST billing
 *   - AWS-managed encryption at rest
 *   - Point-in-time recovery enabled
 *   - RETAIN removal policy in prod, DESTROY elsewhere (overridable)
 */
export class DynamoDBTable extends Construct {
  public readonly table: dynamodb.Table;

  constructor(scope: Construct, id: string, props: DynamoDBTableProps) {
    super(scope, id);

    const { envName } = resolveContext(this.node);
    const defaultRemoval =
      envName === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;

    this.table = new dynamodb.Table(this, "Table", {
      tableName: prefixed(this.node, props.tableName),
      partitionKey: props.partitionKey,
      sortKey: props.sortKey,
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: props.removalPolicy ?? defaultRemoval,
      stream: props.stream,
      timeToLiveAttribute: props.timeToLiveAttribute,
    });
  }

  /** Delegate addGlobalSecondaryIndex to the underlying table. */
  addGlobalSecondaryIndex(props: dynamodb.GlobalSecondaryIndexProps): void {
    this.table.addGlobalSecondaryIndex(props);
  }
}
