// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the SSM client before importing the module under test.
const sendMock = vi.fn();
vi.mock("@aws-sdk/client-ssm", () => ({
  SSMClient: vi.fn(function () {
    return { send: sendMock };
  }),
  GetParameterCommand: vi.fn(function (input: unknown) {
    return { input };
  }),
}));

import { resolveConfig } from "../src/config.js";

const ENV_KEYS = [
  "AWS_REGION",
  "RESOURCE_PREFIX",
  "SCL_PREFIX",
  "SCL_CLIENT_ID",
  "SCL_ISSUER",
  "SCL_RUNTIME_ARN",
  "SCL_RUNTIME_URL",
];

describe("resolveConfig", () => {
  let saved: Record<string, string | undefined>;

  beforeEach(() => {
    saved = {};
    for (const k of ENV_KEYS) {
      saved[k] = process.env[k];
      delete process.env[k];
    }
    sendMock.mockReset();
  });

  afterEach(() => {
    for (const k of ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });

  it("resolves entirely from env vars without touching SSM", async () => {
    process.env.SCL_CLIENT_ID = "cid";
    process.env.SCL_ISSUER = "https://issuer.example";
    process.env.SCL_RUNTIME_URL = "https://runtime.example/invoke";
    process.env.AWS_REGION = "eu-west-1";

    const cfg = await resolveConfig();

    expect(sendMock).not.toHaveBeenCalled();
    expect(cfg).toEqual({
      clientId: "cid",
      issuer: "https://issuer.example",
      runtimeArn: "",
      runtimeUrl: "https://runtime.example/invoke",
      region: "eu-west-1",
    });
  });

  it("derives the AgentCore runtime URL from the ARN when no URL is given", async () => {
    process.env.SCL_CLIENT_ID = "cid";
    process.env.SCL_ISSUER = "iss";
    process.env.SCL_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123:runtime/abc";

    const cfg = await resolveConfig();

    expect(cfg.region).toBe("us-east-1"); // default
    expect(cfg.runtimeUrl).toBe(
      "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/" +
        encodeURIComponent("arn:aws:bedrock-agentcore:us-east-1:123:runtime/abc") +
        "/invocations?qualifier=DEFAULT",
    );
  });

  it("falls back to SSM for missing values using the default scl prefix", async () => {
    // Only runtime provided via env; clientId + issuer come from SSM.
    process.env.SCL_RUNTIME_URL = "https://runtime.example/invoke";
    sendMock.mockImplementation((cmd: { input: { Name: string } }) => {
      const name = cmd.input.Name;
      const values: Record<string, string> = {
        "/coa/mcp-client-id": "ssm-client",
        "/coa/issuer": "https://ssm-issuer",
      };
      return Promise.resolve({ Parameter: { Value: values[name] ?? "" } });
    });

    const cfg = await resolveConfig();

    expect(cfg.clientId).toBe("ssm-client");
    expect(cfg.issuer).toBe("https://ssm-issuer");
    expect(sendMock).toHaveBeenCalled();
  });

  it("honors RESOURCE_PREFIX when building SSM parameter names", async () => {
    process.env.RESOURCE_PREFIX = "myproj";
    const seen: string[] = [];
    sendMock.mockImplementation((cmd: { input: { Name: string } }) => {
      seen.push(cmd.input.Name);
      const values: Record<string, string> = {
        "/myproj/mcp-client-id": "c",
        "/myproj/issuer": "i",
        "/myproj/mcp/runtime-arn": "arn:aws:x:us-east-1:1:runtime/r",
      };
      return Promise.resolve({ Parameter: { Value: values[cmd.input.Name] ?? "" } });
    });

    const cfg = await resolveConfig();

    expect(seen).toContain("/myproj/mcp-client-id");
    expect(cfg.clientId).toBe("c");
    expect(cfg.runtimeUrl).toContain("bedrock-agentcore");
  });

  it("throws a clear error when clientId cannot be resolved", async () => {
    sendMock.mockResolvedValue({ Parameter: { Value: "" } });
    await expect(resolveConfig()).rejects.toThrow(/SCL_CLIENT_ID/);
  });

  it("throws when issuer cannot be resolved", async () => {
    process.env.SCL_CLIENT_ID = "cid";
    sendMock.mockResolvedValue({ Parameter: { Value: "" } });
    await expect(resolveConfig()).rejects.toThrow(/SCL_ISSUER/);
  });

  it("throws when neither runtime ARN nor URL can be resolved", async () => {
    process.env.SCL_CLIENT_ID = "cid";
    process.env.SCL_ISSUER = "iss";
    sendMock.mockResolvedValue({ Parameter: { Value: "" } });
    await expect(resolveConfig()).rejects.toThrow(/runtime ARN/);
  });

  it("treats SSM failures as unresolved (swallows the error, then throws resolution error)", async () => {
    process.env.SCL_ISSUER = "iss";
    process.env.SCL_RUNTIME_URL = "url";
    sendMock.mockRejectedValue(new Error("SSM down"));
    // clientId can't be resolved → resolution error, not the raw SSM error.
    await expect(resolveConfig()).rejects.toThrow(/SCL_CLIENT_ID/);
  });

  it("logs the SSM failure reason so it is not silently swallowed", async () => {
    process.env.SCL_ISSUER = "iss";
    process.env.SCL_RUNTIME_URL = "url";
    sendMock.mockRejectedValue(new Error("access denied"));
    const errSpy = vi.spyOn(process.stderr, "write").mockReturnValue(true);
    await expect(resolveConfig()).rejects.toThrow(/SCL_CLIENT_ID/);
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("access denied"));
  });
});
