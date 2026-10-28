// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Template } from "aws-cdk-lib/assertions";

/**
 * A resource_prefix + env that makes every OpenSearch Serverless name in the
 * app longer than 32 characters, so each one goes through aossName()'s
 * shortening path. A name built with plain prefixed() fails the pattern below.
 */
export const LONG_PREFIX_CONTEXT = {
  resource_prefix: "coa-demo-long-prefix",
  env: "staging",
};

// OpenSearch Serverless collection, collection group and policy names: 3-32
// characters, lowercase letters, digits and hyphens, starting with a letter.
export const AOSS_NAME_PATTERN = /^[a-z][a-z0-9-]{2,31}$/;

const AOSS_NAMED_TYPES = [
  "AWS::OpenSearchServerless::Collection",
  "AWS::OpenSearchServerless::CollectionGroup",
  "AWS::OpenSearchServerless::SecurityPolicy",
  "AWS::OpenSearchServerless::AccessPolicy",
];

/** Every OpenSearch Serverless resource name in *template*. */
export function aossNames(template: Template): string[] {
  return AOSS_NAMED_TYPES.flatMap((type) =>
    Object.values(template.findResources(type)).map((r) =>
      String(r.Properties.Name),
    ),
  );
}

/** Assert *template* has at least *minCount* OpenSearch Serverless names and all are valid. */
export function expectValidAossNames(template: Template, minCount = 1): void {
  const names = aossNames(template);
  expect(names.length).toBeGreaterThanOrEqual(minCount);
  names.forEach((n) => expect(n).toMatch(AOSS_NAME_PATTERN));
}
