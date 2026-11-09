// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { MetricDefinition } from "./use-metrics";

/**
 * Whether the server withheld this metric's definition (its SQL dialects and
 * source table) from the caller, which it does for a role without
 * source-schema visibility or when it cannot evaluate that right.
 *
 * A stored metric always has both (create validation requires them), so both
 * missing at once can only mean they were withheld.
 */
export function isMetricDefinitionWithheld(
  m: Pick<MetricDefinition, "expression" | "sourceTable">,
): boolean {
  return (m.expression?.dialects ?? []).length === 0 && !m.sourceTable;
}
