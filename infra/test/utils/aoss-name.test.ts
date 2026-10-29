// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";

import { AOSS_NAME_MAX_LENGTH, aossName, prefixed } from "../../lib/context";
import { NetworkStack } from "../../lib/stacks/foundation/network-stack";
import { StorageStack } from "../../lib/stacks/foundation/storage-stack";
import { SourcesStack } from "../../lib/stacks/services/sources-stack";
import {
  AOSS_NAME_PATTERN,
  LONG_PREFIX_CONTEXT,
  expectValidAossNames,
} from "./aoss-name-assertions";

function nodeWith(context: Record<string, string>) {
  return new cdk.Stack(new cdk.App({ context }), "test-stack").node;
}

describe("aossName", () => {
  it("returns prefixed() unchanged when the name fits, so existing deployments keep their names", () => {
    const node = nodeWith({ resource_prefix: "coa", env: "dev" });
    expect(aossName(node, "src-ingestion-access")).toBe(
      "coa-dev-src-ingestion-access",
    );
  });

  it("leaves a name of exactly 32 characters unchanged", () => {
    // "coa-dev-" (8) + 24 characters = 32.
    const node = nodeWith({ resource_prefix: "coa", env: "dev" });
    const name = "x".repeat(24);
    expect(prefixed(node, name)).toHaveLength(AOSS_NAME_MAX_LENGTH);
    expect(aossName(node, name)).toBe(prefixed(node, name));
  });

  it("shortens a name of 33 characters to exactly 32", () => {
    const node = nodeWith({ resource_prefix: "coa", env: "dev" });
    const name = "x".repeat(25);
    const short = aossName(node, name);
    expect(short).toHaveLength(AOSS_NAME_MAX_LENGTH);
    expect(short).toMatch(/^coa-dev-x+-[0-9a-f]{6}$/);
  });

  it("shortens a long name to a valid, deterministic name", () => {
    const node = nodeWith({ resource_prefix: "coa-demo", env: "dev" });
    const name = aossName(node, "src-ingestion-access");

    expect(name.length).toBeLessThanOrEqual(AOSS_NAME_MAX_LENGTH);
    expect(name).toMatch(AOSS_NAME_PATTERN);
    expect(name.startsWith("coa-demo-dev-")).toBe(true);
    expect(aossName(node, "src-ingestion-access")).toBe(name);
    // Pinned: a change to the shortening renames, and so replaces, deployed resources.
    expect(name).toBe("coa-demo-dev-src-ingestio-3989de");
  });

  it("does not leave a double hyphen when the cut lands on a hyphen", () => {
    // "abcd-dev-" + "aaaaaaaaaaaaaaa-bbbbbbbb": character 25 (the last one
    // kept before the hash) is the hyphen after the 15 a's.
    const node = nodeWith({ resource_prefix: "abcd", env: "dev" });
    const full = prefixed(node, "aaaaaaaaaaaaaaa-bbbbbbbb");
    expect(full[24]).toBe("-");
    const short = aossName(node, "aaaaaaaaaaaaaaa-bbbbbbbb");
    expect(short).not.toContain("--");
    expect(short).toMatch(/^abcd-dev-a+-[0-9a-f]{6}$/);
  });

  it("keeps shortened names distinct for different policies and deployments", () => {
    const demo = nodeWith(LONG_PREFIX_CONTEXT);
    const other = nodeWith({ ...LONG_PREFIX_CONTEXT, env: "staging2" });
    const names = [
      aossName(demo, "src-ingestion-access"),
      aossName(demo, "metric-data-access"),
      aossName(other, "src-ingestion-access"),
    ];

    expect(new Set(names).size).toBe(names.length);
    names.forEach((n) => expect(n).toMatch(AOSS_NAME_PATTERN));
  });
});

describe("OpenSearch Serverless names with a long resource_prefix", () => {
  it("are all valid in the storage and sources stacks", () => {
    const env = { account: "123456789012", region: "us-east-1" };
    const app = new cdk.App({
      context: {
        ...LONG_PREFIX_CONTEXT,
        ecr_repository_arn:
          "arn:aws:ecr:us-east-1:123456789012:repository/coa-test",
        ecr_repository_name: "coa-test",
        sources_db_enrichment_image_uri:
          "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:db-enrichment-test",
        sources_preprocessing_image_uri:
          "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:preprocessing-test",
        sources_kg_build_image_uri:
          "123456789012.dkr.ecr.us-east-1.amazonaws.com/coa-test:kg-build-test",
        "aws:cdk:bundling-stacks": [],
      },
    });
    const network = new NetworkStack(app, "TestNetwork", { env });
    const storage = new StorageStack(app, "TestStorage", { network, env });
    const sources = new SourcesStack(app, "TestSources", {
      network,
      storage,
      allowedOrigin: "https://test.example.com",
      env,
    });

    // Collection, collection group, encryption and network policies.
    expectValidAossNames(Template.fromStack(storage), 4);
    expectValidAossNames(Template.fromStack(sources), 1);
  });
});
