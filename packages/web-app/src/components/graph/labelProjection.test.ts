// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { projectEdgeLabel } from "./labelProjection";

describe("projectEdgeLabel", () => {
  it("linear interpolates when the edge is a straight chord", () => {
    // Horizontal chord from (0,0) to (200,0), label at t = 0.5 → midpoint.
    expect(projectEdgeLabel(0, 0, 200, 0, 0, 0.5)).toEqual({ x: 100, y: 0 });
    // Diagonal, hub-side stagger t = 0.35.
    const p = projectEdgeLabel(0, 0, 100, 100, 0, 0.35);
    expect(p.x).toBeCloseTo(35, 5);
    expect(p.y).toBeCloseTo(35, 5);
  });

  it("lifts the label off the chord onto the Bézier arc when curved", () => {
    // Horizontal chord in CSS (y-down) coordinates. The left-hand normal
    // `(nx, ny) = (-dy, dx) / len` is (0, 1) — "down" — so curve +40 bows
    // the Bézier below the chord. At t = 0.5 the bow displacement is
    // 2 * 0.5 * 0.5 * 40 = 20 along that normal.
    const p = projectEdgeLabel(0, 0, 200, 0, 40, 0.5);
    expect(p.x).toBeCloseTo(100, 5);
    expect(p.y).toBeCloseTo(20, 5);
    // Opposite curve bows the label above (−y) the chord by the same amount.
    const q = projectEdgeLabel(0, 0, 200, 0, -40, 0.5);
    expect(q.y).toBeCloseTo(-20, 5);
  });

  it("bow displacement follows the 2 t (1-t) weighting", () => {
    // At t = 0.25 the Bézier normal displacement is 2 * 0.25 * 0.75 = 0.375
    // times the curve. 0.375 * 40 = 15 along the (0, 1) normal.
    const p = projectEdgeLabel(0, 0, 200, 0, 40, 0.25);
    // Chord point at t=0.25 is x=50; Bézier adds normal · 15.
    expect(p.x).toBeCloseTo(50, 5);
    expect(p.y).toBeCloseTo(15, 5);
  });

  it("parallel edges with different curves land on different points at t = 0.5", () => {
    // Two parallel edges between the same chord, curves ±40.
    const up = projectEdgeLabel(0, 0, 200, 0, 40, 0.5);
    const down = projectEdgeLabel(0, 0, 200, 0, -40, 0.5);
    expect(up.y).not.toBeCloseTo(down.y, 1);
    expect(Math.abs(up.y - down.y)).toBeCloseTo(40, 5);
  });

  it("returns the source for a self-loop (zero-length chord)", () => {
    expect(projectEdgeLabel(5, 7, 5, 7, 0, 0.5)).toEqual({ x: 5, y: 7 });
    // Curve is meaningless at zero length and should not NaN.
    expect(projectEdgeLabel(5, 7, 5, 7, 40, 0.5)).toEqual({ x: 5, y: 7 });
  });

  it("endpoints at t = 0 and t = 1 land exactly on the source / target", () => {
    const s = projectEdgeLabel(10, 20, 110, 20, 40, 0);
    expect(s).toEqual({ x: 10, y: 20 });
    const t = projectEdgeLabel(10, 20, 110, 20, 40, 1);
    expect(t.x).toBeCloseTo(110, 5);
    expect(t.y).toBeCloseTo(20, 5);
  });
});
