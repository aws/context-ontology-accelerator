// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { buildMcpEndpoint } from "./build-mcp-endpoint";

describe("buildMcpEndpoint", () => {
  const arn =
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/coa-mcp-abc123";

  it("builds the AgentCore invocations URL from ARN + region", () => {
    const url = buildMcpEndpoint({ region: "us-east-1", mcpRuntimeArn: arn });
    expect(url).toBe(
      `https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/${encodeURIComponent(arn)}/invocations?qualifier=DEFAULT`,
    );
  });

  it("URL-encodes the ARN so the colons/slashes are safe in the path", () => {
    const url = buildMcpEndpoint({ region: "us-east-1", mcpRuntimeArn: arn });
    // The raw ARN's colons must not appear unencoded in the path segment.
    expect(url).toContain(encodeURIComponent(arn));
    expect(url).not.toContain(`/runtimes/${arn}/`);
  });

  it("returns undefined when the ARN is missing", () => {
    expect(buildMcpEndpoint({ region: "us-east-1" })).toBeUndefined();
    expect(
      buildMcpEndpoint({ region: "us-east-1", mcpRuntimeArn: "" }),
    ).toBeUndefined();
  });

  it("returns undefined for undefined context", () => {
    expect(buildMcpEndpoint(undefined)).toBeUndefined();
  });

  it("rejects a malformed region (URL-injection guard)", () => {
    expect(
      buildMcpEndpoint({
        region: "evil.example.com/",
        mcpRuntimeArn: arn,
      }),
    ).toBeUndefined();
  });

  it("accepts other well-formed regions", () => {
    const url = buildMcpEndpoint({ region: "eu-west-1", mcpRuntimeArn: arn });
    expect(url).toContain("bedrock-agentcore.eu-west-1.amazonaws.com");
  });
});
