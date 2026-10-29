// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { LocaleProvider } from "../../i18n/LocaleProvider";
import {
  OntologyGraphFlow,
  type GraphEdgeData,
  type GraphNodeData,
} from "./OntologyGraphFlow";

// React Flow's control buttons carry aria-labels that default to English. The
// graph passes them through the catalogue so a screen reader announces them in
// the UI language.

class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver =
  ResizeObserver as unknown as typeof globalThis.ResizeObserver;

const nodes: GraphNodeData[] = [
  { id: "ex:Claim", label: "Claim", kind: "class" },
  { id: "ex:Policy", label: "Policy", kind: "class" },
];
const edges: GraphEdgeData[] = [
  { id: "e1", source: "ex:Claim", target: "ex:Policy", label: "hasPolicy" },
];

function renderIn(locale: "en" | "ja") {
  window.localStorage.setItem("scl.locale", locale);
  return render(
    <LocaleProvider>
      <OntologyGraphFlow nodes={nodes} edges={edges} />
    </LocaleProvider>,
  );
}

describe("OntologyGraphFlow — control labels", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("labels the zoom and fit controls in Japanese", async () => {
    renderIn("ja");
    expect(
      await screen.findByRole("button", { name: "拡大" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "縮小" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "全体を表示" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Zoom In" })).toBeNull();
    expect(screen.getByLabelText("操作パネル")).toBeInTheDocument();
  });

  it("keeps React Flow's English labels in English", async () => {
    renderIn("en");
    expect(
      await screen.findByRole("button", { name: "Zoom In" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Fit View" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Control Panel")).toBeInTheDocument();
  });
});
