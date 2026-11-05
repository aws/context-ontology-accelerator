// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The commands the MCP Server page tells users to run after unzipping
 * `coa-mcp-proxy-src.zip`. Kept in its own module so the page and the test
 * that builds the zip and runs these exact commands share one string.
 *
 * `npm run build` runs the bundled package.json's `build` script (`tsc`) from
 * `node_modules/.bin`. If the install failed it stops with "tsc: not found";
 * `npx tsc` would instead download an unrelated npm package named `tsc`.
 */
export const PROXY_BUILD_COMMAND =
  "cd coa-mcp-proxy && npm install && npm run build";

/**
 * The same build from the root of a repo checkout, where the proxy is a pnpm
 * workspace member (plain `npm install` there pulls in its test toolchain).
 */
export const PROXY_REPO_BUILD_COMMAND =
  "pnpm install && pnpm --dir packages/mcp-proxy run build";

/**
 * What the published SHA-256 covers, shown next to it. The zip pins the
 * proxy's direct dependencies to the versions in the repo's pnpm-lock.yaml,
 * but `npm install` resolves their own dependencies from the registry, and the
 * `.sha256` file is served from the same place as the zip.
 */
export const PROXY_CHECKSUM_SCOPE =
  "The checksum covers the source and the exact versions of the proxy's direct dependencies, not the packages those dependencies pull in when npm install runs. It shows the download is complete and unchanged in transit; it comes from the same site as the zip, so it does not prove who published it.";
