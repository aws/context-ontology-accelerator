// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// This file tests the REAL useBreadcrumbTrail; opt out of the global no-op mock
// installed in vitest-setup.ts.
vi.unmock("@api-hooks/use-breadcrumb-trail");

import { useBreadcrumbTrail } from "./use-breadcrumb-trail";
import type { BreadcrumbItem } from "@components/BreadcrumbProvider";

const navigate = vi.fn();
const setBreadcrumbs = vi.fn();
const clearBreadcrumbs = vi.fn();
const mockNamespace = vi.fn();

vi.mock("react-router-dom", () => ({
  useNavigate: () => navigate,
}));

vi.mock("@components/BreadcrumbProvider", () => ({
  useBreadcrumbs: () => ({ setBreadcrumbs, clearBreadcrumbs }),
}));

vi.mock("./use-namespace", () => ({
  useNamespace: (id: string | undefined) => mockNamespace(id),
}));

beforeEach(() => {
  navigate.mockReset();
  setBreadcrumbs.mockReset();
  clearBreadcrumbs.mockReset();
  mockNamespace.mockReset();
  mockNamespace.mockReturnValue({
    name: "",
    canAccess: true,
    isLoading: false,
  });
});

/** The crumbs passed to setBreadcrumbs on the latest call. */
function published(): BreadcrumbItem[] {
  const call = setBreadcrumbs.mock.calls.at(-1);
  if (!call) throw new Error("setBreadcrumbs was not called");
  return call[0];
}

describe("useBreadcrumbTrail", () => {
  it("prepends a clickable COA root that navigates home", () => {
    renderHook(() => useBreadcrumbTrail([{ text: "Namespaces" }]));

    const items = published();
    expect(items[0].text).toBe("COA");
    items[0].onClick?.();
    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("turns a crumb's href into a navigate click handler", () => {
    renderHook(() =>
      useBreadcrumbTrail([
        { text: "Sources", href: "/namespaces/ns-a/sources" },
      ]),
    );

    const sources = published()[1];
    expect(sources.text).toBe("Sources");
    sources.onClick?.();
    expect(navigate).toHaveBeenCalledWith("/namespaces/ns-a/sources");
  });

  it("leaves a crumb without an href as plain, non-navigable text", () => {
    renderHook(() => useBreadcrumbTrail([{ text: "Current page" }]));

    const last = published()[1];
    expect(last.text).toBe("Current page");
    expect(last.onClick).toBeUndefined();
  });

  it("keeps a namespace link when the user has access", () => {
    mockNamespace.mockReturnValue({
      name: "My NS",
      canAccess: true,
      isLoading: false,
    });
    renderHook(() =>
      useBreadcrumbTrail([
        { text: "My NS", href: "/namespaces/ns-a", namespaceId: "ns-a" },
      ]),
    );

    const nsCrumb = published()[1];
    expect(nsCrumb.onClick).toBeTypeOf("function");
    nsCrumb.onClick?.();
    expect(navigate).toHaveBeenCalledWith("/namespaces/ns-a");
    expect(mockNamespace).toHaveBeenCalledWith("ns-a");
  });

  it("security-trims a namespace link to plain text when the user lacks access", () => {
    mockNamespace.mockReturnValue({
      name: "My NS",
      canAccess: false,
      isLoading: false,
    });
    renderHook(() =>
      useBreadcrumbTrail([
        { text: "My NS", href: "/namespaces/ns-a", namespaceId: "ns-a" },
      ]),
    );

    const nsCrumb = published()[1];
    expect(nsCrumb.text).toBe("My NS");
    // The crumb is still shown (you are there) but offers no navigation.
    expect(nsCrumb.onClick).toBeUndefined();
  });

  it("does not gate a namespace-agnostic link (no namespaceId in the trail)", () => {
    mockNamespace.mockReturnValue({
      name: "",
      canAccess: false,
      isLoading: false,
    });
    renderHook(() =>
      useBreadcrumbTrail([{ text: "Namespaces", href: "/namespaces" }]),
    );

    const crumb = published()[1];
    expect(crumb.onClick).toBeTypeOf("function");
    // No namespaceId in the trail, so useNamespace is asked about `undefined`
    // and must not gate this link.
    expect(mockNamespace).toHaveBeenCalledWith(undefined);
  });

  it("publishes nothing when given undefined (falls back to the shell default)", () => {
    renderHook(() => useBreadcrumbTrail(undefined));
    expect(setBreadcrumbs).not.toHaveBeenCalled();
  });

  it("clears the trail on unmount", () => {
    const { unmount } = renderHook(() =>
      useBreadcrumbTrail([{ text: "Namespaces" }]),
    );
    unmount();
    expect(clearBreadcrumbs).toHaveBeenCalled();
  });

  it("re-links a namespace crumb once GetNamespace resolves after the first render", () => {
    // Models the real sequence: on first render GetNamespace is still
    // loading, so canAccess is false and the crumb is trimmed to plain text;
    // once it resolves successfully, canAccess flips to true and the crumb
    // must become a link.
    mockNamespace.mockReturnValue({
      name: "My NS",
      canAccess: false,
      isLoading: true,
    });
    const { rerender } = renderHook(
      (crumbs: Parameters<typeof useBreadcrumbTrail>[0]) =>
        useBreadcrumbTrail(crumbs),
      {
        initialProps: [
          { text: "My NS", href: "/namespaces/ns-a", namespaceId: "ns-a" },
        ],
      },
    );

    // First render: GetNamespace not yet resolved → plain text, no navigation.
    expect(published()[1].onClick).toBeUndefined();

    // GetNamespace resolves: access confirmed.
    mockNamespace.mockReturnValue({
      name: "My NS",
      canAccess: true,
      isLoading: false,
    });
    rerender([
      { text: "My NS", href: "/namespaces/ns-a", namespaceId: "ns-a" },
    ]);

    const nsCrumb = published()[1];
    expect(nsCrumb.onClick).toBeTypeOf("function");
    nsCrumb.onClick?.();
    expect(navigate).toHaveBeenCalledWith("/namespaces/ns-a");
  });
});
