// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Build the MCP Server AgentCore /invocations URL from runtime ARN + region.
 *
 * The MCP Server is an AgentCore Runtime, addressed by the same URL shape as the
 * serve runtime (see build-query-endpoint.ts and packages/mcp-proxy/src/config.ts,
 * which constructs the identical URL for the stdio→streamable-http proxy). The URL
 * is derived from the MCP runtime ARN (stored in runtime-config.json as
 * mcpRuntimeArn) plus the deployment region. Returns undefined if the ARN is
 * missing or the region format is invalid (prevents URL injection via tampered
 * config), matching build-query-endpoint.ts's contract so callers can treat both
 * endpoints the same way.
 */
export function buildMcpEndpoint(
  ctx: { region: string; mcpRuntimeArn?: string } | undefined,
): string | undefined {
  if (!ctx?.mcpRuntimeArn) return undefined;
  // Validate region format to prevent URL injection via tampered config.
  if (!/^[a-z]{2}-[a-z]+-\d+$/.test(ctx.region)) return undefined;
  const encoded = encodeURIComponent(ctx.mcpRuntimeArn);
  return `https://bedrock-agentcore.${ctx.region}.amazonaws.com/runtimes/${encoded}/invocations?qualifier=DEFAULT`;
}
