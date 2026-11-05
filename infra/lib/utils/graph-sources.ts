// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Node } from "constructs";

/** CDK context key of the graph-sources (RDF_SPARQL) deploy switch. Off by default. */
export const GRAPH_SOURCES_CONTEXT_KEY = "graph_sources";

/**
 * Runtime env var set to "true" only when the switch is on. Absent means off,
 * so an off deployment carries no new variables (coa_common.sparql_config).
 */
export const GRAPH_SOURCES_ENV = "GRAPH_SOURCES_ENABLED";

const TRUTHY = new Set(["on", "true", "1", "yes"]);

/** Whether `-c graph_sources=on|true|1|yes` was given (trimmed, case-insensitive). */
export function graphSourcesEnabled(node: Node): boolean {
  const raw: unknown = node.tryGetContext(GRAPH_SOURCES_CONTEXT_KEY);
  return TRUTHY.has(
    String(raw ?? "")
      .trim()
      .toLowerCase(),
  );
}
