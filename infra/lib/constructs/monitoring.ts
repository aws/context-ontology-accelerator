// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Duration } from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import {
  AlarmStatusWidget,
  ComparisonOperator,
  type IAlarm,
  Metric,
  Stats,
  TreatMissingData,
} from "aws-cdk-lib/aws-cloudwatch";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as stepfunctions from "aws-cdk-lib/aws-stepfunctions";
import {
  MonitoringFacade,
  DefaultDashboardFactory,
} from "cdk-monitoring-constructs";
import type { IAlarmActionStrategy } from "cdk-monitoring-constructs/lib/common/alarm/action";
import { Construct } from "constructs";

/** Config for a per-stack OE monitoring facade. */
export interface SclMonitoringProps {
  /** Alarm name prefix, typically the stack's this.prefixed("..."). */
  readonly alarmNamePrefix: string;
  /**
   * Optional alarm action strategy. Defaults to undefined: alarms are
   * action-ready (actionsEnabled) but route nowhere until a strategy is
   * plugged in at the app.ts seam. This is intentional for round one.
   */
  readonly alarmAction?: IAlarmActionStrategy;
}

// ── OE alarm-threshold conventions (one source of truth) ────────────────
const LAMBDA_MAX_FAULTS = 1;
const LAMBDA_MAX_THROTTLES = 1;
const API_P99_LATENCY = Duration.seconds(5);
const API_MAX_5XX = 1;
const SFN_MAX_FAILED = 1;
const DLQ_MAX_MESSAGES = 1;
const TABLE_MAX_THROTTLED = 1;
const TABLE_MAX_SYSTEM_ERRORS = 1;
const ECS_MAX_CPU_PERCENT = 90;
const ECS_MAX_MEM_PERCENT = 90;
const ALB_MAX_5XX = 1;

/**
 * Builder-style wrapper around a cdk-monitoring-constructs MonitoringFacade.
 *
 * Instantiate once per stack (config-only), then register resources the
 * stack owns via the monitorX methods, co-located with where each resource
 * is created. The facade builds dashboards/alarms incrementally.
 */
export class SclMonitoring extends Construct {
  private readonly facade: MonitoringFacade;

  constructor(scope: Construct, id: string, props: SclMonitoringProps) {
    super(scope, id);
    this.facade = new MonitoringFacade(this, "Facade", {
      alarmFactoryDefaults: {
        alarmNamePrefix: props.alarmNamePrefix,
        actionsEnabled: true,
        action: props.alarmAction,
      },
      dashboardFactory: new DefaultDashboardFactory(this, "Dashboards", {
        dashboardNamePrefix: props.alarmNamePrefix,
      }),
    });
  }

  monitorLambda(fn: lambda.IFunction): this {
    this.facade.monitorLambdaFunction({
      lambdaFunction: fn,
      addFaultCountAlarm: {
        Warning: { maxErrorCount: LAMBDA_MAX_FAULTS },
      },
      addThrottlesCountAlarm: {
        Warning: { maxErrorCount: LAMBDA_MAX_THROTTLES },
      },
    });
    return this;
  }

  /**
   * Puts an alarm the stack built itself onto this facade's dashboard.
   *
   * For a detection whose metric is not one of the facade's shapes — a log metric filter, say. It
   * adds a status widget only: the alarm, its threshold and its action are the caller's, so nothing
   * here creates a second alarm on the same metric.
   */
  showAlarm(alarm: IAlarm, title: string): this {
    this.facade.addWidget(
      new AlarmStatusWidget({ alarms: [alarm], title, width: 6, height: 3 }),
    );
    return this;
  }

  monitorApi(api: apigateway.IRestApi): this {
    this.facade.monitorApiGateway({
      api,
      // Pin the alarm-friendly name. Left unset, cdk-monitoring-constructs
      // derives it from the RestApi name, which is an unresolved CDK token, so
      // the alarm construct IDs came out as
      // `...scldevapiscldevapiTokenTOKEN2572FaultCountWarning`. The token
      // *counter* shifts whenever anything earlier in the app allocates a
      // different number of tokens, which silently replaced these two alarms on
      // unrelated changes. A literal makes the logical IDs deterministic.
      // Only one API is monitored (api-stack.ts), so a fixed name cannot collide.
      alarmFriendlyName: "RestApi",
      addLatencyP99Alarm: {
        Warning: { maxLatency: API_P99_LATENCY },
      },
      add5XXFaultCountAlarm: {
        Warning: { maxErrorCount: API_MAX_5XX },
      },
    });
    return this;
  }

  monitorFargateService(service: ecs.FargateService): this {
    this.facade.monitorSimpleFargateService({
      fargateService: service,
      addCpuUsageAlarm: {
        Warning: { maxUsagePercent: ECS_MAX_CPU_PERCENT },
      },
      addMemoryUsageAlarm: {
        Warning: { maxUsagePercent: ECS_MAX_MEM_PERCENT },
      },
    });
    return this;
  }

  monitorQueueWithDlq(queue: sqs.IQueue, deadLetterQueue: sqs.IQueue): this {
    this.facade.monitorSqsQueueWithDlq({
      queue,
      deadLetterQueue,
      addDeadLetterQueueMaxSizeAlarm: {
        Warning: { maxMessageCount: DLQ_MAX_MESSAGES },
      },
    });
    return this;
  }

  monitorStateMachine(stateMachine: stepfunctions.IStateMachine): this {
    this.facade.monitorStepFunction({
      stateMachine,
      addFailedExecutionCountAlarm: {
        Warning: { maxErrorCount: SFN_MAX_FAILED },
      },
    });
    return this;
  }

  monitorTable(table: dynamodb.ITable): this {
    this.facade.monitorDynamoTable({
      table,
      addReadThrottledEventsCountAlarm: {
        Warning: { maxThrottledEventsThreshold: TABLE_MAX_THROTTLED },
      },
      addWriteThrottledEventsCountAlarm: {
        Warning: { maxThrottledEventsThreshold: TABLE_MAX_THROTTLED },
      },
      addSystemErrorCountAlarm: {
        Warning: {
          maxErrorCount: TABLE_MAX_SYSTEM_ERRORS,
          treatMissingDataOverride: TreatMissingData.NOT_BREACHING,
        },
      },
    });
    return this;
  }

  /**
   * Cluster-level CPU/Memory alarms via custom metrics. Used where a stack
   * runs an ECS cluster with runtime-created services (no static
   * FargateService object), e.g. VKG.
   */
  monitorClusterCpuMem(clusterName: string, friendlyName: string): this {
    this.facade.monitorCustom({
      alarmFriendlyName: `${friendlyName}-cluster`,
      metricGroups: [
        {
          title: `${friendlyName} Cluster CPU`,
          metrics: [
            {
              metric: new Metric({
                namespace: "AWS/ECS",
                metricName: "CPUUtilization",
                dimensionsMap: { ClusterName: clusterName },
                statistic: Stats.MAXIMUM,
                period: Duration.minutes(5),
              }),
              alarmFriendlyName: `${friendlyName}-cpu-high`,
              addAlarm: {
                Warning: {
                  threshold: ECS_MAX_CPU_PERCENT,
                  comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
                  treatMissingDataOverride: TreatMissingData.NOT_BREACHING,
                  evaluationPeriods: 3,
                },
              },
            },
          ],
        },
        {
          title: `${friendlyName} Cluster Memory`,
          metrics: [
            {
              metric: new Metric({
                namespace: "AWS/ECS",
                metricName: "MemoryUtilization",
                dimensionsMap: { ClusterName: clusterName },
                statistic: Stats.AVERAGE,
                period: Duration.minutes(5),
              }),
              alarmFriendlyName: `${friendlyName}-memory-high`,
              addAlarm: {
                Warning: {
                  threshold: ECS_MAX_MEM_PERCENT,
                  comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
                  treatMissingDataOverride: TreatMissingData.NOT_BREACHING,
                  evaluationPeriods: 3,
                },
              },
            },
          ],
        },
      ],
    });
    return this;
  }

  /**
   * Serve ALB health via always-emitted ELB metrics (5XX + unhealthy hosts).
   * AgentCore Runtime *internal* health behind the ALB is out of round one.
   */
  monitorAlb(
    loadBalancer: elbv2.IApplicationLoadBalancer,
    friendlyName: string,
  ): this {
    this.facade.monitorCustom({
      alarmFriendlyName: `${friendlyName}-alb`,
      metricGroups: [
        {
          title: `${friendlyName} ALB 5XX`,
          metrics: [
            {
              metric: loadBalancer.metrics.httpCodeElb(
                elbv2.HttpCodeElb.ELB_5XX_COUNT,
                { statistic: Stats.SUM, period: Duration.minutes(5) },
              ),
              alarmFriendlyName: `${friendlyName}-alb-5xx`,
              addAlarm: {
                Warning: {
                  threshold: ALB_MAX_5XX,
                  comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
                  treatMissingDataOverride: TreatMissingData.NOT_BREACHING,
                  evaluationPeriods: 1,
                  datapointsToAlarm: 1,
                },
              },
            },
          ],
        },
      ],
    });
    return this;
  }
}
