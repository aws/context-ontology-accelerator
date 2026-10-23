// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { TurtleHighlight } from "./TurtleHighlight";

describe("TurtleHighlight", () => {
  it("highlights small Turtle and HTML-escapes content", () => {
    const { container } = render(
      <TurtleHighlight>
        {'ex:Foo a owl:Class ;\n    rdfs:comment "a <b> & c" .'}
      </TurtleHighlight>,
    );
    const pre = container.querySelector("pre")!;
    // Syntax coloring is present (per-token color spans in the HTML string).
    expect(pre.querySelector("span")).toBeTruthy();
    // Content is HTML-escaped, not injected as markup.
    expect(pre.innerHTML).toContain("&lt;");
    expect(pre.innerHTML).toContain("&amp;");
    // ...but the visible text is preserved.
    expect(pre.textContent).toContain("a <b> & c");
  });

  it("neutralizes a <script> / attribute-breakout payload at the innerHTML sink", () => {
    const payload =
      'ex:Foo rdfs:comment "<script>alert(1)</script>" ;\n  rdfs:label "\\" onmouseover=\\"alert(2)" .';
    const { container } = render(<TurtleHighlight>{payload}</TurtleHighlight>);
    const pre = container.querySelector("pre")!;
    // No live <script> element is created from the input.
    expect(pre.querySelector("script")).toBeNull();
    // The angle brackets are escaped, so the markup cannot execute.
    expect(pre.innerHTML).toContain("&lt;script&gt;");
    expect(pre.innerHTML).not.toContain("<script>");
    // The quote in the payload stays in text context — no element picks up an
    // event handler (only fixed hex colors ever reach a style attribute).
    expect(pre.querySelector("[onmouseover]")).toBeNull();
    expect(pre.querySelectorAll("span").length).toBeGreaterThan(0);
    for (const span of Array.from(pre.querySelectorAll("span"))) {
      expect(span.getAttribute("style")).toMatch(/^color:#[0-9a-f]+$/i);
    }
    // The literal text is still displayed verbatim.
    expect(pre.textContent).toContain("<script>alert(1)</script>");
  });

  it("falls back to plain text (no color spans) above the size ceiling", () => {
    const big = "ex:Foo a owl:Class .\n".repeat(60000); // > 1 MB
    const { container } = render(<TurtleHighlight>{big}</TurtleHighlight>);
    const pre = container.querySelector("pre")!;
    expect(pre.querySelector("span")).toBeNull();
    expect((pre.textContent ?? "").length).toBeGreaterThan(1_000_000);
  });
});
