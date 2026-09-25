// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import {
  OntologyGraphFlow,
  parallelEdgeCurves,
  radialPositions,
  type GraphNodeData,
  type GraphEdgeData,
} from "./OntologyGraphFlow";

// React Flow needs ResizeObserver
class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver =
  ResizeObserver as unknown as typeof globalThis.ResizeObserver;

const nodes: GraphNodeData[] = [
  { id: "ex:Claim", label: "Claim", kind: "class" },
  { id: "ex:Policy", label: "Policy", kind: "grounded" },
  { id: "ex:hasPolicy", label: "has policy", kind: "object-property" },
];

const edges: GraphEdgeData[] = [
  { id: "e1", source: "ex:Claim", target: "ex:Policy", label: "hasPolicy" },
];

describe("OntologyGraphFlow", () => {
  it("renders without crashing", () => {
    const { container } = render(
      <OntologyGraphFlow nodes={nodes} edges={edges} />,
    );
    expect(container.querySelector(".react-flow")).toBeTruthy();
  });

  it("renders nodes with labels", () => {
    render(<OntologyGraphFlow nodes={nodes} edges={edges} />);
    expect(screen.getByText("Claim")).toBeTruthy();
    expect(screen.getByText("Policy")).toBeTruthy();
  });

  it("calls onNodeSelect when a node is clicked", () => {
    const onNodeSelect = vi.fn();
    render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        onNodeSelect={onNodeSelect}
      />,
    );
    fireEvent.click(screen.getByText("Claim"));
    expect(onNodeSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "ex:Claim" }),
    );
  });

  it("renders with empty nodes/edges", () => {
    const { container } = render(<OntologyGraphFlow nodes={[]} edges={[]} />);
    expect(container.querySelector(".react-flow")).toBeTruthy();
  });

  it("lays out edge-less nodes on a circle (not a single row)", () => {
    const pts = radialPositions(6);
    // A flat row collapses to one y; a circle uses many distinct y values.
    expect(new Set(pts.map((p) => Math.round(p.y))).size).toBeGreaterThan(1);
    // All points are roughly equidistant from the centre (on a circle).
    const radii = pts.map((p) => Math.hypot(p.x, p.y));
    const maxR = Math.max(...radii);
    const minR = Math.min(...radii);
    expect(maxR - minR).toBeLessThan(1);
  });

  it("places a single node at the origin", () => {
    expect(radialPositions(1)).toEqual([{ x: 0, y: 0 }]);
  });

  it("calls onNodeExpand when a node is double-clicked", () => {
    const onNodeExpand = vi.fn();
    render(
      <OntologyGraphFlow
        nodes={nodes}
        edges={edges}
        onNodeExpand={onNodeExpand}
      />,
    );
    fireEvent.doubleClick(screen.getByText("Claim"));
    expect(onNodeExpand).toHaveBeenCalledWith(
      expect.objectContaining({ id: "ex:Claim" }),
    );
  });

  it("shows an expand affordance for collapsed nodes with children", () => {
    const withChildren: GraphNodeData[] = [
      { id: "ex:Claim", label: "Claim", kind: "class", hasChildren: true },
    ];
    render(<OntologyGraphFlow nodes={withChildren} edges={[]} />);
    expect(screen.getByText("+")).toBeTruthy();
  });

  it("shows a collapse affordance for expanded nodes", () => {
    const expandedNode: GraphNodeData[] = [
      {
        id: "ex:Claim",
        label: "Claim",
        kind: "class",
        hasChildren: true,
        expanded: true,
      },
    ];
    render(<OntologyGraphFlow nodes={expandedNode} edges={[]} />);
    expect(screen.getByText("−")).toBeTruthy();
  });

  describe("force layout", () => {
    const schema: GraphNodeData[] = [
      { id: "ex:A", label: "A", kind: "class" },
      { id: "ex:B", label: "B", kind: "class" },
      { id: "ex:Lonely", label: "Lonely", kind: "class" },
      { id: "ex:Alone", label: "Alone", kind: "class" },
    ];
    const fk: GraphEdgeData[] = [
      { id: "e1", source: "ex:A", target: "ex:B", label: "b_id" },
    ];

    it("hides standalone classes by default and offers a toggle with the count", () => {
      render(<OntologyGraphFlow nodes={schema} edges={fk} layout="force" />);
      expect(screen.getByText("A")).toBeTruthy();
      expect(screen.queryByText("Lonely")).toBeNull();
      expect(screen.queryByText("Alone")).toBeNull();
      expect(screen.getByText(/Show 2 standalone classes/)).toBeTruthy();
    });

    it("reveals standalone classes when the toggle is switched on", () => {
      render(<OntologyGraphFlow nodes={schema} edges={fk} layout="force" />);
      fireEvent.click(screen.getByRole("checkbox"));
      expect(screen.getByText("Lonely")).toBeTruthy();
      expect(screen.getByText("Alone")).toBeTruthy();
    });

    it("does not offer the toggle in hierarchical mode (explorer root view is edge-less)", () => {
      render(<OntologyGraphFlow nodes={schema} edges={[]} />);
      expect(screen.getByText("Lonely")).toBeTruthy();
      expect(screen.queryByText(/standalone/)).toBeNull();
    });

    it("with collapseStandalone=false, shows standalone classes by default but still offers the toggle", () => {
      render(
        <OntologyGraphFlow
          nodes={schema}
          edges={fk}
          layout="force"
          collapseStandalone={false}
        />,
      );
      // Curated nodes are visible from the start …
      expect(screen.getByText("Lonely")).toBeTruthy();
      expect(screen.getByText("Alone")).toBeTruthy();
      // … and the toggle is still available to hide them.
      expect(screen.getByText(/Show 2 standalone classes/)).toBeTruthy();
    });
  });
});

describe("parallelEdgeCurves", () => {
  it("fans edges sharing a node pair symmetrically and leaves lone edges straight", () => {
    const edges: GraphEdgeData[] = [
      { id: "1", source: "colour", target: "hero" },
      { id: "2", source: "colour", target: "hero" },
      { id: "3", source: "colour", target: "hero" },
      { id: "4", source: "colour", target: "gender" },
    ];
    // Lone edge is straight; the triple fans out 44px apart around zero.
    expect(parallelEdgeCurves(edges).map((c) => c + 0)).toEqual([
      -44, 0, 44, 0,
    ]);
  });

  it("puts a reversed edge on the opposite side of its forward twin", () => {
    const edges: GraphEdgeData[] = [
      { id: "1", source: "a", target: "b" },
      { id: "2", source: "b", target: "a" },
    ];
    const [forward, reverse] = parallelEdgeCurves(edges);
    // Each edge's normal is derived from its own direction, so equal-signed
    // offsets would land on the same side; the sign flip separates them.
    expect(forward).toBe(-22);
    expect(reverse).toBe(-22);
  });
});
