// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";

import {
  GRAPH_SOURCES_CONTEXT_KEY,
  GRAPH_SOURCES_ENV,
  graphSourcesEnabled,
} from "../../lib/utils/graph-sources";

function nodeWith(context: Record<string, unknown> = {}) {
  return new cdk.Stack(new cdk.App({ context }), "test-stack").node;
}

describe("graphSourcesEnabled", () => {
  it("names the context key and the runtime env var", () => {
    expect(GRAPH_SOURCES_CONTEXT_KEY).toBe("graph_sources");
    expect(GRAPH_SOURCES_ENV).toBe("GRAPH_SOURCES_ENABLED");
  });

  it("is off when the context key is unset", () => {
    expect(graphSourcesEnabled(nodeWith())).toBe(false);
  });

  it.each(["on", "true", "1", "yes", "ON", " True ", "Yes"])(
    "is on for %p",
    (value) => {
      expect(graphSourcesEnabled(nodeWith({ graph_sources: value }))).toBe(
        true,
      );
    },
  );

  it.each([true, 1])(
    "is on for the non-string %p (cdk.json context)",
    (value) => {
      expect(graphSourcesEnabled(nodeWith({ graph_sources: value }))).toBe(
        true,
      );
    },
  );

  it.each(["off", "false", "0", "no", "", "  ", "enabled", "onn"])(
    "is off for %p",
    (value) => {
      expect(graphSourcesEnabled(nodeWith({ graph_sources: value }))).toBe(
        false,
      );
    },
  );

  it.each([false, 0, null])("is off for the non-string %p", (value) => {
    expect(graphSourcesEnabled(nodeWith({ graph_sources: value }))).toBe(false);
  });
});
