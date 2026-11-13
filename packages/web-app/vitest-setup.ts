// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// Page tests provide no control-plane client, so the breadcrumb hook is a no-op
// here. use-breadcrumb-trail.test.ts unmocks it to test it.
vi.mock("@api-hooks/use-breadcrumb-trail", () => ({
  useBreadcrumbTrail: () => undefined,
}));

// The stub echoes the id as the name with canAccess false, so namespace crumbs
// render as plain text. Breadcrumb tests supply their own mock.
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
