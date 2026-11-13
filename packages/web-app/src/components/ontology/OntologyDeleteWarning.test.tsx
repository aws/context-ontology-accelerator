// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The warning is rendered standalone rather than through the delete modal:
 * driving a Cloudscape Modal open in happy-dom is flaky, and the copy is the
 * part worth pinning. These assertions keep the copy from drifting.
 */
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { OntologyDeleteWarning } from "./OntologyDeleteWarning";

describe("OntologyDeleteWarning", () => {
  it("frames a foundational removal as reversible", () => {
    render(<OntologyDeleteWarning ontologyType="foundational" name="FIBO" />);
    expect(
      screen.getByText(/Remove this foundational ontology\?/),
    ).toBeInTheDocument();
    expect(screen.getByText("FIBO")).toBeInTheDocument();
    expect(screen.getByText(/re-load it from the catalog/)).toBeInTheDocument();
  });

  it("warns that an induced ontology takes its proposals with it", () => {
    render(<OntologyDeleteWarning ontologyType="induced" name="hcp360" />);
    expect(
      screen.getByText(/This action cannot be undone/),
    ).toBeInTheDocument();
    expect(screen.getByText("hcp360")).toBeInTheDocument();
    // The induced-only half: deleting also removes the proposals tied to it.
    expect(
      screen.getByText(/deletes the proposals tied to it/),
    ).toBeInTheDocument();
    expect(screen.getByText(/re-running induction/)).toBeInTheDocument();
  });

  it("treats an uploaded ontology as unrecoverable user content", () => {
    render(<OntologyDeleteWarning ontologyType="user_uploaded" name="acme" />);
    expect(
      screen.getByText(/This action cannot be undone/),
    ).toBeInTheDocument();
    expect(screen.getByText(/registry entry/)).toBeInTheDocument();
    // Induced-specific copy must not leak into the uploaded case.
    expect(screen.queryByText(/re-running induction/)).not.toBeInTheDocument();
  });

  it("falls back to the unrecoverable wording for an unknown type", () => {
    render(<OntologyDeleteWarning ontologyType="something_else" name="x" />);
    expect(
      screen.getByText(/This action cannot be undone/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/re-load it from the catalog/),
    ).not.toBeInTheDocument();
  });
});
