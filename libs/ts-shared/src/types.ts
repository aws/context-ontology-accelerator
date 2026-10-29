// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Runtime configuration delivered to the frontend at deploy time.
 *
 * - **CloudFront mode**: written as `runtime-config.json` to S3.
 * - **ECS mode**: passed as the `RUNTIME_CONFIG_JSON` environment variable.
 *
 * The web-app fetches this at startup; CDK writes it during deployment.
 */
export interface RuntimeConfig {
  readonly region: string;
  readonly stage: string;
  /** OIDC / Cognito authority URL. */
  readonly authority: string;
  /** OIDC / Cognito client ID. */
  readonly clientId: string;
  /** Backend API endpoint (set once API stack is wired). */
  readonly apiEndpoint?: string;
  /** AgentCore Runtime ARN for SSE streaming queries. Frontend constructs the invocations URL. */
  readonly serveRuntimeArn?: string;
  /** AgentCore Runtime ARN for the MCP Server. Frontend derives the MCP endpoint URL from this + region. */
  readonly mcpRuntimeArn?: string;
  /** Monorepo version (repo-root VERSION file) surfaced in the UI. E.g. "0.2.2". */
  readonly version?: string;
}
