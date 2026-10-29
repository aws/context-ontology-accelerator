// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import { ConstraintPanel } from "./ConstraintPanel";

// The SHACL panel's "Beta" badge goes through the catalogue.

function renderIn(locale: "en" | "ja") {
  window.localStorage.setItem("scl.locale", locale);
  return render(
    <LocaleProvider>
      <ConstraintPanel
        config={{ classes: [] }}
        onConfigChange={() => {}}
        onInfer={() => {}}
        onValidate={() => {}}
        customTurtle=""
        onCustomTurtleChange={() => {}}
      />
    </LocaleProvider>,
  );
}

describe("ConstraintPanel — Beta badge", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("shows the badge in Japanese", async () => {
    renderIn("ja");
    expect(await screen.findByText("ベータ")).toBeInTheDocument();
    expect(screen.queryByText("Beta")).not.toBeInTheDocument();
  });

  it("keeps Beta in English", async () => {
    renderIn("en");
    expect(await screen.findByText("Beta")).toBeInTheDocument();
  });
});
