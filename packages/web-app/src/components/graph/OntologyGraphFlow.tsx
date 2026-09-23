// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * OntologyGraphFlow — React Flow-based ontology graph visualization.
 *
 * Two layouts:
 *  - `hierarchical` (default): dagre top-to-bottom. Right for taxonomies where
 *    `subClassOf` edges give a real "up" (the class explorer).
 *  - `force`: force-directed per connected component, then packed. Right for
 *    proposed schema ontologies, whose edges are mostly foreign keys and which
 *    split into several disconnected sub-schemas (see `layout.ts`).
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Panel,
  MarkerType,
  BaseEdge,
  EdgeLabelRenderer,
  getStraightPath,
  useInternalNode,
  type InternalNode,
  type EdgeProps,
  type EdgeTypes,
  type Node,
  type Edge,
  type NodeTypes,
  type NodeChange,
  Handle,
  Position,
  ReactFlowProvider,
  applyNodeChanges,
  useStore,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./graph.css";
import Dagre from "@dagrejs/dagre";
import Toggle from "@cloudscape-design/components/toggle";
import { isolatedNodeIds, layoutForceGraph } from "./layout";
import { useLiveForceSimulation } from "./useLiveForceSimulation";

// ─── Types ──────────────────────────────────────────────────────────

export interface GraphNodeData {
  id: string;
  label: string;
  kind:
    | "class"
    | "object-property"
    | "datatype-property"
    | "grounded"
    | "unknown";
  uri?: string;
  /** True when the node has sub-classes that can be revealed by expanding. */
  hasChildren?: boolean;
  /** True when the node's sub-classes are currently all visible. */
  expanded?: boolean;
  /** Datatype properties of this class, revealed as nodes on hover. */
  attributes?: GraphAttribute[];
  [key: string]: unknown;
}

/** A datatype property (attribute) of a class. */
export interface GraphAttribute {
  label: string;
  range?: string;
}

export type GraphEdgeKind =
  | "subClassOf"
  | "suggestedSubClassOf"
  | "objectProperty";

export interface GraphEdgeData {
  id: string;
  source: string;
  target: string;
  label?: string;
  /** Drives edge styling: subclass edges are dashed purple with a hollow arrow. */
  kind?: GraphEdgeKind;
}

export type GraphLayout = "hierarchical" | "force";

interface Props {
  nodes: GraphNodeData[];
  edges: GraphEdgeData[];
  /** See module doc. Defaults to `hierarchical`. */
  layout?: GraphLayout;
  /**
   * In force layout, whether standalone (relationship-less) classes start
   * hidden. Default true (proposal). The toggle to show/hide them is offered
   * either way; set false (Explorer) so curated search results aren't hidden
   * on first render.
   */
  collapseStandalone?: boolean;
  onNodeSelect?: (node: GraphNodeData | null) => void;
  /**
   * Called when a node is double-clicked. Used to lazily expand a class's
   * sub-classes (or collapse them again when already expanded).
   */
  onNodeExpand?: (node: GraphNodeData) => void;
}

/** React Flow node carrying our typed ontology payload. */
type OntologyFlowNode = Node<GraphNodeData>;

/** Rendered node diameter (px). Shared with the layout so spacing matches. */
const NODE_SIZE = 80;

/** Edge labels are hidden below this zoom — they are unreadable confetti there. */
export const EDGE_LABEL_MIN_ZOOM = 0.55;

/** Arrowhead size (px, user space) and the gap it needs before the node edge. */
const ARROW_SIZE = 16;
/** Attribute (datatype-property) node diameter, revealed on class hover. */
const ATTR_SIZE = 52;
const ARROW_GAP = 3;

// ─── Layout ─────────────────────────────────────────────────────────

/**
 * Evenly spaced points around a circle, starting at the top (12 o'clock)
 * and going clockwise. Radius grows with the node count so neighbours stay
 * ~110px apart along the circumference.
 */
export function radialPositions(count: number): { x: number; y: number }[] {
  if (count <= 0) return [];
  if (count === 1) return [{ x: 0, y: 0 }];
  const radius = Math.max(160, (count * 110) / (2 * Math.PI));
  return Array.from({ length: count }, (_, i) => {
    const angle = (2 * Math.PI * i) / count - Math.PI / 2;
    return { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
  });
}

/**
 * Arrange edge-less nodes evenly around a circle.
 *
 * Dagre stacks disconnected nodes into a single horizontal row, which is
 * what the initial "root classes" view is (no edges until a class is
 * expanded). A circular arrangement reads as a graph cluster instead of a
 * line and scales gracefully as the count grows.
 */
function circularLayout<T extends Node>(
  nodes: T[],
  edges: Edge[],
): {
  nodes: T[];
  edges: Edge[];
} {
  const positions = radialPositions(nodes.length);
  return {
    nodes: nodes.map((node, i) => ({ ...node, position: positions[i] })),
    edges,
  };
}

function layoutGraph<T extends Node>(
  nodes: T[],
  edges: Edge[],
  layout: GraphLayout,
): { nodes: T[]; edges: Edge[] } {
  if (layout === "force") {
    const positions = layoutForceGraph(
      nodes.map((n) => n.id),
      edges,
      { nodeSize: NODE_SIZE },
    );
    return {
      nodes: nodes.map((node) => ({
        ...node,
        position: positions.get(node.id) ?? { x: 0, y: 0 },
      })),
      edges,
    };
  }

  // No edges → circular cluster (initial root view). Dagre would otherwise
  // lay these out as one flat row.
  if (edges.length === 0) {
    return circularLayout(nodes, edges);
  }

  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", ranksep: 100, nodesep: 80 });

  for (const node of nodes) {
    g.setNode(node.id, { width: NODE_SIZE, height: NODE_SIZE });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  Dagre.layout(g);

  const half = NODE_SIZE / 2;
  return {
    nodes: nodes.map((node) => {
      const pos = g.node(node.id);
      // Defensive: dagre returns undefined for a node it never positioned
      // (shouldn't happen since every node is added above, but guard anyway).
      if (!pos) return { ...node, position: { x: 0, y: 0 } };
      return { ...node, position: { x: pos.x - half, y: pos.y - half } };
    }),
    edges,
  };
}

// ─── Custom Node ────────────────────────────────────────────────────

const KIND_COLORS: Record<string, { bg: string; border: string }> = {
  class: { bg: "#dbeafe", border: "#3b82f6" }, // blue — novel induced class
  grounded: { bg: "#ccfbf1", border: "#0d9488" }, // teal — grounded to reference ontology
  "object-property": { bg: "#fef3c7", border: "#f59e0b" }, // amber — relationship
  "datatype-property": { bg: "#fee2e2", border: "#ef4444" }, // red — attribute
  unknown: { bg: "#f3f4f6", border: "#9ca3af" },
};

const LEGEND_ITEMS = [
  { kind: "class", label: "Novel class" },
  { kind: "grounded", label: "Grounded class" },
  { kind: "object-property", label: "Relationship" },
  { kind: "datatype-property", label: "Attribute" },
];

/** Edge visuals by kind. Subclass edges: dashed purple, hollow arrow (UML). */
const EDGE_STYLES: Record<
  GraphEdgeKind,
  { stroke: string; dash?: string; marker: MarkerType }
> = {
  objectProperty: { stroke: "#94a3b8", marker: MarkerType.ArrowClosed },
  subClassOf: {
    stroke: "#7c3aed",
    dash: "6 4",
    marker: MarkerType.Arrow,
  },
  suggestedSubClassOf: {
    stroke: "#a78bfa",
    dash: "2 4",
    marker: MarkerType.Arrow,
  },
};

function styleEdge(e: GraphEdgeData, showLabel: boolean): Edge {
  const s = EDGE_STYLES[e.kind ?? "objectProperty"];
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    label: showLabel ? e.label : undefined,
    style: { stroke: s.stroke, strokeWidth: 1.5, strokeDasharray: s.dash },
    markerEnd: {
      type: s.marker,
      color: s.stroke,
      width: ARROW_SIZE,
      height: ARROW_SIZE,
      markerUnits: "userSpaceOnUse",
      strokeWidth: 1.5,
    },
    // Hierarchical mode still uses React Flow's SVG label; force mode renders
    // an HTML label (see FloatingEdge) styled by .coa-graph-edge-label.
    labelStyle: { fontSize: 10, fill: "#0f172a", fontWeight: 500 },
    labelBgStyle: { fill: "#ffffff", stroke: "#cbd5e1", strokeWidth: 1 },
    labelBgPadding: [5, 3],
    labelBgBorderRadius: 3,
  };
}

/**
 * Hover state delivered to nodes via context rather than through node `data`,
 * so hovering never re-creates node objects (which would make React Flow
 * re-render every node and fight the live simulation's position updates).
 */
const HoverContext = createContext<{
  hoveredId: string | null;
  neighbourIds: ReadonlySet<string> | null;
}>({ hoveredId: null, neighbourIds: null });

function OntologyNode({ data }: { data: GraphNodeData }) {
  const colors = KIND_COLORS[data.kind] ?? KIND_COLORS.unknown;
  const { hoveredId, neighbourIds } = useContext(HoverContext);
  const dimmed =
    hoveredId !== null && neighbourIds !== null && !neighbourIds.has(data.id);
  return (
    <div
      className="coa-graph-node"
      title={
        data.hasChildren
          ? `${data.label} — double-click to expand sub-classes`
          : data.label
      }
      style={{
        opacity: dimmed ? 0.25 : 1,
        position: "relative",
        width: NODE_SIZE,
        height: NODE_SIZE,
        borderRadius: "50%",
        border: `3px solid ${colors.border}`,
        background: colors.bg,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 11,
        fontFamily: "system-ui, sans-serif",
        textAlign: "center",
        cursor: "grab",
        overflow: "hidden",
        padding: 6,
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div
        style={{
          fontWeight: 600,
          lineHeight: 1.2,
          wordBreak: "break-word",
          color: "#000000",
          // Clamp long labels to three lines; the full name is in the tooltip.
          display: "-webkit-box",
          WebkitLineClamp: 3,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {data.label}
      </div>
      {data.hasChildren && (
        <span
          style={{
            position: "absolute",
            bottom: -4,
            right: -4,
            width: 18,
            height: 18,
            borderRadius: "50%",
            background: colors.border,
            color: "#ffffff",
            fontSize: 13,
            fontWeight: 700,
            lineHeight: "16px",
            textAlign: "center",
            border: "2px solid #ffffff",
            overflow: "hidden",
          }}
        >
          {data.expanded ? "−" : "+"}
        </span>
      )}
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  );
}

/**
 * Small red node for a class's datatype property (attribute). Rendered only
 * transiently around a class while it is hovered (see attributeOverlay), so it
 * is pointer-transparent — you hover the class, not its attributes.
 */
function AttributeNode({ data }: { data: GraphNodeData }) {
  const colors = KIND_COLORS["datatype-property"];
  return (
    <div
      title={data.label}
      style={{
        width: ATTR_SIZE,
        height: ATTR_SIZE,
        borderRadius: "50%",
        border: `2px solid ${colors.border}`,
        background: colors.bg,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        fontSize: 9,
        fontFamily: "system-ui, sans-serif",
        color: "#000000",
        overflow: "hidden",
        padding: 4,
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div
        style={{
          fontWeight: 500,
          lineHeight: 1.15,
          wordBreak: "break-word",
          display: "-webkit-box",
          WebkitLineClamp: 3,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {data.label}
      </div>
    </div>
  );
}

const nodeTypes: NodeTypes = {
  ontology: OntologyNode,
  attribute: AttributeNode,
};

// ─── Floating edge (force layout) ───────────────────────────────────

interface FloatingEdgeData extends Record<string, unknown> {
  /** Perpendicular offset (px) so parallel edges between one pair fan out. */
  curve?: number;
}

/**
 * Edge drawn from circle boundary to circle boundary along the line between
 * node centres, WebVOWL-style. The default edges anchor to the top/bottom
 * handles, which reads as a hierarchy the force layout does not have.
 */
function FloatingEdge({
  id,
  source,
  target,
  data,
  markerEnd,
  style,
  label,
}: EdgeProps<Edge<FloatingEdgeData>>) {
  const sourceNode = useInternalNode(source);
  const targetNode = useInternalNode(target);
  if (!sourceNode || !targetNode) return null;

  const centre = (n: InternalNode) => ({
    x: n.internals.positionAbsolute.x + (n.measured.width ?? NODE_SIZE) / 2,
    y: n.internals.positionAbsolute.y + (n.measured.height ?? NODE_SIZE) / 2,
  });
  const s = centre(sourceNode);
  const t = centre(targetNode);
  const rSource = (sourceNode.measured.width ?? NODE_SIZE) / 2;
  const rTarget = (targetNode.measured.width ?? NODE_SIZE) / 2;
  const dx = t.x - s.x;
  const dy = t.y - s.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const curve = data?.curve ?? 0;

  // Control point offset along the normal; the end points move slightly
  // around the circle in the same direction so the curve meets it cleanly.
  // The target end stops ARROW_GAP px short of the boundary: nodes paint
  // above edges, so a tip touching the circle is hidden under it.
  const nx = -uy;
  const ny = ux;
  const sx = s.x + ux * rSource + nx * curve * 0.3;
  const sy = s.y + uy * rSource + ny * curve * 0.3;
  const tx = t.x - ux * (rTarget + ARROW_GAP) + nx * curve * 0.3;
  const ty = t.y - uy * (rTarget + ARROW_GAP) + ny * curve * 0.3;

  let path: string;
  let labelX: number;
  let labelY: number;
  if (curve === 0) {
    [path, labelX, labelY] = getStraightPath({
      sourceX: sx,
      sourceY: sy,
      targetX: tx,
      targetY: ty,
    });
  } else {
    const cx = (sx + tx) / 2 + nx * curve;
    const cy = (sy + ty) / 2 + ny * curve;
    path = `M ${sx} ${sy} Q ${cx} ${cy} ${tx} ${ty}`;
    // Point on the quadratic Bézier at t = 0.5.
    labelX = 0.25 * sx + 0.5 * cx + 0.25 * tx;
    labelY = 0.25 * sy + 0.5 * cy + 0.25 * ty;
  }

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {label !== undefined && label !== null && label !== "" && (
        // HTML label instead of React Flow's SVG <EdgeText>: that one measures
        // itself with getBBox() on every render, which thrashes layout when
        // dozens of labels move each simulation tick.
        <EdgeLabelRenderer>
          <div
            className="coa-graph-edge-label"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              opacity: style?.opacity,
            }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes: EdgeTypes = {
  floating: FloatingEdge,
};

/**
 * Spread edges that share the same (unordered) node pair so their labels do
 * not stack: offsets are symmetric around zero, 44px apart.
 */
export function parallelEdgeCurves(edges: readonly GraphEdgeData[]): number[] {
  const groups = new Map<string, number[]>();
  edges.forEach((e, i) => {
    const key = [e.source, e.target].sort().join("\u0000");
    const g = groups.get(key) ?? [];
    g.push(i);
    groups.set(key, g);
  });
  const curves = new Array<number>(edges.length).fill(0);
  for (const indices of groups.values()) {
    const n = indices.length;
    indices.forEach((edgeIndex, k) => {
      // The normal is computed from each edge's own direction, so a reversed
      // edge in the same group must flip its sign to land on the other side.
      const e = edges[edgeIndex];
      const sign = e.source <= e.target ? 1 : -1;
      curves[edgeIndex] = sign * (k - (n - 1) / 2) * 44;
    });
  }
  return curves;
}

/**
 * Keep the graph's focal point pinned when the pane is resized.
 *
 * React Flow's viewport transform is anchored at the flow origin, so growing
 * the pane opens the new space at the right/bottom and whatever you were
 * looking at drifts toward the top-left corner. To hold the pane CENTER
 * stationary we shift the viewport by half the size delta: the flow point shown
 * at screen-center is `((w / 2) - x) / zoom`; keeping it fixed as `w → w'` gives
 * `x' = x + (w' - w) / 2` (same for height/y). Zoom is untouched — unlike
 * fitView, this preserves the user's current zoom and pan.
 */
function usePreserveCenterOnResize() {
  const width = useStore((s) => s.width);
  const height = useStore((s) => s.height);
  const { getViewport, setViewport } = useReactFlow();
  const prev = useRef<{ width: number; height: number } | null>(null);

  useEffect(() => {
    if (!width || !height) return; // pane not measured yet
    const last = prev.current;
    prev.current = { width, height };
    if (!last) return; // first measurement — nothing to compensate against
    const dw = width - last.width;
    const dh = height - last.height;
    if (dw === 0 && dh === 0) return;
    const vp = getViewport();
    setViewport({ x: vp.x + dw / 2, y: vp.y + dh / 2, zoom: vp.zoom });
  }, [width, height, getViewport, setViewport]);
}

// ─── Component ──────────────────────────────────────────────────────

function OntologyGraphFlowInner({
  nodes,
  edges,
  layout = "hierarchical",
  collapseStandalone = true,
  onNodeSelect,
  onNodeExpand,
}: Props) {
  const isForce = layout === "force";

  // Classes with no relationships carry no information in a *relationship*
  // graph; in real proposals they are ~40% of the nodes. A toggle hides them
  // (WebVOWL's "collapsing filter"). `collapseStandalone` only sets the DEFAULT:
  // the proposal starts collapsed; the Explorer starts expanded so a curated
  // search result is never hidden — but the toggle is offered either way.
  const isolated = useMemo(
    () =>
      isForce
        ? isolatedNodeIds(
            nodes.map((n) => n.id),
            edges,
          )
        : new Set<string>(),
    [isForce, nodes, edges],
  );
  const [showIsolated, setShowIsolated] = useState(!collapseStandalone);
  const visibleNodes = useMemo(
    () =>
      showIsolated || isolated.size === 0
        ? nodes
        : nodes.filter((n) => !isolated.has(n.id)),
    [nodes, isolated, showIsolated],
  );

  // Edge labels only when zoomed in enough to read them. Selecting the boolean
  // (not the zoom) means the graph re-renders only when the threshold is
  // crossed, not on every pan/zoom frame.
  const showEdgeLabels = useStore((s) => s.transform[2] >= EDGE_LABEL_MIN_ZOOM);

  const flowNodes: OntologyFlowNode[] = useMemo(
    () =>
      visibleNodes.map((n) => ({
        id: n.id,
        type: "ontology",
        position: { x: 0, y: 0 },
        data: n,
        draggable: true,
      })),
    [visibleNodes],
  );

  // Hovering a node highlights its relationships (labels shown regardless of
  // zoom) and dims everything else — the graph answers "what is this
  // connected to?" without a click.
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const flowEdges: Edge[] = useMemo(() => {
    const curves = parallelEdgeCurves(edges);
    return edges.map((e, i) => {
      const touchesHovered =
        hoveredId !== null &&
        (e.source === hoveredId || e.target === hoveredId);
      const styled = styleEdge(e, showEdgeLabels || touchesHovered);
      if (hoveredId !== null) {
        styled.style = {
          ...styled.style,
          opacity: touchesHovered ? 1 : 0.12,
          strokeWidth: touchesHovered ? 2.5 : 1.5,
        };
        styled.zIndex = touchesHovered ? 1 : 0;
      }
      return isForce
        ? { ...styled, type: "floating", data: { curve: curves[i] } }
        : styled;
    });
  }, [edges, isForce, showEdgeLabels, hoveredId]);

  // Layout depends on topology only, not on label visibility — key it on the
  // structural inputs so toggling labels never re-runs the simulation.
  const structuralEdges = useMemo(
    () => edges.map((e) => ({ id: e.id, source: e.source, target: e.target })),
    [edges],
  );
  const initialNodes = useMemo(
    () => layoutGraph(flowNodes, structuralEdges, layout).nodes,
    [flowNodes, structuralEdges, layout],
  );

  // Hold the focal point centered across pane resizes (see hook doc).
  usePreserveCenterOnResize();

  const [liveNodes, setLiveNodes] = useState<OntologyFlowNode[]>(initialNodes);

  // Keep in sync when the computed layout changes (state sync is a side
  // effect, so it belongs in useEffect — React may skip a useMemo body).
  useEffect(() => {
    setLiveNodes(initialNodes);
  }, [initialNodes]);

  // Force mode keeps the physics running: nodes spring into place on load and
  // follow a dragged neighbour (see hook doc).
  const { fitView, fitBounds } = useReactFlow();
  const simulation = useLiveForceSimulation(
    initialNodes,
    structuralEdges,
    setLiveNodes,
    {
      enabled: isForce,
      nodeSize: NODE_SIZE,
      linkDistance: 170,
    },
  );

  // Re-frame promptly whenever the node SET changes (e.g. a search or expand),
  // by fitting to the FINAL packed layout immediately rather than waiting for
  // the settle animation to finish — otherwise the viewport visibly lags the
  // results. The settle then animates inside the already-correct frame.
  const nodeSetSig = useMemo(
    () =>
      `${initialNodes.length}:${initialNodes[0]?.id ?? ""}:${
        initialNodes[initialNodes.length - 1]?.id ?? ""
      }`,
    [initialNodes],
  );
  useEffect(() => {
    if (!isForce || initialNodes.length === 0) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of initialNodes) {
      minX = Math.min(minX, n.position.x);
      minY = Math.min(minY, n.position.y);
      maxX = Math.max(maxX, n.position.x);
      maxY = Math.max(maxY, n.position.y);
    }
    const bounds = {
      x: minX,
      y: minY,
      width: maxX - minX + NODE_SIZE,
      height: maxY - minY + NODE_SIZE,
    };
    const handle = window.setTimeout(
      () => void fitBounds(bounds, { padding: 0.1, duration: 300 }),
      0,
    );
    return () => window.clearTimeout(handle);
  }, [isForce, nodeSetSig, initialNodes, fitBounds]);

  // Re-fit after the user toggles standalone classes: the graph's extent
  // changes substantially and the old viewport would show half of it.
  const isFirstRender = useRef(true);
  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false; // the `fitView` prop handles mount
      return;
    }
    const handle = window.setTimeout(
      () => void fitView({ padding: 0.1, duration: 250 }),
      0,
    );
    return () => window.clearTimeout(handle);
  }, [showIsolated, fitView]);

  const onNodesChange = useCallback(
    (changes: NodeChange<OntologyFlowNode>[]) =>
      setLiveNodes((nds) => applyNodeChanges(changes, nds)),
    [],
  );

  const handleNodeDragStart = useCallback(
    (_: unknown, node: OntologyFlowNode) =>
      simulation.onDragStart(node.id, node.position),
    [simulation],
  );
  const handleNodeDrag = useCallback(
    (_: unknown, node: OntologyFlowNode) =>
      simulation.onDrag(node.id, node.position),
    [simulation],
  );
  const handleNodeDragStop = useCallback(
    (_: unknown, node: OntologyFlowNode) => simulation.onDragStop(node.id),
    [simulation],
  );

  const handleNodeMouseEnter = useCallback(
    (_: unknown, node: OntologyFlowNode) => setHoveredId(node.id),
    [],
  );
  const handleNodeMouseLeave = useCallback(() => setHoveredId(null), []);

  const handleNodeClick = useCallback(
    (_: unknown, node: OntologyFlowNode) => {
      onNodeSelect?.(node.data);
    },
    [onNodeSelect],
  );

  const handleNodeDoubleClick = useCallback(
    (_: unknown, node: OntologyFlowNode) => {
      onNodeExpand?.(node.data);
    },
    [onNodeExpand],
  );

  const handlePaneClick = useCallback(() => {
    onNodeSelect?.(null);
  }, [onNodeSelect]);

  // Nodes not connected to the hovered one fade back (delivered via context —
  // see HoverContext).
  const neighbourIds = useMemo(() => {
    if (hoveredId === null) return null;
    const ids = new Set<string>([hoveredId]);
    for (const e of edges) {
      if (e.source === hoveredId) ids.add(e.target);
      if (e.target === hoveredId) ids.add(e.source);
    }
    return ids;
  }, [edges, hoveredId]);
  const hoverContext = useMemo(
    () => ({ hoveredId, neighbourIds }),
    [hoveredId, neighbourIds],
  );

  // Attributes are drawn only for the hovered class: a ring of small red nodes
  // around it, wired with red spokes. They live outside the simulation (pure
  // derived state) and are pointer-transparent, so hovering stays on the class.
  const attributeOverlay = useMemo(() => {
    const empty: { nodes: OntologyFlowNode[]; edges: Edge[] } = {
      nodes: [],
      edges: [],
    };
    if (!isForce || hoveredId === null) return empty;
    const host = liveNodes.find((n) => n.id === hoveredId);
    const attrs = host?.data.attributes ?? [];
    if (!host || attrs.length === 0) return empty;
    const cx = host.position.x + NODE_SIZE / 2;
    const cy = host.position.y + NODE_SIZE / 2;
    const radius = Math.max(140, ATTR_SIZE + attrs.length * 11);
    const attrColor = KIND_COLORS["datatype-property"].border;

    // Seed the attributes on a ring around the class, then relax them so they
    // don't overlap other graph nodes or each other — the same "nodes keep
    // their distance" feel as the force layout. Cheap: runs only while hovering.
    const centres = attrs.map((_, i) => {
      const angle = (2 * Math.PI * i) / attrs.length - Math.PI / 2;
      return {
        x: cx + radius * Math.cos(angle),
        y: cy + radius * Math.sin(angle),
      };
    });
    const obstacles = liveNodes
      .filter((n) => n.id !== hoveredId)
      .map((n) => ({
        x: n.position.x + NODE_SIZE / 2,
        y: n.position.y + NODE_SIZE / 2,
        r: NODE_SIZE / 2,
      }));
    // Relationship / FK edges as segments between their endpoints' centres, so
    // attributes can be pushed off the lines too (not just the nodes).
    const centreById = new Map(
      liveNodes.map((n) => [
        n.id,
        { x: n.position.x + NODE_SIZE / 2, y: n.position.y + NODE_SIZE / 2 },
      ]),
    );
    const segments = edges
      .map((e) => {
        const a = centreById.get(e.source);
        const b = centreById.get(e.target);
        return a && b ? { ax: a.x, ay: a.y, bx: b.x, by: b.y } : null;
      })
      .filter((s): s is NonNullable<typeof s> => s !== null);
    const attrR = ATTR_SIZE / 2;
    const gap = 12;
    const push = (
      p: { x: number; y: number },
      ox: number,
      oy: number,
      minDist: number,
      share: number,
    ) => {
      const dx = p.x - ox;
      const dy = p.y - oy;
      const d = Math.hypot(dx, dy) || 0.01;
      if (d < minDist) {
        const f = ((minDist - d) * share) / d;
        p.x += dx * f;
        p.y += dy * f;
      }
    };
    // Push a point off a line SEGMENT (its nearest point), so an attribute
    // never lands on a relationship/FK line.
    const pushOffSegment = (
      p: { x: number; y: number },
      ax: number,
      ay: number,
      bx: number,
      by: number,
      minDist: number,
    ) => {
      const abx = bx - ax;
      const aby = by - ay;
      const len2 = abx * abx + aby * aby || 1;
      const t = Math.max(
        0,
        Math.min(1, ((p.x - ax) * abx + (p.y - ay) * aby) / len2),
      );
      const qx = ax + t * abx;
      const qy = ay + t * aby;
      let dx = p.x - qx;
      let dy = p.y - qy;
      let d = Math.hypot(dx, dy);
      if (d < 0.5) {
        // On the line: shove along the segment's normal to break the tie.
        dx = -aby;
        dy = abx;
        d = Math.hypot(dx, dy) || 1;
      }
      if (d < minDist) {
        const f = (minDist - d) / d;
        p.x += dx * f;
        p.y += dy * f;
      }
    };
    for (let iter = 0; iter < 10; iter++) {
      for (const p of centres) {
        for (const o of obstacles) push(p, o.x, o.y, o.r + attrR + gap, 1);
        for (const s of segments)
          pushOffSegment(p, s.ax, s.ay, s.bx, s.by, attrR + gap);
      }
      for (let i = 0; i < centres.length; i++) {
        for (let j = i + 1; j < centres.length; j++) {
          const a = centres[i];
          const b = centres[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const d = Math.hypot(dx, dy) || 0.01;
          const min = attrR * 2 + gap;
          if (d < min) {
            const f = (min - d) / 2 / d;
            a.x += dx * f;
            a.y += dy * f;
            b.x -= dx * f;
            b.y -= dy * f;
          }
        }
      }
    }

    const nodes: OntologyFlowNode[] = attrs.map((a, i) => {
      return {
        id: `attr:${hoveredId}:${i}`,
        type: "attribute",
        position: {
          x: centres[i].x - ATTR_SIZE / 2,
          y: centres[i].y - ATTR_SIZE / 2,
        },
        data: {
          id: `attr:${hoveredId}:${i}`,
          label: a.label,
          kind: "datatype-property",
        },
        draggable: false,
        selectable: false,
        zIndex: 5,
        style: { pointerEvents: "none" },
      };
    });
    const overlayEdges: Edge[] = attrs.map((a, i) => ({
      id: `attre:${hoveredId}:${i}`,
      source: hoveredId,
      target: `attr:${hoveredId}:${i}`,
      type: "floating",
      zIndex: 5,
      data: { curve: 0 },
      style: { stroke: attrColor, strokeWidth: 1.25 },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: attrColor,
        width: ARROW_SIZE * 0.7,
        height: ARROW_SIZE * 0.7,
        markerUnits: "userSpaceOnUse",
      },
    }));
    return { nodes, edges: overlayEdges };
  }, [isForce, hoveredId, liveNodes, edges]);

  const displayNodes = useMemo(
    () =>
      attributeOverlay.nodes.length
        ? [...liveNodes, ...attributeOverlay.nodes]
        : liveNodes,
    [liveNodes, attributeOverlay],
  );
  const displayEdges = useMemo(
    () =>
      attributeOverlay.edges.length
        ? [...flowEdges, ...attributeOverlay.edges]
        : flowEdges,
    [flowEdges, attributeOverlay],
  );

  // Fullscreen the graph via the native Fullscreen API on a wrapper around the
  // canvas, and refit once the size settles.
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const toggleFullscreen = useCallback(() => {
    const el = wrapperRef.current;
    if (!el) return;
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void el.requestFullscreen?.();
    }
  }, []);
  useEffect(() => {
    const onChange = () => {
      setIsFullscreen(document.fullscreenElement === wrapperRef.current);
      // The pane resizes after the fullscreen transition; reframe once settled.
      window.setTimeout(() => void fitView({ padding: 0.1 }), 60);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [fitView]);

  return (
    <HoverContext.Provider value={hoverContext}>
      <div
        ref={wrapperRef}
        style={{
          width: "100%",
          height: "100%",
          // Transparent so the graph shows the (dark) surrounding container
          // rather than painting its own background. In fullscreen the browser
          // backdrop shows through, which is also dark.
          background: "transparent",
        }}
      >
        <ReactFlow
          nodes={displayNodes}
          edges={displayEdges}
          onNodesChange={onNodesChange}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodeClick={handleNodeClick}
          onNodeDoubleClick={handleNodeDoubleClick}
          onNodeDragStart={handleNodeDragStart}
          onNodeDrag={handleNodeDrag}
          onNodeDragStop={handleNodeDragStop}
          onNodeMouseEnter={handleNodeMouseEnter}
          onNodeMouseLeave={handleNodeMouseLeave}
          onPaneClick={handlePaneClick}
          fitView
          fitViewOptions={{ padding: 0.1 }}
          minZoom={0.15}
          maxZoom={2.5}
          nodesDraggable
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
          <Controls showInteractive={false} />
          <Panel position="top-left">
            <button
              type="button"
              onClick={toggleFullscreen}
              aria-label={isFullscreen ? "Exit full screen" : "Full screen"}
              title={isFullscreen ? "Exit full screen" : "Full screen"}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                background: "white",
                border: "1px solid #e5e7eb",
                borderRadius: 6,
                padding: "6px 10px",
                fontSize: 11,
                color: "#000000",
                cursor: "pointer",
              }}
            >
              <span aria-hidden="true">{isFullscreen ? "🡖" : "⛶"}</span>
              {isFullscreen ? "Exit full screen" : "Full screen"}
            </button>
          </Panel>
          <MiniMap
            nodeColor={(n) => {
              const kind =
                typeof n.data?.kind === "string" ? n.data.kind : "unknown";
              return KIND_COLORS[kind]?.border ?? "#9ca3af";
            }}
            style={{ height: 80, width: 120 }}
          />
          <Panel position="top-right">
            <div
              style={{
                background: "white",
                borderRadius: 6,
                padding: "8px 12px",
                fontSize: 11,
                color: "#000000",
                border: "1px solid #e5e7eb",
              }}
            >
              {LEGEND_ITEMS.map(({ kind, label }) => (
                <div
                  key={kind}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    marginBottom: 3,
                  }}
                >
                  <span
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      background: KIND_COLORS[kind].border,
                      display: "inline-block",
                    }}
                  />
                  <span>{label}</span>
                </div>
              ))}
              {isolated.size > 0 && (
                <div
                  style={{
                    marginTop: 8,
                    paddingTop: 8,
                    borderTop: "1px solid #e5e7eb",
                  }}
                >
                  <Toggle
                    checked={showIsolated}
                    onChange={({ detail }) => setShowIsolated(detail.checked)}
                  >
                    Show {isolated.size} standalone{" "}
                    {isolated.size === 1 ? "class" : "classes"}
                  </Toggle>
                </div>
              )}
            </div>
          </Panel>
        </ReactFlow>
      </div>
    </HoverContext.Provider>
  );
}

export function OntologyGraphFlow(props: Props) {
  return (
    <ReactFlowProvider>
      <OntologyGraphFlowInner {...props} />
    </ReactFlowProvider>
  );
}
