// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure layout helpers for the ontology graph.
 *
 * A proposed ontology is a *schema graph*, not a tree: most edges are foreign
 * keys with no natural "up", and real-world proposals split into several
 * disconnected sub-schemas plus a long tail of classes with no relationships at
 * all. A single hierarchical (dagre) pass puts every root of every component in
 * rank 0, which renders as one screen-wide strip with the interesting structure
 * squeezed underneath it.
 *
 * `layoutForceGraph` does what WebVOWL does instead: a force-directed layout per
 * connected component (so each sub-schema becomes a compact cluster), then packs
 * the components into a roughly landscape rectangle, largest first. Everything
 * here is synchronous and deterministic so it can be unit-tested on positions.
 */
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";

export interface Point {
  x: number;
  y: number;
}

export interface LayoutEdge {
  source: string;
  target: string;
}

export interface ForceLayoutOptions {
  /** Rendered node diameter in px; drives collision radius and packing size. */
  nodeSize?: number;
  /** Preferred edge length in px. */
  linkDistance?: number;
  /** Gap between packed components in px. */
  componentGap?: number;
  /** Target width/height ratio of the packed result. */
  aspect?: number;
  /** Simulation ticks per component. */
  iterations?: number;
}

const DEFAULTS: Required<ForceLayoutOptions> = {
  nodeSize: 80,
  linkDistance: 170,
  componentGap: 90,
  aspect: 2.4,
  iterations: 300,
};

/** Ids of nodes that appear in no edge (degree 0). */
export function isolatedNodeIds(
  nodeIds: readonly string[],
  edges: readonly LayoutEdge[],
): Set<string> {
  const linked = new Set<string>();
  for (const e of edges) {
    linked.add(e.source);
    linked.add(e.target);
  }
  return new Set(nodeIds.filter((id) => !linked.has(id)));
}

/**
 * Connected components (undirected), largest first; ties keep input order so
 * the result is stable for a given input.
 */
export function connectedComponents(
  nodeIds: readonly string[],
  edges: readonly LayoutEdge[],
): string[][] {
  const adjacency = new Map<string, string[]>();
  for (const id of nodeIds) adjacency.set(id, []);
  for (const e of edges) {
    // Edges to nodes not in the set (e.g. filtered out) are ignored.
    if (!adjacency.has(e.source) || !adjacency.has(e.target)) continue;
    adjacency.get(e.source)?.push(e.target);
    adjacency.get(e.target)?.push(e.source);
  }

  const seen = new Set<string>();
  const components: string[][] = [];
  for (const start of nodeIds) {
    if (seen.has(start)) continue;
    const component: string[] = [];
    const stack = [start];
    seen.add(start);
    while (stack.length) {
      const id = stack.pop();
      if (id === undefined) break;
      component.push(id);
      for (const next of adjacency.get(id) ?? []) {
        if (!seen.has(next)) {
          seen.add(next);
          stack.push(next);
        }
      }
    }
    components.push(component);
  }
  return components.sort((a, b) => b.length - a.length);
}

interface SimNode extends SimulationNodeDatum {
  id: string;
}

/** Deterministic PRNG so the simulation's tie-break jitter is reproducible. */
function seededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    // Park–Miller minimal standard LCG.
    state = (state * 48271) % 2147483647;
    return state / 2147483647;
  };
}

/**
 * Force-directed positions for one connected component, centred on (0, 0).
 * Runs the simulation to completion synchronously.
 */
export function forceLayoutComponent(
  nodeIds: readonly string[],
  edges: readonly LayoutEdge[],
  opts: ForceLayoutOptions = {},
): Map<string, Point> {
  const o = { ...DEFAULTS, ...opts };
  const positions = new Map<string, Point>();
  if (nodeIds.length === 0) return positions;
  if (nodeIds.length === 1) {
    positions.set(nodeIds[0], { x: 0, y: 0 });
    return positions;
  }

  const ids = new Set(nodeIds);
  const simNodes: SimNode[] = nodeIds.map((id) => ({ id }));
  const links: SimulationLinkDatum<SimNode>[] = edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  const simulation = forceSimulation<SimNode>(simNodes)
    .randomSource(seededRandom(nodeIds.length * 7919 + links.length))
    .force(
      "link",
      forceLink<SimNode, SimulationLinkDatum<SimNode>>(links)
        .id((n) => n.id)
        .distance(o.linkDistance)
        .strength(0.7),
    )
    .force("charge", forceManyBody().strength(-o.linkDistance * 4.5))
    .force("collide", forceCollide(o.nodeSize / 2 + 18).iterations(2))
    .force("center", forceCenter(0, 0))
    .stop();

  simulation.tick(o.iterations);

  for (const n of simNodes) {
    positions.set(n.id, { x: n.x ?? 0, y: n.y ?? 0 });
  }
  return positions;
}

export interface Box {
  width: number;
  height: number;
}

/**
 * Pack boxes (given largest-first) into rows whose total width targets
 * `aspect`. Within a row, boxes shorter than the row are stacked vertically in
 * the same column while they fit, so small clusters fill the space beside a
 * tall one instead of each taking a full-height slot. Returns the top-left
 * offset of each box in input order.
 *
 * ponytail: this is shelf packing with column stacking; a skyline/MaxRects
 * packer would tighten it further if graphs ever reach many hundreds of
 * classes.
 */
export function packComponents(
  boxes: readonly Box[],
  gap: number,
  aspect: number,
): Point[] {
  if (boxes.length === 0) return [];
  const totalArea = boxes.reduce(
    (sum, b) => sum + (b.width + gap) * (b.height + gap),
    0,
  );
  const widest = Math.max(...boxes.map((b) => b.width));
  const targetWidth = Math.max(widest, Math.sqrt(totalArea * aspect));

  const offsets: Point[] = new Array<Point>(boxes.length);
  let x = 0; // left edge of the current column
  let y = 0; // top of the current row
  let rowHeight = 0; // set by the first (tallest) box in the row
  let colWidth = 0; // widest box in the current column
  let colY = 0; // next free y inside the current column

  boxes.forEach((box, i) => {
    const fitsInColumn = colWidth > 0 && colY + box.height <= rowHeight;
    if (fitsInColumn) {
      offsets[i] = { x, y: y + colY };
      colY += box.height + gap;
      colWidth = Math.max(colWidth, box.width);
      return;
    }
    // Start a new column; wrap to a new row if it would overflow the target.
    if (colWidth > 0) x += colWidth + gap;
    if (x > 0 && x + box.width > targetWidth) {
      x = 0;
      y += rowHeight + gap;
      rowHeight = 0;
    }
    if (rowHeight === 0) rowHeight = box.height;
    offsets[i] = { x, y };
    colWidth = box.width;
    colY = box.height + gap;
  });
  return offsets;
}

/**
 * Lay out the whole graph: force layout per connected component, then pack the
 * components. Returns a top-left position (React Flow convention) per node id.
 */
export function layoutForceGraph(
  nodeIds: readonly string[],
  edges: readonly LayoutEdge[],
  opts: ForceLayoutOptions = {},
): Map<string, Point> {
  const o = { ...DEFAULTS, ...opts };
  const components = connectedComponents(nodeIds, edges);

  // Lay out each component around its own origin and measure its bounding box.
  const laidOut = components.map((ids) => {
    const positions = forceLayoutComponent(ids, edges, o);
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const p of positions.values()) {
      minX = Math.min(minX, p.x);
      minY = Math.min(minY, p.y);
      maxX = Math.max(maxX, p.x);
      maxY = Math.max(maxY, p.y);
    }
    return {
      positions,
      minX,
      minY,
      box: {
        width: maxX - minX + o.nodeSize,
        height: maxY - minY + o.nodeSize,
      },
    };
  });

  const offsets = packComponents(
    laidOut.map((c) => c.box),
    o.componentGap,
    o.aspect,
  );

  const result = new Map<string, Point>();
  laidOut.forEach((component, i) => {
    const offset = offsets[i];
    for (const [id, p] of component.positions) {
      // Shift so the component's top-left node sits at the packed offset, and
      // convert the node centre to React Flow's top-left anchor.
      result.set(id, {
        x: p.x - component.minX + offset.x,
        y: p.y - component.minY + offset.y,
      });
    }
  });
  return result;
}
