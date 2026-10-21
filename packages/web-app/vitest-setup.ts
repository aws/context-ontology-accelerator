// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// Breadcrumb publishing is a cross-cutting, presentation-only concern that
// every page invokes via `useBreadcrumbTrail`. It depends on `useNamespace`
// (→ `GetNamespace` → control-plane client) purely to resolve the namespace
// crumb's label and to security-trim its up-link to whatever the server
// itself would authorize. Page tests mock only the data hooks each page uses
// for its own content, not this shared navigation hook, so left live it would
// force a control-plane client + QueryClient into every page test. Mock it to
// a no-op globally; the hook's own behavior (trail shape + access trimming) is
// covered directly in use-breadcrumb-trail.test.ts, which opts out of this
// mock. Tests that need the real hook can `vi.unmock("@api-hooks/use-breadcrumb-trail")`.
vi.mock("@api-hooks/use-breadcrumb-trail", () => ({
  useBreadcrumbTrail: () => undefined,
}));

// `useNamespace` resolves a namespace id to its display name and whether the
// current user can open it, both derived from a single `useGetNamespace`
// call. It is used directly by many pages (for the breadcrumb label) and by
// `useBreadcrumbTrail` (mocked above), reaching the control-plane client, so
// left live it would require that client in every page test. Its output
// feeds only breadcrumbs, never page-body assertions, so stub the name to
// echo the id and access to "not confirmed" (fail-safe: namespace crumbs
// render as plain text). Covered for real where it matters by
// use-breadcrumb-trail.test.ts and GraphSearch.breadcrumb.test.tsx, which
// supply their own mock.
vi.mock("@api-hooks/use-namespace", () => ({
  useNamespace: (namespaceId?: string) => ({
    name: namespaceId ?? "",
    canAccess: false,
    isLoading: false,
  }),
}));

// Unmount any component rendered by a test after it finishes. Without this,
// happy-dom accumulates every test's DOM in one document, so a test that
// asserts the ABSENCE of text (e.g. `queryByText(...).toBeNull()`) can match a
// leaked node from an earlier test's render and flake under parallel load.
afterEach(() => {
  cleanup();
});
