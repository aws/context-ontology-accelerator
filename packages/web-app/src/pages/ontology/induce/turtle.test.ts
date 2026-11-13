// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { groupByClass } from "./ProposalEntitiesPanel";
import { parseTurtleEntities } from "./turtle";
import { addAlternativeLabel, replacePreferredLabel } from "./vocabulary";

const RDF_LANG_STRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString";

describe("parseTurtleEntities ordering", () => {
  it("orders by kind (Class → ObjectProperty → DatatypeProperty) then alphabetically (case-insensitive) within each kind", () => {
    // Deliberately out of order and mixed-case to exercise both the kind
    // grouping and the case-insensitive alphabetical secondary sort.
    const ttl = `@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ind: <http://example.com/ind#> .

ind:Zebra a owl:Class ;
    rdfs:label "Zebra" .

ind:apple a owl:Class ;
    rdfs:label "apple" .

ind:Mango a owl:Class .

ind:zeta_rel a owl:ObjectProperty ;
    rdfs:domain ind:Zebra .

ind:alpha_rel a owl:ObjectProperty ;
    rdfs:domain ind:apple .

ind:beta_attr a owl:DatatypeProperty .`;

    const { entities, prelude } = parseTurtleEntities(ttl);

    expect(entities.map((e) => e.subject)).toEqual([
      // Classes, A→Z case-insensitive (apple < Mango < Zebra)
      "ind:apple",
      "ind:Mango",
      "ind:Zebra",
      // Object properties, A→Z
      "ind:alpha_rel",
      "ind:zeta_rel",
      // Datatype properties
      "ind:beta_attr",
    ]);
    expect(prelude).toContain("@prefix owl:");
  });
});

const SALES = `@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix ex: <http://example.org/ns#> .

ex: a owl:Ontology .

ex:Customer a owl:Class ;
    rdfs:label "Customer" ;
    skos:prefLabel "Customer"@en ;
    skos:altLabel "顧客"@ja .

ex:Order a owl:Class ;
    rdfs:label "Order" ;
    rdfs:comment "One order. Placed by a customer." .

ex:customerName a owl:DatatypeProperty ;
    rdfs:domain ex:Customer ;
    rdfs:label "customer name" .
`;

const summary = (turtle: string) =>
  parseTurtleEntities(turtle).entities.map((e) => `${e.fullSubject} ${e.kind}`);

describe("parseTurtleEntities statement boundaries", () => {
  it("keeps every entity after a vocabulary edit re-serializes the Turtle", () => {
    // n3's Writer emits no blank lines between subjects.
    const edited = replacePreferredLabel(
      SALES,
      "http://example.org/ns#Customer",
      { value: "Customer", language: "en", datatype: RDF_LANG_STRING },
      { value: "Client", language: "en", datatype: RDF_LANG_STRING },
    );
    expect(edited).toMatch(/\.\nex:Order a owl:Class/);

    const expected = [
      "ex:Customer owl:Class",
      "ex:Order owl:Class",
      "ex:customerName owl:DatatypeProperty",
    ];
    expect(summary(SALES)).toEqual(expected);
    expect(summary(edited)).toEqual(expected);
    expect(parseTurtleEntities(edited).ontologyDecl).toContain("owl:Ontology");
  });

  it("returns each body as exact source text so entity edits can splice it back", () => {
    const { entities } = parseTurtleEntities(SALES);
    const order = entities.find((e) => e.fullSubject === "ex:Order");
    expect(order?.body).toBe(
      'ex:Order a owl:Class ;\n    rdfs:label "Order" ;\n    rdfs:comment "One order. Placed by a customer." .',
    );
    for (const entity of entities) expect(SALES).toContain(entity.body);
  });

  it("keeps a table class as one statement with its key and restriction inline", () => {
    // Table induction writes a cardinality restriction and an owl:hasKey list
    // as blank nodes. Written back as separate `_:n3-0` statements they would
    // add the class to the list a second time and drop its key columns.
    const ttl = `@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix ind: <http://example.org/ind#> .

ind:Orders a owl:Class ;
    rdfs:label "orders" ;
    rdfs:subClassOf [ a owl:Restriction ;
            owl:cardinality "1"^^xsd:nonNegativeInteger ;
            owl:onProperty ind:orders_orderId ] ;
    owl:hasKey ( ind:orders_orderId ) ;
    skos:altLabel "受注"@ja .

ind:orders_orderId a owl:DatatypeProperty ;
    rdfs:domain ind:Orders ;
    rdfs:label "order_id" .
`;
    const edited = addAlternativeLabel(ttl, "http://example.org/ind#Orders", {
      value: "注文",
      language: "ja",
      datatype: RDF_LANG_STRING,
    });
    expect(edited).not.toContain("_:");

    const { entities } = parseTurtleEntities(edited);
    expect(entities.map((e) => `${e.fullSubject} ${e.kind}`)).toEqual([
      "ind:Orders owl:Class",
      "ind:orders_orderId owl:DatatypeProperty",
    ]);
    const orders = groupByClass(entities).groups[0];
    expect(orders.pkColumns).toEqual(["orders_orderId"]);
    expect(orders.classEntity.body).toContain("owl:Restriction");
  });

  it("keeps Japanese names as prefixed names after an edit", () => {
    // n3's Writer only abbreviates ASCII local names; the panel writes the
    // rest back so an edit does not change how untouched subjects are spelled.
    const ttl = `@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ind: <http://example.org/ind#> .

ind:受注明細 a owl:Class ;
    rdfs:label "受注明細" ;
    rdfs:comment "参照: <http://example.org/ind#受注明細>" .

ind:受注明細_受注日 a owl:DatatypeProperty ;
    rdfs:domain ind:受注明細 .

<http://example.org/ind#a(b)> a owl:Class .
`;
    const edited = addAlternativeLabel(ttl, "http://example.org/ind#受注明細", {
      value: "注文明細",
      language: "ja",
      datatype: RDF_LANG_STRING,
    });

    expect(summary(edited)).toEqual(summary(ttl));
    expect(edited).toContain("ind:受注明細 a owl:Class");
    expect(edited).toContain("rdfs:domain ind:受注明細");
    // Literal text is never rewritten, and a local name Turtle can't write
    // as a prefixed name stays a full IRI.
    expect(edited).toContain('"参照: <http://example.org/ind#受注明細>"');
    expect(edited).toContain("<http://example.org/ind#a(b)>");
    expect(edited).toMatch(/^@prefix ind: <http:\/\/example\.org\/ind#>\.$/mu);
  });

  it("does not end a statement at a dot inside a literal or blank node", () => {
    const ttl = `@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix ex: <http://example.org/ns#> .
ex:A a owl:Class ; ex:note "v1.2. see below." ; ex:shape [ ex:min 1.5 ] .
ex:B a owl:Class .`;
    expect(summary(ttl)).toEqual(["ex:A owl:Class", "ex:B owl:Class"]);
  });

  it("reports the class type when a subject lists several types", () => {
    const ttl = `@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix coa: <http://example.org/coa#> .
@prefix ex: <http://example.org/ns#> .
ex:A a coa:Induced, owl:Class .`;
    expect(summary(ttl)).toEqual(["ex:A owl:Class"]);
  });

  it("keeps SPARQL-style PREFIX directives in the prelude", () => {
    const ttl = `PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX ex: <http://example.org/ns#>
ex:A a owl:Class .`;
    const parsed = parseTurtleEntities(ttl);
    expect(parsed.prelude).toBe(
      "PREFIX owl: <http://www.w3.org/2002/07/owl#>\nPREFIX ex: <http://example.org/ns#>",
    );
    expect(parsed.entities.map((e) => e.fullSubject)).toEqual(["ex:A"]);
  });

  it("gives each IRI subject its absolute IRI, for any prefix form", () => {
    const { entities } = parseTurtleEntities(`PREFIX : <http://example.org/a#>
@prefix b: <http://example.org/b#> .
PREFIX owl: <http://www.w3.org/2002/07/owl#>

:Order a owl:Class .
b:Line a owl:Class .
<http://example.org/c#Item> a owl:Class .
`);
    expect(
      Object.fromEntries(entities.map((e) => [e.fullSubject, e.iri])),
    ).toEqual({
      ":Order": "http://example.org/a#Order",
      "b:Line": "http://example.org/b#Line",
      "<http://example.org/c#Item>": "http://example.org/c#Item",
    });
  });

  it("throws on malformed Turtle so the caller can show the parse error", () => {
    expect(() => parseTurtleEntities("ex:A a owl:Class .")).toThrow(
      /Undefined prefix/,
    );
  });
});
