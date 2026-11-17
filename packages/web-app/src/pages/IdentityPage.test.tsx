// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { IdentityPage } from "./IdentityPage";

vi.mock("../api-hooks/use-list-platform-roles", () => ({
  useListPlatformRoles: () => ({
    data: {
      roles: [
        {
          roleId: "platform-admin",
          name: "Platform Admin",
          description: "Full platform access",
          scope: "PLATFORM",
          isBuiltIn: true,
        },
        {
          roleId: "platform-viewer",
          name: "Platform Viewer",
          description: "Read-only platform access",
          scope: "PLATFORM",
          isBuiltIn: true,
        },
        {
          roleId: "namespace-owner",
          name: "Namespace Owner",
          description: "Owns a namespace",
          scope: "NAMESPACE",
          isBuiltIn: true,
        },
      ],
    },
    isLoading: false,
  }),
}));

vi.mock("../api-hooks/use-list-platform-grants", () => ({
  useListPlatformGrants: () => ({
    data: {
      pages: [
        {
          grants: [
            {
              grantId: "g1",
              principalType: "User",
              principalId: "admin@example.com",
              role: "platform-admin",
              grantedBy: "system",
              grantedAt: "2026-01-01T00:00:00Z",
            },
            {
              grantId: "g2",
              principalType: "Group",
              principalId: "platform-ops",
              role: "platform-viewer",
              grantedBy: "system",
              grantedAt: "2026-01-01T00:00:00Z",
            },
          ],
        },
      ],
    },
    isLoading: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  }),
}));

vi.mock("../api-hooks/use-create-platform-grant", () => ({
  useCreatePlatformGrant: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("../api-hooks/use-delete-platform-grant", () => ({
  useDeletePlatformGrant: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn().mockResolvedValue({}),
    isPending: false,
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
);

describe("IdentityPage", () => {
  it("renders the Identity header", () => {
    render(<IdentityPage />, { wrapper });
    expect(
      screen.getByRole("heading", { name: "Identity" }),
    ).toBeInTheDocument();
  });

  it("shows Members and Roles tabs", () => {
    render(<IdentityPage />, { wrapper });
    expect(screen.getByRole("tab", { name: /Members/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Roles/i })).toBeInTheDocument();
  });

  it("shows platform grants on the Members tab with a Grant access button", () => {
    render(<IdentityPage />, { wrapper });
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByText("platform-ops")).toBeInTheDocument();
    // roleId is rendered via its display name
    expect(screen.getByText("Platform Admin")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /grant access/i }),
    ).toBeInTheDocument();
  });

  it("shows only PLATFORM-scoped roles on the Roles tab", async () => {
    render(<IdentityPage />, { wrapper });
    const rolesTab = screen.getByRole("tab", { name: /Roles/i });
    await act(async () => {
      rolesTab.click();
    });
    expect(screen.getByText("Full platform access")).toBeInTheDocument();
    expect(screen.getByText("Read-only platform access")).toBeInTheDocument();
    // NAMESPACE-scoped role is filtered out
    expect(screen.queryByText("Owns a namespace")).not.toBeInTheDocument();
  });
});
