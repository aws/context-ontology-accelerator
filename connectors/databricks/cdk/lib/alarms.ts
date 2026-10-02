// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import {
  AthenaFederationConnector,
  ConnectorMetricName,
  optionalIntEnv,
} from "coa-connector-cdk";
import { DEFAULT_MAX_ROWS_PER_TABLE } from "./constants";

/**
 * The two metrics only a `coa-managed` deployment can emit.
 *
 * Named from the toolkit's copy of the metric contract rather than restated: the jar emits and this
 * alarms, and a disagreement leaves the alarm in `INSUFFICIENT_DATA` for ever rather than failing.
 */
export const MANAGED_METRIC_NAMES = {
  /** Parameter Store throttled a configuration read. Distinct from a missing parameter. */
  configThrottles: ConnectorMetricName.configThrottles,
  /** `sts:AssumeRole` on the customer's credential-access role failed. */
  credentialAssumeFailures: ConnectorMetricName.credentialAssumeFailures,
} as const;

const PERIOD = cdk.Duration.minutes(5);

/** Every alarm here is on a *caught* failure, so the invocation succeeds and breaches nothing else. */
const COUNT_BREACH = {
  threshold: 0,
  comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
  evaluationPeriods: 1,
  treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
} as const;

/**
 * Alarms on the four metrics this connector emits itself, which the three Lambda alarms in the
 * construct cannot see: each of these failures is caught, reported and counted, so the invocation
 * **succeeds** and shows up in neither the error rate nor the duration.
 *
 * @param configFailureDescription what an unresolvable configuration means in this mode, which is
 *                                 the one alarm whose cause differs between the two.
 */
export function addConnectorAlarms(
  connector: AthenaFederationConnector,
  configFailureDescription: string,
): void {
  // Fleet-wide rather than per catalog, like every alarm below. Naming a catalog would mean an alarm
  // that silently stops matching the day a second source is registered against this connector.
  connector.addAlarm("ConfigResolutionFailuresAlarm", {
    alarmName: `${connector.functionName}-config-resolution-failures`,
    alarmDescription: configFailureDescription,
    metric: connector.connectorMetric(ConnectorMetricName.configResolutionFailures, {
      period: cdk.Duration.minutes(15),
      statistic: "Sum",
    }),
    ...COUNT_BREACH,
  });

  // Five, not one: a warehouse that has scaled to zero refuses the first connections of a resume,
  // and a threshold of one would page on every idle period ending.
  connector.addAlarm("WarehouseConnectFailuresAlarm", {
    alarmName: `${connector.functionName}-warehouse-connect-failures`,
    alarmDescription:
      "The connector cannot reach the SQL Warehouse. Distinguish a stopped warehouse (recovers on " +
      "resume) from a rejected credential (DATABRICKS_AUTHENTICATION_FAILED in the log, so the " +
      "secret has expired or rotated) from a network fault.",
    metric: connector.connectorMetric(ConnectorMetricName.warehouseConnectFailures, {
      period: PERIOD,
      statistic: "Sum",
    }),
    ...COUNT_BREACH,
    threshold: 5,
  });

  // Any breach at all. The query failed and the user saw it; this alarm says which table, so the
  // answer is a filter or an exclusion rather than a raised ceiling.
  connector.addAlarm("TableCeilingExceededAlarm", {
    alarmName: `${connector.functionName}-table-ceiling-exceeded`,
    alarmDescription:
      "A query was refused for exceeding DATABRICKS_MAX_ROWS_PER_TABLE. The connector's log names " +
      "the table. Narrow the predicate or set tableExcludeFilter; raising the ceiling means raising " +
      "memory and timeout with it.",
    metric: connector.connectorMetric(ConnectorMetricName.tableCeilingExceeded, {
      period: PERIOD,
      statistic: "Sum",
    }),
    ...COUNT_BREACH,
  });

  // The leading indicator, and the only one here that fires before anything has failed: p95
  // approaching the ceiling means the next slightly-wider question breaches it. Two periods, because
  // one aggregate query against a large table is a fact about that question rather than a trend.
  const ceiling = optionalIntEnv("DATABRICKS_MAX_ROWS_PER_TABLE") ?? DEFAULT_MAX_ROWS_PER_TABLE;
  connector.addAlarm("RowsReturnedAlarm", {
    alarmName: `${connector.functionName}-rows-returned-p95`,
    alarmDescription:
      `p95 rows per read is within 20% of the ${ceiling}-row ceiling. Either push-down has ` +
      "regressed or the workload has turned aggregate-heavy; the two look identical from Athena's " +
      "side.",
    metric: connector.connectorMetric(ConnectorMetricName.rowsReturned, {
      period: PERIOD,
      statistic: "p95",
    }),
    ...COUNT_BREACH,
    threshold: Math.round(ceiling * 0.8),
    evaluationPeriods: 2,
  });
}

/**
 * The two alarms only a `coa-managed` deployment can breach, because only that mode resolves
 * configuration and credentials per request. Both would sit in `INSUFFICIENT_DATA` for ever in
 * `environment` mode.
 */
export function addManagedAlarms(connector: AthenaFederationConnector): void {
  // Parameter Store's default is 40 TPS per account, shared by every Databricks source at once, and
  // discovery's per-table DESCRIBE fan-out is the peak.
  connector.addAlarm("ConfigThrottlesAlarm", {
    alarmName: `${connector.functionName}-config-throttles`,
    alarmDescription:
      "Parameter Store throttled the connector's configuration reads. Nothing is misconfigured: " +
      "raise the account's Parameter Store throughput setting. The connector retries with " +
      "backoff and caches for its TTL, so a small number of these degrades latency rather than " +
      "failing queries.",
    metric: connector.connectorMetric(MANAGED_METRIC_NAMES.configThrottles, {
      period: PERIOD,
      statistic: "Sum",
    }),
    ...COUNT_BREACH,
  });

  // Counted separately from ConnectorConfigResolutionFailures because the cause is a policy COA does
  // not own and cannot repair, so the response is to tell the source's owner rather than to page COA.
  connector.addAlarm("CredentialAssumeFailuresAlarm", {
    alarmName: `${connector.functionName}-credential-assume-failures`,
    alarmDescription:
      "sts:AssumeRole on a source's credential-access role failed, so no query against that " +
      "source can read its credential. One source means its role's trust policy no longer names " +
      "this connector or no longer carries the namespace's sts:ExternalId condition; every " +
      "source at once means COA's side moved. The connector's log names the catalog.",
    metric: connector.connectorMetric(MANAGED_METRIC_NAMES.credentialAssumeFailures, {
      period: PERIOD,
      statistic: "Sum",
    }),
    ...COUNT_BREACH,
  });
}
