// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as lambda from "aws-cdk-lib/aws-lambda";

import { resolveContext } from "../../lib/context";
import { BrandEnvAspect, brandEnv } from "../../lib/utils/brand-env";

/** Minimal app/stack pair with an optional resource_prefix context value. */
function appWith(context: Record<string, string> = {}) {
  const app = new cdk.App({ context });
  const stack = new cdk.Stack(app, "test-stack");
  return { app, stack };
}

describe("data-plane brand tokens", () => {
  // These identifiers land in Neptune RDF and DataZone metadata. If they ever
  // start tracking resource_prefix, a prefix change re-mints every IRI and
  // orphans the graph data already stored. They are safe to keep static because
  // each lives inside a container that is itself prefix-isolated (own Neptune
  // cluster, own DataZone domain) — unlike the event bus, see below.
  it("do not vary with resource_prefix", () => {
    const def = resolveContext(appWith().stack.node);
    const scl = resolveContext(appWith({ resource_prefix: "scl" }).stack.node);

    expect(scl.graphBaseUri).toBe(def.graphBaseUri);
    expect(scl.urnPrefix).toBe(def.urnPrefix);
    expect(scl.vocabPrefix).toBe(def.vocabPrefix);
    expect(scl.dzTypePrefix).toBe(def.dzTypePrefix);
  });

  // The event source is the ONE brand token that must track resource_prefix:
  // the `default` bus is account-global, so two co-located deployments emitting
  // the same source have each one's rules matching the other's events. Nothing
  // persists keyed on the source, so per-deployment derivation orphans nothing.
  it("event source prefix DOES track resource_prefix (shared bus isolation)", () => {
    const def = resolveContext(appWith().stack.node);
    const scl = resolveContext(appWith({ resource_prefix: "scl" }).stack.node);
    const sclz = resolveContext(
      appWith({ resource_prefix: "sclz" }).stack.node,
    );

    expect(scl.eventSourcePrefix).toBe("scl");
    expect(sclz.eventSourcePrefix).toBe("sclz");
    // Two co-located deployments must not share an event source.
    expect(scl.eventSourcePrefix).not.toBe(sclz.eventSourcePrefix);
    expect(scl.eventSourcePrefix).not.toBe(def.eventSourcePrefix);
  });

  it("keeps resource naming tracking resource_prefix", () => {
    const ctx = resolveContext(appWith({ resource_prefix: "scl" }).stack.node);
    expect(ctx.prefix).toBe("scl");
    expect(ctx.ssmPrefix).toBe("/scl");
  });

  it("resolves to the coa brand by default", () => {
    const ctx = resolveContext(appWith().stack.node);
    expect(ctx.graphBaseUri).toBe("http://coa.amazon.com");
    expect(ctx.eventSourcePrefix).toBe("coa");
    expect(ctx.dzTypePrefix).toBe("Coa");
  });

  it("honours the graph_base_uri and event_source_prefix overrides", () => {
    const { stack } = appWith({
      graph_base_uri: "http://example.internal",
      event_source_prefix: "acme",
    });
    expect(brandEnv(stack.node)).toEqual({
      GRAPH_BASE_URI: "http://example.internal",
      EVENT_SOURCE_PREFIX: "acme",
    });
  });
});

describe("BrandEnvAspect", () => {
  it("injects the brand tokens into every lambda", () => {
    const { app, stack } = appWith({ resource_prefix: "scl" });
    new lambda.Function(stack, "Fn", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromInline("def handler(e, c): pass"),
    });
    cdk.Aspects.of(app).add(new BrandEnvAspect());

    // GRAPH_BASE_URI stays on the brand; EVENT_SOURCE_PREFIX tracks
    // resource_prefix so the emitter matches the prefix-scoped rule pattern.
    Template.fromStack(stack).hasResourceProperties("AWS::Lambda::Function", {
      Environment: {
        Variables: {
          GRAPH_BASE_URI: "http://coa.amazon.com",
          EVENT_SOURCE_PREFIX: "scl",
        },
      },
    });
  });
});
