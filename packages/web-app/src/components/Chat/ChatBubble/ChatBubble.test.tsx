// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { ChatBubble } from "./ChatBubble";

describe("ChatBubble", () => {
  const baseProps = {
    content: "Hello world",
    timestamp: new Date("2026-01-01T12:00:00Z"),
  };

  describe("user messages", () => {
    it("renders user content as plain text", () => {
      render(<ChatBubble role="user" state="success" {...baseProps} />);
      expect(screen.getByText("Hello world")).toBeInTheDocument();
    });
  });

  describe("assistant messages - loading state", () => {
    it("renders LiveSteps with 'Resolving query' when no steps", () => {
      render(<ChatBubble role="assistant" state="loading" {...baseProps} />);
      expect(screen.getByText("Resolving query")).toBeInTheDocument();
    });

    it("renders 'Resolving query' with current step label when steps are provided", () => {
      render(
        <ChatBubble
          role="assistant"
          state="loading"
          {...baseProps}
          liveTraceSteps={[
            { step: "t1.metric_match", status: "success", durationMs: 42 },
          ]}
        />,
      );
      expect(
        screen.getByText("Resolving query · Searching metric catalog"),
      ).toBeInTheDocument();
    });

    it("shows the last step label when multiple steps provided", () => {
      render(
        <ChatBubble
          role="assistant"
          state="loading"
          {...baseProps}
          liveTraceSteps={[
            { step: "t1.metric_match", status: "miss", durationMs: 10 },
            { step: "t3.vector_search", status: "active", durationMs: 0 },
          ]}
        />,
      );
      expect(
        screen.getByText("Resolving query · Searching knowledge base"),
      ).toBeInTheDocument();
    });

    it("uses StatusIndicator with loading type", () => {
      const { container } = render(
        <ChatBubble role="assistant" state="loading" {...baseProps} />,
      );
      // StatusIndicator renders a span with the loading state
      expect(container.textContent).toContain("Resolving query");
    });
  });

  describe("assistant messages - error state", () => {
    it("renders error message with StatusIndicator", () => {
      render(
        <ChatBubble
          role="assistant"
          state="error"
          {...baseProps}
          errorMessage="Connection failed"
        />,
      );
      expect(screen.getByText("Connection failed")).toBeInTheDocument();
    });

    it("falls back to content when no errorMessage provided", () => {
      render(
        <ChatBubble
          role="assistant"
          state="error"
          content="Fallback error"
          timestamp={baseProps.timestamp}
        />,
      );
      expect(screen.getByText("Fallback error")).toBeInTheDocument();
    });
  });

  describe("assistant messages - guardrail blocked state", () => {
    const guardrailResponse = {
      tier: 0,
      confidence: 0,
      rationale: "Prompt injection detected",
      synthesizedAnswer:
        "Blocked by Guardrails. Event logged for red-team review.",
      guardrailBlocked: true,
    };

    it("renders guardrail blocked message with warning indicator", () => {
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={guardrailResponse}
        />,
      );
      expect(
        screen.getByText(guardrailResponse.synthesizedAnswer),
      ).toBeInTheDocument();
    });

    it("renders the blocked answer through Markdown (headings/lists/code) rather than raw text", () => {
      const markdownAnswer =
        "## Analysis\n\n- point one\n- point two\n\nSee `columnDenylist` for details.";
      const { container } = render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={{ ...guardrailResponse, synthesizedAnswer: markdownAnswer }}
        />,
      );
      // Markdown must be parsed into real elements — the bug was raw "##"/"-"/"`" text.
      expect(container.querySelector("h2")?.textContent).toBe("Analysis");
      expect(container.querySelectorAll("ul > li").length).toBe(2);
      expect(container.querySelector("code")?.textContent).toBe(
        "columnDenylist",
      );
      // The literal Markdown syntax must NOT appear as visible text.
      expect(container.textContent).not.toContain("## Analysis");
    });
  });

  describe("assistant messages - success state", () => {
    const response = {
      tier: 3,
      confidence: 0.95,
      rationale: "Resolved via ontology",
      synthesizedAnswer: "The loss ratio is 71.3%",
      latencyMs: 120,
    };

    it("renders synthesized answer via Markdown", () => {
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={response}
        />,
      );
      expect(screen.getByText("The loss ratio is 71.3%")).toBeInTheDocument();
    });

    it("renders table when resultRows are present", () => {
      const tableResponse = {
        ...response,
        synthesizedAnswer: "47 open claims",
        resultRows: [
          { claim_id: "CL-44201", status: "Open", amount: "$14,200" },
          { claim_id: "CL-44318", status: "Open", amount: "$6,800" },
        ],
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={tableResponse}
        />,
      );
      expect(screen.getByText("CL-44201")).toBeInTheDocument();
      expect(screen.getByText("$14,200")).toBeInTheDocument();
    });

    it("renders suppressed rows count", () => {
      const suppressedResponse = {
        ...response,
        synthesizedAnswer: "47 open claims",
        suppressedRows: 14333,
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={suppressedResponse}
        />,
      );
      expect(
        screen.getByText(/14,333 suppressed by SQL Firewall/),
      ).toBeInTheDocument();
    });

    it("renders 'Show trace' button when onRationaleClick is provided", () => {
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={response}
          onRationaleClick={() => {}}
        />,
      );
      expect(screen.getByText("Show trace")).toBeInTheDocument();
    });

    it("does not render 'Show trace' button when onRationaleClick is not provided", () => {
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={response}
        />,
      );
      expect(screen.queryByText("Show trace")).not.toBeInTheDocument();
    });

    it("renders copy button", () => {
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={response}
        />,
      );
      expect(screen.getByLabelText("Copy answer")).toBeInTheDocument();
    });

    it("renders 'Compiled artifacts' section when sparqlGenerated is present", () => {
      const artifactsResponse = {
        ...response,
        sparqlGenerated: "SELECT ?x WHERE { ?x rdf:type :Claim }",
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(screen.getByText("Compiled artifacts")).toBeInTheDocument();
      expect(
        screen.getByText("SELECT ?x WHERE { ?x rdf:type :Claim }"),
      ).toBeInTheDocument();
    });

    it("renders 'Compiled artifacts' section when queryUsed is present", () => {
      const artifactsResponse = {
        ...response,
        queryUsed: "SELECT * FROM claims WHERE status = 'Open'",
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(screen.getByText("Compiled artifacts")).toBeInTheDocument();
      expect(
        screen.getByText("SELECT * FROM claims WHERE status = 'Open'"),
      ).toBeInTheDocument();
    });

    it("renders both SPARQL and SQL labels in compiled artifacts", () => {
      const artifactsResponse = {
        ...response,
        sparqlGenerated: "SELECT ?x WHERE { ?x rdf:type :Claim }",
        queryUsed: "SELECT * FROM claims",
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(screen.getByText("SPARQL")).toBeInTheDocument();
      expect(screen.getByText("SQL")).toBeInTheDocument();
    });

    it("shows Tier 2 caption with the executing engine when sparqlGenerated is present", () => {
      const artifactsResponse = {
        ...response,
        sparqlGenerated: "SELECT ?x WHERE { ?x rdf:type :Claim }",
        queryUsed: "SELECT * FROM claims",
        trace: [
          {
            step: "t2.sql.execute",
            status: "success",
            durationMs: 5,
            detail: { engine: "athena" },
          },
        ],
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(
        screen.getByText(
          "The SPARQL the LLM emitted (Tier 2) and the SQL Athena executed.",
        ),
      ).toBeInTheDocument();
    });

    it("drops the '(Tier 2)' framing in the caption for Deep Reasoning artifacts", () => {
      // Deep Reasoning also emits SPARQL+SQL but not via the Tier-2 cascade, so
      // labelling its artifacts "(Tier 2)" is wrong (#1166 Cut #3). Same fixture
      // as the Tier-2 case, only executionMode flipped.
      const artifactsResponse = {
        ...response,
        sparqlGenerated: "SELECT ?x WHERE { ?x rdf:type :Claim }",
        queryUsed: "SELECT * FROM claims",
        executionMode: "deep-reasoning" as const,
        trace: [
          {
            step: "t2.sql.execute",
            status: "success",
            durationMs: 5,
            detail: { engine: "athena" },
          },
        ],
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(
        screen.getByText(
          "The SPARQL and SQL Deep Reasoning generated and Athena executed.",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByText(/\(Tier 2\)/)).not.toBeInTheDocument();
    });

    it("shows SQL-only caption naming the ATHENA engine from the trace tag", () => {
      const artifactsResponse = {
        ...response,
        tier: 1,
        queryUsed: "SELECT COUNT(*) FROM claims",
        trace: [
          {
            step: "t1.execute",
            status: "success",
            durationMs: 5,
            detail: { engine: "athena" },
          },
        ],
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(screen.getByText("The SQL Athena executed.")).toBeInTheDocument();
      expect(
        screen.queryByText(/SPARQL/i, { selector: "*" }),
      ).not.toBeInTheDocument();
    });

    it("names the REDSHIFT engine in the caption when the trace engine tag is redshift", () => {
      const artifactsResponse = {
        ...response,
        tier: 1,
        queryUsed: "SELECT COUNT(*) FROM claims",
        trace: [
          {
            step: "t1.execute",
            status: "success",
            durationMs: 5,
            detail: { engine: "redshift" },
          },
        ],
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(
        screen.getByText("The SQL Redshift executed."),
      ).toBeInTheDocument();
    });

    it("names the JDBC engine in the caption when the trace engine tag is jdbc", () => {
      const artifactsResponse = {
        ...response,
        tier: 1,
        queryUsed: "SELECT COUNT(*) FROM claims",
        trace: [
          {
            step: "t2.sql.execute",
            status: "success",
            durationMs: 5,
            detail: { engine: "jdbc" },
          },
        ],
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(
        screen.getByText("The SQL the JDBC engine executed."),
      ).toBeInTheDocument();
    });

    it("falls back to a neutral engine label when no trace engine tag is present", () => {
      const artifactsResponse = {
        ...response,
        tier: 1,
        queryUsed: "SELECT COUNT(*) FROM claims",
      };
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={artifactsResponse}
        />,
      );
      expect(
        screen.getByText("The SQL the query engine executed."),
      ).toBeInTheDocument();
      expect(
        screen.queryByText(/SPARQL/i, { selector: "*" }),
      ).not.toBeInTheDocument();
    });

    it("does not render compiled artifacts when neither sparql nor query present", () => {
      render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={response}
        />,
      );
      expect(screen.queryByText("Compiled artifacts")).not.toBeInTheDocument();
    });

    it("returns null when response is undefined in success state", () => {
      const { container } = render(
        <ChatBubble role="assistant" state="success" {...baseProps} />,
      );
      expect(container.firstChild).toBeNull();
    });
  });

  describe("markdown rendering", () => {
    const markdownResponse = (answer: string) => ({
      tier: 3,
      confidence: 0.9,
      rationale: "test",
      synthesizedAnswer: answer,
    });

    it("renders GFM tables as HTML table elements", () => {
      const tableMarkdown =
        "| Metric | Value |\n| --- | --- |\n| Loss Ratio | 71% |";
      const { container } = render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={markdownResponse(tableMarkdown)}
        />,
      );
      const table = container.querySelector("table");
      expect(table).not.toBeNull();
      expect(table?.querySelector("th")?.textContent).toBe("Metric");
      expect(table?.querySelector("td")?.textContent).toBe("Loss Ratio");
    });

    it("renders strikethrough via GFM", () => {
      const { container } = render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={markdownResponse("~~deprecated~~ current")}
        />,
      );
      expect(container.querySelector("del")?.textContent).toBe("deprecated");
    });

    it("renders bullet lists as ul elements", () => {
      const listMarkdown = "Key findings:\n\n- Item one\n- Item two";
      const { container } = render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={markdownResponse(listMarkdown)}
        />,
      );
      const items = container.querySelectorAll("ul > li");
      expect(items.length).toBe(2);
      expect(items[0]?.textContent).toBe("Item one");
    });

    it("renders headings with correct hierarchy", () => {
      const headingMarkdown = "## Summary\n\nContent here";
      const { container } = render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={markdownResponse(headingMarkdown)}
        />,
      );
      expect(container.querySelector("h2")?.textContent).toBe("Summary");
    });

    it("wraps markdown output in TextContent for Cloudscape styling", () => {
      const { container } = render(
        <ChatBubble
          role="assistant"
          state="success"
          {...baseProps}
          response={markdownResponse("Simple text")}
        />,
      );
      // TextContent renders a div with a specific Cloudscape class
      const textContentDiv = container.querySelector('[class*="text-content"]');
      expect(textContentDiv).not.toBeNull();
    });
  });
});
