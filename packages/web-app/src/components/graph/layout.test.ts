// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import {
  connectedComponents,
  forceLayoutComponent,
  isolatedNodeIds,
  layoutForceGraph,
  packComponents,
  type LayoutEdge,
} from "./layout";

/**
 * Shape of the real "cross-source-uat" proposal (df41a4a3): several
 * disconnected FK sub-schemas of different sizes plus a long tail of classes
 * with no relationships at all. Node names are illustrative.
 */
function proposalLikeGraph(): { ids: string[]; edges: LayoutEdge[] } {
  const edges: LayoutEdge[] = [];
  const ids: string[] = [];
  const link = (a: string, b: string) => edges.push({ source: a, target: b });

  // F1 cluster (12 nodes, hub-and-spoke on races/drivers/constructors)
  const f1 = [
    "races",
    "drivers",
    "constructors",
    "results",
    "qualifying",
    "laptimes",
    "pitstops",
    "driverstandings",
    "constructorstandings",
    "constructorresults",
    "circuits",
    "seasons",
    "status",
  ];
  ids.push(...f1);
  link("results", "races");
  link("results", "drivers");
  link("results", "constructors");
  link("results", "status");
  link("qualifying", "races");
  link("qualifying", "drivers");
  link("qualifying", "constructors");
  link("laptimes", "races");
  link("laptimes", "drivers");
  link("pitstops", "races");
  link("pitstops", "drivers");
  link("driverstandings", "drivers");
  link("constructorstandings", "constructors");
  link("constructorresults", "constructors");
  link("races", "circuits");
  link("races", "seasons");

  // Banking cluster (9 nodes) incl. a subClassOf edge
  const bank = [
    "account",
    "district",
    "client",
    "disp",
    "card",
    "loan",
    "trans",
    "order",
    "Orders",
  ];
  ids.push(...bank);
  link("account", "district");
  link("client", "district");
  link("disp", "account");
  link("disp", "client");
  link("card", "disp");
  link("loan", "account");
  link("trans", "account");
  link("order", "account");
  link("order", "Orders"); // subClassOf

  // Superhero cluster (8) with three parallel superhero→colour edges
  const hero = [
    "superhero",
    "colour",
    "gender",
    "publisher",
    "race",
    "alignment",
    "hero_power",
    "superpower",
  ];
  ids.push(...hero);
  link("superhero", "colour");
  link("superhero", "colour");
  link("superhero", "colour");
  link("superhero", "gender");
  link("superhero", "publisher");
  link("superhero", "race");
  link("superhero", "alignment");
  link("hero_power", "superhero");
  link("hero_power", "superpower");

  // Three-node medical cluster
  ids.push("patient", "examination", "laboratory");
  link("examination", "patient");
  link("laboratory", "patient");

  // 35 standalone classes
  for (let i = 0; i < 35; i++) ids.push(`standalone_${i}`);

  return { ids, edges };
}

describe("isolatedNodeIds", () => {
  it("returns the degree-0 nodes only", () => {
    const { ids, edges } = proposalLikeGraph();
    const isolated = isolatedNodeIds(ids, edges);
    expect(isolated.size).toBe(35);
    for (const id of isolated) expect(id.startsWith("standalone_")).toBe(true);
    expect(isolated.has("superhero")).toBe(false);
  });
});

describe("connectedComponents", () => {
  it("splits the graph into sub-schemas, largest first, singletons last", () => {
    const { ids, edges } = proposalLikeGraph();
    const comps = connectedComponents(ids, edges);
    // 4 connected clusters + 35 singletons
    expect(comps).toHaveLength(4 + 35);
    expect(comps[0]).toHaveLength(13); // F1
    expect(comps[1]).toHaveLength(9); // banking
    expect(comps[2]).toHaveLength(8); // superhero
    expect(comps[3]).toHaveLength(3); // medical
    expect(comps.slice(4).every((c) => c.length === 1)).toBe(true);
    // every node appears exactly once
    expect(comps.flat().sort()).toEqual([...ids].sort());
  });

  it("ignores edges that reference nodes outside the set", () => {
    const comps = connectedComponents(
      ["a", "b"],
      [{ source: "a", target: "ghost" }],
    );
    expect(comps).toEqual([["a"], ["b"]]);
  });
});

describe("forceLayoutComponent", () => {
  it("is deterministic and keeps linked nodes near each other", () => {
    const edges: LayoutEdge[] = [
      { source: "hub", target: "a" },
      { source: "hub", target: "b" },
      { source: "hub", target: "c" },
    ];
    const ids = ["hub", "a", "b", "c", "far"]; // `far` is in the set but unlinked
    const p1 = forceLayoutComponent(ids, edges);
    const p2 = forceLayoutComponent(ids, edges);
    expect([...p1.entries()]).toEqual([...p2.entries()]);

    const hub = p1.get("hub");
    const a = p1.get("a");
    const far = p1.get("far");
    if (!hub || !a || !far) throw new Error("missing positions");
    const dHubA = Math.hypot(a.x - hub.x, a.y - hub.y);
    const dHubFar = Math.hypot(far.x - hub.x, far.y - hub.y);
    expect(dHubA).toBeLessThan(dHubFar);
  });

  it("places a single node at the origin", () => {
    expect([...forceLayoutComponent(["x"], []).values()]).toEqual([
      { x: 0, y: 0 },
    ]);
  });
});

describe("packComponents", () => {
  it("never overlaps boxes and keeps the first box at the origin", () => {
    const boxes = [
      { width: 900, height: 700 },
      { width: 600, height: 500 },
      { width: 500, height: 450 },
      { width: 300, height: 200 },
      ...Array.from({ length: 35 }, () => ({ width: 80, height: 80 })),
    ];
    const offsets = packComponents(boxes, 90, 2.4);
    expect(offsets[0]).toEqual({ x: 0, y: 0 });
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = { ...offsets[i], ...boxes[i] };
        const b = { ...offsets[j], ...boxes[j] };
        const overlap =
          a.x < b.x + b.width &&
          b.x < a.x + a.width &&
          a.y < b.y + b.height &&
          b.y < a.y + a.height;
        expect(overlap, `boxes ${i} and ${j} overlap`).toBe(false);
      }
    }
  });

  it("stacks small boxes beside a tall one instead of opening a new row", () => {
    const boxes = [
      { width: 400, height: 400 },
      { width: 100, height: 100 },
      { width: 100, height: 100 },
    ];
    const offsets = packComponents(boxes, 20, 2.4);
    // Both small boxes sit in one column to the right of the big one.
    expect(offsets[1].x).toBe(420);
    expect(offsets[2].x).toBe(420);
    expect(offsets[1].y).toBe(0);
    expect(offsets[2].y).toBe(120);
  });
});

describe("layoutForceGraph", () => {
  it("lays out the proposal-shaped graph compactly with no node overlaps", () => {
    const { ids, edges } = proposalLikeGraph();
    const nodeSize = 80;
    const pos = layoutForceGraph(ids, edges, { nodeSize });
    expect(pos.size).toBe(ids.length);

    const pts = [...pos.values()];
    // No two node circles overlap (centres at least a diameter apart, with a
    // small tolerance for the collision force's soft boundary).
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const d = Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y);
        expect(d).toBeGreaterThan(nodeSize * 0.9);
      }
    }
    // Landscape-ish extent: nowhere near the old single-row strip, whose
    // width/height ratio for 88 nodes was >20.
    const xs = pts.map((p) => p.x);
    const ys = pts.map((p) => p.y);
    const width = Math.max(...xs) - Math.min(...xs) + nodeSize;
    const height = Math.max(...ys) - Math.min(...ys) + nodeSize;
    const ratio = width / height;
    expect(ratio).toBeGreaterThan(1);
    expect(ratio).toBeLessThan(4);
  });

  it("hidden standalone classes shrink the extent (what the toggle does)", () => {
    const { ids, edges } = proposalLikeGraph();
    const isolated = isolatedNodeIds(ids, edges);
    const connectedOnly = ids.filter((id) => !isolated.has(id));
    const area = (m: Map<string, { x: number; y: number }>) => {
      const pts = [...m.values()];
      const w =
        Math.max(...pts.map((p) => p.x)) - Math.min(...pts.map((p) => p.x));
      const h =
        Math.max(...pts.map((p) => p.y)) - Math.min(...pts.map((p) => p.y));
      return w * h;
    };
    expect(area(layoutForceGraph(connectedOnly, edges))).toBeLessThan(
      area(layoutForceGraph(ids, edges)),
    );
  });
});
