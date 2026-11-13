// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { nodeKindFromData } from "./ProposalDetail";
import { turtleToGraph } from "@components/graph/turtleToGraph";

const OWL_OBJECT_PROPERTY = "http://www.w3.org/2002/07/owl#ObjectProperty";

// Compact prefixed serialization, the shape the inducer emits: the grounded
// subject is written ``ind:ClinicalTrial skos:exactMatch <…>`` while the graph
// node id is the absolute IRI ``http://ex.org/ind#ClinicalTrial``.
const GROUNDED_TURTLE = `
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix ind: <http://ex.org/ind#> .

ind:ClinicalTrial a owl:Class ;
    rdfs:label "clinical_trial" ;
    skos:exactMatch <https://parallax.aws/onto/ClinicalStudy> .

ind:Sponsor a owl:Class ;
    rdfs:label "sponsor" ;
    skos:closeMatch <https://parallax.aws/onto/Organization> .

ind:Arm a owl:Class ;
    rdfs:label "arm" .
`;

describe("nodeKindFromData (ProposalDetail flow-node color coding)", () => {
  it("marks a class grounded via skos:exactMatch (compact prefixed subject)", () => {
    const { nodes } = turtleToGraph(GROUNDED_TURTLE);
    const ct = nodes.find((n) => n.id === "http://ex.org/ind#ClinicalTrial");
    expect(ct?.data.isGrounded).toBe(true);
    // The flow-node kind is "grounded".
    expect(nodeKindFromData(ct!.data)).toBe("grounded");
  });

  it("marks a class grounded via skos:closeMatch too", () => {
    const { nodes } = turtleToGraph(GROUNDED_TURTLE);
    const sponsor = nodes.find((n) => n.id === "http://ex.org/ind#Sponsor");
    expect(sponsor?.data.isGrounded).toBe(true);
    expect(nodeKindFromData(sponsor!.data)).toBe("grounded");
  });

  it("leaves an ungrounded class as kind 'class'", () => {
    const { nodes } = turtleToGraph(GROUNDED_TURTLE);
    const arm = nodes.find((n) => n.id === "http://ex.org/ind#Arm");
    expect(arm?.data.isGrounded).toBe(false);
    expect(nodeKindFromData(arm!.data)).toBe("class");
  });

  it("classifies object properties before grounding", () => {
    expect(
      nodeKindFromData({ typeIri: OWL_OBJECT_PROPERTY, isGrounded: true }),
    ).toBe("object-property");
  });
});
