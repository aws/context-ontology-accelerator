// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Link from "@cloudscape-design/components/link";
import Pagination from "@cloudscape-design/components/pagination";
import Select from "@cloudscape-design/components/select";
import type { SelectProps } from "@cloudscape-design/components/select";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import { PendingReviewBanner } from "@components/ontology/PendingReviewBanner";
import { OntologyInventoryView } from "./explorer/OntologyInventoryView";
import { InductedSourcesView } from "./explorer/InductedSourcesView";

import {
  searchEntities,
  getClass,
  getObjectProperty,
  getOntologyOverview,
  listOntologies,
  type GraphSearchHit,
  type GraphVertex,
  type ListedOntology,
} from "../../services/ontology-engine";
import {
  useGraphClasses,
  CLASS_DISPLAY_PAGE_SIZE,
} from "@api-hooks/use-graph-classes";
import { useApiClient } from "@components/ApiClientProvider";
import {
  useBreadcrumbs,
  type BreadcrumbItem,
} from "@components/BreadcrumbProvider";
import {
  OntologyGraphFlow,
  type GraphAttribute,
  type GraphNodeData,
} from "../../components/graph/OntologyGraphFlow";

/**
 * How many relationship hops out from a search match to pull into the graph,
 * so a matched class is shown together with its neighbours rather than alone.
 * One hop = the match's direct neighbours (both incoming and outgoing edges).
 */
const SEARCH_NEIGHBOR_HOPS = 1;

/** Hard cap on nodes shown for a search so dense graphs stay readable. */
const MAX_SEARCH_NODES = 60;

/**
 * Per-ontology cap on classes shown in the initial (no-search) graph seed. An
 * ontology at or below this renders in FULL (every class + its relationships)
 * from a single bulk overview call — no per-class fan-out. One above it falls
 * back to that ontology's bounded sample (largest-subtree roots + capped
 * descendants), because past this point dagre layout and React Flow's DOM
 * rendering both degrade.
 *
 * The cap is deliberately PER ONTOLOGY, not on the namespace total: a namespace
 * that loaded a large foundational reference for grounding (Schema.org alone is
 * ~1010 classes, FIBO more) would otherwise blow a combined budget and drag its
 * small induced ontology down into the sampled path with it — which is exactly
 * the "graph is tiny and has no relationships" complaint this seed exists to
 * fix. Scoping per ontology means the induced ontology a user actually cares
 * about always renders in full, however big the references beside it are.
 *
 * Deliberately higher than MAX_SEARCH_NODES: a search result is meant to stay
 * tightly focused on a few matches, while the initial seed's whole point is to
 * show as much of the real namespace shape as the renderer can still handle.
 */
const MAX_SEED_CLASSES = 500;

/**
 * Ceiling on TOTAL seeded classes across all ontologies, so a namespace with
 * many mid-sized ontologies (each individually under MAX_SEED_CLASSES) can't
 * multiply into a graph the browser won't lay out. Ontologies are considered
 * induced-first, then smallest-first, and each one that no longer fits the
 * remaining budget falls back to its bounded sample — so the budget is spent on
 * the ontologies most likely to be the point of the view, and running out
 * degrades those ontologies rather than failing the whole seed.
 */
const MAX_SEED_CLASSES_TOTAL = 1500;

/**
 * How many display pages ahead of the current one must be backed by already
 * loaded rows. One page of lookahead means the next server chunk is requested
 * while the user is still reading the last page the current chunk backs, so the
 * fetch round-trip overlaps with reading time instead of being paid as a blank
 * table on the next page. Cheap: one chunk backs several display pages, so this
 * pulls at most one extra chunk that a user one page from the boundary is very
 * likely to need next.
 */
const PREFETCH_LOOKAHEAD_PAGES = 1;

function localName(uri: string | undefined | null): string {
  if (!uri) return "?";
  const hash = uri.lastIndexOf("#");
  const slash = uri.lastIndexOf("/");
  return uri.slice(Math.max(hash, slash) + 1) || uri;
}

type GraphNode = GraphNodeData;
type GraphEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
};
type GraphData = { nodes: GraphNode[]; edges: GraphEdge[] };

// Kinds rendered as graph NODES. Datatype properties (attributes) are
// deliberately excluded — they appear only on hover (see the attribute overlay
// in OntologyGraphFlow), never as standalone nodes here.
const VALID_KINDS = new Set(["class", "object-property"]);

/** Narrow an arbitrary backend kind string to the node-kind union. */
export function toKind(kind: string | undefined): GraphNode["kind"] {
  switch (kind) {
    case "class":
    case "object-property":
    case "datatype-property":
    case "grounded":
      return kind;
    default:
      return "unknown";
  }
}

function isSubClassEdge(predicate: string): boolean {
  return predicate.includes("subClassOf");
}

/** Sub-classes of a vertex: incoming `rdfs:subClassOf` edges from classes. */
export function childUrisOf(vertex: GraphVertex | undefined): string[] {
  if (!vertex) return [];
  return (vertex.edges ?? [])
    .filter(
      (e) =>
        isSubClassEdge(e.predicate) &&
        e.direction === "incoming" &&
        e.neighbor?.kind === "class" &&
        Boolean(e.neighbor?.uri),
    )
    .map((e) => e.neighbor.uri);
}

/** Super-classes of a vertex: outgoing `rdfs:subClassOf` edges to classes. */
export function parentUrisOf(vertex: GraphVertex | undefined): string[] {
  if (!vertex) return [];
  return (vertex.edges ?? [])
    .filter(
      (e) =>
        isSubClassEdge(e.predicate) &&
        e.direction === "outgoing" &&
        e.neighbor?.kind === "class" &&
        Boolean(e.neighbor?.uri),
    )
    .map((e) => e.neighbor.uri);
}

/**
 * Build the React Flow graph from the set of currently visible URIs.
 *
 * Nodes are taken from the visible set; edges connect only nodes that are
 * both visible. `rdfs:subClassOf` edges are always drawn parent → child so
 * the dagre top-to-bottom layout renders the taxonomy with roots on top.
 * Each class node is annotated with `hasChildren`/`expanded` so the graph
 * can render an expand (+) / collapse (−) affordance.
 */
export function buildGraphData(
  visibleUris: Set<string>,
  details: Map<string, GraphVertex>,
  attributesByClass?: Map<string, GraphAttribute[]>,
): GraphData {
  const nodes: GraphNode[] = [];
  for (const uri of visibleUris) {
    const vertex = details.get(uri);
    const children = childUrisOf(vertex);
    const hasChildren = children.length > 0;
    const expanded = hasChildren && children.every((c) => visibleUris.has(c));
    nodes.push({
      id: uri,
      label: vertex?.labels?.[0] || localName(uri),
      kind: toKind(vertex?.kind),
      uri,
      hasChildren,
      expanded,
      attributes: attributesByClass?.get(uri),
    });
  }

  const edges: GraphEdge[] = [];
  const seen = new Set<string>();
  let edgeIdx = 0;
  const addEdge = (source: string, target: string, label: string) => {
    const key = `${source}->${target}:${label}`;
    if (seen.has(key)) return;
    seen.add(key);
    edges.push({ id: `e-${edgeIdx++}`, source, target, label });
  };

  for (const uri of visibleUris) {
    const vertex = details.get(uri);
    if (!vertex) continue;
    for (const edge of vertex.edges ?? []) {
      const neighbor = edge.neighbor?.uri;
      if (!neighbor || !visibleUris.has(neighbor)) continue;
      if (!VALID_KINDS.has(edge.neighbor.kind)) continue;
      const label = edge.predicate_label || localName(edge.predicate);
      if (isSubClassEdge(edge.predicate)) {
        // Always parent → child regardless of stored direction.
        const parent = edge.direction === "outgoing" ? neighbor : uri;
        const child = edge.direction === "outgoing" ? uri : neighbor;
        addEdge(parent, child, label);
      } else {
        const source = edge.direction === "outgoing" ? uri : neighbor;
        const target = edge.direction === "outgoing" ? neighbor : uri;
        addEdge(source, target, label);
      }
    }
  }

  return { nodes, edges };
}

/** Collect all currently-visible descendants of `uri` (for collapse). */
export function collectVisibleDescendants(
  uri: string,
  details: Map<string, GraphVertex>,
  visibleUris: Set<string>,
): Set<string> {
  const result = new Set<string>();
  const stack = [uri];
  const seen = new Set([uri]);
  while (stack.length) {
    const current = stack.pop();
    if (!current) continue;
    for (const child of childUrisOf(details.get(current))) {
      if (!visibleUris.has(child) || seen.has(child)) continue;
      seen.add(child);
      result.add(child);
      stack.push(child);
    }
  }
  return result;
}

/**
 * Roots = loaded classes whose super-class is not itself loaded, sorted so
 * classes that have sub-classes (expandable) come first for a useful first view.
 */
export function computeRoots(details: Map<string, GraphVertex>): string[] {
  const loaded = new Set(details.keys());
  const roots: string[] = [];
  for (const [uri, vertex] of details) {
    const parents = parentUrisOf(vertex);
    if (!parents.some((p) => loaded.has(p))) roots.push(uri);
  }
  roots.sort((a, b) => {
    const ea = childUrisOf(details.get(a)).length > 0 ? 0 : 1;
    const eb = childUrisOf(details.get(b)).length > 0 ? 0 : 1;
    return ea - eb;
  });
  return roots;
}

const RDFS_SUBCLASS_OF = "http://www.w3.org/2000/01/rdf-schema#subClassOf";

/**
 * Cap on object-property edges materialized into the initial seed. The
 * proposal graph (client-side Turtle parse) is unbounded, but the Explorer
 * seed can reach thousands of edges (Schema.org alone returns ~1676), which
 * would push dagre into slow layout territory even at 500 nodes. 2× the class
 * cap gives every class room for a couple of relationships on average, which
 * is enough to see the "shape" of the ontology without freezing the browser.
 * Object properties whose domain/range aren't both in the visible class set
 * are dropped before this cap counts, so the budget goes to edges the user
 * actually sees.
 */
const MAX_SEED_RELATIONSHIPS = 1000;

/** A class as returned by the ontology-overview endpoint — the subset of
 *  {@link OntologyOverviewClass} the taxonomy builder needs. */
export interface TaxonomyClass {
  uri: string;
  label?: string | null;
  comment?: string | null;
  subClassOf?: string[] | null;
}

/** An object property as returned by the ontology-overview endpoint — the
 *  subset of {@link OntologyPropertySummary} the seed's relationship builder
 *  reads. The label defaults to the property's local name if omitted. */
export interface TaxonomyRelationship {
  uri: string;
  label?: string | null;
  domain?: string | null;
  range?: string | null;
}

/** One ontology's full (un-sampled) overview, as the seed planner reads it. */
export interface SeedCandidate {
  ontology_id: string;
  /** True for registry rows typed `induced` — these get first call on budget. */
  induced?: boolean;
  classes: TaxonomyClass[];
}

/**
 * Which ontologies the seed renders in full and which fall back to a bounded
 * sample, given each one's full class count.
 */
export interface SeedPlan {
  /** Render every class of these, plus their object-property relationships. */
  full: string[];
  /** Too large (or out of budget) — re-fetch these with `sample=true`. */
  sampled: string[];
}

/**
 * Decide, per ontology, whether the seed can render it in full.
 *
 * An ontology renders in full when its own class count is within
 * {@link MAX_SEED_CLASSES} *and* still fits the remaining
 * {@link MAX_SEED_CLASSES_TOTAL} budget; otherwise it falls back to its bounded
 * server-side sample. Candidates are considered induced-first and then
 * smallest-first, which (a) guarantees the induced ontology — the one the user
 * came to look at — is never starved by a large foundational reference loaded
 * beside it for grounding, and (b) fits as many ontologies as possible into the
 * budget. Ordering is fully deterministic (ties broken on ontology_id), so the
 * same namespace always seeds the same way rather than following whatever order
 * the registry happened to return.
 */
export function planGraphSeed(candidates: SeedCandidate[]): SeedPlan {
  const ordered = [...candidates].sort((a, b) => {
    if (!!a.induced !== !!b.induced) return a.induced ? -1 : 1;
    const byCount = a.classes.length - b.classes.length;
    if (byCount !== 0) return byCount;
    return a.ontology_id.localeCompare(b.ontology_id);
  });
  const plan: SeedPlan = { full: [], sampled: [] };
  let budget = MAX_SEED_CLASSES_TOTAL;
  for (const c of ordered) {
    const n = c.classes.length;
    if (n <= MAX_SEED_CLASSES && n <= budget) {
      plan.full.push(c.ontology_id);
      budget -= n;
    } else {
      plan.sampled.push(c.ontology_id);
    }
  }
  return plan;
}

/**
 * Synthesize a `GraphVertex` map from ontology-overview classes — the cheap
 * replacement for the per-class `getClass` fan-out the Graph view used to pay
 * on first open. Each `rdfs:subClassOf` parent becomes a bidirectional pair of
 * class edges (outgoing on the child, incoming on the parent), which is exactly
 * what `computeRoots` / `childUrisOf` / `parentUrisOf` / `buildGraphData` read.
 * Parent edges are emitted even when the parent is not itself in the overview
 * (e.g. a foundational super-class), so grounding parents still render.
 *
 * When ``relationships`` is passed, each object property whose domain AND
 * range are both present in the class set is materialized as an edge pair too
 * (outgoing on the domain vertex, incoming on the range vertex), so the seed
 * shows the ontology's relationships alongside its taxonomy — matching the
 * proposal-detail graph. Edges are capped at ``MAX_SEED_RELATIONSHIPS`` so
 * dagre layout stays responsive even on a Schema.org-scale ontology.
 *
 * Vertices are flagged `partial`: they carry only the taxonomy + relationship
 * edges, not the class's attributes, so the detail panel upgrades them via
 * `getClass` when a node is selected.
 */
export function buildTaxonomyVertices(
  classes: TaxonomyClass[],
  relationships: TaxonomyRelationship[] = [],
): Map<string, GraphVertex> {
  const labelOf = new Map<string, string>();
  for (const c of classes) {
    if (c.label) labelOf.set(c.uri, c.label);
  }
  const makeVertex = (uri: string): GraphVertex => ({
    uri,
    kind: "class",
    labels: labelOf.has(uri) ? [labelOf.get(uri)!] : [],
    comments: [],
    alt_labels: [],
    graph_uris: [],
    edges: [],
    partial: true,
  });
  const details = new Map<string, GraphVertex>();
  const vertexFor = (uri: string): GraphVertex => {
    let v = details.get(uri);
    if (!v) {
      v = makeVertex(uri);
      details.set(uri, v);
    }
    return v;
  };

  for (const c of classes) {
    const child = vertexFor(c.uri);
    if (c.comment) child.comments = [c.comment];
    for (const parentUri of c.subClassOf ?? []) {
      // Child → parent (outgoing) so parentUrisOf sees it.
      child.edges.push({
        predicate: RDFS_SUBCLASS_OF,
        predicate_label: "subClassOf",
        direction: "outgoing",
        neighbor: {
          uri: parentUri,
          label: labelOf.get(parentUri) ?? null,
          kind: "class",
        },
      });
      // Parent → child (incoming) so childUrisOf sees it (this is what
      // computeRoots' expandable-first sort and the expand affordance use).
      vertexFor(parentUri).edges.push({
        predicate: RDFS_SUBCLASS_OF,
        predicate_label: "subClassOf",
        direction: "incoming",
        neighbor: {
          uri: c.uri,
          label: labelOf.get(c.uri) ?? null,
          kind: "class",
        },
      });
    }
  }

  // Object-property relationships. Emit an edge pair only when domain AND
  // range are both class vertices we're already going to render — otherwise
  // the edge would dangle off-screen and inflate the render budget for
  // nothing. Cap the total so a dense ontology (~1.7k relationships in
  // Schema.org) doesn't push dagre into slow-layout territory; anything past
  // the cap is still reachable via node select (which upgrades the vertex to
  // its full getClass result).
  if (relationships.length > 0) {
    let relEmitted = 0;
    for (const rel of relationships) {
      if (relEmitted >= MAX_SEED_RELATIONSHIPS) break;
      const domain = rel.domain;
      const range = rel.range;
      if (!domain || !range) continue;
      if (!details.has(domain) || !details.has(range)) continue;
      const label = rel.label || localName(rel.uri);
      // Domain → range (outgoing on domain, incoming on range) so the shared
      // buildGraphData renders it as domain → range regardless of which
      // vertex it's read from.
      vertexFor(domain).edges.push({
        predicate: rel.uri,
        predicate_label: label,
        direction: "outgoing",
        neighbor: {
          uri: range,
          label: labelOf.get(range) ?? null,
          kind: "class",
        },
      });
      vertexFor(range).edges.push({
        predicate: rel.uri,
        predicate_label: label,
        direction: "incoming",
        neighbor: {
          uri: domain,
          label: labelOf.get(domain) ?? null,
          kind: "class",
        },
      });
      relEmitted += 1;
    }
  }
  return details;
}

/**
 * Filter class hits for the List view by a case-insensitive substring match
 * on label or URI. Empty/whitespace query returns the list unchanged.
 */
export function filterClassHits(
  classes: GraphSearchHit[],
  query: string,
): GraphSearchHit[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return classes;
  return classes.filter(
    (c) =>
      (c.label || "").toLowerCase().includes(needle) ||
      c.uri.toLowerCase().includes(needle),
  );
}

/** Info about the ontology a class belongs to, for the List view grouping. */
export type OntologyInfo = { id: string; name: string; induced: boolean };

/**
 * Derive the ontology_id from a class's `graph_uri`. Named graphs are keyed
 * `{base}/{namespace}/{urlencode(ontology_id)}`, so the ontology_id is the
 * decoded last path segment. Returns "" if it can't be derived.
 */
export function ontologyIdFromGraphUri(graphUri: string | undefined): string {
  if (!graphUri) return "";
  const seg = graphUri.split("/").pop() ?? "";
  try {
    return decodeURIComponent(seg);
  } catch {
    return seg;
  }
}

/**
 * Resolve the display info for a class's ontology: prefer the registry title
 * from `listOntologies`; fall back to the ontology_id's local name. `induced`
 * is true only for registry rows typed `induced`.
 */
export function resolveOntologyInfo(
  graphUri: string | undefined,
  byId: Map<string, ListedOntology>,
): OntologyInfo {
  const id = ontologyIdFromGraphUri(graphUri);
  const rec = byId.get(id);
  return {
    id,
    name: rec?.title || localName(id) || "Unknown ontology",
    induced: rec?.ontologyType === "induced",
  };
}

/**
 * Resolve a vertex's source ontologies from its named-graph URIs, for the
 * detail panel's "Defined in" section. A class IRI can be asserted in more than
 * one ontology graph (e.g. a shared IRI referenced by both schema.org and
 * FIBO), so `get_vertex` returns all of them in `graph_uris`; this maps each to
 * its registry display name (deduped, sorted, blanks dropped). The list view
 * shows one row per class; the full set of sources is surfaced only here.
 */
export function resolveVertexSources(
  graphUris: string[] | undefined,
  byId: Map<string, ListedOntology>,
): OntologyInfo[] {
  const byId2 = new Map<string, OntologyInfo>();
  for (const g of graphUris ?? []) {
    const info = resolveOntologyInfo(g, byId);
    if (info.id && !byId2.has(info.id)) byId2.set(info.id, info);
  }
  return [...byId2.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/** Sentinel option value for "no ontology filter" (search all named graphs). */
export const ALL_ONTOLOGIES_VALUE = "__all__";

/**
 * Build the ontology-filter Select options for the List view: an "All
 * ontologies" entry first, then one entry per registry ontology sorted by
 * title. Each option's ``value`` is the ontology_id (or {@link
 * ALL_ONTOLOGIES_VALUE}); its ``description`` marks induced ontologies.
 */
export function buildOntologyFilterOptions(
  byId: Map<string, ListedOntology>,
): SelectProps.Option[] {
  const records = [...byId.values()].sort((a, b) =>
    (a.title || a.ontologyId || "").localeCompare(
      b.title || b.ontologyId || "",
    ),
  );
  return [
    { value: ALL_ONTOLOGIES_VALUE, label: "All ontologies" },
    ...records.map((r) => ({
      value: r.ontologyId,
      label: r.title || localName(r.ontologyId),
      description: r.ontologyType === "induced" ? "Induced" : undefined,
    })),
  ];
}

/**
 * The default ontology-filter value: the first induced ontology by title, so
 * the Explorer opens scoped to the induced ontology the user most likely wants.
 * Falls back to {@link ALL_ONTOLOGIES_VALUE} when the namespace has no induced
 * ontology (or an empty registry).
 */
export function defaultOntologyFilterValue(
  byId: Map<string, ListedOntology>,
): string {
  const induced = [...byId.values()]
    .filter((r) => r.ontologyType === "induced")
    .sort((a, b) =>
      (a.title || a.ontologyId || "").localeCompare(
        b.title || b.ontologyId || "",
      ),
    );
  return induced[0]?.ontologyId ?? ALL_ONTOLOGIES_VALUE;
}

/** A List-view row: a class enriched with its ontology + description. */
export type ClassRow = {
  uri: string;
  label: string;
  description: string;
  /** Display string for the Ontology column: every source ontology's name,
   *  comma-joined (a class can be defined in more than one). */
  ontologyName: string;
  induced: boolean;
};

/**
 * Build the List-view rows from class hits: attach each class's source
 * ontologies (all of them, via the registry — a class IRI can be asserted in
 * more than one, e.g. a shared IRI in both schema.org and FIBO) and description
 * (from the ontology overview's per-class comment, keyed by URI). The Ontology
 * column shows every source name comma-joined.
 *
 * Row order is the server's order, preserved as-is. Rows must NOT be re-sorted
 * here: hits arrive one server page at a time, so any client-side ordering key
 * that differs from the backend's (label, then IRI) re-shuffles the whole
 * accumulated list every time a new chunk lands. Because the display slice is
 * computed by index over that list, re-sorting makes already-viewed rows move
 * across page boundaries — the same class shows up on two pages while others are
 * never shown at all.
 *
 * This is why the table no longer reads as ontology-grouped blocks: grouping
 * would have to come from the server's ORDER BY to be safe across pages. With
 * multi-source rows the grouping key was a comma-joined string anyway
 * ("Schema.org, FIBO"), so client-side grouping bought little and cost correct
 * pagination.
 */
export function buildClassRows(
  classes: GraphSearchHit[],
  ontologyById: Map<string, ListedOntology>,
  descriptionByUri: Map<string, string>,
): ClassRow[] {
  const rows: ClassRow[] = classes.map((c) => {
    const sources = resolveVertexSources(c.graph_uris, ontologyById);
    return {
      uri: c.uri,
      label: c.label || localName(c.uri),
      description: descriptionByUri.get(c.uri) ?? "",
      ontologyName:
        sources.length > 0
          ? sources.map((s) => s.name).join(", ")
          : "Unknown ontology",
      induced: sources.some((s) => s.induced),
    };
  });
  return rows;
}

/** Inputs that decide whether the List view is loading rather than empty. */
export interface ListLoadingState {
  /** First chunk has never resolved (TanStack `isLoading`). */
  readonly initialLoading: boolean;
  /** Any chunk request is in flight (TanStack `isFetching`). */
  readonly fetching: boolean;
  /** Rows loaded across all chunks so far. */
  readonly loadedCount: number;
  /** Rows the current display page actually has. */
  readonly pageRowCount: number;
  /** Another chunk exists on the server. */
  readonly hasNextPage: boolean;
  /** A next-chunk request is in flight right now. */
  readonly isFetchingNextPage: boolean;
}

/**
 * Whether the List view should render Cloudscape's loading state instead of its
 * `empty` slot.
 *
 * The display page size is smaller than the fetch chunk size, so one loaded
 * chunk backs several display pages and the page count comes from the server
 * total — a user can therefore reach a page whose rows have not been fetched
 * yet. That page slices to zero rows, which is "not here yet", not "nothing
 * here": reporting it as empty tells a user with hundreds of classes that the
 * namespace has none. Gated on `hasNextPage || isFetchingNextPage` so a
 * genuinely empty namespace, a no-hit filter, and an exhausted result set all
 * still fall through to the `empty` slot instead of spinning forever.
 *
 * Deliberately NOT `isFetchingNextPage || …`: a background prefetch fires while
 * a fully-backed page is on screen, and Cloudscape's `loading` replaces the
 * rows with a spinner, so an unqualified check would blank rows the user is
 * reading. Only a page with zero rows AND more data coming counts as loading.
 */
export function isListPageLoading(state: ListLoadingState): boolean {
  const {
    initialLoading,
    fetching,
    loadedCount,
    pageRowCount,
    hasNextPage,
    isFetchingNextPage,
  } = state;
  if (initialLoading) return true;
  // First chunk in flight (or a refetch that dropped the cache): nothing to show.
  if (fetching && loadedCount === 0) return true;
  if (pageRowCount > 0) return false;
  return hasNextPage || isFetchingNextPage;
}

/** Fetch a vertex using the endpoint matching its kind. Returns null on error. */
async function fetchVertexByKind(
  apiClient: Parameters<typeof getClass>[0],
  namespace: string,
  uri: string,
  kind: string,
): Promise<GraphVertex | null> {
  try {
    if (kind === "class") return await getClass(apiClient, namespace, uri);
    if (kind === "object-property")
      return await getObjectProperty(apiClient, namespace, uri);
  } catch {
    // Best-effort: a single neighbour failing must not abort the search.
  }
  return null;
}

const EXPLORER_TABS = new Set(["classes", "ontologies", "sources"]);

export function GraphSearchPage() {
  const { namespaceId } = useParams<{ namespaceId: string }>();
  const navigate = useNavigate();
  const apiClient = useApiClient();
  const { setBreadcrumbs, clearBreadcrumbs } = useBreadcrumbs();
  // Page-level tab. Deep-linkable via ?tab= so the Induction page (and any
  // "go look at what you just built" hand-off) can land the user directly on
  // the Ontologies inventory.
  const [searchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const [tab, setTab] = useState(
    requestedTab && EXPLORER_TABS.has(requestedTab) ? requestedTab : "classes",
  );
  const [query, setQuery] = useState("");
  // Graph-view loading (search / seed / focus). The List view has its own
  // loading state derived from the useGraphClasses query.
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasSearched, setHasSearched] = useState(false);
  const [selectedVertex, setSelectedVertex] = useState<GraphVertex | null>(
    null,
  );
  // Track if the selected vertex's full hydration failed (P0: partial shown as complete).
  const [hydrationFailedUri, setHydrationFailedUri] = useState<string | null>(
    null,
  );
  // Track URIs whose sub-class expansion failed (P0: missing children).
  const [expandFailedUris, setExpandFailedUris] = useState<Set<string>>(
    new Set(),
  );
  const [detailsMap, setDetailsMap] = useState<Map<string, GraphVertex>>(
    new Map(),
  );
  const [visibleUris, setVisibleUris] = useState<Set<string>>(new Set());
  // Total number of root classes available (visible roots may be capped).
  const [totalRoots, setTotalRoots] = useState(0);
  // Which pane is shown: a flat paginated class list (default), or the
  // interactive graph.
  const [view, setView] = useState<"graph" | "list">("list");
  // class URI → description, from the per-ontology overview calls. Feeds the
  // List view's Description column without loading each class's full vertex.
  const [descriptionByUri, setDescriptionByUri] = useState<Map<string, string>>(
    new Map(),
  );
  // ontology_id → registry record, for resolving ontology name + induced flag
  // in the List view (fetched once on mount alongside the classes).
  const [ontologyById, setOntologyById] = useState<Map<string, ListedOntology>>(
    new Map(),
  );

  // Attributes (datatype properties) grouped by class IRI, from the ontology
  // overviews the seed fetches — this is what the graph's hover overlay reads.
  const [attrByClass, setAttrByClass] = useState<Map<string, GraphAttribute[]>>(
    new Map(),
  );

  const graphData = useMemo(
    () => buildGraphData(visibleUris, detailsMap, attrByClass),
    [visibleUris, detailsMap, attrByClass],
  );

  // Size the graph row to fill exactly the viewport, with no page scrollbar and
  // no empty gap, in BOTH resize directions.
  //
  // Target: rowHeight = innerHeight - rowTop - bottomInset, recomputed on every
  // resize. `rowTop` is measured live so the PendingReviewBanner appearing above
  // the row (it renders only when pending proposals exist, after an async fetch)
  // pushes the row down and is accounted for. `bottomInset` is the fixed chrome
  // BELOW the row — the AppLayout content region's bottom padding — which a
  // naive `innerHeight - rowTop` ignores and which therefore still leaks a
  // scrollbar.
  //
  // We compute the target from the viewport (innerHeight - rowTop) rather than
  // from documentElement.scrollHeight because scrollHeight is FLOORED at the
  // viewport height: once the page fits, it can no longer report the empty space
  // below the content, so a shrink-only "overshoot" loop never grows the row
  // back when the window is enlarged (the bug this replaces). innerHeight and
  // rowTop are reactive in both directions.
  //
  // bottomInset is only directly observable while the page is actually
  // overflowing (then scrollHeight reflects true content height); we learn it in
  // that window and cache it. It does not vary with the banner, which sits above
  // the row. Until a first overflow is observed we keep the calc() fallback.
  const rowRef = useRef<HTMLDivElement>(null);
  const bottomInsetRef = useRef<number | null>(null);
  const [rowHeight, setRowHeight] = useState<number>();

  // Tracks whether the component is still mounted so async handlers invoked
  // from user events (not from a useEffect) can skip their trailing setState
  // if the user navigated away mid-flight — otherwise React logs a warning
  // and, in Strict Mode, tears the surviving state. useEffect-driven fetches
  // already use their own `cancelled` closure; this ref is for the imperative
  // async paths like ``focusVertexInGraph``.
  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);
  useLayoutEffect(() => {
    const measure = () => {
      const el = rowRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const scrollHeight = document.documentElement.scrollHeight;
      const overshoot = scrollHeight - window.innerHeight;

      // Learn the fixed bottom chrome the one moment it is observable: while the
      // page overflows, scrollHeight is the real content height, so the space
      // below the row's bottom edge is exactly that chrome. Refine whenever we
      // can see a real overflow.
      if (overshoot > 0.5) {
        bottomInsetRef.current = scrollHeight - rect.bottom;
      }
      const bottomInset = bottomInsetRef.current;
      if (bottomInset == null) return; // no overflow seen yet — keep calc fallback

      const next = Math.max(320, window.innerHeight - rect.top - bottomInset);
      setRowHeight((prev) =>
        prev != null && Math.abs(prev - next) < 1 ? prev : next,
      );
    };
    measure();
    window.addEventListener("resize", measure);
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    return () => {
      window.removeEventListener("resize", measure);
      observer.disconnect();
    };
  }, []);

  // Load the List view's Description column: one ontology-overview call per
  // ontology in the namespace registry fills in per-class descriptions in a
  // single SPARQL round-trip each. Keyed off the registry (`ontologyById`)
  // rather than a capped wildcard class fetch, so descriptions are complete for
  // classes on ANY server page — not just the first chunk. The class rows
  // themselves are loaded, paginated, by `useGraphClasses` below; this only
  // enriches them. Best-effort: a failed overview just leaves those rows with
  // no description.
  useEffect(() => {
    if (!namespaceId || ontologyById.size === 0) return;
    let cancelled = false;
    const ontologyIds = [...ontologyById.keys()];
    Promise.all(
      ontologyIds.map((id) =>
        getOntologyOverview(apiClient, namespaceId, id).catch(() => null),
      ),
    )
      .then((overviews) => {
        if (cancelled) return;
        const descByUri = new Map<string, string>();
        for (const ov of overviews) {
          for (const c of ov?.classes ?? []) {
            if (c.comment) descByUri.set(c.uri, c.comment);
          }
        }
        setDescriptionByUri(descByUri);
      })
      .catch(() => {
        // Best-effort: descriptions enrich the List view but aren't required.
      });
    return () => {
      cancelled = true;
    };
  }, [namespaceId, apiClient, ontologyById]);

  // Build the graph seed the first time the Graph view is opened without an
  // active search. Fetches the FULL (un-sampled) class list per ontology —
  // one bulk SPARQL round-trip per ontology via getOntologyOverview, same
  // primitive the description-loading effect above already uses — rather
  // than the old `sample=true` bounded seed (a few largest-subtree roots +
  // ≤50 descendants). Feedback was that the sampled seed felt "too
  // simplistic"; a namespace's real shape only shows once every class and its
  // relationships are on screen.
  //
  // planGraphSeed then decides PER ONTOLOGY which ones render in full and
  // which fall back to their bounded sample, so an oversized foundational
  // reference (Schema.org, FIBO) loaded for grounding degrades only itself and
  // not the induced ontology sitting beside it. Only the ontologies that don't
  // fit pay a second (sampled) round-trip. Classes past a sampled ontology's
  // bound stay reachable via expand/search, and deeper attributes per node are
  // still fetched on demand (expand / select).
  //
  // The "loaded once" latch is a ref (only read as a guard, never rendered), so
  // setting it never re-runs the effect — that self-invalidation was the earlier
  // spinner deadlock. Reset on namespace change so a fresh namespace re-seeds.
  // Graph-view ontology scope: "All ontologies" (default, seed everything) or a
  // single ontology (seed just that one). Separate from the List view's filter,
  // and declared here so the seed effect below can depend on it.
  const [graphOntologyFilter, setGraphOntologyFilter] =
    useState(ALL_ONTOLOGIES_VALUE);
  const graphOntologyId =
    graphOntologyFilter === ALL_ONTOLOGIES_VALUE
      ? undefined
      : graphOntologyFilter;

  // Attributes (datatype properties) grouped by class IRI, from the ontology
  // overviews the seed fetches — this is what the graph's hover overlay reads.

  const graphRootsLoadedRef = useRef(false);
  useEffect(() => {
    graphRootsLoadedRef.current = false;
  }, [namespaceId, graphOntologyId]);
  useEffect(() => {
    if (
      view !== "graph" ||
      graphRootsLoadedRef.current ||
      hasSearched ||
      !namespaceId ||
      ontologyById.size === 0
    )
      return;
    let cancelled = false;
    graphRootsLoadedRef.current = true;
    setLoading(true);

    // The ontologies to seed: the one picked in the graph filter, or every
    // ontology in the registry when "All ontologies" is selected.
    const ontologyIds = graphOntologyId
      ? [graphOntologyId]
      : [...ontologyById.keys()];

    Promise.all(
      ontologyIds.map((id) =>
        getOntologyOverview(apiClient, namespaceId, id)
          .then((ov) => ({ id, ov }))
          .catch(() => ({ id, ov: null })),
      ),
    )
      .then((results) => {
        if (cancelled) return;
        // Decide per ontology whether it renders in full, so one oversized
        // foundational reference can't drag the whole namespace (notably a
        // small induced ontology) onto the sampled path with it.
        const plan = planGraphSeed(
          results.map(({ id, ov }) => ({
            ontology_id: id,
            induced: ontologyById.get(id)?.ontologyType === "induced",
            classes: ov?.classes ?? [],
          })),
        );
        const byId = new Map(results.map(({ id, ov }) => [id, ov]));

        // Group each ontology's datatype properties by their domain class, so
        // the graph can reveal them on hover (see buildGraphData / the overlay).
        const attrs = new Map<string, GraphAttribute[]>();
        for (const { ov } of results) {
          for (const dp of ov?.datatypeProperties ?? []) {
            if (!dp.domain) continue;
            const list = attrs.get(dp.domain) ?? [];
            list.push({
              label: dp.label ?? localName(dp.uri),
              range: dp.range ? localName(dp.range) : undefined,
            });
            attrs.set(dp.domain, list);
          }
        }
        setAttrByClass(attrs);
        // Walk the plan (induced/smallest first) rather than the registry
        // order, so the relationship budget inside buildTaxonomyVertices is
        // also spent induced-first instead of on whichever ontology the
        // registry happened to list first.
        const seedOrder = [...plan.full, ...plan.sampled];
        const classes: TaxonomyClass[] = [];
        const relationships: TaxonomyRelationship[] = [];
        const fullSet = new Set(plan.full);
        for (const id of seedOrder) {
          const ov = byId.get(id);
          if (fullSet.has(id)) {
            for (const c of ov?.classes ?? []) classes.push(c);
          }
          // Relationships are collected for EVERY ontology, sampled ones
          // included: we already hold their full overview, and
          // buildTaxonomyVertices drops any edge whose domain/range isn't a
          // rendered class. So a sampled ontology still shows the
          // relationships that fall inside its sampled subset, instead of
          // rendering as a bare hierarchy.
          for (const p of ov?.objectProperties ?? []) relationships.push(p);
        }

        const finish = () => {
          const details = buildTaxonomyVertices(classes, relationships);
          const visible = new Set(details.keys());
          const roots = computeRoots(details);
          setDetailsMap(details);
          setTotalRoots(roots.length);
          setVisibleUris(visible);
        };

        // Everything fit — no second round-trip.
        if (plan.sampled.length === 0) {
          finish();
          return;
        }
        // Only the oversized ontologies pay a second call, for their bounded
        // sample (largest-subtree roots + capped descendants) — a coherent set
        // of taxonomies rather than an arbitrary slice of a truncated list.
        return Promise.all(
          plan.sampled.map((id) =>
            getOntologyOverview(apiClient, namespaceId, id, {
              sample: true,
            }).catch(() => null),
          ),
        ).then((sampledOverviews) => {
          if (cancelled) return;
          for (const ov of sampledOverviews) {
            for (const c of ov?.classes ?? []) classes.push(c);
          }
          finish();
        });
      })
      .catch((e) =>
        setError(e instanceof Error ? e.message : "Failed to load graph"),
      )
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, hasSearched, namespaceId, ontologyById, graphOntologyId]);

  // Load the ontology registry once to resolve names + induced flag for the
  // List view's per-ontology grouping. Best-effort: on failure the list falls
  // back to ontology_id local-names with no induced marker.
  useEffect(() => {
    if (!namespaceId) return;
    let cancelled = false;
    listOntologies(apiClient, namespaceId)
      .then((records) => {
        if (cancelled) return;
        setOntologyById(new Map(records.map((r) => [r.ontologyId, r])));
      })
      .catch(() => {
        // Best-effort: registry enriches ontology names in the List view.
      });
    return () => {
      cancelled = true;
    };
  }, [namespaceId, apiClient]);

  const handleSearch = async (term?: string) => {
    const q = (term ?? query).trim();
    if (!q || !namespaceId) return;
    setLoading(true);
    setError(null);
    setSelectedVertex(null);
    setHasSearched(true);
    try {
      const results = await searchEntities(apiClient, namespaceId, q, {
        limit: 50,
        // Honour the graph-view ontology dropdown: "All ontologies" leaves this
        // undefined (search everywhere); a specific pick restricts the matches.
        // Neighbour expansion below stays cross-ontology on purpose, so a
        // grounded class still shows its foundational links.
        ontology_id: graphOntologyId,
      });

      if (!results.length) {
        // No matches: clear only what the GRAPH shows. detailsMap is a shared
        // vertex cache the List view reads descriptions from — don't wipe it,
        // or every list row loses its description until a full reload.
        setVisibleUris(new Set());
        setTotalRoots(0);
        return;
      }

      // Only class / object-property matches become graph nodes; datatype
      // properties (attributes) are shown on hover, not as standalone nodes.
      const graphResults = results.filter((h) => VALID_KINDS.has(h.kind));
      if (graphResults.length === 0) {
        setVisibleUris(new Set());
        setTotalRoots(0);
        return;
      }

      // Merge into the existing cache rather than replacing it, so classes
      // loaded on mount (and read by the List view) survive a graph search.
      // The graph decides what to render via visibleUris, not the map size —
      // so track the URIs this search touched separately for visibility.
      const details = new Map<string, GraphVertex>(detailsMap);
      const searchUris = new Set<string>();
      await Promise.all(
        graphResults.map(async (hit) => {
          try {
            const vertex =
              hit.kind === "class"
                ? await getClass(apiClient, namespaceId, hit.uri)
                : await getObjectProperty(apiClient, namespaceId, hit.uri);
            details.set(hit.uri, vertex);
            searchUris.add(hit.uri);
          } catch {
            // Best-effort: skip individual search hits that fail to load.
          }
        }),
      );

      // Expand outward from the matches so each result is shown in context
      // with its neighbours (up to SEARCH_NEIGHBOR_HOPS hops), capped at
      // MAX_SEARCH_NODES to keep dense graphs readable.
      let frontier = new Set(searchUris);
      for (
        let hop = 0;
        hop < SEARCH_NEIGHBOR_HOPS && searchUris.size < MAX_SEARCH_NODES;
        hop++
      ) {
        // Collect not-yet-visible neighbours of the current frontier, keyed
        // by URI with the kind we need to fetch them.
        const toFetch = new Map<string, string>();
        for (const uri of frontier) {
          for (const edge of details.get(uri)?.edges ?? []) {
            const nUri = edge.neighbor?.uri;
            if (!nUri || searchUris.has(nUri) || toFetch.has(nUri)) continue;
            if (!VALID_KINDS.has(edge.neighbor.kind)) continue;
            toFetch.set(nUri, edge.neighbor.kind);
          }
        }
        if (toFetch.size === 0) break;

        const budget = MAX_SEARCH_NODES - searchUris.size;
        const batch = Array.from(toFetch.entries()).slice(0, budget);
        const fetched = await Promise.all(
          batch.map(([nUri, nKind]) =>
            // Reuse the cache when the neighbour is already loaded.
            details.get(nUri)
              ? Promise.resolve(details.get(nUri) ?? null)
              : fetchVertexByKind(apiClient, namespaceId, nUri, nKind),
          ),
        );
        const next = new Set<string>();
        batch.forEach(([nUri], i) => {
          const vertex = fetched[i];
          if (vertex) {
            details.set(nUri, vertex);
            searchUris.add(nUri);
            next.add(nUri);
          }
        });
        frontier = next;
      }

      setDetailsMap(details);
      setVisibleUris(searchUris);
      setTotalRoots(0);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Search failed");
    } finally {
      setLoading(false);
    }
  };

  // Reset the graph search: clear the query and fall back to the seed graph
  // (scoped to the current dropdown). Re-arms the seed latch so it re-runs.
  const handleClearSearch = () => {
    setQuery("");
    setSelectedVertex(null);
    setError(null);
    graphRootsLoadedRef.current = false;
    setHasSearched(false);
  };

  // Re-run the active search when the graph ontology scope changes, so the
  // dropdown re-filters results live instead of only on the next manual Search.
  // Skips the initial mount; only fires while a search is showing.
  const scopeChangeArmedRef = useRef(false);
  useEffect(() => {
    if (!scopeChangeArmedRef.current) {
      scopeChangeArmedRef.current = true;
      return;
    }
    if (hasSearched) void handleSearch();
    // handleSearch is re-created each render but reads current state; keying on
    // the scope alone is intentional (avoid re-running on unrelated renders).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphOntologyId]);

  const handleNodeSelect = (nodeData: GraphNodeData | null) => {
    if (!nodeData || !nodeData.id) {
      setSelectedVertex(null);
      setHydrationFailedUri(null);
      return;
    }
    const uri = nodeData.id;
    const vertex = detailsMap.get(uri);
    if (vertex) setSelectedVertex(vertex);
    setHydrationFailedUri(null);
    // Taxonomy vertices from the overview are `partial` — they carry the
    // subclass hierarchy but not the class's relationships/attributes. Upgrade
    // to the full vertex on demand so the detail panel is complete. The partial
    // vertex is shown immediately (label + description), then replaced.
    if ((!vertex || vertex.partial) && namespaceId) {
      void getClass(apiClient, namespaceId, uri)
        .then((full) => {
          setDetailsMap((prev) => new Map(prev).set(uri, full));
          // Only swap the panel if this node is still the selected one.
          setSelectedVertex((cur) => (cur?.uri === uri ? full : cur));
          // Clear ONLY our own marker. A slow success here must not wipe a
          // different node's legitimate warning: select A (slow), select B
          // (fails fast → marker=B), then A resolves — an unguarded clear would
          // drop B's warning while B is still selected and still partial.
          setHydrationFailedUri((cur) => (cur === uri ? null : cur));
        })
        .catch(() => {
          // P0: full hydration failed — the panel shows a partial vertex.
          // Safe to set unconditionally: the marker is only rendered when it
          // equals the selected vertex's URI, so a stale failure for a
          // deselected node stays invisible.
          setHydrationFailedUri(uri);
        });
    }
  };

  // Re-attempt the full-vertex fetch for a URI whose hydration failed. Keyed on
  // the URI alone so the "Retry" affordance in the detail panel doesn't have to
  // fabricate a GraphNodeData (which requires label + kind).
  const retryVertexHydration = async (uri: string) => {
    if (!namespaceId) return;
    try {
      const full = await getClass(apiClient, namespaceId, uri);
      setDetailsMap((prev) => new Map(prev).set(uri, full));
      setSelectedVertex((cur) => (cur?.uri === uri ? full : cur));
      // Same guard as the hydration path: clear only our own marker, so a slow
      // retry landing after the user moved on can't wipe the new node's warning.
      setHydrationFailedUri((cur) => (cur === uri ? null : cur));
    } catch {
      // Still failing — keep the warning marker so the user can retry again.
      setHydrationFailedUri(uri);
    }
  };

  // List-view row click: open the shared detail panel for the class, fetching
  // its full vertex if it wasn't already loaded by the graph.
  const handleClassRowClick = async (uri: string) => {
    if (!namespaceId) return;
    const cached = detailsMap.get(uri);
    if (cached) {
      setSelectedVertex(cached);
      return;
    }
    try {
      const vertex = await getClass(apiClient, namespaceId, uri);
      setDetailsMap((prev) => new Map(prev).set(uri, vertex));
      setSelectedVertex(vertex);
    } catch (e) {
      setError(
        e instanceof Error ? e.message : `Failed to load ${localName(uri)}`,
      );
    }
  };

  // "Graph view" link from the detail panel: switch to the graph view and
  // run a graph search for this class's name, so it opens centred on the class
  // with its neighbourhood. The selected class stays selected, so the
  // breadcrumb keeps its {Class} crumb and appends "Graph".
  const handleOpenInGraph = (vertex: GraphVertex) => {
    setSelectedVertex(null);
    setView("graph");
    // We already hold the exact vertex — skip the redundant label search +
    // re-fetch (each ~1s) and seed the graph directly from it, loading only
    // its 1-hop neighbours.
    void focusVertexInGraph(vertex);
  };

  // Seed the graph centred on a known vertex + its direct (1-hop) neighbours.
  // No search round-trip and no re-fetch of the vertex itself — only the
  // not-yet-cached neighbours are loaded.
  const focusVertexInGraph = async (vertex: GraphVertex) => {
    if (!namespaceId) return;
    setLoading(true);
    setError(null);
    setHasSearched(true);
    try {
      const details = new Map<string, GraphVertex>(detailsMap);
      details.set(vertex.uri, vertex);
      const visible = new Set<string>([vertex.uri]);

      // One hop out: distinct valid neighbours, capped for readability.
      const toFetch = new Map<string, string>();
      for (const edge of vertex.edges ?? []) {
        const nUri = edge.neighbor?.uri;
        if (!nUri || toFetch.has(nUri)) continue;
        if (!VALID_KINDS.has(edge.neighbor.kind)) continue;
        toFetch.set(nUri, edge.neighbor.kind);
      }
      const batch = Array.from(toFetch.entries()).slice(
        0,
        MAX_SEARCH_NODES - 1,
      );
      const fetched = await Promise.all(
        batch.map(([nUri, nKind]) =>
          details.get(nUri)
            ? Promise.resolve(details.get(nUri) ?? null)
            : fetchVertexByKind(apiClient, namespaceId, nUri, nKind),
        ),
      );
      // Neighbour fetches can outlive an unmount (event-handler async, not a
      // useEffect); skip the trailing setState in that case.
      if (!isMountedRef.current) return;
      batch.forEach(([nUri], i) => {
        const v = fetched[i];
        if (v) {
          details.set(nUri, v);
          visible.add(nUri);
        }
      });

      setDetailsMap(details);
      setVisibleUris(visible);
      setTotalRoots(0);
      // Reflect the focus in the search box without triggering a search.
      setQuery(vertex.labels?.[0] || localName(vertex.uri));
    } catch (e) {
      if (!isMountedRef.current) return;
      setError(e instanceof Error ? e.message : "Failed to open graph view");
    } finally {
      if (isMountedRef.current) setLoading(false);
    }
  };

  // "Expand" link from the detail panel: close the panel and navigate to the
  // full class-detail page (the same view reached from Induced Ontology →
  // class), which needs ontology_id + class_uri query params. The ontology_id
  // is derived from the vertex's named-graph URI.
  const handleExpandClass = (vertex: GraphVertex) => {
    if (!namespaceId) return;
    const ontologyId = ontologyIdFromGraphUri(vertex.graph_uris?.[0]);
    setSelectedVertex(null);
    navigate(
      `/namespaces/${namespaceId}/ontology/induced/class` +
        `?ontology_id=${encodeURIComponent(ontologyId)}` +
        `&class_uri=${encodeURIComponent(vertex.uri)}` +
        // Tag the origin so the class page's breadcrumb reflects that we came
        // from the Ontology Explorer, not the Ontology Induction path.
        `&from=explorer`,
    );
  };

  // "Ontology Explorer" crumb: return to the list root, clearing any selection.
  const handleGoToRoot = () => {
    setSelectedVertex(null);
    setView("list");
  };

  // Publish a live breadcrumb trail to the AppLayout breadcrumbs slot (the bar
  // below the top nav). Always present on this page and reflects the current
  // state:  COA › Ontology Explorer › [Class] › [Graph]
  //   - COA              → app root view
  //   - Ontology Explorer → clear selection, back to the list root
  //   - {Class}          → the selected class's detail panel (when one is open)
  //   - Graph            → shown when the graph view is active
  // Cleared on unmount.
  useEffect(() => {
    const crumbs: BreadcrumbItem[] = [
      { text: "COA", onClick: () => navigate("/") },
      { text: "Ontology" },
      { text: "Explorer", onClick: handleGoToRoot },
    ];
    if (selectedVertex) {
      crumbs.push({
        text: selectedVertex.labels?.[0] || localName(selectedVertex.uri),
        onClick: () => setSelectedVertex(selectedVertex),
      });
    }
    if (view === "graph") {
      crumbs.push({ text: "Graph", onClick: () => setView("graph") });
    }
    setBreadcrumbs(crumbs);
    return () => clearBreadcrumbs();
    // Re-run whenever the crumb-affecting state changes. The handlers are
    // recreated each render but that's fine — the trail is cheap to rebuild.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, selectedVertex, namespaceId]);

  // The List view now pages across EVERY class in the namespace via the
  // server, not a capped client-side slice. Typing in the search box filters
  // server-side too (the backend does the substring match over all classes),
  // so it is debounced to avoid a request per keystroke. The graph view keeps
  // its explicit Search button (handleSearch) — this debounce only drives the
  // list query.
  const [listQuery, setListQuery] = useState(query);
  useEffect(() => {
    const id = setTimeout(() => setListQuery(query), 300);
    return () => clearTimeout(id);
  }, [query]);

  // Ontology filter for the List view. Options are "All ontologies" + one per
  // registry ontology; the default lands on the induced ontology (see
  // defaultOntologyFilterValue). The selected ontology_id restricts the server
  // search; ALL_ONTOLOGIES_VALUE means no restriction (undefined to the hook).
  const ontologyFilterOptions = useMemo(
    () => buildOntologyFilterOptions(ontologyById),
    [ontologyById],
  );
  const [ontologyFilter, setOntologyFilter] = useState(ALL_ONTOLOGIES_VALUE);
  // Apply the induced-first default once the registry has loaded. A ref latches
  // it so a user's later choice of "All ontologies" isn't overwritten when the
  // registry map identity changes.
  const ontologyFilterDefaultedRef = useRef(false);
  useEffect(() => {
    ontologyFilterDefaultedRef.current = false;
  }, [namespaceId]);
  useEffect(() => {
    if (ontologyFilterDefaultedRef.current || ontologyById.size === 0) return;
    ontologyFilterDefaultedRef.current = true;
    setOntologyFilter(defaultOntologyFilterValue(ontologyById));
  }, [ontologyById]);
  const selectedOntologyOption =
    ontologyFilterOptions.find((o) => o.value === ontologyFilter) ??
    ontologyFilterOptions[0];
  const ontologyIdFilter =
    ontologyFilter === ALL_ONTOLOGIES_VALUE ? undefined : ontologyFilter;

  const {
    data: classPages,
    isLoading: classesLoading,
    isFetching: classesFetching,
    error: classesError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useGraphClasses(
    namespaceId,
    view === "list" ? listQuery : "",
    ontologyIdFilter,
    // Only the List view renders these rows; the Graph view has its own
    // sample/search path, so don't fetch a chunk + COUNT it won't use.
    view === "list",
  );

  // Flatten the loaded server pages into display rows, enriched with ontology
  // name/induced (registry) + description (overview map). Server order (label
  // A–Z, then IRI) is preserved — cross-page ontology grouping isn't possible
  // once results span multiple server pages, and re-sorting client-side would
  // reshuffle the accumulated list on every new chunk, pushing already-viewed
  // rows onto other display pages (duplicates + skips). See buildClassRows.
  const loadedHits = useMemo(
    () => (classPages?.pages ?? []).flatMap((p) => p.hits),
    [classPages],
  );
  const totalClassCount = classPages?.pages?.[0]?.total_count ?? 0;
  const classRows = useMemo(
    () => buildClassRows(loadedHits, ontologyById, descriptionByUri),
    [loadedHits, ontologyById, descriptionByUri],
  );

  // Surface a class-list load failure through the shared error banner.
  useEffect(() => {
    if (classesError)
      setError(
        classesError instanceof Error
          ? classesError.message
          : "Failed to load ontology classes",
      );
  }, [classesError]);

  // Controlled Cloudscape pagination. Page count comes from the server total
  // (known-total UX), so users see "N of M" even before every chunk is loaded.
  // Display page size ≤ fetch chunk size, so one loaded chunk backs several
  // display pages; crossing into a not-yet-loaded page triggers the next fetch.
  const [listPageIndex, setListPageIndex] = useState(1);
  // The server COUNT is authoritative while more chunks remain, so this is
  // closed-end pagination — the user can see and jump to the real last page.
  // Once paging is exhausted the loaded row count wins: `total_count` can
  // over-report relative to what the query actually yields (getNextPageParam
  // stops on a short chunk), and a page that can never be filled must not be
  // offered.
  const listPagesCount = useMemo(() => {
    const effectiveTotal = hasNextPage
      ? Math.max(totalClassCount, loadedHits.length)
      : loadedHits.length || totalClassCount;
    return Math.max(1, Math.ceil(effectiveTotal / CLASS_DISPLAY_PAGE_SIZE));
  }, [hasNextPage, totalClassCount, loadedHits.length]);
  // Reset to page 1 whenever the (debounced) query, ontology filter, or
  // namespace changes the result set. Namespace is included because the route
  // reuses this component across namespaces: without it, a switch keeps a page
  // index the freshly-loaded first chunk cannot back (an instant empty table).
  useEffect(() => {
    setListPageIndex(1);
  }, [listQuery, ontologyIdFilter, namespaceId]);
  // Never leave the controlled index pointing past the last real page — the
  // count shrinks when the backend runs dry short of `total_count`.
  useEffect(() => {
    setListPageIndex((prev) => Math.min(prev, listPagesCount));
  }, [listPagesCount]);
  // Prefetch the next server chunk one display page ahead of where the user is,
  // so the round-trip overlaps with reading the current page rather than
  // stalling on the next. Self-terminating: once the new chunk lands the row
  // count clears the threshold, so it never cascades through every chunk.
  useEffect(() => {
    if (!hasNextPage || isFetchingNextPage) return;
    const rowsNeeded =
      (listPageIndex + PREFETCH_LOOKAHEAD_PAGES) * CLASS_DISPLAY_PAGE_SIZE;
    if (rowsNeeded > loadedHits.length) void fetchNextPage();
  }, [
    listPageIndex,
    loadedHits.length,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  ]);

  const pagedClasses = useMemo(() => {
    const start = (listPageIndex - 1) * CLASS_DISPLAY_PAGE_SIZE;
    return classRows.slice(start, start + CLASS_DISPLAY_PAGE_SIZE);
  }, [classRows, listPageIndex]);

  const listLoading = isListPageLoading({
    initialLoading: classesLoading,
    fetching: classesFetching,
    loadedCount: loadedHits.length,
    pageRowCount: pagedClasses.length,
    hasNextPage,
    isFetchingNextPage,
  });

  // Double-click a class to reveal its sub-classes (or collapse them again).
  const handleNodeExpand = async (nodeData: GraphNodeData | null) => {
    if (!nodeData?.id || !namespaceId) return;
    const uri = nodeData.id;

    let vertex = detailsMap.get(uri);
    if (!vertex) {
      try {
        vertex = await getClass(apiClient, namespaceId, uri);
      } catch (e) {
        setError(
          e instanceof Error ? e.message : `Failed to expand ${localName(uri)}`,
        );
        return;
      }
    }

    const children = childUrisOf(vertex);
    if (children.length === 0) return;

    const allVisible = children.every((c) => visibleUris.has(c));
    if (allVisible) {
      // Collapse: hide this node's visible descendants.
      const toRemove = collectVisibleDescendants(uri, detailsMap, visibleUris);
      if (toRemove.size === 0) return;
      setVisibleUris((prev) => {
        const next = new Set(prev);
        for (const u of toRemove) next.delete(u);
        return next;
      });
      return;
    }

    // Expand: fetch any not-yet-loaded child vertices, then reveal children.
    const nextDetails = new Map(detailsMap);
    nextDetails.set(uri, vertex);
    const failed = new Set<string>();
    await Promise.all(
      children.map(async (child) => {
        if (nextDetails.has(child)) return;
        try {
          nextDetails.set(child, await getClass(apiClient, namespaceId, child));
        } catch {
          // P0: sub-class load failed — track it so the user knows it's missing.
          failed.add(child);
        }
      }),
    );
    setDetailsMap(nextDetails);
    setExpandFailedUris((prev) => {
      const next = new Set(prev);
      for (const f of failed) next.add(f);
      return next;
    });
    setVisibleUris((prev) => {
      const next = new Set(prev);
      next.add(uri);
      // Only reveal children that loaded successfully.
      for (const child of children) {
        if (!failed.has(child)) next.add(child);
      }
      return next;
    });
  };

  // The Classes tab body: the graph/list browse experience. Extracted as a
  // variable (not a component) so none of the surrounding state/effects change.
  const classesTab = (
    <>
      {/* position: relative so the detail panel can overlay the right edge
          (position: absolute) instead of being a flex sibling that steals
          width from — and shrinks — the graph plot when a node is selected. */}
      <div
        ref={rowRef}
        style={{
          display: "flex",
          // Measured at runtime (see the rowHeight effect above). The calc
          // fallback only applies for the first paint if measurement hasn't
          // landed yet; it is not the source of truth for the final layout.
          height: rowHeight ?? "calc(100vh - 120px)",
          gap: 16,
          position: "relative",
        }}
      >
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            minWidth: 0,
          }}
        >
          <SpaceBetween size="s">
            <SpaceBetween direction="horizontal" size="xs">
              <SegmentedControl
                selectedId={view}
                onChange={({ detail }) =>
                  setView(detail.selectedId as "graph" | "list")
                }
                label="View"
                options={[
                  { id: "list", text: "List", iconName: "list-view" },
                  { id: "graph", text: "Graph", iconName: "share" },
                ]}
              />
              <div style={{ flexGrow: 1, minWidth: 300 }}>
                <Input
                  value={query}
                  onChange={({ detail }) => setQuery(detail.value)}
                  onKeyDown={({ detail }) => {
                    if (detail.key === "Enter" && view === "graph")
                      handleSearch();
                  }}
                  placeholder={
                    view === "list"
                      ? "Filter classes..."
                      : "Search classes, properties..."
                  }
                  type="search"
                />
              </div>
              {view === "list" && (
                <Select
                  selectedOption={selectedOntologyOption}
                  onChange={({ detail }) =>
                    setOntologyFilter(
                      detail.selectedOption.value ?? ALL_ONTOLOGIES_VALUE,
                    )
                  }
                  options={ontologyFilterOptions}
                  ariaLabel="Restrict classes by ontology"
                  expandToViewport
                />
              )}
              {view === "graph" && (
                <Button
                  variant="primary"
                  onClick={() => handleSearch()}
                  loading={loading}
                >
                  Search
                </Button>
              )}
              {view === "graph" && hasSearched && (
                <Button
                  variant="normal"
                  iconName="close"
                  onClick={handleClearSearch}
                >
                  Clear
                </Button>
              )}
              {view === "graph" && (
                <Select
                  selectedOption={
                    ontologyFilterOptions.find(
                      (o) => o.value === graphOntologyFilter,
                    ) ?? ontologyFilterOptions[0]
                  }
                  onChange={({ detail }) =>
                    setGraphOntologyFilter(
                      detail.selectedOption.value ?? ALL_ONTOLOGIES_VALUE,
                    )
                  }
                  options={ontologyFilterOptions}
                  ariaLabel="Show one ontology or all"
                  expandToViewport
                />
              )}
            </SpaceBetween>
            {error && <StatusIndicator type="error">{error}</StatusIndicator>}
          </SpaceBetween>

          {view === "graph" &&
            graphData.nodes.length === 0 &&
            !loading &&
            hasSearched && (
              <Box
                textAlign="center"
                padding={{ top: "xxxl" }}
                color="text-body-secondary"
              >
                <Box variant="p" fontSize="heading-m">
                  No results found. Try a different search term.
                </Box>
              </Box>
            )}

          {view === "graph" &&
            graphData.nodes.length === 0 &&
            !loading &&
            !hasSearched && (
              <Box
                textAlign="center"
                padding={{ top: "xxxl" }}
                color="text-body-secondary"
              >
                <Box variant="p" fontSize="heading-m">
                  No classes found in this namespace yet.
                </Box>
              </Box>
            )}

          {view === "graph" && graphData.nodes.length > 0 && (
            <div
              style={{
                marginTop: 12,
                borderRadius: 8,
                // Fill the remaining column height (parent is a flex column
                // inside the measured full-height row) instead of a fixed
                // 500px, so the graph uses the whole page. minHeight:0 lets the
                // flex child shrink correctly; ReactFlow fills 100% of this box.
                flex: 1,
                minHeight: 0,
                overflow: "hidden",
              }}
            >
              <OntologyGraphFlow
                nodes={graphData.nodes}
                edges={graphData.edges}
                layout="force"
                onNodeSelect={handleNodeSelect}
                onNodeExpand={handleNodeExpand}
              />
            </div>
          )}

          {view === "graph" && graphData.nodes.length > 0 && (
            <SpaceBetween size="xs" direction="vertical">
              <Box
                variant="small"
                color="text-status-inactive"
                padding={{ top: "xs" }}
              >
                {totalRoots > graphData.nodes.length
                  ? `Showing ${graphData.nodes.length} of ${totalRoots} root classes · `
                  : `${graphData.nodes.length} nodes · ${graphData.edges.length} edges · `}
                double-click a class to expand its sub-classes
              </Box>
              {expandFailedUris.size > 0 && (
                <StatusIndicator type="warning">
                  {expandFailedUris.size} sub-class
                  {expandFailedUris.size > 1 ? "es" : ""} couldn't be loaded.{" "}
                  <Link
                    variant="primary"
                    onFollow={() => setExpandFailedUris(new Set())}
                  >
                    Dismiss
                  </Link>
                </StatusIndicator>
              )}
            </SpaceBetween>
          )}

          {/* List view — a paginated table of every class in the namespace,
              grouped by ontology (induced first). Clicking a row opens the same
              right-hand detail panel the graph uses. */}
          {view === "list" && (
            <div
              style={{ marginTop: 12, flex: 1, minHeight: 0, overflow: "auto" }}
            >
              <Table<ClassRow>
                items={pagedClasses}
                variant="embedded"
                loading={listLoading}
                loadingText="Loading classes…"
                trackBy="uri"
                selectionType="single"
                selectedItems={
                  selectedVertex
                    ? pagedClasses.filter((c) => c.uri === selectedVertex.uri)
                    : []
                }
                onSelectionChange={({ detail }) => {
                  const row = detail.selectedItems[0];
                  if (row) handleClassRowClick(row.uri);
                }}
                columnDefinitions={[
                  {
                    id: "label",
                    header: "Class",
                    cell: (c) => c.label,
                    isRowHeader: true,
                  },
                  {
                    id: "ontology",
                    header: "Ontology",
                    cell: (c) => c.ontologyName,
                  },
                  {
                    id: "description",
                    header: "Description",
                    cell: (c) =>
                      c.description || (
                        <Box variant="small" color="text-status-inactive">
                          —
                        </Box>
                      ),
                  },
                ]}
                empty={
                  <Box textAlign="center" color="text-body-secondary">
                    {classesError
                      ? "Couldn't load classes. See the error above and retry."
                      : listQuery.trim()
                        ? "No classes match your search."
                        : "No classes found in this namespace yet."}
                  </Box>
                }
                pagination={
                  <Pagination
                    currentPageIndex={listPageIndex}
                    pagesCount={listPagesCount}
                    onChange={({ detail }) =>
                      setListPageIndex(
                        Math.min(
                          Math.max(1, detail.currentPageIndex),
                          listPagesCount,
                        ),
                      )
                    }
                  />
                }
              />
            </div>
          )}
        </div>

        {/* Right detail panel — overlays the graph's right edge (absolute)
            rather than sitting in the flex row, so opening it does NOT shrink
            the graph plot area. Capped height with internal scroll; sits above
            the canvas. Click-away on the graph pane (onPaneClick → onNodeSelect
            null) and the header close button both dismiss it. */}
        {selectedVertex && (
          <div
            style={{
              position: "absolute",
              top: 0,
              right: 0,
              width: 570,
              maxHeight: "100%",
              overflowY: "auto",
              zIndex: 5,
            }}
          >
            <Container
              header={
                <Header
                  variant="h3"
                  actions={
                    <Button
                      variant="icon"
                      iconName="close"
                      ariaLabel="Close detail panel"
                      onClick={() => setSelectedVertex(null)}
                    />
                  }
                >
                  <SpaceBetween direction="horizontal" size="s">
                    <span>
                      {selectedVertex.labels?.[0] ||
                        localName(selectedVertex.uri)}
                    </span>
                    <Box variant="span" color="text-status-inactive">
                      <Link
                        variant="primary"
                        onFollow={() => handleExpandClass(selectedVertex)}
                      >
                        Expand
                      </Link>
                      {" | "}
                      <Link
                        variant="primary"
                        onFollow={() => handleOpenInGraph(selectedVertex)}
                      >
                        Graph view
                      </Link>
                    </Box>
                  </SpaceBetween>
                </Header>
              }
            >
              <SpaceBetween size="s">
                {hydrationFailedUri === selectedVertex.uri && (
                  <StatusIndicator type="warning">
                    Some details couldn't be loaded.{" "}
                    <Link
                      variant="primary"
                      // Retry through the URI-keyed path rather than
                      // synthesising a GraphNodeData: that interface requires
                      // `label` and `kind`, so building one from a URI alone
                      // needs an `as` cast the project forbids. This helper
                      // performs the same re-fetch keyed only on the URI.
                      onFollow={() =>
                        void retryVertexHydration(selectedVertex.uri)
                      }
                    >
                      Retry
                    </Link>
                  </StatusIndicator>
                )}
                <Box variant="p">
                  <strong>Kind:</strong> {selectedVertex.kind}
                </Box>
                <Box variant="p" fontSize="body-s" color="text-status-inactive">
                  {selectedVertex.uri}
                </Box>
                {selectedVertex.comments?.length > 0 && (
                  <Box variant="p">
                    <strong>Description:</strong>{" "}
                    {selectedVertex.comments.join(" ")}
                  </Box>
                )}

                {selectedVertex.alt_labels?.length > 0 && (
                  <Box variant="p">
                    <strong>Synonyms:</strong>{" "}
                    {selectedVertex.alt_labels.join(", ")}
                  </Box>
                )}

                {(() => {
                  // "Defined in": the ontologies whose named graphs assert this
                  // class IRI. Usually one; more than one when a shared IRI is
                  // referenced by multiple loaded ontologies (e.g. schema.org +
                  // FIBO). The list view collapses to one row per class — this
                  // is where the multiple sources are exposed.
                  const sources = resolveVertexSources(
                    selectedVertex.graph_uris,
                    ontologyById,
                  );
                  if (sources.length === 0) return null;
                  return (
                    <Box variant="p">
                      <strong>
                        Defined in{" "}
                        {sources.length > 1 ? "ontologies" : "ontology"}:
                      </strong>{" "}
                      {sources.map((s) => s.name).join(", ")}
                    </Box>
                  );
                })()}

                {(() => {
                  const edges = selectedVertex.edges ?? [];
                  const relEdges = edges.filter(
                    (e) => e.neighbor?.kind === "object-property",
                  );
                  const attrEdges = edges.filter(
                    (e) => e.neighbor?.kind === "datatype-property",
                  );
                  const matchEdges = edges.filter(
                    (e) =>
                      e.predicate?.includes("closeMatch") ||
                      e.predicate?.includes("exactMatch"),
                  );
                  return (
                    <>
                      {relEdges.length > 0 && (
                        <>
                          <Header variant="h3">
                            Relationships ({relEdges.length})
                          </Header>
                          <Table
                            items={relEdges}
                            columnDefinitions={[
                              {
                                id: "predicate",
                                header: "Predicate",
                                cell: (e) =>
                                  e.predicate_label || localName(e.predicate),
                              },
                              {
                                id: "neighbor",
                                header: "Neighbor",
                                cell: (e) =>
                                  e.neighbor?.label ||
                                  localName(e.neighbor?.uri),
                              },
                              {
                                id: "dir",
                                header: "↔",
                                cell: (e) =>
                                  e.direction === "incoming" ? "←" : "→",
                              },
                            ]}
                            variant="embedded"
                            wrapLines
                          />
                        </>
                      )}
                      {attrEdges.length > 0 && (
                        <>
                          <Header variant="h3">
                            Attributes ({attrEdges.length})
                          </Header>
                          <Table
                            items={attrEdges}
                            columnDefinitions={[
                              {
                                id: "predicate",
                                header: "Attribute",
                                cell: (e) =>
                                  e.predicate_label || localName(e.predicate),
                              },
                              {
                                id: "neighbor",
                                header: "Type / target",
                                cell: (e) =>
                                  e.neighbor?.label ||
                                  localName(e.neighbor?.uri),
                              },
                            ]}
                            variant="embedded"
                            wrapLines
                          />
                        </>
                      )}
                      {matchEdges.length > 0 && (
                        <>
                          <Header variant="h3">
                            Matches ({matchEdges.length})
                          </Header>
                          <Box variant="small" color="text-status-inactive">
                            Near-duplicate / grounding links (skos:closeMatch /
                            exactMatch) — drawn as edges in the graph; not
                            counted as relationships.
                          </Box>
                          <Table
                            items={matchEdges}
                            columnDefinitions={[
                              {
                                id: "predicate",
                                header: "Match type",
                                cell: (e) => localName(e.predicate),
                              },
                              {
                                id: "neighbor",
                                header: "Class",
                                cell: (e) =>
                                  e.neighbor?.label ||
                                  localName(e.neighbor?.uri),
                              },
                            ]}
                            variant="embedded"
                            wrapLines
                          />
                        </>
                      )}
                    </>
                  );
                })()}
              </SpaceBetween>
            </Container>
          </div>
        )}
      </div>
    </>
  );

  return (
    <SpaceBetween size="l">
      <PendingReviewBanner />
      <Header
        variant="h1"
        description="Browse the ontologies in this namespace, the classes inside them, and the sources they were induced from."
      >
        Explorer
      </Header>
      <Tabs
        activeTabId={tab}
        onChange={({ detail }) => setTab(detail.activeTabId)}
        tabs={[
          { id: "classes", label: "Classes", content: classesTab },
          {
            id: "ontologies",
            label: "Ontologies",
            content: (
              <OntologyInventoryView
                apiClient={apiClient}
                namespace={namespaceId}
              />
            ),
          },
          {
            id: "sources",
            label: "Inducted sources",
            content: (
              <InductedSourcesView
                apiClient={apiClient}
                namespace={namespaceId}
              />
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
