// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Live force simulation for the force-layout graph.
 *
 * `layout.ts` computes a settled, packed layout up front. This hook then keeps
 * a d3-force simulation *running* on top of it so the graph behaves like
 * WebVOWL's: nodes spring into place on load, and dragging a node pins it and
 * re-heats the simulation so its neighbours follow instead of stretching
 * rubber-band edges. Weak "home" forces pull every node back toward its packed
 * position, so the cluster arrangement survives the physics.
 */
import { useCallback, useEffect, useRef } from "react";
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import type { Node } from "@xyflow/react";
import { componentScale, connectedComponents, type LayoutEdge } from "./layout";

interface SimNode extends SimulationNodeDatum {
  id: string;
  /** Packed layout position (node centre) the node is pulled back toward. */
  homeX: number;
  homeY: number;
}

type SimLink = SimulationLinkDatum<SimNode>;

interface Options {
  enabled: boolean;
  nodeSize: number;
  linkDistance: number;
  /**
   * Per-node collision radius in px. Falls back to `nodeSize / 2`. Used to let
   * a hovered class carve out room for its attribute ring + edge labels on
   * the fly: the hovered node's radius inflates, forceCollide pushes its
   * neighbours outward live, and the home force pulls them back on unhover.
   */
  nodeRadius?: (id: string) => number;
}

/** Fraction of the distance from cluster centroid nodes start at (fly-in). */
const START_COMPRESSION = 0.35;
/** Sub-pixel jitter below this is not worth a React re-render. */
const MOVE_EPSILON = 0.4;
/** Pull toward the packed position: firm at rest, almost off while dragging so
 *  a dragged node's neighbours follow it instead of snapping back. */
const HOME_STRENGTH_REST = 0.06;
const HOME_STRENGTH_DRAG = 0.004;

export function useLiveForceSimulation<T extends Node>(
  initialNodes: readonly T[],
  edges: readonly LayoutEdge[],
  setNodes: (updater: (prev: T[]) => T[]) => void,
  { enabled, nodeSize, linkDistance, nodeRadius }: Options,
) {
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);
  const byIdRef = useRef<Map<string, SimNode>>(new Map());
  const half = nodeSize / 2;

  useEffect(() => {
    if (!enabled || initialNodes.length === 0) return;

    const ids = initialNodes.map((n) => n.id);
    const home = new Map(
      initialNodes.map((n) => [
        n.id,
        { x: n.position.x + half, y: n.position.y + half },
      ]),
    );

    // Start each cluster compressed toward its own centroid so nodes visibly
    // spring out to their places, without flying through other clusters.
    const start = new Map<string, { x: number; y: number }>();
    for (const component of connectedComponents(ids, edges)) {
      let cx = 0;
      let cy = 0;
      for (const id of component) {
        const h = home.get(id);
        if (h) {
          cx += h.x;
          cy += h.y;
        }
      }
      cx /= component.length;
      cy /= component.length;
      for (const id of component) {
        const h = home.get(id);
        if (h) {
          start.set(id, {
            x: cx + (h.x - cx) * START_COMPRESSION,
            y: cy + (h.y - cy) * START_COMPRESSION,
          });
        }
      }
    }

    const simNodes: SimNode[] = ids.map((id) => {
      const h = home.get(id) ?? { x: 0, y: 0 };
      const s = start.get(id) ?? h;
      return { id, x: s.x, y: s.y, homeX: h.x, homeY: h.y };
    });
    const idSet = new Set(ids);
    const links: SimLink[] = edges
      .filter((e) => idSet.has(e.source) && idSet.has(e.target))
      .map((e) => ({ source: e.source, target: e.target }));

    const byId = new Map(simNodes.map((n) => [n.id, n]));
    byIdRef.current = byId;

    // Per-connected-component spacing scale: a subgraph with a popular hub
    // gets its own larger linkDistance; a 3-node side cluster stays at the
    // baseline. Shared helper so static homes (layout.ts) and this live link
    // force never disagree about preferred edge length.
    const linkScaleByNode = new Map<string, number>();
    for (const component of connectedComponents(ids, edges)) {
      const compSet = new Set(component);
      const degree = new Map<string, number>();
      for (const e of edges) {
        if (!compSet.has(e.source) || !compSet.has(e.target)) continue;
        degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
        degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
      }
      let compMaxDeg = 0;
      for (const d of degree.values()) if (d > compMaxDeg) compMaxDeg = d;
      const scale = componentScale(compMaxDeg);
      for (const id of component) linkScaleByNode.set(id, scale);
    }

    // Push positions into React state — but only for nodes that actually
    // moved. Re-creating all node objects every tick makes React Flow re-render
    // every node and edge ~60×/s during a drag; reusing the unchanged objects
    // confines re-renders to the cluster that is moving.
    const applyPositions = () => {
      let moved = false;
      setNodes((prev) => {
        const next = prev.map((n) => {
          const s = byId.get(n.id);
          if (!s || s.x === undefined || s.y === undefined) return n;
          const x = s.x - half;
          const y = s.y - half;
          if (
            Math.abs(x - n.position.x) < MOVE_EPSILON &&
            Math.abs(y - n.position.y) < MOVE_EPSILON
          ) {
            return n;
          }
          moved = true;
          return { ...n, position: { x, y } };
        });
        return moved ? next : prev;
      });
    };

    const simulation = forceSimulation<SimNode>(simNodes)
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((n) => n.id)
          .distance((link) => {
            const src = link.source;
            // forceLink's SimLink generic narrows link.source to SimNode when
            // it's an object; the string/number cases (pre-resolution) coerce.
            const sId = typeof src === "object" ? src.id : String(src);
            return linkDistance * (linkScaleByNode.get(sId) ?? 1);
          })
          .strength(0.6),
      )
      // distanceMax keeps repulsion local so clusters don't push each other
      // across the canvas; the home forces hold the packed arrangement.
      .force(
        "charge",
        forceManyBody()
          .strength(-linkDistance * 4)
          .distanceMax(linkDistance * 2.5),
      )
      .force(
        "collide",
        forceCollide<SimNode>(
          (n) => (nodeRadius?.(n.id) ?? half) + 16,
        ).iterations(2),
      )
      .force("x", forceX<SimNode>((n) => n.homeX).strength(0.06))
      .force("y", forceY<SimNode>((n) => n.homeY).strength(0.06))
      .alpha(1)
      .alphaDecay(0.035)
      .velocityDecay(0.4)
      .on("tick", applyPositions);

    simRef.current = simulation;
    applyPositions(); // show the compressed start before the first tick

    return () => {
      simulation.stop();
      simRef.current = null;
      byIdRef.current = new Map();
    };
  }, [enabled, initialNodes, edges, setNodes, half, linkDistance, nodeRadius]);

  const setHomeStrength = (strength: number) => {
    const sim = simRef.current;
    if (!sim) return;
    sim.force<ReturnType<typeof forceX<SimNode>>>("x")?.strength(strength);
    sim.force<ReturnType<typeof forceY<SimNode>>>("y")?.strength(strength);
  };

  /** Pin the dragged node to the pointer and re-heat so neighbours follow. */
  const onDragStart = useCallback(
    (nodeId: string, position: { x: number; y: number }) => {
      const s = byIdRef.current.get(nodeId);
      const sim = simRef.current;
      if (!s || !sim) return;
      s.fx = position.x + half;
      s.fy = position.y + half;
      setHomeStrength(HOME_STRENGTH_DRAG);
      sim.alphaTarget(0.2).restart();
    },
    [half],
  );

  const onDrag = useCallback(
    (nodeId: string, position: { x: number; y: number }) => {
      const s = byIdRef.current.get(nodeId);
      if (!s) return;
      s.fx = position.x + half;
      s.fy = position.y + half;
    },
    [half],
  );

  /**
   * Release the node. Only the dragged node adopts its current position as
   * its new home — leaving neighbours' homes untouched so a hover-expanded
   * layout does not get baked in by every drag.
   */
  const onDragStop = useCallback((nodeId: string) => {
    const s = byIdRef.current.get(nodeId);
    const sim = simRef.current;
    if (!s || !sim) return;
    s.fx = null;
    s.fy = null;
    if (s.x !== undefined) s.homeX = s.x;
    if (s.y !== undefined) s.homeY = s.y;
    setHomeStrength(HOME_STRENGTH_REST);
    sim.alphaTarget(0);
  }, []);

  /**
   * Re-invoke `forceCollide`'s radius accessor against every node, so a
   * recently changed per-node radius (e.g. the hover-expansion ref) actually
   * reaches the running simulation. d3-force caches radii at `initialize()`
   * time and never re-reads them per tick; calling `.radius(...)` with the
   * current accessor forces a re-initialize.
   */
  const refreshCollide = useCallback(() => {
    const sim = simRef.current;
    const collide = sim?.force("collide") as
      | { radius: (fn?: unknown) => unknown }
      | null
      | undefined;
    if (!sim || !collide) return;
    collide.radius(collide.radius());
  }, []);

  /**
   * Pin a node at its current simulation position (`fx`/`fy`), so forces
   * cannot move it. The parent uses this on hover so the hovered node stays
   * under the pointer while neighbours drift outward around it — otherwise
   * the sim's one-shot alpha kick jiggles the hovered node and the pointer
   * keeps losing it, flipping the hover on and off.
   */
  const pinNode = useCallback((nodeId: string) => {
    const s = byIdRef.current.get(nodeId);
    if (!s) return;
    if (s.x !== undefined) s.fx = s.x;
    if (s.y !== undefined) s.fy = s.y;
  }, []);

  /** Release a previously pinned node. No-op for ids the sim does not know. */
  const unpinNode = useCallback((nodeId: string) => {
    const s = byIdRef.current.get(nodeId);
    if (!s) return;
    s.fx = null;
    s.fy = null;
  }, []);

  /**
   * One-shot alpha boost — gives the sim a burst of energy that decays
   * naturally (via `alphaDecay`), without holding `alphaTarget` above
   * `alphaMin`. The caller (hover enter / leave) uses this to let a radius
   * change actually move neighbours out, without a continuous 60fps warm.
   * Does not touch `alphaTarget`, so a parallel drag keeps ownership of it.
   */
  const kickAlpha = useCallback((value: number) => {
    const sim = simRef.current;
    if (!sim) return;
    if (sim.alpha() < value) sim.alpha(value);
    sim.restart();
  }, []);

  return {
    onDragStart,
    onDrag,
    onDragStop,
    refreshCollide,
    kickAlpha,
    pinNode,
    unpinNode,
  };
}
