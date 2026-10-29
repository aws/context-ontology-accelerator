// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi, describe, it, expect, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import { ProfilePage } from "../../../src/pages/ProfilePage";
import { LocaleProvider } from "../../../src/i18n/LocaleProvider";

const mockUseUser = vi.fn();
const mockUseListPrincipalGrants = vi.fn();
const mockUseListPlatformRoles = vi.fn();
const mockUseCurrentNamespace = vi.fn();
const mockUseControlPlaneClient = vi.fn();

vi.mock("../../../src/auth", () => ({
  useUser: () => mockUseUser(),
}));

vi.mock("../../../src/api-hooks", () => ({
  useListPrincipalGrants: () => mockUseListPrincipalGrants(),
  useListPlatformRoles: () => mockUseListPlatformRoles(),
}));

vi.mock("../../../src/components/NamespaceProvider", () => ({
  useCurrentNamespace: () => mockUseCurrentNamespace(),
}));

vi.mock("../../../src/components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => mockUseControlPlaneClient(),
}));

vi.mock("@coa/control-plane-client", () => ({
  GetNamespaceRoleCommand: vi.fn(),
}));

const defaultNamespace = {
  current: {
    namespaceId: "ns-1",
    name: "test",
    displayName: "Test NS",
    status: "ACTIVE",
    sourceCount: 3,
  },
  namespaceId: "ns-1",
  namespaces: [
    {
      namespaceId: "ns-1",
      name: "test",
      displayName: "Test NS",
      status: "ACTIVE",
      sourceCount: 3,
    },
  ],
  setNamespace: vi.fn(),
  isLoading: false,
};

describe("ProfilePage", () => {
  beforeEach(() => {
    mockUseCurrentNamespace.mockReturnValue(defaultNamespace);
    mockUseControlPlaneClient.mockReturnValue({ send: vi.fn() });
    mockUseListPlatformRoles.mockReturnValue({ data: { roles: [] } });
  });

  it("renders nothing when user is not available", () => {
    mockUseUser.mockReturnValue({ user: undefined, logout: vi.fn() });
    mockUseListPrincipalGrants.mockReturnValue({
      data: undefined,
      isLoading: false,
    });
    const { container } = render(<ProfilePage />);
    expect(container.innerHTML).toBe("");
  });

  it("renders identity card with name, email, group badges", () => {
    mockUseUser.mockReturnValue({
      user: {
        userid: "u1",
        name: "Alice",
        email: "alice@co.com",
        groups: ["engineering"],
      },
      logout: vi.fn(),
    });
    mockUseListPrincipalGrants.mockReturnValue({
      data: { grants: [] },
      isLoading: false,
    });

    render(<ProfilePage />);

    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("alice@co.com")).toBeInTheDocument();
    expect(screen.getByText("engineering")).toBeInTheDocument();
  });

  it("shows dash when name/groups are empty", () => {
    mockUseUser.mockReturnValue({
      user: { userid: "u2", name: "", email: "b@c.com", groups: [] },
      logout: vi.fn(),
    });
    mockUseListPrincipalGrants.mockReturnValue({
      data: { grants: [] },
      isLoading: false,
    });

    render(<ProfilePage />);

    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("renders grants in roles tab", () => {
    mockUseUser.mockReturnValue({
      user: { userid: "u1", name: "Alice", email: "a@b.com", groups: ["eng"] },
      logout: vi.fn(),
    });
    mockUseListPrincipalGrants.mockReturnValue({
      data: {
        grants: [
          {
            grantId: "g1",
            namespaceId: "ns-1",
            principalType: "User",
            principalId: "u1",
            role: "analyst",
            grantedBy: "admin@co.com",
            grantedAt: "2026-05-01T10:00:00Z",
          },
        ],
      },
      isLoading: false,
    });

    render(<ProfilePage />);

    expect(screen.getByText("analyst")).toBeInTheDocument();
    expect(screen.getByText("admin@co.com")).toBeInTheDocument();
  });

  it("shows namespaces tab label with count", () => {
    mockUseUser.mockReturnValue({
      user: { userid: "u1", name: "Alice", email: "a@b.com", groups: [] },
      logout: vi.fn(),
    });
    mockUseListPrincipalGrants.mockReturnValue({
      data: { grants: [] },
      isLoading: false,
    });

    render(<ProfilePage />);

    expect(screen.getByText("Namespaces (1)")).toBeInTheDocument();
  });

  it("shows the grant's principal type in Japanese", async () => {
    window.localStorage.setItem("scl.locale", "ja");
    mockUseUser.mockReturnValue({
      user: { userid: "u1", name: "Alice", email: "a@b.com", groups: ["eng"] },
      logout: vi.fn(),
    });
    mockUseListPrincipalGrants.mockReturnValue({
      data: {
        grants: [
          {
            grantId: "g1",
            namespaceId: "ns-1",
            principalType: "Group",
            principalId: "eng",
            role: "analyst",
            grantedAt: "2026-05-01T10:00:00Z",
          },
        ],
      },
      isLoading: false,
    });
    try {
      render(
        <LocaleProvider>
          <ProfilePage />
        </LocaleProvider>,
      );
      // The locale bundle loads asynchronously and re-renders the table, so
      // read the row from the settled DOM.
      await waitFor(() => {
        const row = screen.getByText("analyst").closest("tr");
        expect(row).not.toBeNull();
        expect(
          within(row as HTMLElement).getByText("グループ"),
        ).toBeInTheDocument();
      });
      expect(screen.queryByText("Group")).toBeNull();
    } finally {
      window.localStorage.clear();
      document.documentElement.lang = "";
    }
  });
});
