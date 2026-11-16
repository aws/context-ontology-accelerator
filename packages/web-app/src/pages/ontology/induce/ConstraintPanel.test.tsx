// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { ConstraintPanel } from "./ConstraintPanel";

const downloadTextFile = vi.fn();
vi.mock("@utils/download", () => ({
  downloadTextFile: (...args: unknown[]) => downloadTextFile(...args),
}));

const CONFIG = {
  classes: [
    {
      class_uri: "http://ex.org/o#Policy",
      class_name: "Policy",
      constraints: [
        {
          property_path: "http://ex.org/o#premium",
          property_name: "premium",
          constraint_type: "required",
          enabled: true,
          params: {},
          source: "db_constraint",
          description: "Premium is required",
        },
      ],
    },
  ],
};

function renderPanel(overrides: Record<string, unknown> = {}) {
  return render(
    <ConstraintPanel
      config={CONFIG}
      onConfigChange={() => {}}
      onInfer={() => {}}
      onValidate={() => {}}
      customTurtle=""
      onCustomTurtleChange={() => {}}
      {...overrides}
    />,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("ConstraintPanel — Export SHACL", () => {
  it("disables Export SHACL when no compiled shapes are cached", () => {
    renderPanel({ compiledShapes: null });
    const exportBtn = screen.getByText("Export SHACL").closest("button");
    expect(exportBtn).toBeTruthy();
    expect(exportBtn).toBeDisabled();
  });

  it("enables Export and downloads the compiled shapes with a .shapes.ttl name", () => {
    renderPanel({
      compiledShapes: "@prefix sh: <http://www.w3.org/ns/shacl#> .",
      proposalName: "My Policy Ontology",
    });
    const exportBtn = screen.getByText("Export SHACL").closest("button")!;
    expect(exportBtn).not.toBeDisabled();
    fireEvent.click(exportBtn);
    expect(downloadTextFile).toHaveBeenCalledWith(
      "My_Policy_Ontology.shapes.ttl",
      "@prefix sh: <http://www.w3.org/ns/shacl#> .",
    );
  });

  it("falls back to a shapes.* filename when no proposal name is given", () => {
    renderPanel({ compiledShapes: "sh:x" });
    fireEvent.click(screen.getByText("Export SHACL").closest("button")!);
    expect(downloadTextFile).toHaveBeenCalledWith("shapes.shapes.ttl", "sh:x");
  });

  it("explains that natural-language custom rules are not compiled to SHACL", () => {
    const { container } = renderPanel();
    expect(container.textContent).toContain(
      "Natural-language rules are recorded for reviewer context but are not automatically compiled into SHACL",
    );
  });

  it("offers a SHACL info affordance that explains what constraints are on open", () => {
    renderPanel();
    // The circled-i trigger is present and labelled; its explanation is hidden
    // until opened.
    const trigger = screen.getByLabelText("About SHACL constraints");
    expect(trigger).toBeInTheDocument();
    expect(
      screen.queryByText(/data-quality rules for this ontology/i),
    ).not.toBeInTheDocument();
    fireEvent.click(trigger);
    expect(
      screen.getByText(/data-quality rules for this ontology/i),
    ).toBeInTheDocument();
  });
});
