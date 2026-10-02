// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// The strings COA's IAM policies match on; changing any one breaks a grant. Stated here rather than
// imported so a copied-out connector still builds, and nothing verifies they match the policies COA
// enforces. A missing CONNECTOR_TAG_KEY denies the first scan, while the spill three fail only once
// a response exceeds Athena's 6 MB limit, where a connector that cannot write spill has been
// observed returning SUCCEEDED with zero rows rather than an error.

/** Tag COA's invoke policy matches on. Without it the function cannot be invoked at all. */
export const CONNECTOR_TAG_KEY = "coa:connector";

/** Tag COA's key policy matches on, on the customer-managed spill key. */
export const CONNECTOR_SPILL_KMS_TAG_KEY = "coa:connector-spill";

/** Value both tags carry. */
export const CONNECTOR_TAG_VALUE = "true";

/** Key shape COA's spill-read policy matches. Only the shape; the id segment is yours. */
export const CONNECTOR_SPILL_KEY_GLOB = "connectors/*/spills/*";

/**
 * The SSM paths in COA's account that publish the two role ARNs. Whoever deploys a connector reads them
 * with `aws ssm get-parameter` and passes the values in; the connector's own stack cannot read them,
 * because it usually runs in another account and SSM parameters are not readable across accounts.
 *
 * Both paths carry the COA environment name, so a second COA environment in the same account and prefix
 * cannot overwrite these values. A script still reading the older `/{prefix}/serve/runtime-role-arn`
 * finds nothing.
 */
export const COA_ROLE_SSM_PARAMS = {
  /** Runs queries. `/{prefix}/{envName}/serve/runtime-role-arn`. */
  serve: "/{prefix}/{envName}/serve/runtime-role-arn",
  /** Runs `DESCRIBE` during a scan. `/{prefix}/{envName}/sources/db-connector-role-arn`. */
  discovery: "/{prefix}/{envName}/sources/db-connector-role-arn",
} as const;

/**
 * CloudWatch namespace a connector's own metrics land in, written as Embedded Metric Format on
 * stdout. Must equal `ConnectorMetrics.NAMESPACE` on the Java side — one emits, the other alarms on
 * it, and a disagreement produces an alarm stuck in `INSUFFICIENT_DATA` rather than an error.
 */
export const CONNECTOR_METRIC_NAMESPACE = "COA/Connectors";

/** Dimension carrying the connector's id. Present on every metric. */
export const CONNECTOR_DIMENSION = "Connector";

/**
 * Dimension carrying the Athena catalog a request arrived under. Absent from metrics emitted below
 * the request, so an alarm that needs to fire for those must not name it.
 */
export const CATALOG_DIMENSION = "Catalog";

/**
 * The metrics a connector emits. Mirrors the constants on `ConnectorMetrics` in the Java toolkit;
 * the two are the same contract expressed twice, because nothing can share a constant across the
 * jar and the CDK app.
 */
export const ConnectorMetricName = {
  /** Configuration could not be resolved for the catalog a request arrived under. */
  configResolutionFailures: "ConnectorConfigResolutionFailures",
  /**
   * The configuration store throttled the connector. Distinct from a resolution failure: the
   * configuration is fine and the first action is a rate limit rather than a fix.
   */
  configThrottles: "ConnectorConfigThrottles",
  /**
   * `sts:AssumeRole` on the customer-owned role guarding a credential failed. Distinct again,
   * because the cause is a policy COA neither owns nor can repair.
   */
  credentialAssumeFailures: "ConnectorCredentialAssumeFailures",
  /** A connection to the upstream data source could not be opened. */
  warehouseConnectFailures: "ConnectorWarehouseConnectFailures",
  /** Rows returned by one table read. */
  rowsReturned: "ConnectorRowsReturned",
  /** A read was refused for exceeding the connector's row ceiling. */
  tableCeilingExceeded: "ConnectorTableCeilingExceeded",
} as const;
