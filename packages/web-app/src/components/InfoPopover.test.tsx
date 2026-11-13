// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { InfoPopover } from "./InfoPopover";

describe("InfoPopover", () => {
  it("renders a trigger with an accessible label and hides content until opened", () => {
    render(
      <InfoPopover header="SHACL constraints" ariaLabel="About SHACL">
        Data-quality rules for this ontology.
      </InfoPopover>,
    );
    expect(screen.getByLabelText("About SHACL")).toBeInTheDocument();
    expect(
      screen.queryByText("Data-quality rules for this ontology."),
    ).not.toBeInTheDocument();
  });

  it("shows the header and body content when the trigger is clicked", () => {
    render(
      <InfoPopover header="SHACL constraints">
        Data-quality rules for this ontology.
      </InfoPopover>,
    );
    fireEvent.click(screen.getByLabelText("SHACL constraints"));
    expect(
      screen.getByText("Data-quality rules for this ontology."),
    ).toBeInTheDocument();
    expect(screen.getByText("SHACL constraints")).toBeInTheDocument();
  });

  it("falls back to a default aria-label when neither ariaLabel nor header is given", () => {
    render(<InfoPopover>Body</InfoPopover>);
    expect(screen.getByLabelText("More info")).toBeInTheDocument();
  });
});
