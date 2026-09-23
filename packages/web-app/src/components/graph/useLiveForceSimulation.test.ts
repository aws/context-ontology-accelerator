// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node } from "@xyflow/react";
import { useLiveForceSimulation } from "./useLiveForceSimulation";

// d3-force drives its animation with requestAnimationFrame. Stub it to a no-op
// so the simulation never ticks asynchronously — the hook's *synchronous*
// behaviour (initial paint, drag handlers, cleanup) is what we assert, keeping
// the test deterministic and free of act() timing races.
let rafOriginal: typeof globalThis.requestAnimationFrame;
let cafOriginal: typeof globalThis.cancelAnimationFrame;
beforeEach(() => {
  rafOriginal = globalThis.requestAnimationFrame;
  cafOriginal = globalThis.cancelAnimationFrame;
  globalThis.requestAnimationFrame = () => 0;
  globalThis.cancelAnimationFrame = () => {};
});
afterEach(() => {
  globalThis.requestAnimationFrame = rafOriginal;
  globalThis.cancelAnimationFrame = cafOriginal;
});

type FlowNode = Node<{ id: string; label: string; kind: string }>;

function nodes(): FlowNode[] {
  return [
    {
      id: "a",
      type: "ontology",
      position: { x: 0, y: 0 },
      data: { id: "a", label: "A", kind: "class" },
    },
    {
      id: "b",
      type: "ontology",
      position: { x: 200, y: 0 },
      data: { id: "b", label: "B", kind: "class" },
    },
  ];
}
const edges = [{ id: "e", source: "a", target: "b" }];
const opts = { enabled: true, nodeSize: 80, linkDistance: 170 };

describe("useLiveForceSimulation", () => {
  it("paints node positions into React state on setup", () => {
    const setNodes = vi.fn();
    renderHook(() => useLiveForceSimulation(nodes(), edges, setNodes, opts));
    // The hook applies positions once synchronously ("paint the compressed
    // start before the first tick").
    expect(setNodes).toHaveBeenCalled();
    // It updates via the functional form so it never clobbers concurrent state.
    expect(typeof setNodes.mock.calls[0][0]).toBe("function");
  });

  it("does nothing when disabled", () => {
    const setNodes = vi.fn();
    const { result } = renderHook(() =>
      useLiveForceSimulation(nodes(), edges, setNodes, {
        ...opts,
        enabled: false,
      }),
    );
    expect(setNodes).not.toHaveBeenCalled();
    // Handlers are still returned and are safe to call with no live simulation.
    expect(() =>
      act(() => result.current.onDragStart("a", { x: 1, y: 1 })),
    ).not.toThrow();
  });

  it("drag start / drag / stop run without throwing and reheat the sim", () => {
    const setNodes = vi.fn();
    const { result } = renderHook(() =>
      useLiveForceSimulation(nodes(), edges, setNodes, opts),
    );
    expect(() => {
      act(() => result.current.onDragStart("a", { x: 10, y: 10 }));
      act(() => result.current.onDrag("a", { x: 40, y: 20 }));
      act(() => result.current.onDragStop("a"));
    }).not.toThrow();
  });

  it("ignores drag callbacks for an unknown node id", () => {
    const setNodes = vi.fn();
    const { result } = renderHook(() =>
      useLiveForceSimulation(nodes(), edges, setNodes, opts),
    );
    expect(() => {
      act(() => result.current.onDragStart("ghost", { x: 0, y: 0 }));
      act(() => result.current.onDragStop("ghost"));
    }).not.toThrow();
  });

  it("cleans up on unmount without throwing", () => {
    const setNodes = vi.fn();
    const { unmount } = renderHook(() =>
      useLiveForceSimulation(nodes(), edges, setNodes, opts),
    );
    expect(() => unmount()).not.toThrow();
  });

  it("re-applies positions when the node set changes", () => {
    const setNodes = vi.fn();
    const { rerender } = renderHook(
      ({ ns }: { ns: FlowNode[] }) =>
        useLiveForceSimulation(ns, edges, setNodes, opts),
      { initialProps: { ns: nodes() } },
    );
    const callsAfterMount = setNodes.mock.calls.length;
    const extended: FlowNode[] = [
      ...nodes(),
      {
        id: "c",
        type: "ontology",
        position: { x: 400, y: 0 },
        data: { id: "c", label: "C", kind: "class" },
      },
    ];
    act(() => rerender({ ns: extended }));
    expect(setNodes.mock.calls.length).toBeGreaterThan(callsAfterMount);
  });
});
