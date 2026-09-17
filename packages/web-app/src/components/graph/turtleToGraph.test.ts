// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { turtleToGraph } from "./turtleToGraph";

const SAMPLE = `
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ind: <http://ex.org/ind#> .

ind:LossPayment a owl:Class .
ind:ClaimAmount a owl:Class .
ind:LossPayment rdfs:subClassOf ind:ClaimAmount .
ind:LossPayment rdfs:label "loss_payment" .

ind:claim_paidBy a owl:ObjectProperty .
ind:claim_paidBy rdfs:domain ind:LossPayment .
ind:claim_paidBy rdfs:range ind:ClaimAmount .
`;

describe("turtleToGraph", () => {
  it("emits one node per owl:Class and labels them", () => {
    const { nodes } = turtleToGraph(SAMPLE);
    const ids = nodes.map((n) => n.id).sort();
    expect(ids).toEqual([
      "http://ex.org/ind#ClaimAmount",
      "http://ex.org/ind#LossPayment",
    ]);
    const labelled = nodes.find(
      (n) => n.id === "http://ex.org/ind#LossPayment",
    );
    expect(labelled?.data.prefLabel).toBe("loss_payment");
  });

  it("emits subClassOf edges as kind=subClassOf so the renderer applies the UML style", () => {
    const { edges } = turtleToGraph(SAMPLE);
    const sc = edges.find((e) => e.data?.kind === "subClassOf");
    expect(sc).toBeTruthy();
    expect(sc?.source).toBe("http://ex.org/ind#LossPayment");
    expect(sc?.target).toBe("http://ex.org/ind#ClaimAmount");
  });

  it("emits an objectProperty edge from domain to range", () => {
    const { edges } = turtleToGraph(SAMPLE);
    const op = edges.find((e) => e.data?.kind === "objectProperty");
    expect(op).toBeTruthy();
    expect(op?.source).toBe("http://ex.org/ind#LossPayment");
    expect(op?.target).toBe("http://ex.org/ind#ClaimAmount");
  });

  it("ignores triples whose tokens cannot be expanded against known prefixes", () => {
    const result = turtleToGraph("ind:Foo a owl:Class .");
    expect(result.nodes).toEqual([]);
  });

  // ── COA provenance + owl:hasKey extraction ──

  const RICH_SAMPLE = `
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ind: <http://ex.org/ind#> .

ind:LossPayment a owl:Class ;
    rdfs:label "loss_payment" ;
    rdfs:subClassOf ind:ClaimAmount ;
    coa:datasourceId "ds-7f3a9" ;
    coa:sourceSchema "pc_insurance" ;
    coa:subClassProvenance "PK_SHARING" ;
    owl:hasKey ( ind:lossPayment_claimAmountIdentifier ind:lossPayment_kind ) .

ind:ClaimAmount a owl:Class ;
    rdfs:label "claim_amount" ;
    coa:datasourceId "ds-7f3a9" ;
    coa:sourceSchema "pc_insurance" ;
    owl:hasKey ( ind:claimAmount_id ) .

ind:claim_filedBy a owl:ObjectProperty ;
    rdfs:domain ind:ClaimAmount ;
    rdfs:range ind:LossPayment ;
    coa:fkProvenance "DETERMINISTIC" .

ind:claim_assignedAdjuster a owl:ObjectProperty ;
    rdfs:domain ind:ClaimAmount ;
    rdfs:range ind:LossPayment ;
    coa:fkProvenance "AI_INFERRED" .
`;

  it("captures coa:datasourceId + coa:sourceSchema on the class node", () => {
    const { nodes } = turtleToGraph(RICH_SAMPLE);
    const lp = nodes.find((n) => n.id === "http://ex.org/ind#LossPayment");
    expect(lp?.data.datasourceId).toBe("ds-7f3a9");
    expect(lp?.data.sourceSchema).toBe("pc_insurance");
  });

  it("attaches coa:subClassProvenance to the child class node + subClassOf edge", () => {
    const result = turtleToGraph(RICH_SAMPLE);
    const lpNode = result.nodes.find(
      (n) => n.id === "http://ex.org/ind#LossPayment",
    );
    expect(lpNode?.data.subClassProvenance).toBe("PK_SHARING");
    const sc = result.edges.find((e) => e.data?.kind === "subClassOf");
    expect(sc?.data?.subClassProvenance).toBe("PK_SHARING");
  });

  it("attaches coa:fkProvenance per ObjectProperty edge", () => {
    const { edges } = turtleToGraph(RICH_SAMPLE);
    const fkEdges = edges.filter((e) => e.data?.kind === "objectProperty");
    const provenance = fkEdges.map((e) => e.data?.fkProvenance).sort();
    expect(provenance).toEqual(["AI_INFERRED", "DETERMINISTIC"]);
  });

  it("parses owl:hasKey inline lists into pkColumns (local names)", () => {
    const { nodes } = turtleToGraph(RICH_SAMPLE);
    const lp = nodes.find((n) => n.id === "http://ex.org/ind#LossPayment");
    expect(lp?.data.pkColumns).toEqual([
      "lossPayment_claimAmountIdentifier",
      "lossPayment_kind",
    ]);
    const ca = nodes.find((n) => n.id === "http://ex.org/ind#ClaimAmount");
    expect(ca?.data.pkColumns).toEqual(["claimAmount_id"]);
  });

  it("handles multi-predicate blocks separated by semicolons", () => {
    // Regression: the old line-oriented parser would only see the first
    // predicate-object pair on a multi-line ``;``-joined statement.
    const { nodes, edges } = turtleToGraph(RICH_SAMPLE);
    expect(nodes).toHaveLength(2);
    expect(edges.filter((e) => e.data?.kind === "objectProperty")).toHaveLength(
      2,
    );
    expect(edges.filter((e) => e.data?.kind === "subClassOf")).toHaveLength(1);
  });

  // ── Item 8: grounding keyed off skos:exactMatch/closeMatch ──

  const GROUNDED_SAMPLE = `
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix fnd: <https://fibo.org/FND/> .
@prefix ind: <http://ex.org/ind#> .

ind:Party a owl:Class ;
    rdfs:label "party" ;
    skos:exactMatch fnd:Party .

ind:Agreement a owl:Class ;
    rdfs:label "agreement" ;
    skos:closeMatch fnd:Contract .

ind:LossPayment a owl:Class ;
    rdfs:label "loss_payment" ;
    rdfs:subClassOf ind:Agreement ;
    coa:subClassProvenance "PK_SHARING" .
`;

  it("marks a class grounded from skos:exactMatch (not coa:groundedTo)", () => {
    const { nodes } = turtleToGraph(GROUNDED_SAMPLE);
    const party = nodes.find((n) => n.id === "http://ex.org/ind#Party");
    expect(party?.data.isGrounded).toBe(true);
    expect(party?.data.groundedTo).toBe("Party");
  });

  it("marks a class grounded from skos:closeMatch too", () => {
    const { nodes } = turtleToGraph(GROUNDED_SAMPLE);
    const agreement = nodes.find((n) => n.id === "http://ex.org/ind#Agreement");
    expect(agreement?.data.isGrounded).toBe(true);
    expect(agreement?.data.groundedTo).toBe("Contract");
  });

  it("does NOT mark PK-sharing (rdfs:subClassOf to a sibling) as grounded", () => {
    // rdfs:subClassOf must not be treated as grounding — it is also used for
    // PK-sharing to sibling ind: classes and would false-positive.
    const { nodes } = turtleToGraph(GROUNDED_SAMPLE);
    const lp = nodes.find((n) => n.id === "http://ex.org/ind#LossPayment");
    expect(lp?.data.isGrounded).toBe(false);
    expect(lp?.data.groundedTo).toBeUndefined();
  });
});
