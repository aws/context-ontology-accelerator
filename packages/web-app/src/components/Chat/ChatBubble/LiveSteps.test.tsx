// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { LiveSteps } from "./LiveSteps";

describe("LiveSteps", () => {
  it("renders 'Resolving query' when steps is undefined", () => {
    render(<LiveSteps steps={undefined} />);
    expect(screen.getByText("Resolving query")).toBeInTheDocument();
  });

  it("renders 'Resolving query' when steps is empty array", () => {
    render(<LiveSteps steps={[]} />);
    expect(screen.getByText("Resolving query")).toBeInTheDocument();
  });

  it("renders 'Resolving query · {label}' with the last step's label", () => {
    render(
      <LiveSteps
        steps={[{ step: "t1.metric_match", status: "success", durationMs: 42 }]}
      />,
    );
    expect(
      screen.getByText("Resolving query · Searching metric catalog"),
    ).toBeInTheDocument();
  });

  it("shows the current (last) step label when multiple steps provided", () => {
    render(
      <LiveSteps
        steps={[
          { step: "t1.metric_match", status: "miss", durationMs: 10 },
          { step: "t2.vkg.context", status: "success", durationMs: 25 },
          { step: "t3.vector_search", status: "active", durationMs: 0 },
        ]}
      />,
    );
    expect(
      screen.getByText("Resolving query · Searching knowledge base"),
    ).toBeInTheDocument();
  });

  it("formats unknown step names by replacing underscores and capitalizing", () => {
    render(
      <LiveSteps
        steps={[{ step: "custom_step_name", status: "active", durationMs: 0 }]}
      />,
    );
    expect(
      screen.getByText("Resolving query · Custom Step Name"),
    ).toBeInTheDocument();
  });

  it("maps known step names to friendly labels", () => {
    const knownSteps = [
      { step: "t2.vkg.translate", expected: "Translating to SPARQL" },
      { step: "t2.sql.generate", expected: "Generating SQL" },
      { step: "t2.vkg.execute", expected: "Running knowledge graph query" },
      { step: "t3.synthesize", expected: "Synthesizing answer" },
    ];

    for (const { step, expected } of knownSteps) {
      const { unmount } = render(
        <LiveSteps steps={[{ step, status: "active", durationMs: 0 }]} />,
      );
      expect(
        screen.getByText(`Resolving query · ${expected}`),
      ).toBeInTheDocument();
      unmount();
    }
  });
});
