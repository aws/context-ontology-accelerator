// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock fs + os before importing the module under test. index.ts imports these
// at module load; findUvx/buildPath call them at invocation.
const existsSyncMock = vi.fn();
vi.mock("node:fs", () => ({ existsSync: (p: string) => existsSyncMock(p) }));
vi.mock("node:os", () => ({ homedir: () => "/home/tester" }));
// getToken/resolveConfig/spawn are only used by main() (guarded, not run under test),
// but the imports must resolve — stub them so importing index.ts is side-effect free.
vi.mock("../src/auth.js", () => ({ getToken: vi.fn() }));
vi.mock("../src/config.js", () => ({ resolveConfig: vi.fn() }));
vi.mock("node:child_process", () => ({ spawn: vi.fn() }));

const { findUvx, buildPath, buildProxyArgs } = await import("../src/index.js");

describe("findUvx", () => {
  beforeEach(() => existsSyncMock.mockReset());

  it("returns the first candidate that exists (~/.local/bin/uvx)", () => {
    existsSyncMock.mockImplementation((p: string) => p === "/home/tester/.local/bin/uvx");
    expect(findUvx()).toBe("/home/tester/.local/bin/uvx");
  });

  it("falls through to a later candidate when earlier ones are absent", () => {
    existsSyncMock.mockImplementation((p: string) => p === "/opt/homebrew/bin/uvx");
    expect(findUvx()).toBe("/opt/homebrew/bin/uvx");
  });

  it("returns null when uvx is nowhere on the candidate list", () => {
    existsSyncMock.mockReturnValue(false);
    expect(findUvx()).toBeNull();
  });
});

describe("buildPath", () => {
  let savedPath: string | undefined;

  beforeEach(() => {
    existsSyncMock.mockReset();
    savedPath = process.env.PATH;
  });
  afterEach(() => {
    if (savedPath === undefined) delete process.env.PATH;
    else process.env.PATH = savedPath;
  });

  it("prepends existing extra dirs and preserves the current PATH entries", () => {
    process.env.PATH = "/usr/bin:/bin";
    // Only the toolbox + homebrew extras "exist".
    existsSyncMock.mockImplementation(
      (p: string) => p === "/home/tester/.toolbox/bin" || p === "/opt/homebrew/bin",
    );
    const parts = buildPath().split(":");
    expect(parts).toContain("/usr/bin");
    expect(parts).toContain("/bin");
    expect(parts).toContain("/home/tester/.toolbox/bin");
    expect(parts).toContain("/opt/homebrew/bin");
    // A non-existent extra is not added.
    expect(parts).not.toContain("/home/tester/.local/bin");
  });

  it("adds all extra dirs when they all exist", () => {
    process.env.PATH = "/usr/bin";
    existsSyncMock.mockReturnValue(true);
    const parts = new Set(buildPath().split(":"));
    for (const d of [
      "/home/tester/.local/bin",
      "/home/tester/.toolbox/bin",
      "/usr/local/bin",
      "/opt/homebrew/bin",
      "/usr/bin",
    ]) {
      expect(parts.has(d)).toBe(true);
    }
  });

  it("deduplicates when an extra dir is already on PATH", () => {
    process.env.PATH = "/usr/local/bin:/usr/bin";
    existsSyncMock.mockImplementation((p: string) => p === "/usr/local/bin");
    const parts = buildPath().split(":");
    expect(parts.filter((p) => p === "/usr/local/bin")).toHaveLength(1);
  });

  it("falls back to a sane default PATH when the env var is unset", () => {
    delete process.env.PATH;
    existsSyncMock.mockReturnValue(false);
    const parts = buildPath().split(":");
    expect(parts).toContain("/usr/bin");
    expect(parts).toContain("/bin");
  });
});

describe("buildProxyArgs", () => {
  it("runs mcp-proxy 0.13.3 over streamablehttp with the bearer header", () => {
    expect(buildProxyArgs("tok", "https://rt.example/mcp")).toEqual([
      "mcp-proxy==0.13.3",
      "--transport", "streamablehttp",
      "--headers", "Authorization", "Bearer tok",
      "https://rt.example/mcp",
    ]);
  });

  it("leaves the mcp SDK version to mcp-proxy instead of pinning it", () => {
    expect(buildProxyArgs("tok", "https://rt.example/mcp")).not.toContain("--with");
  });
});
