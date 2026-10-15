// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  NamespaceList,
  getStatusActionItems,
  getHeaderCounterText,
} from "./NamespaceList";
import { NamespaceStatus } from "@coa/control-plane-client";

const mockDeleteMutate = vi.fn();
let mockNamespaces: Array<{
  namespaceId: string;
  name: string;
  status: string;
  sourceCount?: number;
  language?: string;
}> = [];

vi.mock("../api-hooks", () => ({
  useListNamespaces: () => ({
    data: { pages: [{ namespaces: mockNamespaces, nextToken: undefined }] },
    isLoading: false,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  }),
  useUpdateNamespaceStatus: () => ({
    mutate: vi.fn(),
  }),
  useDeleteNamespace: () => ({
    mutate: mockDeleteMutate,
    isPending: false,
  }),
  parseBlockers: () => null,
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
);

describe("NamespaceList", () => {
  beforeEach(() => {
    mockNamespaces = [];
    mockDeleteMutate.mockReset();
  });

  it("renders the header", () => {
    render(<NamespaceList />, { wrapper });
    expect(screen.getByText("Namespaces")).toBeInTheDocument();
  });

  it("shows empty state when no namespaces exist", () => {
    render(<NamespaceList />, { wrapper });
    expect(screen.getByText("No namespaces")).toBeInTheDocument();
  });

  it("renders create namespace button", () => {
    render(<NamespaceList />, { wrapper });
    expect(
      screen.getAllByRole("button", { name: /create namespace/i }).length,
    ).toBeGreaterThan(0);
  });
});

describe("NamespaceList language column", () => {
  beforeEach(() => {
    mockNamespaces = [
      { namespaceId: "ns-en", name: "en-ns", status: "ACTIVE", language: "en" },
      { namespaceId: "ns-ja", name: "ja-ns", status: "ACTIVE", language: "ja" },
      {
        namespaceId: "ns-pt",
        name: "pt-ns",
        status: "ACTIVE",
        language: "pt-BR",
      },
    ];
  });

  it("shows each namespace language read-only with a human label", () => {
    render(<NamespaceList />, { wrapper });
    expect(
      screen.getByRole("columnheader", { name: /language/i }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("row", { name: /en-ns/i })).getByText(
        "English (en)",
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("row", { name: /ja-ns/i })).getByText(
        "Japanese (ja)",
      ),
    ).toBeInTheDocument();
  });

  it("shows an unknown language tag as the raw tag", () => {
    render(<NamespaceList />, { wrapper });
    expect(
      within(screen.getByRole("row", { name: /pt-ns/i })).getByText("pt-BR"),
    ).toBeInTheDocument();
  });
});

describe("NamespaceList delete confirmation modal", () => {
  beforeEach(() => {
    mockNamespaces = [
      {
        namespaceId: "ns-1",
        name: "sales-data",
        status: NamespaceStatus.ARCHIVED,
        sourceCount: 3,
      },
    ];
    mockDeleteMutate.mockReset();
  });

  async function openDeleteModal() {
    const user = userEvent.setup();
    render(<NamespaceList />, { wrapper });

    // Select the row via its checkbox.
    const row = screen.getByRole("row", { name: /sales-data/i });
    const checkbox = within(row).getByRole("checkbox");
    await user.click(checkbox);

    // Open the "Change status" dropdown and click "Delete".
    const dropdownTrigger = screen.getByRole("button", {
      name: /change status/i,
    });
    await user.click(dropdownTrigger);
    const deleteItem = await screen.findByRole("menuitem", {
      name: /delete/i,
    });
    await user.click(deleteItem);

    return user;
  }

  it("shows a warning that deletion is permanent and cascades to all resources", async () => {
    await openDeleteModal();

    expect(
      screen.getByText(/this action cannot be undone/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/all data sources/i)).toBeInTheDocument();
    expect(screen.getByText(/all metric definitions/i)).toBeInTheDocument();
    expect(screen.getByText(/all ontology data/i)).toBeInTheDocument();
    expect(screen.getByText(/role and permission grants/i)).toBeInTheDocument();
    expect(
      screen.getByText(/athena workgroup and datazone project/i),
    ).toBeInTheDocument();
  });

  it("Delete button is disabled until the namespace name is typed exactly", async () => {
    const user = await openDeleteModal();

    const dialog = screen.getByRole("dialog");
    const deleteButton = within(dialog).getByRole("button", {
      name: /^delete$/i,
    });
    expect(deleteButton).toBeDisabled();

    const confirmInput = screen.getByPlaceholderText("sales-data");
    await user.type(confirmInput, "sales-dat");
    expect(deleteButton).toBeDisabled();

    await user.type(confirmInput, "a");
    expect(deleteButton).toBeEnabled();
  });

  it("calls deleteNamespace.mutate only after exact confirmation and click", async () => {
    const user = await openDeleteModal();

    const confirmInput = screen.getByPlaceholderText("sales-data");
    await user.type(confirmInput, "sales-data");

    const dialog = screen.getByRole("dialog");
    const deleteButton = within(dialog).getByRole("button", {
      name: /^delete$/i,
    });
    await user.click(deleteButton);

    expect(mockDeleteMutate).toHaveBeenCalledTimes(1);
    expect(mockDeleteMutate).toHaveBeenCalledWith(
      { namespaceId: "ns-1" },
      expect.objectContaining({
        onSuccess: expect.any(Function),
        onError: expect.any(Function),
      }),
    );
  });

  it("Cancel closes the modal without deleting", async () => {
    const user = await openDeleteModal();

    const cancelButton = screen.getByRole("button", { name: /^cancel$/i });
    await user.click(cancelButton);

    expect(mockDeleteMutate).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("getStatusActionItems", () => {
  const byId = (items: ReturnType<typeof getStatusActionItems>, id: string) =>
    items.find((i) => i.id === id)!;

  it("always returns Activate, Archive, and Delete", () => {
    const items = getStatusActionItems(NamespaceStatus.ACTIVE);
    expect(items.map((i) => i.text)).toEqual(["Activate", "Archive", "Delete"]);
  });

  it("ACTIVE: only Archive is enabled", () => {
    const items = getStatusActionItems(NamespaceStatus.ACTIVE);
    expect(byId(items, NamespaceStatus.ARCHIVED).disabled).toBe(false);
    expect(byId(items, NamespaceStatus.ACTIVE).disabled).toBe(true);
    expect(byId(items, NamespaceStatus.DELETED).disabled).toBe(true);
  });

  it("ARCHIVED: Activate and Delete are enabled, Archive disabled", () => {
    const items = getStatusActionItems(NamespaceStatus.ARCHIVED);
    expect(byId(items, NamespaceStatus.ACTIVE).disabled).toBe(false);
    expect(byId(items, NamespaceStatus.DELETED).disabled).toBe(false);
    expect(byId(items, NamespaceStatus.ARCHIVED).disabled).toBe(true);
  });

  it("DELETE_FAILED: only Delete is enabled", () => {
    const items = getStatusActionItems(NamespaceStatus.DELETE_FAILED);
    expect(byId(items, NamespaceStatus.DELETED).disabled).toBe(false);
    expect(byId(items, NamespaceStatus.ACTIVE).disabled).toBe(true);
    expect(byId(items, NamespaceStatus.ARCHIVED).disabled).toBe(true);
  });

  it("DELETED: every action is disabled", () => {
    const items = getStatusActionItems(NamespaceStatus.DELETED);
    expect(items.every((i) => i.disabled)).toBe(true);
  });

  it("DELETING: every action is disabled (system-managed, no user-initiated transitions)", () => {
    const items = getStatusActionItems(NamespaceStatus.DELETING);
    expect(items.every((i) => i.disabled)).toBe(true);
  });

  it("undefined status: every action is disabled", () => {
    const items = getStatusActionItems(undefined);
    expect(items.every((i) => i.disabled)).toBe(true);
  });

  it("disabled items carry an explanatory reason", () => {
    const items = getStatusActionItems(NamespaceStatus.ACTIVE);
    expect(byId(items, NamespaceStatus.ACTIVE).disabledReason).toMatch(
      /cannot activate/i,
    );
    expect(
      byId(items, NamespaceStatus.ARCHIVED).disabledReason,
    ).toBeUndefined();
  });
});

describe("getHeaderCounterText", () => {
  it("shows just the total when nothing is selected", () => {
    expect(getHeaderCounterText(2, 0)).toBe("(2)");
  });

  it("shows selected/total when rows are selected (selected first)", () => {
    expect(getHeaderCounterText(2, 1)).toBe("(1/2 selected)");
  });

  it("handles all rows selected", () => {
    expect(getHeaderCounterText(2, 2)).toBe("(2/2 selected)");
  });
});
