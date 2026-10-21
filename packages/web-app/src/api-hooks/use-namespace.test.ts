// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { renderHook } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// This file tests the REAL useNamespace; opt out of the global stub in
// vitest-setup.ts.
vi.unmock("@api-hooks/use-namespace");

import { useNamespace } from "./use-namespace";
import { useGetNamespace } from "./use-get-namespace";

// The hook reads only `data`, `isSuccess`, and `isLoading` off the query
// result, so the mock returns a partial of the real return type — no `as`
// assertion, per .claude/rules/frontend-typescript.md.
type NamespaceQueryResult = ReturnType<typeof useGetNamespace>;

vi.mock("./use-get-namespace", () => ({
  useGetNamespace: vi.fn<() => Partial<NamespaceQueryResult>>(),
}));

const mockGetNamespace = vi.mocked(useGetNamespace);

beforeEach(() => {
  mockGetNamespace.mockReset();
});

/** Minimal slice of the useGetNamespace result the hook actually reads. */
function nsResult(
  isSuccess: boolean,
  namespace?: { displayName?: string; name?: string },
  extra?: Partial<NamespaceQueryResult>,
): Partial<NamespaceQueryResult> {
  return {
    data: namespace ? { namespace } : undefined,
    isSuccess,
    isLoading: false,
    ...extra,
  };
}

describe("useNamespace", () => {
  it("prefers displayName when present", () => {
    mockGetNamespace.mockReturnValue(
      nsResult(true, { displayName: "Sales", name: "sales-ns" }),
    );

    const { result } = renderHook(() => useNamespace("ns-a"));

    expect(result.current.name).toBe("Sales");
  });

  it("falls back to name when displayName is absent", () => {
    mockGetNamespace.mockReturnValue(nsResult(true, { name: "sales-ns" }));

    const { result } = renderHook(() => useNamespace("ns-a"));

    expect(result.current.name).toBe("sales-ns");
  });

  it("falls back to the id while loading or when the namespace is absent", () => {
    mockGetNamespace.mockReturnValue(
      nsResult(false, undefined, { isLoading: true }),
    );

    const { result } = renderHook(() => useNamespace("ns-a"));

    expect(result.current.name).toBe("ns-a");
  });

  it("falls back to an empty string with no id and no data", () => {
    mockGetNamespace.mockReturnValue(nsResult(false, undefined));

    const { result } = renderHook(() => useNamespace(undefined));

    expect(result.current.name).toBe("");
  });

  it("grants access via GetNamespace success regardless of grant type (platform-admin regression)", () => {
    // Regression guard for the bug that motivated this hook: the old
    // useNamespaceAccess derived access by enumerating grant types
    // (namespace grants, then platform grants) and still missed any
    // namespace reachable only through a grant kind it didn't enumerate
    // (e.g. a group grant) — a platform-admin or group-granted user saw
    // every such namespace's breadcrumb trimmed to plain text. This hook
    // asks GetNamespace directly instead of reasoning about grant types at
    // all, so it has no such category to miss: any namespace the server
    // lets this user open resolves access, independent of *which*
    // authorization path (namespace grant, platform grant, group grant, or
    // anything the server adds later) made that true.
    mockGetNamespace.mockReturnValue(nsResult(true, { name: "sales-ns" }));

    const { result } = renderHook(() => useNamespace("ns-a"));

    expect(result.current.canAccess).toBe(true);
  });

  it("denies access (fail-safe) while GetNamespace is still loading", () => {
    mockGetNamespace.mockReturnValue(
      nsResult(false, undefined, { isLoading: true }),
    );

    const { result } = renderHook(() => useNamespace("ns-a"));

    expect(result.current.canAccess).toBe(false);
    expect(result.current.isLoading).toBe(true);
  });

  it("denies access (fail-safe) when GetNamespace errors, e.g. AccessDeniedError", () => {
    mockGetNamespace.mockReturnValue(
      nsResult(false, undefined, {
        isError: true,
        error: new Error("AccessDeniedError"),
      }),
    );

    const { result } = renderHook(() => useNamespace("ns-a"));

    expect(result.current.canAccess).toBe(false);
  });

  it("denies access for an empty or undefined namespace id", () => {
    mockGetNamespace.mockReturnValue(nsResult(true, { name: "sales-ns" }));

    const { result: withEmpty } = renderHook(() => useNamespace(""));
    expect(withEmpty.current.canAccess).toBe(false);

    const { result: withUndefined } = renderHook(() => useNamespace(undefined));
    expect(withUndefined.current.canAccess).toBe(false);
  });
});
