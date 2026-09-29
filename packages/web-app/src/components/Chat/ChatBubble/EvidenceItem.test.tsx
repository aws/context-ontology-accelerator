// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { EvidenceItem } from "./EvidenceItem";

describe("EvidenceItem", () => {
  it("renders the Smithy source document name ahead of opaque and legacy fields", () => {
    render(
      <EvidenceItem
        item={{
          chunkId: "chunk-1",
          text: "Evidence text",
          sourceDocumentId: "aws:tenant:source:document-42",
          sourceDocumentName: "annual-report.pdf",
          sourceDoc: "legacy-source.pdf",
          label: "Legacy label",
          relevanceScore: 0.91,
        }}
      />,
    );

    expect(screen.getByText("annual-report.pdf")).toBeInTheDocument();
    expect(screen.queryByText("legacy-source.pdf")).not.toBeInTheDocument();
    expect(screen.queryByText("Legacy label")).not.toBeInTheDocument();
  });

  it.each([
    [{ sourceDoc: "legacy-source.pdf" }, "legacy-source.pdf"],
    [{ label: "Legacy label" }, "Legacy label"],
    [
      {
        sourceDocumentName: "",
        sourceDoc: "legacy-source.pdf",
        sourceDocumentId: "aws:tenant:source:document-42",
      },
      "legacy-source.pdf",
    ],
    [
      { sourceDocumentId: "aws:tenant:source:document-42" },
      "aws:tenant:source:document-42",
    ],
  ])("renders a compatibility fallback for %o", (provenance, expected) => {
    render(<EvidenceItem item={{ text: "Evidence text", ...provenance }} />);

    expect(screen.getByText(expected)).toBeInTheDocument();
  });
});
