// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";
import { OntologyClassDetailPage } from "./OntologyClassDetail";
import { BreadcrumbProvider } from "@components/BreadcrumbProvider";

const OVERVIEW = {
  ontologyId: "http://ex.org/o#",
  namespace: "ns",
  graphUri: "g",
  classes: [
    {
      uri: "http://ex.org/o#Claim",
      label: "Claim",
      comment: "A claim.",
      groundedTo: "http://customer.example/onto#Party",
      matchType: "exact",
    },
    { uri: "http://ex.org/o#Policy", label: "Policy", comment: undefined },
  ],
  objectProperties: [
    {
      uri: "http://ex.org/o#hasPolicy",
      label: "has policy",
      domain: "http://ex.org/o#Claim",
      range: "http://ex.org/o#Policy",
    },
    {
      uri: "http://ex.org/o#unrelated",
      label: "unrelated rel",
      domain: "http://ex.org/o#Policy",
      range: "http://ex.org/o#Policy",
    },
  ],
  datatypeProperties: [
    {
      uri: "http://ex.org/o#amount",
      label: "amount",
      domain: "http://ex.org/o#Claim",
      range: "http://www.w3.org/2001/XMLSchema#decimal",
    },
  ],
};

vi.mock("@api-hooks/use-ontology-overview", () => ({
  useOntologyOverview: () => ({
    data: OVERVIEW,
    isLoading: false,
    error: null,
  }),
}));

// The class-detail page also reads the ontology registry (for the breadcrumb's
// ontology-name crumb). Mock it so the test doesn't need the control-plane
// client provider.
vi.mock("@api-hooks/use-list-ontologies", () => ({
  useListOntologies: () => ({ data: [], isLoading: false, error: null }),
}));

function renderClass(classUri: string) {
  const search = `?ontology_id=${encodeURIComponent(
    "http://ex.org/o#",
  )}&class_uri=${encodeURIComponent(classUri)}`;
  return render(
    <MemoryRouter
      initialEntries={[`/namespaces/ns/ontology/induced/class${search}`]}
    >
      <BreadcrumbProvider>
        <Routes>
          <Route
            path="/namespaces/:namespaceId/ontology/induced/class"
            element={<OntologyClassDetailPage />}
          />
        </Routes>
      </BreadcrumbProvider>
    </MemoryRouter>,
  );
}

describe("OntologyClassDetailPage", () => {
  it("scopes relationships and attributes to the selected class", () => {
    const { container } = renderClass("http://ex.org/o#Claim");
    // Claim is the domain of hasPolicy (relationship) and amount (attribute);
    // the Policy→Policy "unrelated" property must be excluded.
    expect(container.textContent).toContain("has policy");
    expect(container.textContent).toContain("Relationships (1)");
    expect(container.textContent).toContain("Attributes (1)");
    expect(container.textContent).toContain("Grounded (exact)");
    expect(container.textContent).not.toContain("unrelated rel");
  });

  it("shows an error when class_uri is missing", () => {
    render(
      <MemoryRouter
        initialEntries={[
          "/namespaces/ns/ontology/induced/class?ontology_id=http%3A%2F%2Fex.org%2Fo%23",
        ]}
      >
        <Routes>
          <Route
            path="/namespaces/:namespaceId/ontology/induced/class"
            element={<OntologyClassDetailPage />}
          />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("Missing class")).toBeInTheDocument();
  });
});
