// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { TracePanel } from "./TracePanel";
import type { TraceStep } from "@app-types";

describe("TracePanel", () => {
  it("renders nothing when no steps and not live", () => {
    const { container } = render(<TracePanel steps={[]} isLive={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows user-meaningful steps and hides internal plumbing on the happy path", () => {
    const steps: TraceStep[] = [
      { step: "t1.metric_match", status: "miss", durationMs: 0 },
      { step: "query.embed", status: "success", durationMs: 100 },
      { step: "t2.sql.generate", status: "success", durationMs: 500 },
      { step: "t2.sql.authorize", status: "allow", durationMs: 1 },
    ];
    render(<TracePanel steps={steps} isLive={false} />);
    expect(screen.getByText("Metric Match")).toBeInTheDocument();
    expect(screen.getByText("Sql Generate")).toBeInTheDocument();
    expect(screen.queryByText("Embed")).not.toBeInTheDocument();
    expect(screen.queryByText("Sql Authorize")).not.toBeInTheDocument();
  });

  it("reveals a hidden step when it is denied/errored", () => {
    const steps: TraceStep[] = [
      { step: "t2.sql.authorize", status: "denied", durationMs: 2 },
    ];
    render(<TracePanel steps={steps} isLive={false} />);
    expect(screen.getByText("Sql Authorize")).toBeInTheDocument();
  });

  it("renders the t3.retrieve group with wall time and nested parallel children", () => {
    const steps: TraceStep[] = [
      {
        step: "t3.retrieve",
        status: "success",
        durationMs: 0,
        wallMs: 790,
        detail: { sources: ["vector_search", "graph_traverse"] },
      },
      {
        step: "t3.vector_search",
        status: "success",
        durationMs: 610,
        toolUsed: "opensearch",
        parallelGroup: "t3.retrieve",
      },
      {
        step: "t3.graph_traverse",
        status: "success",
        durationMs: 790,
        toolUsed: "neptune",
        parallelGroup: "t3.retrieve",
      },
    ];
    render(<TracePanel steps={steps} isLive={false} />);
    expect(screen.getByText("790ms wall")).toBeInTheDocument();
    expect(screen.getByText("Vector Search")).toBeInTheDocument();
    expect(screen.getByText("Graph Traverse")).toBeInTheDocument();
  });

  it("hides a successful t3.guardrail row (the badge surfaces the pass, not the timeline)", () => {
    const steps: TraceStep[] = [
      { step: "t3.synthesize", status: "success", durationMs: 800 },
      { step: "t3.guardrail", status: "success", durationMs: 0 },
    ];
    render(<TracePanel steps={steps} isLive={false} />);
    expect(screen.getByText("Synthesize")).toBeInTheDocument();
    expect(screen.queryByText("Guardrail")).not.toBeInTheDocument();
  });

  it("reveals the t3.guardrail row when the guardrail denied the response", () => {
    const steps: TraceStep[] = [
      { step: "t3.guardrail", status: "denied", durationMs: 0 },
    ];
    render(<TracePanel steps={steps} isLive={false} />);
    expect(screen.getByText("Guardrail")).toBeInTheDocument();
  });

  it("counts only visible steps in the header", () => {
    const steps: TraceStep[] = [
      { step: "t1.metric_match", status: "miss", durationMs: 0 },
      { step: "query.embed", status: "success", durationMs: 100 }, // hidden
      { step: "t2.sql.generate", status: "success", durationMs: 500 },
    ];
    render(<TracePanel steps={steps} isLive={false} />);
    expect(
      screen.getByText(/Processing trace \(2 steps\)/),
    ).toBeInTheDocument();
  });
});
