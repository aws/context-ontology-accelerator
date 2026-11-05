// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi, describe, it, expect } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import React from "react";
import {
  NamespaceProvider,
  useCurrentNamespace,
} from "../../../src/components/NamespaceProvider";

const mockUseListNamespaces = vi.fn();

vi.mock("../../../src/api-hooks", () => ({
  useListNamespaces: () => mockUseListNamespaces(),
  // The provider looks up a namespace by id only when the URL names one that
  // is not in the list; these tests never do, so the lookup stays idle.
  useGetNamespace: () => ({
    data: undefined,
    isSuccess: false,
    isError: false,
    isLoading: false,
  }),
}));

function TestConsumer() {
  const { current, namespaces, namespaceId, isLoading } = useCurrentNamespace();
  return (
    <div>
      <span data-testid="ns-id">{namespaceId}</span>
      <span data-testid="ns-count">{namespaces.length}</span>
      <span data-testid="ns-current">{current?.name ?? "none"}</span>
      <span data-testid="loading">{isLoading ? "yes" : "no"}</span>
    </div>
  );
}

function renderWithProvider() {
  return render(
    <MemoryRouter>
      <NamespaceProvider>
        <TestConsumer />
      </NamespaceProvider>
    </MemoryRouter>,
  );
}

describe("NamespaceProvider", () => {
  it("provides namespaces from API", () => {
    mockUseListNamespaces.mockReturnValue({
      data: {
        pages: [{ namespaces: [{ namespaceId: "ns-1", name: "test-ns" }] }],
      },
      isLoading: false,
    });

    renderWithProvider();

    expect(screen.getByTestId("ns-id")).toHaveTextContent("ns-1");
    expect(screen.getByTestId("ns-count")).toHaveTextContent("1");
    expect(screen.getByTestId("ns-current")).toHaveTextContent("test-ns");
  });

  it("shows loading state", () => {
    mockUseListNamespaces.mockReturnValue({
      data: undefined,
      isLoading: true,
    });

    renderWithProvider();

    expect(screen.getByTestId("loading")).toHaveTextContent("yes");
  });

  it("throws when useCurrentNamespace is used outside provider", () => {
    const Comp = () => {
      try {
        useCurrentNamespace();
        return <span>no error</span>;
      } catch (e: any) {
        return <span>{e.message}</span>;
      }
    };
    render(<Comp />);
    expect(
      screen.getByText(
        "useCurrentNamespace must be used inside NamespaceProvider",
      ),
    ).toBeInTheDocument();
  });

  it("defaults to first namespace when none selected", () => {
    mockUseListNamespaces.mockReturnValue({
      data: {
        pages: [
          {
            namespaces: [
              { namespaceId: "ns-a", name: "alpha" },
              { namespaceId: "ns-b", name: "beta" },
            ],
          },
        ],
      },
      isLoading: false,
    });

    renderWithProvider();

    expect(screen.getByTestId("ns-current")).toHaveTextContent("alpha");
  });
});
