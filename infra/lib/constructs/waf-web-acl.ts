// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";
import { SCLConstruct } from "./scl-construct";
import { DEFAULT_WAF_RATE_LIMIT_PER_5MIN } from "../constants";

export interface WafWebAclProps {
  /**
   * WAFv2 scope. `REGIONAL` for API Gateway / ALB / AppSync; `CLOUDFRONT` for
   * CloudFront distributions.
   *
   * NOTE: a `CLOUDFRONT`-scope WebACL can only be created in `us-east-1` — the
   * stack hosting this construct MUST be in that region when scope is
   * `CLOUDFRONT` (see {@link EdgeWafStack}).
   */
  readonly scope: "REGIONAL" | "CLOUDFRONT";
  /**
   * Short name fragment used for the WebACL name and CloudWatch metric name
   * (e.g. `"api"` → `{prefix}-{env}-api-waf`). Must match `[A-Za-z0-9-_]`.
   */
  readonly namePrefix: string;
  /**
   * Max requests per rolling 5-minute window from a single source IP before
   * that IP is blocked (WAF rate-based rule, `aggregateKeyType: IP`). This is
   * the per-entity edge throttle that protects the auth layer
   * from anonymous floods before Cognito runs. Defaults to
   * {@link DEFAULT_WAF_RATE_LIMIT_PER_5MIN}. Set to `0` to disable the rule
   * (e.g. when the caller brings a WebACL that already rate-limits).
   */
  readonly rateLimit?: number;
}

/**
 * A WAFv2 WebACL pre-configured with the AWS-managed **Common Rule Set**
 * (`AWSManagedRulesCommonRuleSet`) and a default `allow` action.
 *
 * Used to auto-provision baseline protection for a surface (API Gateway or
 * CloudFront) when the deployer does not bring their own WebACL. The ACL's ARN
 * is exposed via {@link webAclArn} for association (`CfnWebACLAssociation` for
 * REGIONAL resources) or direct reference (CloudFront `webAclId`).
 */
export class WafWebAcl extends SCLConstruct {
  public readonly webAcl: wafv2.CfnWebACL;
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: WafWebAclProps) {
    super(scope, id);

    const name = this.prefixed(`${props.namePrefix}-waf`);

    const rateLimit = props.rateLimit ?? DEFAULT_WAF_RATE_LIMIT_PER_5MIN;

    const rules: wafv2.CfnWebACL.RuleProperty[] = [
      {
        name: "AWS-AWSManagedRulesCommonRuleSet",
        priority: 0,
        statement: {
          managedRuleGroupStatement: {
            vendorName: "AWS",
            name: "AWSManagedRulesCommonRuleSet",
          },
        },
        // Managed rule groups define their own actions; the WebACL must not
        // override them (use `none`), only observe via metrics.
        overrideAction: { none: {} },
        visibilityConfig: {
          cloudWatchMetricsEnabled: true,
          metricName: `${name}-common-rule-set`,
          sampledRequestsEnabled: true,
        },
      },
    ];

    // Per-IP rate-based rule, block on breach since that is unambiguous abuse.
    // A rateLimit of 0 disables the rule (see the rateLimit prop).
    if (rateLimit > 0) {
      rules.push({
        name: "RateLimitPerIp",
        priority: 1,
        statement: {
          rateBasedStatement: {
            limit: rateLimit,
            aggregateKeyType: "IP",
          },
        },
        action: { block: {} },
        visibilityConfig: {
          cloudWatchMetricsEnabled: true,
          metricName: `${name}-rate-limit-per-ip`,
          sampledRequestsEnabled: true,
        },
      });
    }

    this.webAcl = new wafv2.CfnWebACL(this, "Resource", {
      name,
      scope: props.scope,
      // Allow by default; the managed rule groups block.
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: name,
        sampledRequestsEnabled: true,
      },
      rules,
    });

    this.webAclArn = this.webAcl.attrArn;
  }
}
