// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Unique, traceable names for test-created entities so parallel workers never
 * collide and leftovers are easy to spot.
 *
 * Format: e2e-<area>-<epochMillis>-<workerIndex>
 */

/** Prefix marking an entity as created by the E2E suite. */
export const E2E_PREFIX = "e2e";

/**
 * Build a unique entity name for a feature area.
 *
 * @param area  short feature-area tag, e.g. "namespace", "metric", "source"
 * @param workerIndex  the Playwright worker index (`testInfo.workerIndex`)
 */
export function uniqueName(area: string, workerIndex: number): string {
  return `${E2E_PREFIX}-${area}-${Date.now()}-${workerIndex}`;
}
