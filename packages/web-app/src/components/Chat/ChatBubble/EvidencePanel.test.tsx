// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen } from "@testing-library/react";
import type { GraphContextItem } from "@app-types";
import { EvidencePanel } from "./EvidencePanel";

/**
 * A `graphContext` value the API contract forbids but a stored record or a
 * mismatched client can still produce. Only the runtime guard stands between it
 * and the renderer, which is exactly what these cases exercise.
 */
const malformed = (value: Record<string, unknown>): GraphContextItem =>
  value as unknown as GraphContextItem;

describe("EvidencePanel", () => {
  it("renders canonical graph context when no document citations exist", () => {
    render(
      <EvidencePanel
        supportingContent={[]}
        graphContext={{
          entities: [
            { uri: "e:amazon", label: "Amazon", type: "Company" },
            { uri: "e:walmart", label: "Walmart", type: "Company" },
          ],
          relationships: [
            {
              sourceUri: "e:amazon",
              predicateUri: "COMPETES_WITH",
              targetUri: "e:walmart",
            },
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByText("Graph evidence"));

    expect(screen.getByText("Graph context")).toBeInTheDocument();
    expect(screen.getAllByText("Amazon", { exact: false })).not.toHaveLength(0);
    expect(screen.getAllByText("Walmart", { exact: false })).not.toHaveLength(
      0,
    );
    expect(
      screen.getByText(/Amazon.*COMPETES_WITH.*Walmart/),
    ).toBeInTheDocument();
  });

  it("renders citations and relationships from a previous-runtime response", () => {
    // The exact Tier-3 shape the service emitted before #984: citations named only
    // by `sourceDoc` / `label`, and relationships nested under each entity with
    // snake_case keys. The web app ships before the service, so it has to render
    // this shape with nothing lost.
    render(
      <EvidencePanel
        supportingContent={[
          {
            chunkId: "c1",
            text: "Document evidence.",
            sourceDoc: "aws:tenant:source:document-42",
            label: "",
            relevanceScore: 0.9,
          },
        ]}
        graphContext={{
          entities: [
            {
              uri: "e:walmart",
              label: "Walmart",
              type: "Company",
              relationships: [
                {
                  predicate: "COMPETES_WITH",
                  source_uri: "e:amazon",
                  target_uri: "e:walmart",
                  target_label: "Walmart",
                },
              ],
            },
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByText("1 source cited"));

    expect(
      screen.getByText("aws:tenant:source:document-42"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Unknown source")).not.toBeInTheDocument();
    expect(screen.getByText(/COMPETES_WITH.*Walmart/)).toBeInTheDocument();
  });

  it("renders entity-nested legacy relationships from restored sessions", () => {
    render(
      <EvidencePanel
        supportingContent={[]}
        graphContext={{
          entities: [
            {
              uri: "e:amazon",
              label: "Amazon",
              type: "Company",
              relationships: [
                {
                  predicate: "COMPETES_WITH",
                  target_uri: "e:walmart",
                  target_label: "Walmart",
                },
              ],
            },
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByText("Graph evidence"));

    expect(
      screen.getByText(/Amazon.*COMPETES_WITH.*Walmart/),
    ).toBeInTheDocument();
  });

  it("renders a legacy top-level graph entity array from a restored session", () => {
    render(
      <EvidencePanel
        supportingContent={[]}
        graphContext={[
          {
            uri: "e:legacy",
            label: "Legacy entity",
            type: "Company",
            properties: {
              supportingStatements: JSON.stringify([
                "Legacy graph-only evidence.",
              ]),
            },
          },
        ]}
      />,
    );

    fireEvent.click(screen.getByText("Graph evidence"));

    expect(screen.getByText("Legacy entity")).toBeInTheDocument();
    expect(
      screen.getByText(/Legacy graph-only evidence\./),
    ).toBeInTheDocument();
  });

  // A malformed `graphContext` used to reach the renderer intact: `"abc".length`
  // read as truthy, the section called `.map` on a string, and the error boundary
  // replaced the entire chat panel.
  it.each([
    ["a string", "abc"],
    ["an object", {}],
  ])("renders nothing instead of throwing when entities is %s", (_, bad) => {
    const { container } = render(
      <EvidencePanel
        supportingContent={[]}
        graphContext={malformed({ entities: bad, relationships: bad })}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("still renders document citations when graph context is malformed", () => {
    render(
      <EvidencePanel
        supportingContent={[
          {
            chunkId: "c1",
            text: "Document evidence.",
            sourceDocumentName: "report.pdf",
            relevanceScore: 0.9,
          },
        ]}
        graphContext={malformed({ entities: "abc" })}
      />,
    );

    fireEvent.click(screen.getByText("1 source cited"));

    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    expect(screen.queryByText("Graph context")).not.toBeInTheDocument();
  });

  it("renders valid entities when nested entity fields are malformed", () => {
    render(
      <EvidencePanel
        graphContext={malformed({
          entities: [
            { uri: "e:1", label: "One", type: { x: 1 }, relationships: "abc" },
            { uri: "e:2", label: "Two", relationships: [null] },
          ],
        })}
      />,
    );

    fireEvent.click(screen.getByText("Graph evidence"));

    expect(screen.getByText("One")).toBeInTheDocument();
    expect(screen.getByText("Two")).toBeInTheDocument();
  });
});
