// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Portable path helpers for the CDK infra package.
 *
 * All paths are resolved from the **monorepo root** (the directory that
 * contains `pnpm-workspace.yaml`), so they work identically on local
 * machines, CI runners, and fresh clones regardless of working directory.
 */
import * as path from "path";
import * as fs from "fs";

/**
 * Walk up from `startDir` until we find a directory containing `marker`.
 */
function findRootByMarker(
  marker: string,
  startDir: string = __dirname,
): string {
  let dir = startDir;
  while (true) {
    if (fs.existsSync(path.join(dir, marker))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) {
      throw new Error(
        `Could not locate monorepo root (looked for "${marker}" starting from ${startDir})`,
      );
    }
    dir = parent;
  }
}

/** Absolute path to the monorepo root (contains pnpm-workspace.yaml). */
export const MONOREPO_ROOT = findRootByMarker("pnpm-workspace.yaml");

/** Resolve one or more segments relative to the monorepo root. */
export function fromRoot(...segments: string[]): string {
  return path.join(MONOREPO_ROOT, ...segments);
}

// ── Pre-built paths used across stacks ──────────────────────────────────────

export const Paths = {
  /** Monorepo root — used as Docker build context. */
  root: MONOREPO_ROOT,

  /** packages/control-plane — Python Lambda source. */
  controlPlane: fromRoot("packages/control-plane"),

  /** packages/control-plane/src — flattened module root for Lambda. */
  controlPlaneSrc: fromRoot("packages/control-plane/src"),

  /** Authorization seed data directory (coa_authorization lives in libs/common). */
  authorizationSeed: fromRoot("libs/common/src/coa_authorization/seed"),

  /** Smithy-generated OpenAPI spec for the control-plane service. */
  controlPlaneOpenApiSpec: fromRoot(
    "smithy-generated/openapi/openapi/ControlPlaneService.openapi.json",
  ),

  /** Smithy-generated OpenAPI spec for the data-layer (serve) service. */
  dataLayerOpenApiSpec: fromRoot(
    "smithy-generated/openapi/openapi/DataLayerService.openapi.json",
  ),

  /** Web app dist directory (Vite build output). */
  webAppDist: fromRoot("packages/web-app/dist"),

  /** smithy-generated/control-plane-python-server — generated Python server models. */
  smithyGeneratedControlPlanePythonServer: fromRoot(
    "smithy-generated/control-plane-python-server",
  ),

  /** libs/common — shared Python library. */
  commonLib: fromRoot("libs/common/src"),

  /** packages/sources/database/trigger — database scan trigger Lambda source. */
  sourcesDatabaseTrigger: fromRoot("packages/sources/database/trigger"),

  /** packages/sources/src/coa_sources/documents/deletion — document deletion cleanup Lambda source. */
  sourcesDocumentsDeletion: fromRoot(
    "packages/sources/src/coa_sources/documents/deletion",
  ),

  /** packages/sources/src/coa_sources/documents/trigger — document ingestion trigger Lambda source. */
  sourcesDocumentsTrigger: fromRoot(
    "packages/sources/src/coa_sources/documents/trigger",
  ),

  /** packages/ontology-engine/src — ontology engine API proxy Lambda source. */
  ontologyEngineSrc: fromRoot("packages/ontology-engine/src"),
} as const;
