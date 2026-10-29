// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { RuntimeConfigContext } from "@components/RuntimeContext";
import type { RuntimeContext } from "@components/RuntimeContext";
import { McpPage } from "./McpPage";

// McpPage reads the active namespace via useCurrentNamespace (router + data
// hooks under the hood). Mock it so the page can render without a full
// NamespaceProvider/router tree.
const POPULATED_NS = {
  current: { namespaceId: "ns-123", name: "Sales" },
  namespaceId: "ns-123",
  namespaces: [],
  setNamespace: vi.fn(),
  isLoading: false,
};
const EMPTY_NS = {
  current: undefined,
  namespaceId: "",
  namespaces: [],
  setNamespace: vi.fn(),
  isLoading: false,
};
let mockNamespace: typeof POPULATED_NS | typeof EMPTY_NS = POPULATED_NS;
vi.mock("@components", async (orig) => {
  const actual = await orig<typeof import("@components")>();
  return { ...actual, useCurrentNamespace: () => mockNamespace };
});

const arn =
  "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/coa-mcp-abc123";

// BundleChecksum fetches the .sha256 sidecar on mount. Stub fetch so it
// resolves a known digest (and restore it after) — happy-dom has no network.
const SHA_FIXTURE =
  "ef7e2654d33c6c7475dccdcad20b4f1dc87157af555a80291da047a28ccc8b6e";
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      text: async () => `${SHA_FIXTURE}  coa-mcp-proxy-src.zip\n`,
    })),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const baseCtx: RuntimeContext = {
  region: "us-east-1",
  authority: "https://issuer.example.com",
  clientId: "client-abc",
  oidcConfig: {
    authority: "https://issuer.example.com",
    clientId: "client-abc",
  },
};

function renderPage(ctx: RuntimeContext | undefined) {
  return render(
    <RuntimeConfigContext.Provider value={ctx}>
      <McpPage />
    </RuntimeConfigContext.Provider>,
  );
}

describe("McpPage", () => {
  describe("when mcpRuntimeArn is configured", () => {
    const ctx: RuntimeContext = { ...baseCtx, mcpRuntimeArn: arn };

    it("renders the AgentCore endpoint URL built from the ARN + region", () => {
      renderPage(ctx);
      expect(
        screen.getByText(
          `https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/${encodeURIComponent(arn)}/invocations?qualifier=DEFAULT`,
        ),
      ).toBeInTheDocument();
    });

    it("lists every MCP tool the server exposes", () => {
      renderPage(ctx);
      for (const tool of [
        "list_metrics",
        "describe_schema",
        "query",
        "translate_sparql",
        "rag_retrieval",
        "graph_traversal",
      ]) {
        expect(screen.getAllByText(tool).length).toBeGreaterThanOrEqual(1);
      }
    });

    it("leads with 'Add the MCP server' as the primary action in do-order", () => {
      renderPage(ctx);
      expect(
        screen.getByText("2. Add the MCP server to your client"),
      ).toBeInTheDocument();
      expect(screen.getByText("1. Before you start")).toBeInTheDocument();
      expect(screen.getByText("3. Verify it works")).toBeInTheDocument();
    });

    it("renders a per-client tab set (Kiro / Claude Code / Claude Desktop / Cursor)", () => {
      renderPage(ctx);
      for (const client of [
        "Kiro",
        "Claude Code",
        "Claude Desktop",
        "Cursor",
      ]) {
        expect(screen.getAllByText(client).length).toBeGreaterThanOrEqual(1);
      }
    });

    it("surfaces the active namespace id in the verify step", () => {
      renderPage(ctx);
      // The switcher is hidden on narrow screens, so the id must appear here
      // (it shows in the namespace label and the copy-paste verify prompt).
      expect(screen.getAllByText(/ns-123/).length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText("3. Verify it works")).toBeInTheDocument();
    });

    it("shows a no-namespace placeholder when none exists yet", () => {
      mockNamespace = EMPTY_NS;
      try {
        renderPage(ctx);
        // No blank copy box — a helpful empty state instead.
        expect(screen.getByText(/No namespace yet/)).toBeInTheDocument();
        // The verify prompt degrades to the open-a-namespace-first guidance.
        expect(screen.getByText(/Open a namespace first/)).toBeInTheDocument();
      } finally {
        mockNamespace = POPULATED_NS;
      }
    });

    it("describes the PKCE browser-login auth (not a shared account)", () => {
      renderPage(ctx);
      expect(screen.getByText(/Cognito PKCE/)).toBeInTheDocument();
      // Appears in the auth summary and again in troubleshooting.
      expect(
        screen.getAllByText(/not a shared account/).length,
      ).toBeGreaterThanOrEqual(1);
    });

    it("drops the old get-token.sh / password-secret flow", () => {
      renderPage(ctx);
      // The PKCE proxy removes the token script, the password placeholder, and
      // the shared integration-test secret entirely.
      expect(screen.queryByText(/get-token\.sh/)).toBeNull();
      expect(screen.queryByText(/your-cognito-password/)).toBeNull();
      expect(screen.queryByText(/integ-test-user/)).toBeNull();
    });

    it("does not plant an AWS_PROFILE placeholder that breaks default creds", () => {
      renderPage(ctx);
      expect(screen.queryByText(/your-aws-profile/)).toBeNull();
      expect(screen.queryByText(/your-profile/)).toBeNull();
    });

    it("offers troubleshooting for the three real failure modes", () => {
      renderPage(ctx);
      expect(screen.getByText("Troubleshooting")).toBeInTheDocument();
    });

    it("offers a raw-source download bundle (no repo access needed)", () => {
      renderPage(ctx);
      const link = screen
        .getByText("Download proxy source (.zip)")
        .closest("a");
      expect(link).not.toBeNull();
      expect(link).toHaveAttribute("href", "/downloads/coa-mcp-proxy-src.zip");
      // Raw source, not a prebuilt binary — the auditability point.
      expect(screen.getByText(/TypeScript/).textContent).toMatch(/source/i);
    });

    it("surfaces the bundle SHA-256 so the download can be verified", async () => {
      renderPage(ctx);
      // Fetched from the .sha256 sidecar (stubbed), shown for sha256sum -c.
      expect(await screen.findByText(SHA_FIXTURE)).toBeInTheDocument();
      expect(
        screen.getByText(/SHA-256 \(verify your download\)/),
      ).toBeInTheDocument();
    });

    it("hides the checksum when the sidecar falls back to the SPA index.html", async () => {
      // A missing static file is served as index.html with HTTP 200 (not 404),
      // so ok=true must not be trusted: a non-hex body degrades to hidden
      // rather than rendering "<!DOCTYPE" as the checksum.
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => ({
          ok: true,
          text: async () =>
            '<!DOCTYPE html>\n<html lang="en"><head></head></html>',
        })),
      );
      renderPage(ctx);
      await Promise.resolve();
      expect(
        screen.queryByText(/SHA-256 \(verify your download\)/),
      ).not.toBeInTheDocument();
      expect(screen.queryByText(/DOCTYPE/)).not.toBeInTheDocument();
    });
  });

  describe("when mcpRuntimeArn is absent", () => {
    it("shows the not-configured guidance instead of a broken endpoint", () => {
      renderPage(baseCtx);
      expect(
        screen.getByText(/MCP endpoint not configured/i),
      ).toBeInTheDocument();
    });

    it("shows not-configured when the region is malformed (endpoint build fails)", () => {
      renderPage({
        ...baseCtx,
        region: "not a region",
        mcpRuntimeArn: arn,
      });
      expect(
        screen.getByText(/MCP endpoint not configured/i),
      ).toBeInTheDocument();
    });
  });
});
