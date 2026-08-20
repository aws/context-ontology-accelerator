// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * COA MCP Proxy
 *
 * Stdio bridge to AgentCore MCP Runtime with Cognito PKCE auth.
 * Auto-discovers config from SSM. Caches tokens for 30 days.
 *
 * Usage in .kiro/settings/mcp.json:
 *   { "command": "npx", "args": ["tsx", "./packages/mcp-proxy/src/index.ts"] }
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { getToken } from "./auth.js";
import { resolveConfig } from "./config.js";

export function findUvx(): string | null {
  const home = homedir();
  const candidates = [
    join(home, ".local/bin/uvx"),
    "/usr/local/bin/uvx",
    "/opt/homebrew/bin/uvx",
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return null;
}

export function buildPath(): string {
  const home = homedir();
  const extra = [
    join(home, ".local/bin"),
    join(home, ".toolbox/bin"),
    "/usr/local/bin",
    "/opt/homebrew/bin",
  ];
  const current = process.env.PATH || "/usr/bin:/bin";
  const parts = new Set(current.split(":"));
  for (const p of extra) {
    if (existsSync(p)) parts.add(p);
  }
  return [...parts].join(":");
}

/* v8 ignore start -- stdio bridge: spawns the uvx child, wires process signals
   and exit codes; exercised end-to-end at runtime, not unit-testable without a
   real child process. The pure helpers (findUvx/buildPath) above are covered. */
async function main(): Promise<void> {
  // Enrich PATH for child processes (uvx, credential helpers)
  process.env.PATH = buildPath();

  const config = await resolveConfig();

  process.stderr.write("Authenticating...\n");
  const token = await getToken(config.clientId, config.issuer);
  process.stderr.write("Authenticated.\n");

  const uvxPath = findUvx();
  if (!uvxPath) {
    process.stderr.write("ERROR: uvx not found. Install uv: https://docs.astral.sh/uv/\n");
    process.exit(1);
  }

  const child = spawn(
    uvxPath,
    [
      // Pin mcp too: mcp-proxy 0.12.0 needs mcp's pre-v2 `request_ctx` API,
      // which the v2 rewrite removed. Bump once mcp-proxy supports v2.
      "--with", "mcp==1.27.1",
      "mcp-proxy==0.12.0",
      "--transport", "streamablehttp",
      "--headers", "Authorization", `Bearer ${token}`,
      config.runtimeUrl,
    ],
    { stdio: "inherit", env: { ...process.env, PATH: buildPath() } },
  );

  child.on("exit", (code) => process.exit(code ?? 1));
  child.on("error", (err) => {
    process.stderr.write(`Failed to launch mcp-proxy: ${err.message}\n`);
    process.exit(1);
  });
}

// Only auto-run when executed directly (not when imported by tests).
if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    process.stderr.write(`ERROR: ${err.message}\n`);
    process.exit(1);
  });
}
/* v8 ignore stop */
