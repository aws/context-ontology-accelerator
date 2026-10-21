// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * OntologyClassDetail's breadcrumb trail is origin-aware: reached from the
 * Explorer ("Expand" link, `?from=explorer`) it is one shape, reached from
 * the Induced Ontology page (default) it is a different, longer shape. This
 * is the one non-trivial per-page trail in the breadcrumb feature, so it
 * gets a dedicated test asserting both branches rather than only mounting
 * the page under BreadcrumbProvider without checking what it published.
 */
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { OntologyClassDetailPage } from "./OntologyClassDetail";
import type { BreadcrumbItem } from "@components/BreadcrumbProvider";

// This is a breadcrumb-trail test, so it opts out of the global
// vitest-setup mocks that no-op useBreadcrumbTrail and stub useNamespace.
vi.unmock("@api-hooks/use-breadcrumb-trail");
vi.unmock("@api-hooks/use-namespace");

const OVERVIEW = {
  ontologyId: "http://ex.org/o#",
  namespace: "ns",
  graphUri: "g",
  classes: [{ uri: "http://ex.org/o#Claim", label: "Claim" }],
  objectProperties: [],
  datatypeProperties: [],
};

vi.mock("@api-hooks/use-ontology-overview", () => ({
  useOntologyOverview: () => ({
    data: OVERVIEW,
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@api-hooks/use-list-ontologies", () => ({
  useListOntologies: () => ({
    data: [{ ontologyId: "http://ex.org/o#", title: "My Ontology" }],
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@api-hooks/use-get-namespace", () => ({
  useGetNamespace: () => ({
    data: { namespace: { displayName: "My Namespace" } },
    isSuccess: true,
    isLoading: false,
  }),
}));

let captured: BreadcrumbItem[] = [];
vi.mock("@components/BreadcrumbProvider", () => ({
  useBreadcrumbs: () => ({
    setBreadcrumbs: (items: BreadcrumbItem[]) => {
      captured = items;
    },
    clearBreadcrumbs: () => {
      captured = [];
    },
  }),
}));

function renderClass(extraQuery = "") {
  const search =
    `?ontology_id=${encodeURIComponent("http://ex.org/o#")}` +
    `&class_uri=${encodeURIComponent("http://ex.org/o#Claim")}${extraQuery}`;
  return render(
    <MemoryRouter
      initialEntries={[`/namespaces/ns/ontology/induced/class${search}`]}
    >
      <Routes>
        <Route
          path="/namespaces/:namespaceId/ontology/induced/class"
          element={<OntologyClassDetailPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  captured = [];
});

describe("OntologyClassDetail breadcrumb trail", () => {
  it("publishes the Induction branch by default", async () => {
    renderClass();

    await waitFor(() => expect(captured.length).toBeGreaterThan(1));
    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "My Namespace",
      "Ontology",
      "Induction",
      "My Ontology",
      "Claim",
    ]);
    // The current-page crumb (the class itself) is non-navigable.
    expect(captured.at(-1)?.onClick).toBeUndefined();
    // The Ontology group label is never navigable.
    expect(
      captured.find((c) => c.text === "Ontology")?.onClick,
    ).toBeUndefined();
  });

  it("publishes the Explorer branch when reached via ?from=explorer", async () => {
    renderClass("&from=explorer");

    await waitFor(() => expect(captured.length).toBeGreaterThan(1));
    expect(captured.map((c) => c.text)).toEqual([
      "COA",
      "My Namespace",
      "Ontology",
      "Explorer",
      "Claim",
    ]);
    expect(captured.at(-1)?.onClick).toBeUndefined();
  });
});
