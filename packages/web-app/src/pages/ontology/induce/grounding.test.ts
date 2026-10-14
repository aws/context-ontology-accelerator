// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import normalizeNameKeyCases from "@test-fixtures/normalize-name-key.json";
import {
  buildClassMatchResolver,
  classLocalName,
  classToTableFromR2rml,
  conceptMatchKey,
  parseR2rmlByClass,
  parseConceptMatches,
  parseConceptMatchesEnvelope,
  normalizeNameKey,
  qualifiedSqlIdent,
  unquoteSqlIdent,
  type ConceptMatch,
} from "./grounding";

// Real rdflib serialization from the inducer's ``build_r2rml`` (verified).
// The load-bearing property is that ``rr:class``, ``rr:tableName``,
// ``rr:predicate`` and ``rr:column`` all live in SEPARATE subject
// statements, joined only by the ``TriplesMap_X`` / ``POM_Y`` id tokens.
const REAL_R2RML = `
@prefix ind: <http://ex.org/ind#> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ind:TriplesMap_ClinicalTrial a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"clinical_trial\\"" ] ;
    rr:predicateObjectMap <http://ex.org/ind#TriplesMap_ClinicalTrial/POM_SponsorId>,
        <http://ex.org/ind#TriplesMap_ClinicalTrial/POM_Title>,
        <http://ex.org/ind#TriplesMap_ClinicalTrial/POM_TrialId> ;
    rr:subjectMap <http://ex.org/ind#TriplesMap_ClinicalTrial/SubjectMap> .

<http://ex.org/ind#TriplesMap_ClinicalTrial/POM_SponsorId> rr:objectMap <http://ex.org/ind#TriplesMap_ClinicalTrial/POM_SponsorId/ObjectMap> ;
    rr:predicate ind:clinicalTrial_sponsorId .

<http://ex.org/ind#TriplesMap_ClinicalTrial/POM_SponsorId/ObjectMap> rr:joinCondition [ rr:child "\\"sponsor_id\\"" ;
            rr:parent "\\"sponsor_id\\"" ] ;
    rr:parentTriplesMap ind:TriplesMap_Sponsor .

<http://ex.org/ind#TriplesMap_ClinicalTrial/POM_Title> rr:objectMap <http://ex.org/ind#TriplesMap_ClinicalTrial/POM_Title/ObjectMap> ;
    rr:predicate ind:clinicalTrial_title .

<http://ex.org/ind#TriplesMap_ClinicalTrial/POM_Title/ObjectMap> rr:column "\\"title\\"" ;
    rr:datatype xsd:string .

<http://ex.org/ind#TriplesMap_ClinicalTrial/POM_TrialId> rr:objectMap <http://ex.org/ind#TriplesMap_ClinicalTrial/POM_TrialId/ObjectMap> ;
    rr:predicate ind:clinicalTrial_trialId .

<http://ex.org/ind#TriplesMap_ClinicalTrial/POM_TrialId/ObjectMap> rr:column "\\"trial_id\\"" ;
    rr:datatype xsd:integer .

<http://ex.org/ind#TriplesMap_ClinicalTrial/SubjectMap> rr:class ind:ClinicalTrial ;
    rr:template "http://ex.org/ind#clinical_trial/{\\"trial_id\\"}" .

ind:TriplesMap_Sponsor a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"sponsor\\"" ] ;
    rr:predicateObjectMap <http://ex.org/ind#TriplesMap_Sponsor/POM_Name>,
        <http://ex.org/ind#TriplesMap_Sponsor/POM_SponsorId> ;
    rr:subjectMap <http://ex.org/ind#TriplesMap_Sponsor/SubjectMap> .

<http://ex.org/ind#TriplesMap_Sponsor/POM_Name> rr:objectMap <http://ex.org/ind#TriplesMap_Sponsor/POM_Name/ObjectMap> ;
    rr:predicate ind:sponsor_name .

<http://ex.org/ind#TriplesMap_Sponsor/POM_Name/ObjectMap> rr:column "\\"name\\"" ;
    rr:datatype xsd:string .

<http://ex.org/ind#TriplesMap_Sponsor/POM_SponsorId> rr:objectMap <http://ex.org/ind#TriplesMap_Sponsor/POM_SponsorId/ObjectMap> ;
    rr:predicate ind:sponsor_sponsorId .

<http://ex.org/ind#TriplesMap_Sponsor/POM_SponsorId/ObjectMap> rr:column "\\"sponsor_id\\"" ;
    rr:datatype xsd:integer .

<http://ex.org/ind#TriplesMap_Sponsor/SubjectMap> rr:class ind:Sponsor ;
    rr:template "http://ex.org/ind#sponsor/{\\"sponsor_id\\"}" .
`;

// Compact inline form (the shape used in ClassDetailPanel unit fixtures):
// class + table + predicate + column all co-located in one statement.
const INLINE_R2RML = `<#ClaimMap> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "claims" ] ;
    rr:subjectMap [ rr:class ex:Claim ] ;
    rr:predicateObjectMap [
      rr:predicate ex:amount ;
      rr:objectMap [ rr:column "claim_amount" ; rr:datatype xsd:decimal ]
    ] .`;

describe("classLocalName", () => {
  it("percent-decodes combining marks minted into an IRI path", () => {
    expect(classLocalName("http://example.test/class/क%E0%A4%BF")).toBe("कि");
  });

  it.each(["%FF", "A%20B%ZZ", "%E0%A4%BF%ZZ", "%", "%2"])(
    "preserves the whole local name when an escape is malformed: %s",
    (localName) => {
      expect(classLocalName(`http://example.test/class/${localName}`)).toBe(
        localName,
      );
    },
  );
});

describe("unquoteSqlIdent", () => {
  it("peels Turtle + SQL delimiter layers off a double-escaped identifier", () => {
    expect(unquoteSqlIdent('"\\"clinical_trial\\""')).toBe("clinical_trial");
  });
  it("returns a plain (un-delimited) value unchanged", () => {
    expect(unquoteSqlIdent('"claims"')).toBe("claims");
  });
  it("drops the schema qualifier a shared table name carries", () => {
    // The mapping emits "public"."customers" when two datasources both expose
    // `customers`; consumers here join on the bare name (#965).
    expect(unquoteSqlIdent('"\\"public\\".\\"customers\\""')).toBe("customers");
  });
  it("keeps a dot that is part of the table name itself", () => {
    expect(unquoteSqlIdent('"\\"q1.results\\""')).toBe("q1.results");
  });
  it("un-doubles an embedded quote", () => {
    // SQL escapes a quote inside a delimited identifier by doubling it, so
    // `say"hi` is emitted as `"say""hi"` — one segment, not two.
    expect(unquoteSqlIdent('"\\"say\\"\\"hi\\""')).toBe('say"hi');
  });
  it("keeps only the table of a 3-part name", () => {
    expect(unquoteSqlIdent('"\\"cat\\".\\"public\\".\\"customers\\""')).toBe(
      "customers",
    );
  });
});

describe("qualifiedSqlIdent", () => {
  // The bare name is what the ConceptMatch join needs; the qualified one is what
  // the class panel shows, so two twins don't both read "Mapping: customers".
  it("keeps the schema a shared table name carries", () => {
    expect(qualifiedSqlIdent('"\\"public\\".\\"customers\\""')).toBe(
      "public.customers",
    );
  });
  it("leaves an unqualified name exactly as it was", () => {
    expect(qualifiedSqlIdent('"\\"clinical_trial\\""')).toBe("clinical_trial");
  });
  it("does not invent a schema for a dot inside the table name", () => {
    expect(qualifiedSqlIdent('"\\"q1.results\\""')).toBe("q1.results");
  });
});

// The RIGOR strategy keys its TriplesMap IRI on ``{schema}.{table}``, so both
// tables of a schema whose bare names collide with another datasource's carry the
// SAME leading segment. A ``\w+`` join key stopped at the dot, keyed both on
// ``public``, and the second silently overwrote the first (#965).
const RIGOR_QUALIFIED_R2RML = `
@prefix ind: <http://ex.org/ind#> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .

ind:TriplesMap_public.customers a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"public\\".\\"customers\\"" ] ;
    rr:subjectMap <http://ex.org/ind#TriplesMap_public.customers/SubjectMap> .

<http://ex.org/ind#TriplesMap_public.customers/SubjectMap> rr:class ind:Customers ;
    rr:template "http://ex.org/ind#Customers/{id}" .

ind:TriplesMap_public.orders a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"public\\".\\"orders\\"" ] ;
    rr:subjectMap <http://ex.org/ind#TriplesMap_public.orders/SubjectMap> .

<http://ex.org/ind#TriplesMap_public.orders/SubjectMap> rr:class ind:Orders ;
    rr:template "http://ex.org/ind#Orders/{id}" .
`;

// Backend-emitted R2RML for two datasources that both expose
// ``public.accounts``. The class suffix is derived from the full table identity;
// the exact join dimensions are carried separately as datasource/schema/table.
const COLLIDING_TABLE_R2RML = `
@prefix ind: <http://example.test/induced#> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix scl: <http://coa.amazon.com/vocab/coa#> .

ind:TriplesMap_Accounts a rr:TriplesMap ;
    scl:datasourceId "DS#finance" ;
    scl:sourceSchema "public" ;
    rr:logicalTable [ rr:tableName "\\"public__aad6897a\\".\\"accounts\\"" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_Accounts/SubjectMap> .

ind:TriplesMap_Accounts_782870db a rr:TriplesMap ;
    scl:datasourceId "DS#identity" ;
    scl:sourceSchema "public" ;
    rr:logicalTable [ rr:tableName "\\"public__782870db\\".\\"accounts\\"" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_Accounts_782870db/SubjectMap> .

<http://example.test/induced#TriplesMap_Accounts/SubjectMap>
    rr:class ind:Accounts .

<http://example.test/induced#TriplesMap_Accounts_782870db/SubjectMap>
    rr:class ind:Accounts_782870db .
`;

// Same datasource, same bare table name, distinct schemas. This backend output
// has no explicit sourceSchema annotation, so the resolver must recover it from
// the qualified rr:tableName.
const QUALIFIED_TABLE_R2RML = `
@prefix ind: <http://example.test/induced#> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix scl: <http://coa.amazon.com/vocab/coa#> .

ind:TriplesMap_Accounts a rr:TriplesMap ;
    scl:datasourceId "DS#shared" ;
    rr:logicalTable [ rr:tableName "\\"finance\\".\\"accounts\\"" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_Accounts/SubjectMap> .

ind:TriplesMap_Accounts_525ca243 a rr:TriplesMap ;
    scl:datasourceId "DS#shared" ;
    rr:logicalTable [ rr:tableName "\\"identity\\".\\"accounts\\"" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_Accounts_525ca243/SubjectMap> .

<http://example.test/induced#TriplesMap_Accounts/SubjectMap>
    rr:class ind:Accounts .

<http://example.test/induced#TriplesMap_Accounts_525ca243/SubjectMap>
    rr:class ind:Accounts_525ca243 .
`;

const DOTTED_TABLE_R2RML = `
@prefix ind: <http://example.test/induced#> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix scl: <http://coa.amazon.com/vocab/coa#> .

ind:TriplesMap_Q1Results a rr:TriplesMap ;
    scl:datasourceId "DS#finance" ;
    scl:sourceSchema "public.v1" ;
    rr:logicalTable [ rr:tableName "\\"public__one\\".\\"q1.results\\"" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_Q1Results/SubjectMap> .

ind:TriplesMap_Q1Results_two a rr:TriplesMap ;
    scl:datasourceId "DS#identity" ;
    scl:sourceSchema "public.v1" ;
    rr:logicalTable [ rr:tableName "\\"public__two\\".\\"q1.results\\"" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_Q1Results_two/SubjectMap> .

<http://example.test/induced#TriplesMap_Q1Results/SubjectMap>
    rr:class ind:Q1Results .

<http://example.test/induced#TriplesMap_Q1Results_two/SubjectMap>
    rr:class ind:Q1Results_two .
`;

const LEGACY_COLLIDING_TABLE_R2RML = `
@prefix ind: <http://example.test/induced#> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .

ind:TriplesMap_Accounts a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "accounts" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_Accounts/SubjectMap> .

ind:TriplesMap_Accounts_two a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "accounts" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_Accounts_two/SubjectMap> .

<http://example.test/induced#TriplesMap_Accounts/SubjectMap>
    rr:class ind:Accounts .

<http://example.test/induced#TriplesMap_Accounts_two/SubjectMap>
    rr:class ind:Accounts_two .
`;

describe("classToTableFromR2rml (TriplesMap-id join)", () => {
  it("maps class local names to their real table names across split blocks", () => {
    const m = classToTableFromR2rml(REAL_R2RML);
    expect(m.get("ClinicalTrial")).toBe("clinical_trial");
    expect(m.get("Sponsor")).toBe("sponsor");
  });

  it("keeps two dotted TriplesMap ids sharing a schema apart", () => {
    const m = classToTableFromR2rml(RIGOR_QUALIFIED_R2RML);
    // Bare, because this map is joined against a ConceptMatch's table name.
    expect(m.get("Customers")).toBe("customers");
    expect(m.get("Orders")).toBe("orders");
  });

  it("keeps a Unicode qualified TriplesMap id intact", () => {
    const unicodeR2rml = REAL_R2RML.replaceAll(
      "TriplesMap_ClinicalTrial",
      "TriplesMap_日本.臨床試験",
    );
    const m = classToTableFromR2rml(unicodeR2rml);
    expect(m.get("ClinicalTrial")).toBe("clinical_trial");
  });
});

describe("parseR2rmlByClass (TriplesMap-id + POM-id join)", () => {
  it("populates sourceTable + column mappings for separate-block R2RML", () => {
    const byClass = parseR2rmlByClass(REAL_R2RML);
    const ct = byClass.get("ClinicalTrial");
    expect(ct).toBeTruthy();
    // Real source table (not "Unknown" — the old parseR2rml bug).
    expect(ct?.sourceTable).toBe("clinical_trial");
    // Every predicate joined to its column via the shared POM id.
    const byProp = new Map(ct?.columnMappings.map((c) => [c.property, c]));
    expect(byProp.get("clinicalTrial_title")?.column).toBe("title");
    expect(byProp.get("clinicalTrial_title")?.type).toBe("attribute");
    expect(byProp.get("clinicalTrial_trialId")?.column).toBe("trial_id");
    // The FK column is a relationship pointing at the parent class. The target
    // renders as the parent CLASS name (``Sponsor``), not the raw
    // ``TriplesMap_Sponsor`` id token.
    expect(byProp.get("clinicalTrial_sponsorId")?.type).toBe("relationship");
    expect(byProp.get("clinicalTrial_sponsorId")?.target).toBe("Sponsor");

    const sp = byClass.get("Sponsor");
    expect(sp?.sourceTable).toBe("sponsor");
    expect(sp?.columnMappings.map((c) => c.column).sort()).toEqual([
      "name",
      "sponsor_id",
    ]);
  });

  it("shows the qualified table so two twins are distinguishable", () => {
    // "Mapping: customers" on both panels would re-fuse in the UI the two tables
    // the mapping went out of its way to separate.
    const byClass = parseR2rmlByClass(RIGOR_QUALIFIED_R2RML);
    expect(byClass.get("Customers")?.sourceTable).toBe("public.customers");
    expect(byClass.get("Orders")?.sourceTable).toBe("public.orders");
  });

  it("joins Unicode TriplesMap and POM ids without crossing path segments", () => {
    const unicodeR2rml = REAL_R2RML.replaceAll(
      "TriplesMap_ClinicalTrial",
      "TriplesMap_日本.臨床試験",
    ).replaceAll("POM_Title", "POM_名称");
    const mapping = parseR2rmlByClass(unicodeR2rml).get("ClinicalTrial");
    expect(mapping?.sourceTable).toBe("clinical_trial");
    expect(mapping?.columnMappings).toContainEqual(
      expect.objectContaining({ column: "title" }),
    );
  });

  it("keeps the join controls and TAG characters to_pascal retains in an id", () => {
    // ``filter_letters_marks_numbers`` keeps ZWJ/ZWNJ and TAG characters behind
    // a retained base, so a generated id can carry them. A scanner that stops
    // there cuts ``POM_T\u200dl`` and ``POM_T\u200cl`` to the same ``T`` and the two
    // columns join under one key.
    const r2rml = REAL_R2RML.replaceAll("POM_Title", "POM_T\u200dl")
      .replaceAll("POM_TrialId", "POM_T\u200cl")
      .replaceAll("POM_Name", "POM_N\u{E0067}a")
      .replaceAll("POM_SponsorId", "POM_N\u{E0068}a");
    const byClass = parseR2rmlByClass(r2rml);
    const trial = new Map(
      byClass.get("ClinicalTrial")?.columnMappings.map((c) => [c.property, c]),
    );
    expect(trial.get("clinicalTrial_title")?.column).toBe("title");
    expect(trial.get("clinicalTrial_trialId")?.column).toBe("trial_id");
    expect(
      byClass
        .get("Sponsor")
        ?.columnMappings.map((c) => c.column)
        .sort(),
    ).toEqual(["name", "sponsor_id"]);
  });

  it("handles the compact inline single-statement form", () => {
    const byClass = parseR2rmlByClass(INLINE_R2RML);
    const claim = byClass.get("Claim");
    expect(claim?.sourceTable).toBe("claims");
    expect(claim?.columnMappings).toHaveLength(1);
    expect(claim?.columnMappings[0]).toMatchObject({
      column: "claim_amount",
      property: "amount",
      type: "attribute",
      target: "decimal",
    });
  });

  it("returns an empty map for null R2RML", () => {
    expect(parseR2rmlByClass(null).size).toBe(0);
    expect(parseR2rmlByClass(undefined).size).toBe(0);
  });

  it("joins Japanese TriplesMap and POM ids (#927 Part B1)", () => {
    // ``\w`` is ASCII-only: ``POM_設備id`` matched nothing and the panel read
    // "Mapping: Unknown"; ``POM_Id設備`` / ``POM_Id建物`` both keyed on ``Id``.
    const m = parseR2rmlByClass(JA_R2RML);
    const equipment = m.get("設備台帳");
    expect(equipment?.sourceTable).toBe("設備台帳");
    expect(
      [...(equipment?.columnMappings ?? [])].sort((a, b) =>
        a.column.localeCompare(b.column),
      ),
    ).toEqual(
      [
        {
          column: "id_建物",
          property: "設備台帳_id建物",
          type: "attribute",
          target: "string",
        },
        {
          column: "id_設備",
          property: "設備台帳_id設備",
          type: "attribute",
          target: "string",
        },
        {
          column: "建物名",
          property: "設備台帳_建物名",
          type: "attribute",
          target: "string",
        },
        {
          column: "設備id",
          property: "設備台帳_設備id",
          type: "attribute",
          target: "integer",
        },
      ].sort((a, b) => a.column.localeCompare(b.column)),
    );
    expect(classToTableFromR2rml(JA_R2RML).get("設備台帳")).toBe("設備台帳");
  });
});

// Real rdflib serialization of ``build_r2rml`` for a Japanese Glue table.
const JA_R2RML = `@prefix ind: <http://example.org/base/> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ind:TriplesMap_設備台帳 a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"設備台帳\\"" ] ;
    rr:predicateObjectMap <http://example.org/base/TriplesMap_設備台帳/POM_Id建物>,
        <http://example.org/base/TriplesMap_設備台帳/POM_Id設備>,
        <http://example.org/base/TriplesMap_設備台帳/POM_建物名>,
        <http://example.org/base/TriplesMap_設備台帳/POM_設備id> ;
    rr:subjectMap <http://example.org/base/TriplesMap_設備台帳/SubjectMap> .

<http://example.org/base/TriplesMap_設備台帳/POM_Id建物> rr:objectMap <http://example.org/base/TriplesMap_設備台帳/POM_Id建物/ObjectMap> ;
    rr:predicate ind:設備台帳_id建物 .

<http://example.org/base/TriplesMap_設備台帳/POM_Id建物/ObjectMap> rr:column "\\"id_建物\\"" ;
    rr:datatype xsd:string .

<http://example.org/base/TriplesMap_設備台帳/POM_Id設備> rr:objectMap <http://example.org/base/TriplesMap_設備台帳/POM_Id設備/ObjectMap> ;
    rr:predicate ind:設備台帳_id設備 .

<http://example.org/base/TriplesMap_設備台帳/POM_Id設備/ObjectMap> rr:column "\\"id_設備\\"" ;
    rr:datatype xsd:string .

<http://example.org/base/TriplesMap_設備台帳/POM_建物名> rr:objectMap <http://example.org/base/TriplesMap_設備台帳/POM_建物名/ObjectMap> ;
    rr:predicate ind:設備台帳_建物名 .

<http://example.org/base/TriplesMap_設備台帳/POM_建物名/ObjectMap> rr:column "\\"建物名\\"" ;
    rr:datatype xsd:string .

<http://example.org/base/TriplesMap_設備台帳/POM_設備id> rr:objectMap <http://example.org/base/TriplesMap_設備台帳/POM_設備id/ObjectMap> ;
    rr:predicate ind:設備台帳_設備id .

<http://example.org/base/TriplesMap_設備台帳/POM_設備id/ObjectMap> rr:column "\\"設備id\\"" ;
    rr:datatype xsd:integer .

<http://example.org/base/TriplesMap_設備台帳/SubjectMap> rr:class ind:設備台帳 ;
    rr:template "http://example.org/base/設備台帳/{\\"設備id\\"}" .
`;

describe("parseConceptMatchesEnvelope", () => {
  const one = {
    source_table: "orders",
    source_table_identity: "DS#sales::warehouse.public.orders",
    source_column: "",
    matched_class_uri: "https://schema.org/Order",
    match_type: "high_confidence" as const,
  };

  it("unwraps the S3 envelope { matches: [...] }", () => {
    // Shape written by put_proposal_matches_s3 and fetched from matches_url.
    const parsed = parseConceptMatchesEnvelope({ matches: [one] });
    expect(parsed).toHaveLength(1);
    expect(parsed[0].source_table).toBe("orders");
    expect(parsed[0].source_table_identity).toBe(
      "DS#sales::warehouse.public.orders",
    );
    expect(parsed[0].matched_class_uri).toBe("https://schema.org/Order");
  });

  it("accepts a bare array (legacy inline metadata.matches shape)", () => {
    const parsed = parseConceptMatchesEnvelope([one]);
    expect(parsed).toHaveLength(1);
    expect(parsed[0].source_table).toBe("orders");
  });

  it("returns [] for null / undefined / a wrongly-typed payload", () => {
    expect(parseConceptMatchesEnvelope(null)).toEqual([]);
    expect(parseConceptMatchesEnvelope(undefined)).toEqual([]);
    expect(parseConceptMatchesEnvelope(42)).toEqual([]);
    // Envelope present but matches is not an array -> [] (no throw).
    expect(parseConceptMatchesEnvelope({ matches: "nope" })).toEqual([]);
  });

  it("preserves every entry (no [:50]-style truncation on the read path)", () => {
    // 75 table-level matches — the #908 repro count — must all survive the
    // envelope parse; the fix delivers the full list, not a capped slice.
    const many = Array.from({ length: 75 }, (_, i) => ({
      source_table: `t${i}`,
      source_column: "",
      matched_class_uri: `https://schema.org/C${i}`,
      match_type: "high_confidence" as const,
    }));
    expect(parseConceptMatchesEnvelope({ matches: many })).toHaveLength(75);
  });
});

describe("conceptMatchKey", () => {
  it("uses the stable identity when the match carries one", () => {
    const [match] = parseConceptMatches([
      {
        source_table: "orders",
        source_table_identity: "DS#sales::warehouse.public.orders",
        source_column: "",
        match_type: "novel",
      },
    ]);
    expect(conceptMatchKey(match)).toBe("DS#sales::warehouse.public.orders");
  });

  it("keeps the bare table key for legacy matches", () => {
    const [match] = parseConceptMatches([
      {
        source_table: "orders",
        source_column: "",
        match_type: "novel",
      },
    ]);
    expect(conceptMatchKey(match)).toBe("orders");
  });
});

describe("buildClassMatchResolver", () => {
  const matches: ConceptMatch[] = parseConceptMatches([
    {
      source_table: "clinical_trial",
      source_column: "",
      matched_class_uri: "https://parallax.aws/onto/ClinicalTrial",
      matched_ontology_id: "https://parallax.aws/onto",
      similarity: 0.95,
      match_type: "high_confidence",
      candidates: [
        {
          entity_uri: "https://parallax.aws/onto/ClinicalTrial",
          ontology_id: "https://parallax.aws/onto",
          lexical_sim: 0.95,
        },
      ],
    },
    // A column-level match must be ignored by the resolver.
    {
      source_table: "clinical_trial",
      source_column: "title",
      match_type: "novel",
    },
  ]);

  it("maps class ClinicalTrial ↔ source_table clinical_trial via normalized name", () => {
    expect(normalizeNameKey("ClinicalTrial")).toBe(
      normalizeNameKey("clinical_trial"),
    );
    const resolve = buildClassMatchResolver(matches, REAL_R2RML);
    const match = resolve("ind:ClinicalTrial");
    expect(match?.source_table).toBe("clinical_trial");
    expect(match?.matched_class_uri).toBe(
      "https://parallax.aws/onto/ClinicalTrial",
    );
    // Column-level match never participates.
    expect(match?.source_column).toBe("");
  });

  it("falls back to the R2RML TriplesMap-id join when names don't normalize", () => {
    // A source_table whose normalized key does not equal the class local
    // name; only the R2RML class→table link recovers it.
    const oddMatches = parseConceptMatches([
      {
        source_table: "clinical_trial",
        source_column: "",
        matched_class_uri: "https://parallax.aws/onto/CT",
        match_type: "high_confidence",
        candidates: [],
      },
    ]);
    // Class local name "ClinicalTrial" normalizes to "clinicaltrial" which
    // equals normalizeNameKey("clinical_trial"), so the primary path already
    // hits. Confirm the resolver returns the match either way.
    const resolve = buildClassMatchResolver(oddMatches, REAL_R2RML);
    expect(resolve("ind:ClinicalTrial")?.source_table).toBe("clinical_trial");
  });

  it("returns null for a class with no table match", () => {
    const resolve = buildClassMatchResolver(matches, REAL_R2RML);
    expect(resolve("ind:Unrelated")).toBeNull();
  });

  it("keeps same-named tables scoped to their datasource-qualified identity", () => {
    const collidingMatches = parseConceptMatches([
      {
        source_table: "accounts",
        source_table_identity: "DS#finance::finance.public.accounts",
        source_column: "",
        matched_class_uri: "https://example.test/FinancialAccount",
        match_type: "high_confidence",
      },
      {
        source_table: "accounts",
        source_table_identity: "DS#identity::identity.public.accounts",
        source_column: "",
        matched_class_uri: "https://example.test/UserAccount",
        match_type: "high_confidence",
      },
    ]);

    const resolve = buildClassMatchResolver(
      collidingMatches,
      COLLIDING_TABLE_R2RML,
    );
    expect(resolve("ind:Accounts")?.matched_class_uri).toBe(
      "https://example.test/FinancialAccount",
    );
    expect(resolve("ind:Accounts_782870db")?.matched_class_uri).toBe(
      "https://example.test/UserAccount",
    );
  });

  it("derives a missing source schema from the qualified logical table", () => {
    const collidingMatches = parseConceptMatches([
      {
        source_table: "accounts",
        source_table_identity: "DS#shared::finance.accounts",
        source_column: "",
        matched_class_uri: "https://example.test/FinancialAccount",
        match_type: "high_confidence",
      },
      {
        source_table: "accounts",
        source_table_identity: "DS#shared::identity.accounts",
        source_column: "",
        matched_class_uri: "https://example.test/UserAccount",
        match_type: "high_confidence",
      },
    ]);

    const resolve = buildClassMatchResolver(
      collidingMatches,
      QUALIFIED_TABLE_R2RML,
    );
    expect(resolve("ind:Accounts")?.matched_class_uri).toBe(
      "https://example.test/FinancialAccount",
    );
    expect(resolve("ind:Accounts_525ca243")?.matched_class_uri).toBe(
      "https://example.test/UserAccount",
    );
  });

  it("uses R2RML provenance before a normalized-name match", () => {
    const identityOnly = parseConceptMatches([
      {
        source_table: "accounts",
        source_table_identity: "DS#identity::identity.public.accounts",
        source_column: "",
        matched_class_uri: "https://example.test/UserAccount",
        match_type: "high_confidence",
      },
    ]);

    const resolve = buildClassMatchResolver(
      identityOnly,
      COLLIDING_TABLE_R2RML,
    );
    expect(resolve("ind:Accounts")).toBeNull();
    expect(resolve("ind:Accounts_782870db")?.matched_class_uri).toBe(
      "https://example.test/UserAccount",
    );
  });

  it("does not use an unqualified identity for a schema-qualified class", () => {
    const unqualified = parseConceptMatches([
      {
        source_table: "accounts",
        source_table_identity: "DS#finance::accounts",
        source_column: "",
        matched_class_uri: "https://example.test/FinancialAccount",
        match_type: "high_confidence",
      },
    ]);

    const resolve = buildClassMatchResolver(unqualified, COLLIDING_TABLE_R2RML);
    expect(resolve("ind:Accounts")).toBeNull();
  });

  it("does not bind an identity without a datasource to datasource-qualified R2RML", () => {
    const unscoped = parseConceptMatches([
      {
        source_table: "accounts",
        source_table_identity: "finance.public.accounts",
        source_column: "",
        matched_class_uri: "https://example.test/FinancialAccount",
        match_type: "high_confidence",
      },
    ]);

    const resolve = buildClassMatchResolver(unscoped, COLLIDING_TABLE_R2RML);
    expect(resolve("ind:Accounts")).toBeNull();
  });

  it("returns null when legacy R2RML cannot disambiguate same-named tables", () => {
    const collidingMatches = parseConceptMatches([
      {
        source_table: "accounts",
        source_table_identity: "DS#finance::finance.public.accounts",
        source_column: "",
        matched_class_uri: "https://example.test/FinancialAccount",
        match_type: "high_confidence",
      },
      {
        source_table: "accounts",
        source_table_identity: "DS#identity::identity.public.accounts",
        source_column: "",
        matched_class_uri: "https://example.test/UserAccount",
        match_type: "high_confidence",
      },
    ]);

    const resolve = buildClassMatchResolver(
      collidingMatches,
      LEGACY_COLLIDING_TABLE_R2RML,
    );
    expect(resolve("ind:Accounts")).toBeNull();
    expect(resolve("ind:Accounts_two")).toBeNull();
  });

  it("uses a unique legacy match when R2RML supplies the table link", () => {
    const legacyR2rml = `
@prefix ind: <http://example.test/induced#> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .

ind:TriplesMap_AccountsLedger a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "accounts" ] ;
    rr:subjectMap <http://example.test/induced#TriplesMap_AccountsLedger/SubjectMap> .

<http://example.test/induced#TriplesMap_AccountsLedger/SubjectMap>
    rr:class ind:AccountsLedger .
`;
    const [legacyMatch] = parseConceptMatches([
      {
        source_table: "accounts",
        source_column: "",
        matched_class_uri: "https://example.test/Account",
        match_type: "high_confidence",
      },
    ]);

    const resolve = buildClassMatchResolver([legacyMatch], legacyR2rml);
    expect(resolve("ind:AccountsLedger")).toBe(legacyMatch);
  });

  it("treats dots inside schema and table names as identity content", () => {
    const matches = parseConceptMatches([
      {
        source_table: "q1.results",
        source_table_identity: "DS#finance::finance.public.v1.q1.results",
        source_column: "",
        matched_class_uri: "https://example.test/FinancialResult",
        match_type: "high_confidence",
      },
      {
        source_table: "q1.results",
        source_table_identity: "DS#identity::identity.public.v1.q1.results",
        source_column: "",
        matched_class_uri: "https://example.test/IdentityResult",
        match_type: "high_confidence",
      },
    ]);

    const resolve = buildClassMatchResolver(matches, DOTTED_TABLE_R2RML);
    expect(resolve("ind:Q1Results")?.matched_class_uri).toBe(
      "https://example.test/FinancialResult",
    );
    expect(resolve("ind:Q1Results_two")?.matched_class_uri).toBe(
      "https://example.test/IdentityResult",
    );
  });

  it("joins a percent-encoded class IRI to its Unicode source label", () => {
    const unicodeMatches = parseConceptMatches([
      {
        source_table: "कि",
        source_column: "",
        matched_class_uri: "https://example.test/Foundation",
        match_type: "exact",
        candidates: [],
      },
    ]);
    const resolve = buildClassMatchResolver(unicodeMatches, null);
    expect(resolve("http://example.test/class/क%E0%A4%BF")?.source_table).toBe(
      "कि",
    );
  });

  it("prefers an exact source label when normalized keys collide", () => {
    const collidingMatches = parseConceptMatches([
      {
        source_table: "A-B",
        source_column: "",
        match_type: "novel",
        candidates: [],
      },
      {
        source_table: "AB",
        source_column: "",
        matched_class_uri: "https://example.test/AB",
        match_type: "exact",
        candidates: [],
      },
    ]);
    const resolve = buildClassMatchResolver(collidingMatches, null);
    expect(resolve("ind:AB")?.matched_class_uri).toBe(
      "https://example.test/AB",
    );
  });
});

describe("normalizeNameKey — script-agnostic", () => {
  it.each(normalizeNameKeyCases)(
    "matches the shared Python/TypeScript contract: $id",
    ({ input, expected }) => {
      expect(normalizeNameKey(input)).toBe(expected);
    },
  );

  // The key drives Map lookup and equality, so a label that keys to the empty
  // string does not merely fail to join: every such label lands in one bucket
  // and grounding results attach to the wrong class. The previous
  // [^a-zA-Z0-9] filter did exactly that for any non-Latin script.
  it.each([
    ["ja kanji", "設置場所"],
    ["ja katakana", "メーカー"],
    ["zh-Hans", "安装位置"],
    ["ko hangul", "제조사"],
    ["ru cyrillic", "ИЗГОТОВИТЕЛЬ"],
  ])("keeps a non-Latin label (%s)", (_label, name) => {
    expect(normalizeNameKey(name)).not.toBe("");
  });

  it("does not collapse unrelated non-Latin labels onto one key", () => {
    expect(normalizeNameKey("設置場所")).not.toBe(normalizeNameKey("メーカー"));
    expect(normalizeNameKey("엘리베이터")).not.toBe(normalizeNameKey("제조사"));
  });

  it("keeps symbols and their attached variation selectors", () => {
    expect(normalizeNameKey("❤️")).toBe("❤️");
    expect(normalizeNameKey("✈️")).toBe("✈️");
    expect(normalizeNameKey("Brand™")).toBe("brand™");
  });

  it("unifies simple sigma variants without expanding sharp s", () => {
    expect(normalizeNameKey("Σ")).toBe(normalizeNameKey("σ"));
    expect(normalizeNameKey("ς")).toBe(normalizeNameKey("σ"));
    expect(normalizeNameKey("Maße")).not.toBe(normalizeNameKey("Masse"));
  });

  it("keeps the accented letter in a Latin label", () => {
    // Previously HÖHE keyed to "hhe" and ÓRGÃO to "rgo" — the letter itself was
    // dropped, not just its case.
    expect(normalizeNameKey("HÖHE")).toBe("höhe");
    expect(normalizeNameKey("ÓRGÃO")).toBe("órgão");
  });

  it("still joins the spaced and CamelCase spellings it exists for", () => {
    expect(normalizeNameKey("Financial Instrument")).toBe(
      normalizeNameKey("FinancialInstrument"),
    );
    expect(normalizeNameKey("clinical_trial")).toBe(
      normalizeNameKey("ClinicalTrial"),
    );
    expect(normalizeNameKey("Policy_Amount")).toBe(
      normalizeNameKey("PolicyAmount"),
    );
  });

  it("still strips punctuation", () => {
    expect(normalizeNameKey("Policy (v2)")).toBe("policyv2");
    expect(normalizeNameKey("設置場所（屋内）")).toBe("設置場所屋内");
  });
});
