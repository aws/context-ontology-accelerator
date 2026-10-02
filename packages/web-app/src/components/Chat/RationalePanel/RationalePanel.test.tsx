// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RationalePanel } from "./RationalePanel";
import type { ChatMessage } from "@app-types";
import { normalizeResponse } from "@api-hooks/normalize-response";

function renderPanel(props: React.ComponentProps<typeof RationalePanel>) {
  return render(
    <MemoryRouter>
      <RationalePanel {...props} />
    </MemoryRouter>,
  );
}

describe("RationalePanel", () => {
  it("renders placeholder when message is undefined", () => {
    renderPanel({ message: undefined });
    expect(
      screen.getByText("Select a response to view its rationale."),
    ).toBeInTheDocument();
  });

  it("renders placeholder when message has no response", () => {
    const message: ChatMessage = {
      id: "msg-1",
      role: "assistant",
      content: "Hello",
      timestamp: new Date("2026-01-01T12:00:00Z"),
    };
    renderPanel({ message });
    expect(
      screen.getByText("Select a response to view its rationale."),
    ).toBeInTheDocument();
  });

  describe("with full response", () => {
    const message: ChatMessage = {
      id: "msg-2",
      role: "assistant",
      content: "The loss ratio is 71.3%",
      timestamp: new Date("2026-01-01T12:00:00Z"),
      response: {
        requestId: "req-abc-123",
        result: {
          tier: 2,
          confidence: { score: 0.92, rationale: "High confidence match" },
          synthesizedAnswer: "The loss ratio is 71.3%",
          trace: [
            { step: "t1.metric_match", status: "miss", durationMs: 15 },
            { step: "t2.vkg.context", status: "success", durationMs: 30 },
            { step: "t2.vkg.translate", status: "success", durationMs: 200 },
            { step: "t2.vkg.firewall", status: "success", durationMs: 5 },
            { step: "t2.vkg.execute", status: "success", durationMs: 120 },
          ],
          partial: false,
          dataSources: ["Athena", "Neptune"],
          supportingContent: [
            {
              chunkId: "chunk-1",
              text: "Loss ratio is calculated as claims paid divided by premium earned.",
              sourceDocumentId: "aws:tenant:source:underwriting-guide",
              sourceDocumentName: "underwriting-guide.pdf",
              relevanceScore: 0.88,
            },
          ],
          metadata: {
            namespace: "insurance-prod",
            modelId: "anthropic.claude-3-sonnet",
            principal: "user:jdoe@example.com",
          },
        },
      },
    };

    it("renders Resolution tier label consistently with the section header", () => {
      renderPanel({ message });
      // The Resolution field and the execution-plan section header now share the
      // same Tier-2 label (fixture trace has only t2.vkg.* steps → VKG strategy).
      expect(
        screen.getAllByText("Tier 2 — Structured Query (VKG (Ontop))").length,
      ).toBeGreaterThanOrEqual(2);
    });

    it("renders latency computed from trace steps", () => {
      renderPanel({ message });
      // 15 + 30 + 200 + 5 + 120 = 370
      expect(screen.getByText("370ms")).toBeInTheDocument();
    });

    it("renders model metadata", () => {
      renderPanel({ message });
      expect(screen.getByText("anthropic.claude-3-sonnet")).toBeInTheDocument();
    });

    it("renders principal metadata", () => {
      renderPanel({ message });
      expect(screen.getByText("user:jdoe@example.com")).toBeInTheDocument();
    });

    it("renders namespace metadata", () => {
      renderPanel({ message });
      expect(screen.getByText("insurance-prod")).toBeInTheDocument();
    });

    it("renders request ID", () => {
      renderPanel({ message });
      expect(screen.getByText("req-abc-123")).toBeInTheDocument();
    });

    it("renders confidence score as percentage badge", () => {
      renderPanel({ message });
      expect(screen.getByText("92%")).toBeInTheDocument();
    });

    it("renders confidence badge in green for high score (>=0.8)", () => {
      const { container } = renderPanel({ message });
      const badge = container.querySelector('[class*="badge-color-green"]');
      expect(badge).not.toBeNull();
      expect(badge?.textContent).toBe("92%");
    });

    it("renders confidence rationale", () => {
      renderPanel({ message });
      expect(screen.getByText("High confidence match")).toBeInTheDocument();
    });

    it("renders boundary control badges", () => {
      renderPanel({ message });
      expect(screen.getByText("Cedar: n/a")).toBeInTheDocument();
      expect(screen.getByText("SQL Firewall: ok")).toBeInTheDocument();
      expect(screen.getByText("Guardrails: n/a")).toBeInTheDocument();
    });

    it("renders data source badges", () => {
      renderPanel({ message });
      expect(screen.getByText("Athena")).toBeInTheDocument();
      expect(screen.getByText("Neptune")).toBeInTheDocument();
    });

    it("renders supporting content section", () => {
      renderPanel({ message });
      expect(screen.getByText("Supporting content (1)")).toBeInTheDocument();
      expect(screen.getByText("underwriting-guide.pdf")).toBeInTheDocument();
    });

    it("renders the legacy sourceDoc fallback for older Tier-3 responses", () => {
      const legacyMessage: ChatMessage = {
        id: "msg-legacy-evidence",
        role: "assistant",
        content: "Legacy evidence",
        timestamp: new Date("2026-01-01T12:00:00Z"),
        response: {
          requestId: "req-legacy-evidence",
          result: {
            tier: 3,
            confidence: { score: 0.8, rationale: "Legacy response" },
            trace: [],
            partial: false,
            supportingContent: [
              {
                chunkId: "legacy-chunk",
                text: "Legacy evidence text",
                sourceDocumentId: "aws:tenant:source:legacy-guide",
                sourceDocumentName: "",
                sourceDoc: "legacy-guide.pdf",
                relevanceScore: 0.8,
              },
            ],
          },
        },
      };

      renderPanel({ message: legacyMessage });
      expect(screen.getByText("legacy-guide.pdf")).toBeInTheDocument();
    });

    it("renders execution plan with step names", () => {
      renderPanel({ message });
      expect(screen.getByText("Execution plan")).toBeInTheDocument();
      expect(screen.getByText("Match metric")).toBeInTheDocument();
      expect(screen.getByText("Assemble ontology context")).toBeInTheDocument();
      expect(screen.getByText("Generate SPARQL")).toBeInTheDocument();
      expect(screen.getByText("SQL Firewall")).toBeInTheDocument();
      expect(screen.getByText("Execute SQL via VKG")).toBeInTheDocument();
    });

    it("renders 'Open full trace' link", () => {
      renderPanel({ message });
      expect(screen.getByText("Open full trace")).toBeInTheDocument();
    });

    it("renders the VKG translate→SQL failure step that triggers the retry", () => {
      // Exact shape from a live sequential trace: NL→SQL errored, then VKG
      // generated/validated SPARQL, the SPARQL→SQL translation failed, retried,
      // and failed again. The translate failure step must be visible.
      const retryMessage: ChatMessage = {
        id: "msg-retry",
        role: "assistant",
        content: "answer",
        timestamp: new Date("2026-01-01T12:00:00Z"),
        response: {
          requestId: "req-retry",
          result: {
            tier: 3,
            confidence: { score: 0.4, rationale: "" },
            trace: [
              { step: "t1.metric_match", status: "miss", durationMs: 0 },
              { step: "query.embed", status: "success", durationMs: 100 },
              { step: "t2.sql.generate", status: "error", durationMs: 1500 },
              { step: "t2.vkg.context", status: "success", durationMs: 1300 },
              { step: "t2.vkg.translate", status: "success", durationMs: 8800 },
              { step: "t2.vkg.validate", status: "success", durationMs: 67 },
              {
                step: "t2.vkg.compile",
                status: "error",
                durationMs: 200,
                detail: { error: "vkg_translation_error" },
              },
              { step: "t2.vkg.context", status: "success", durationMs: 1200 },
              {
                step: "t2.vkg.translate",
                status: "success",
                durationMs: 10100,
              },
              { step: "t2.vkg.validate", status: "success", durationMs: 74 },
              {
                step: "t2.vkg.retry",
                status: "failed",
                durationMs: 14400,
                detail: { reason: "retry also failed: vkg_translation_error" },
              },
              { step: "t3.synthesize", status: "success", durationMs: 4400 },
            ],
            partial: false,
          },
        },
      };
      renderPanel({ message: retryMessage });
      // The step label and its humanized failure detail must both render.
      expect(
        screen.getAllByText("Translate SPARQL → SQL").length,
      ).toBeGreaterThan(0);
      expect(
        screen.getByText(/Ontop could not translate SPARQL to SQL/),
      ).toBeInTheDocument();
    });

    it("renders compile step via the full normalize pipeline (both strategies ran)", () => {
      // Mirrors the live wire payload: NL→SQL fully executed (0 rows) AND VKG
      // ran with a translate failure + retry. Goes through normalizeResponse,
      // exactly like the WebSocket `done` path in production.
      const rawWire = {
        result: {
          tier: 3,
          confidence: { score: 0.2, rationale: "" },
          trace: [
            { step: "t1.metric_match", status: "miss", durationMs: 0 },
            { step: "query.embed", status: "success", durationMs: 116 },
            {
              step: "t2.sql.generate",
              status: "success",
              durationMs: 5700,
              detail: { confidence: 0.2, tables: ["cards"] },
            },
            { step: "t2.sql.authorize", status: "allow", durationMs: 0 },
            { step: "t2.sql.firewall", status: "success", durationMs: 0 },
            { step: "t2.sql.execute", status: "error", durationMs: 3900 },
            { step: "t2.vkg.context", status: "success", durationMs: 1300 },
            { step: "t2.vkg.translate", status: "success", durationMs: 9600 },
            { step: "t2.vkg.validate", status: "success", durationMs: 7 },
            {
              step: "t2.vkg.compile",
              status: "error",
              durationMs: 200,
              detail: { error: "vkg_translation_error" },
            },
            { step: "t2.vkg.context", status: "success", durationMs: 1200 },
            { step: "t2.vkg.translate", status: "success", durationMs: 9200 },
            { step: "t2.vkg.validate", status: "success", durationMs: 14 },
            {
              step: "t2.vkg.retry",
              status: "failed",
              durationMs: 13500,
              detail: { reason: "retry also failed: vkg_translation_error" },
            },
            { step: "t3.synthesize", status: "success", durationMs: 4400 },
          ],
          partial: false,
        },
        requestId: "req-wire",
      };
      const normalized = normalizeResponse(rawWire);
      expect(normalized).not.toBeNull();
      const msg: ChatMessage = {
        id: "msg-wire",
        role: "assistant",
        content: "answer",
        timestamp: new Date("2026-01-01T12:00:00Z"),
        response: normalized ?? undefined,
      };
      renderPanel({ message: msg });
      expect(
        screen.getAllByText("Translate SPARQL → SQL").length,
      ).toBeGreaterThan(0);
    });
  });

  describe("boundary controls - blocked state", () => {
    const blockedMessage: ChatMessage = {
      id: "msg-3",
      role: "assistant",
      content: "Blocked",
      timestamp: new Date("2026-01-01T12:00:00Z"),
      response: {
        requestId: "req-blocked",
        result: {
          tier: 0,
          confidence: { score: 0, rationale: "Blocked by guardrail" },
          guardrailBlocked: true,
          trace: [{ step: "t2.vkg.firewall", status: "error", durationMs: 2 }],
          partial: false,
        },
      },
    };

    it("renders guardrails as blocked", () => {
      renderPanel({ message: blockedMessage });
      expect(screen.getByText("Guardrails: blocked")).toBeInTheDocument();
    });

    it("renders confidence badge in red for low score (<0.5)", () => {
      const { container } = renderPanel({ message: blockedMessage });
      const badge = container.querySelector('[class*="badge-color-red"]');
      expect(badge).not.toBeNull();
      expect(badge?.textContent).toBe("0%");
    });

    it("renders SQL Firewall as blocked when step has error status", () => {
      renderPanel({ message: blockedMessage });
      expect(screen.getByText("SQL Firewall: blocked")).toBeInTheDocument();
    });
  });

  describe("boundary controls - guardrail passed state (Tier-3 answered)", () => {
    // Regression guard for bug #836: a Tier-3 answer that ran through a
    // Bedrock guardrail and passed must render "Guardrails: passed", not
    // "n/a". The trace step is the single source of truth.
    const passedMessage: ChatMessage = {
      id: "msg-t3-passed",
      role: "assistant",
      content: "Claims SLA is 10 business days per Policy v2.3.",
      timestamp: new Date("2026-01-01T12:00:00Z"),
      response: {
        requestId: "req-t3",
        result: {
          tier: 3,
          confidence: { score: 0.85, rationale: "High confidence match" },
          guardrailBlocked: false,
          trace: [
            { step: "t3.vector_search", status: "success", durationMs: 40 },
            { step: "t3.graph_traverse", status: "success", durationMs: 55 },
            { step: "t3.synthesize", status: "success", durationMs: 800 },
            { step: "t3.guardrail", status: "success", durationMs: 0 },
          ],
          partial: false,
        },
      },
    };

    it("renders guardrails as passed when the t3.guardrail step succeeded", () => {
      renderPanel({ message: passedMessage });
      expect(screen.getByText("Guardrails: passed")).toBeInTheDocument();
    });
  });

  describe("missing optional fields", () => {
    const minimalMessage: ChatMessage = {
      id: "msg-4",
      role: "assistant",
      content: "Minimal",
      timestamp: new Date("2026-01-01T12:00:00Z"),
      response: {
        result: {
          tier: 1,
          confidence: { score: 0.5, rationale: "Low confidence" },
          trace: [],
          partial: false,
        },
      },
    };

    it("renders dash for missing model", () => {
      renderPanel({ message: minimalMessage });
      // Model, Principal, Namespace, Request ID all show dashes
      const dashes = screen.getAllByText("—");
      expect(dashes.length).toBeGreaterThanOrEqual(4);
    });

    it("renders confidence badge in amber for medium score (0.5-0.79)", () => {
      const { container } = renderPanel({ message: minimalMessage });
      const badge = container.querySelector(
        '[class*="badge-color-severity-medium"]',
      );
      expect(badge).not.toBeNull();
      expect(badge?.textContent).toBe("50%");
    });

    it("does not render data sources section when empty", () => {
      renderPanel({ message: minimalMessage });
      expect(screen.queryByText("Data sources")).not.toBeInTheDocument();
    });

    it("does not render execution plan when trace is empty", () => {
      renderPanel({ message: minimalMessage });
      expect(screen.queryByText("Execution plan")).not.toBeInTheDocument();
    });

    it("does not render supporting content when not provided", () => {
      renderPanel({ message: minimalMessage });
      expect(screen.queryByText(/Supporting content/)).not.toBeInTheDocument();
    });
  });
});
