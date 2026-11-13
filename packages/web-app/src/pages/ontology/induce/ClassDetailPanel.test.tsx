// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ClassDetailPanel, type ClassGroup } from "./ClassDetailPanel";
import { parseTurtleEntities, type TurtleEntity } from "./turtle";
import { parseVocabulary } from "./vocabulary";
import { parseR2rmlByClass, type ConceptMatch } from "./grounding";

const classEntity: TurtleEntity = {
  subject: "Claim",
  fullSubject: "ex:Claim",
  body: `ex:Claim a owl:Class ;\n    rdfs:label "Claim" ;\n    rdfs:comment "An insurance claim" .`,
  kind: "owl:Class",
};

const relProp: TurtleEntity = {
  subject: "hasPolicy",
  fullSubject: "ex:hasPolicy",
  body: `ex:hasPolicy a owl:ObjectProperty ;\n    rdfs:label "has policy" ;\n    rdfs:domain ex:Claim ;\n    rdfs:range ex:Policy .`,
  kind: "owl:ObjectProperty",
};

const attrProp: TurtleEntity = {
  subject: "amount",
  fullSubject: "ex:amount",
  body: `ex:amount a owl:DatatypeProperty ;\n    rdfs:label "amount" ;\n    rdfs:domain ex:Claim ;\n    rdfs:range xsd:decimal .`,
  kind: "owl:DatatypeProperty",
};

const group: ClassGroup = {
  classEntity,
  label: "Claim",
  description: "An insurance claim",
  groundedTo: null,
  matchType: null,
  pkColumns: [],
  subClassProvenance: null,
  hasSuggestedParent: false,
  relationships: [relProp],
  attributes: [attrProp],
};

describe("ClassDetailPanel", () => {
  it("renders class label and description fields", () => {
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
      />,
    );
    expect(screen.getByDisplayValue("Claim")).toBeTruthy();
    expect(screen.getByDisplayValue("An insurance claim")).toBeTruthy();
  });

  it("calls onSave with updated label", () => {
    const onSave = vi.fn();
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={false}
        onSave={onSave}
      />,
    );
    const labelInput = screen.getByDisplayValue("Claim");
    fireEvent.change(labelInput, { target: { value: "Updated Claim" } });
    fireEvent.click(screen.getByText("Apply"));
    expect(onSave).toHaveBeenCalledWith(
      expect.stringContaining("Updated Claim"),
    );
  });

  it("shows readonly fields when isReadOnly", () => {
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={true}
        onSave={vi.fn()}
      />,
    );
    expect(screen.queryByDisplayValue("Claim")).toBeNull(); // No input, just text
    expect(screen.getByText("Claim")).toBeTruthy();
  });

  it("shows grounded badge when grounded", () => {
    const grounded = {
      ...group,
      groundedTo: "schema:Claim",
      matchType: "exact",
    };
    render(
      <ClassDetailPanel
        group={grounded}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
      />,
    );
    expect(screen.getByText("schema:Claim")).toBeTruthy();
    expect(screen.getByText(/exact match/)).toBeTruthy();
  });

  it("shows Novel badge when not grounded", () => {
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
      />,
    );
    expect(screen.getByText("Novel")).toBeTruthy();
  });

  it("shows relationships and attributes in tabs", () => {
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
      />,
    );
    expect(screen.getByText("Relationships (1)")).toBeTruthy();
    expect(screen.getByText("Attributes (1)")).toBeTruthy();
  });

  it("navigates to property editor on click", () => {
    const onSaveProperty = vi.fn();
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
        onSaveProperty={onSaveProperty}
      />,
    );
    fireEvent.click(screen.getByText("has policy"));
    // Should show property editor with Back button
    expect(screen.getByText("Back to class")).toBeTruthy();
    expect(screen.getByDisplayValue("has policy")).toBeTruthy();
  });

  it("renders human-readable R2RML mapping table when expanded", () => {
    const r2rml = `<#ClaimMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "claims" ] ;
    rr:subjectMap [ rr:class ex:Claim ] ;
    rr:predicateObjectMap [
      rr:predicate ex:amount ;
      rr:objectMap [ rr:column "claim_amount" ; rr:datatype xsd:decimal ]
    ] .`;
    const mapping = parseR2rmlByClass(r2rml).get("Claim") ?? null;
    const { container } = render(
      <ClassDetailPanel
        group={group}
        r2rml={r2rml}
        r2rmlMapping={mapping}
        isReadOnly={false}
        onSave={vi.fn()}
      />,
    );
    // The section header shows the real table name, not "Unknown".
    expect(container.textContent).toContain("claims");
    expect(container.textContent).toContain("claim_amount");
    expect(container.textContent).not.toContain("Unknown");
  });

  it("shows PK columns + PK-sharing + suggested-parent indicators", () => {
    const enriched: ClassGroup = {
      ...group,
      pkColumns: ["claim_id", "policy_id"],
      subClassProvenance: "PK_SHARING",
      hasSuggestedParent: true,
    };
    render(
      <ClassDetailPanel
        group={enriched}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
      />,
    );
    expect(screen.getByText("claim_id")).toBeTruthy();
    expect(screen.getByText("policy_id")).toBeTruthy();
    expect(screen.getByText("PK-sharing")).toBeTruthy();
    // The suggested-parent chip surfaces in the card body.
    expect(screen.getAllByText("Suggested subclass").length).toBeGreaterThan(0);
  });

  it("folds an editable grounding candidate table into the Grounding tab", () => {
    const groundingMatch: ConceptMatch = {
      source_table: "claim",
      source_column: "",
      matched_class_uri: "https://parallax.aws/onto/Claim",
      matched_ontology_id: "https://parallax.aws/onto",
      similarity: 0.9,
      match_type: "high_confidence",
      candidates: [
        {
          entity_uri: "https://parallax.aws/onto/Claim",
          ontology_id: "https://parallax.aws/onto",
          lexical_sim: 0.9,
        },
        {
          entity_uri: "https://parallax.aws/onto/InsuranceClaim",
          ontology_id: "https://parallax.aws/onto",
          lexical_sim: 0.7,
        },
      ],
    };
    const onSelect = vi.fn();
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
        groundingMatch={groundingMatch}
        onSelectGroundingCandidate={onSelect}
      />,
    );
    fireEvent.click(screen.getByText("Grounding (2)"));
    // The current grounding status is surfaced above the candidate table.
    expect(screen.getByText(/Grounded to/)).toBeTruthy();
    // The row is labelled "Ground to <localName>" for the selection control.
    const pickInsurance = screen.getByRole("radio", {
      name: /Ground to InsuranceClaim/,
    });
    fireEvent.click(pickInsurance);
    expect(onSelect).toHaveBeenCalledWith(
      "claim",
      "https://parallax.aws/onto/InsuranceClaim",
    );
  });

  it("renders SuggestionPanel Confirm/Reject when the class has a coa:suggestedSubClassOf", () => {
    // Whole proposal Turtle carrying a suggested-subclass annotation on the
    // selected class (ex:Claim → ex:Policy). SuggestionPanel parses the full
    // document, so it needs the prefix declarations + the annotation triple.
    const fullTurtle = `@prefix ex: <http://example.com/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .

ex:Claim a owl:Class ;
    rdfs:label "Claim" ;
    coa:suggestedSubClassOf ex:Policy ;
    coa:suggestionReason "shared primary key" .

ex:Policy a owl:Class ;
    rdfs:label "Policy" .`;
    const onUpdateTurtle = vi.fn();
    const suggested: ClassGroup = { ...group, hasSuggestedParent: true };
    render(
      <ClassDetailPanel
        group={suggested}
        r2rml={null}
        prelude={`@prefix ex: <http://example.com/> .`}
        fullTurtle={fullTurtle}
        isReadOnly={false}
        onSave={vi.fn()}
        onUpdateTurtle={onUpdateTurtle}
      />,
    );
    // Actionable Confirm/Reject controls (not just a static badge).
    const confirm = screen.getByText("Confirm");
    expect(confirm).toBeTruthy();
    expect(screen.getByText("Reject")).toBeTruthy();
    // Confirming rewrites the whole Turtle and promotes to rdfs:subClassOf.
    fireEvent.click(confirm);
    expect(onUpdateTurtle).toHaveBeenCalledTimes(1);
    const rewritten = onUpdateTurtle.mock.calls[0][0];
    expect(rewritten).toContain("subClassOf");
    expect(rewritten).toContain("USER_CONFIRMED");
    // The soft annotation is dropped by the promotion.
    expect(rewritten).not.toContain("suggestedSubClassOf");
  });

  it("labels the Grounding tab with a (0) count when no grounding match", () => {
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
      />,
    );
    // Always show the count for consistency with Relationships / Attributes.
    expect(screen.getByText("Grounding (0)")).toBeTruthy();
  });

  it("toggles raw Turtle editor", () => {
    const { container } = render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        isReadOnly={false}
        onSave={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Edit raw Turtle"));
    expect(container.querySelector("textarea")).toBeTruthy();
    expect(container.querySelector("textarea")?.value).toContain("owl:Class");
  });

  it("reviews the vocabulary of this class and its own properties only", () => {
    const fullTurtle = `@prefix ex: <http://example.com/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

ex:Claim a owl:Class ;
    rdfs:label "Claim" ;
    skos:altLabel "Insurance claim"@en .

ex:amount a owl:DatatypeProperty ;
    rdfs:label "amount" ;
    rdfs:domain ex:Claim ;
    skos:altLabel "claim amount"@en .

ex:Policy a owl:Class ;
    rdfs:label "Policy" ;
    skos:altLabel "Contract"@en .`;
    const onUpdateTurtle = vi.fn();
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        prelude={`@prefix ex: <http://example.com/> .`}
        fullTurtle={fullTurtle}
        isReadOnly={false}
        onSave={vi.fn()}
        onUpdateTurtle={onUpdateTurtle}
      />,
    );
    const table = screen.getByRole("table", { name: "Business vocabulary" });
    expect(within(table).getByText("Insurance claim @en")).toBeTruthy();
    expect(within(table).getByText("claim amount @en")).toBeTruthy();
    expect(within(table).queryByText("Contract @en")).toBeNull();

    fireEvent.click(
      within(table).getByRole("button", {
        name: "Reject variant claim amount @en",
      }),
    );
    expect(onUpdateTurtle).toHaveBeenCalledTimes(1);
    const concepts = parseVocabulary(onUpdateTurtle.mock.calls[0][0]);
    const variants = (id: string) =>
      concepts
        .find((concept) => concept.id === id)
        ?.alternativeLabels.map((l) => `${l.value}@${l.language}`);
    expect(variants("http://example.com/amount")).toEqual([]);
    expect(variants("http://example.com/Claim")).toEqual([
      "Insurance claim@en",
    ]);
    expect(variants("http://example.com/Policy")).toEqual(["Contract@en"]);
  });

  it("finds the terms by the IRIs n3 resolved, whatever the prefix form", () => {
    const fullTurtle = `PREFIX : <http://example.com/>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

:Claim a owl:Class ;
    rdfs:label "Claim" ;
    skos:altLabel "Insurance claim"@en .

<http://example.com/amount> a owl:DatatypeProperty ;
    rdfs:label "amount" ;
    rdfs:domain :Claim .`;
    const { entities, prelude } = parseTurtleEntities(fullTurtle);
    const claim = entities.find((e) => e.fullSubject === ":Claim")!;
    const amount = entities.find(
      (e) => e.fullSubject === "<http://example.com/amount>",
    )!;
    render(
      <ClassDetailPanel
        group={{
          ...group,
          classEntity: claim,
          relationships: [],
          attributes: [amount],
        }}
        r2rml={null}
        prelude={prelude}
        fullTurtle={fullTurtle}
        isReadOnly={false}
        onSave={vi.fn()}
        onUpdateTurtle={vi.fn()}
      />,
    );
    const table = screen.getByRole("table", { name: "Business vocabulary" });
    expect(within(table).getByText("Insurance claim @en")).toBeTruthy();
    expect(
      within(table).getByRole("button", { name: "Add variant to amount" }),
    ).toBeTruthy();
  });

  it("holds vocabulary edits while the label or description has unapplied changes", () => {
    const fullTurtle = `@prefix ex: <http://example.com/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

ex:Claim a owl:Class ;
    rdfs:label "Claim" ;
    skos:altLabel "Insurance claim"@en .`;
    render(
      <ClassDetailPanel
        group={group}
        r2rml={null}
        prelude={`@prefix ex: <http://example.com/> .`}
        fullTurtle={fullTurtle}
        isReadOnly={false}
        onSave={vi.fn()}
        onUpdateTurtle={vi.fn()}
      />,
    );
    const table = () =>
      screen.getByRole("table", { name: "Business vocabulary" });
    expect(
      within(table()).getByRole("button", {
        name: "Reject variant Insurance claim @en",
      }),
    ).toBeTruthy();

    fireEvent.change(screen.getByDisplayValue("Claim"), {
      target: { value: "Claim draft" },
    });

    expect(within(table()).queryAllByRole("button")).toHaveLength(0);
    expect(
      screen.getByText(
        "Apply the changes above before editing the vocabulary.",
      ),
    ).toBeTruthy();
  });
});
