// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure geometry helpers for projecting the position of an edge label.
 *
 * Shared by `FloatingEdge` (SVG label render) and `attributeOverlay` (which
 * needs to know where a hovered node's incident labels will land so the
 * attribute glyphs can be pushed off them). Keeping the math here means
 * both callers compute the same point, and the behaviour is unit-testable
 * without a React Flow render.
 */

export interface Point {
  x: number;
  y: number;
}

/**
 * Project a label on the edge from (sx, sy) to (tx, ty) at parameter `t`.
 *
 * - When `curve === 0` the result is the straight-chord point.
 * - When `curve !== 0` the edge is drawn as a quadratic Bézier bowed by
 *   `curve` along the chord's left-hand normal (same convention
 *   `FloatingEdge` uses to compute the control point). The projection
 *   lands on that Bézier, not on the chord — so a label on one of several
 *   parallel edges sits on its own drawn arc rather than stacking at the
 *   straight midpoint with every sibling.
 *
 * Self-loop (sx === tx && sy === ty) returns the source point unchanged;
 * the caller decides what, if anything, to render there.
 */
export function projectEdgeLabel(
  sx: number,
  sy: number,
  tx: number,
  ty: number,
  curve: number,
  t: number,
): Point {
  const dx = tx - sx;
  const dy = ty - sy;
  const len = Math.hypot(dx, dy);
  if (len === 0) return { x: sx, y: sy };
  if (curve === 0) {
    return { x: sx + dx * t, y: sy + dy * t };
  }
  // Left-hand normal to the chord, matching FloatingEdge's control-point
  // offset `(nx, ny) * curve`. Quadratic Bézier at `t`:
  //   B(t) = (1-t)² · S + 2(1-t)·t · C + t² · T
  const nx = -dy / len;
  const ny = dx / len;
  const cx = (sx + tx) / 2 + nx * curve;
  const cy = (sy + ty) / 2 + ny * curve;
  const u = 1 - t;
  return {
    x: u * u * sx + 2 * u * t * cx + t * t * tx,
    y: u * u * sy + 2 * u * t * cy + t * t * ty,
  };
}
