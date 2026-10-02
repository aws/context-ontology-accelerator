// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { GraphContextSection } from "./GraphContextSection";

describe("GraphContextSection", () => {
  it("renders canonical graph-only supporting statements", () => {
    render(
      <GraphContextSection
        context={{
          entities: [
            {
              uri: "urn:acme",
              label: "Acme",
              type: "Company",
              properties: {
                supportingStatements: JSON.stringify([
                  "Acme acquired Example Corp.",
                  "Acme operates in retail.",
                ]),
              },
            },
          ],
        }}
      />,
    );

    expect(
      screen.getByText(/Acme acquired Example Corp\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/Acme operates in retail\./)).toBeInTheDocument();
  });

  it("handles legacy array and plain-text supporting statements safely", () => {
    const { rerender } = render(
      <GraphContextSection
        context={{
          entities: [
            {
              uri: "urn:legacy",
              label: "Legacy entity",
              properties: {
                supporting_statements: ["Legacy graph statement.", 42, null],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText(/Legacy graph statement\./)).toBeInTheDocument();
    expect(screen.queryByText("42")).not.toBeInTheDocument();

    rerender(
      <GraphContextSection
        context={{
          entities: [
            {
              uri: "urn:plain",
              label: "Plain entity",
              properties: {
                supportingStatements: "One restored plain-text statement.",
              },
            },
          ],
        }}
      />,
    );

    expect(
      screen.getByText(/One restored plain-text statement\./),
    ).toBeInTheDocument();
  });
});
