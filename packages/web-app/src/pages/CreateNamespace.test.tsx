// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { CreateNamespace } from "./CreateNamespace";
import { LocaleProvider } from "../i18n/LocaleProvider";

const mockMutate = vi.fn();
// Mutable so a test can simulate a failed create; reset in beforeEach.
let mockError: Error | null = null;
let capturedOnError: (() => void) | undefined;

vi.mock("../api-hooks", () => ({
  useCreateNamespace: (options?: { onError?: () => void }) => {
    capturedOnError = options?.onError;
    return {
      mutate: mockMutate,
      isPending: false,
      error: mockError,
    };
  },
}));

vi.mock("../auth", () => ({
  useUser: () => ({ user: { userid: "user-123", email: "test@example.com" } }),
}));

vi.mock("@coa/control-plane-client", () => ({
  AccessDeniedError: class AccessDeniedError extends Error {
    constructor() {
      super("Access denied");
      this.name = "AccessDeniedError";
    }
  },
  // Referenced by extractErrorMessage when the form renders an error.
  ControlPlaneServiceServiceException: class ControlPlaneServiceServiceException extends Error {},
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
);

describe("CreateNamespace", () => {
  it("renders the form header", () => {
    render(<CreateNamespace />, { wrapper });
    expect(
      screen.getByRole("heading", { name: /create namespace/i }),
    ).toBeInTheDocument();
  });

  it("renders name and description fields", () => {
    render(<CreateNamespace />, { wrapper });
    expect(screen.getByText("Namespace name")).toBeInTheDocument();
    expect(screen.getByText(/Description/)).toBeInTheDocument();
  });

  it("shows validation error for empty name on submit", async () => {
    const user = userEvent.setup();
    render(<CreateNamespace />, { wrapper });
    await user.click(screen.getByRole("button", { name: /create namespace/i }));
    expect(screen.getByText("Namespace name is required.")).toBeInTheDocument();
  });

  it("derives machine-friendly name from display name", async () => {
    const user = userEvent.setup();
    render(<CreateNamespace />, { wrapper });
    const input = screen.getByPlaceholderText("e.g., Insurance Prod");
    await user.type(input, "My Namespace");
    expect(screen.getByText(/my-namespace/)).toBeInTheDocument();
  });

  it("collapses consecutive spaces into single hyphen", async () => {
    const user = userEvent.setup();
    render(<CreateNamespace />, { wrapper });
    const input = screen.getByPlaceholderText("e.g., Insurance Prod");
    await user.type(input, "Test  Name");
    expect(screen.getByText(/test-name/)).toBeInTheDocument();
  });

  it("strips special characters from machine name", async () => {
    const user = userEvent.setup();
    render(<CreateNamespace />, { wrapper });
    const input = screen.getByPlaceholderText("e.g., Insurance Prod");
    await user.type(input, "Hello! World@2024");
    expect(screen.getByText(/hello-world2024/)).toBeInTheDocument();
  });

  it("shows validation error for name starting with number", async () => {
    const user = userEvent.setup();
    render(<CreateNamespace />, { wrapper });
    const input = screen.getByPlaceholderText("e.g., Insurance Prod");
    await user.type(input, "123 Test");
    await user.click(screen.getByRole("button", { name: /create namespace/i }));
    expect(
      screen.getByText(/must start with a lowercase letter/),
    ).toBeInTheDocument();
  });
});

describe("CreateNamespace language", () => {
  beforeEach(() => {
    mockMutate.mockReset();
    mockError = null;
    capturedOnError = undefined;
  });

  async function fillNameAndSubmit() {
    const user = userEvent.setup();
    render(<CreateNamespace />, { wrapper });
    await user.type(
      screen.getByPlaceholderText("e.g., Insurance Prod"),
      "Sales Data",
    );
    return user;
  }

  it("offers exactly English and Japanese, with English selected by default", () => {
    render(<CreateNamespace />, { wrapper });
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(2);
    expect(screen.getByRole("radio", { name: "English (en)" })).toBeChecked();
    expect(
      screen.getByRole("radio", { name: "Japanese (ja)" }),
    ).not.toBeChecked();
  });

  it("states that the language cannot be changed and must match the sources", () => {
    render(<CreateNamespace />, { wrapper });
    expect(
      screen.getByText(
        /cannot be changed after the namespace is created, and it must match the language of the sources/i,
      ),
    ).toBeInTheDocument();
  });

  it("asks for confirmation before creating and does not create on submit alone", async () => {
    const user = await fillNameAndSubmit();
    await user.click(screen.getByRole("button", { name: /create namespace/i }));

    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByText("The language cannot be changed"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("sales-data")).toBeInTheDocument();
    expect(within(dialog).getAllByText("English (en)").length).toBeGreaterThan(
      0,
    );
    expect(mockMutate).not.toHaveBeenCalled();
  });

  it("sends language en by default after confirmation", async () => {
    const user = await fillNameAndSubmit();
    await user.click(screen.getByRole("button", { name: /create namespace/i }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: /confirm and create/i,
      }),
    );

    expect(mockMutate).toHaveBeenCalledTimes(1);
    expect(mockMutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: "sales-data", language: "en" }),
    );
  });

  it("sends language ja when Japanese is selected and confirmed", async () => {
    const user = await fillNameAndSubmit();
    await user.click(screen.getByRole("radio", { name: "Japanese (ja)" }));
    await user.click(screen.getByRole("button", { name: /create namespace/i }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getAllByText("Japanese (ja)").length).toBeGreaterThan(
      0,
    );
    await user.click(
      within(dialog).getByRole("button", { name: /confirm and create/i }),
    );

    expect(mockMutate).toHaveBeenCalledTimes(1);
    expect(mockMutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: "sales-data", language: "ja" }),
    );
  });

  it("Back closes the confirmation without creating", async () => {
    const user = await fillNameAndSubmit();
    await user.click(screen.getByRole("button", { name: /create namespace/i }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: /back/i }),
    );

    expect(mockMutate).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the language choice and its confirmation in Japanese", async () => {
    window.localStorage.setItem("scl.locale", "ja");
    try {
      const user = userEvent.setup();
      render(
        <LocaleProvider>
          <CreateNamespace />
        </LocaleProvider>,
        { wrapper },
      );
      await user.type(
        await screen.findByPlaceholderText("例: 保険本番"),
        "Sales Data",
      );
      await user.click(screen.getByRole("radio", { name: "日本語 (ja)" }));
      expect(
        screen.getByRole("radio", { name: "英語 (en)" }),
      ).not.toBeChecked();
      await user.click(screen.getByRole("button", { name: "名前空間を作成" }));

      const dialog = screen.getByRole("dialog");
      expect(
        within(dialog).getByText("言語は変更できません"),
      ).toBeInTheDocument();
      expect(within(dialog).getAllByText("日本語 (ja)").length).toBeGreaterThan(
        0,
      );
      await user.click(
        within(dialog).getByRole("button", { name: "確認して作成" }),
      );
      // Only the copy is localized; the request still carries the tag.
      expect(mockMutate).toHaveBeenCalledWith(
        expect.objectContaining({ name: "sales-data", language: "ja" }),
      );
    } finally {
      window.localStorage.clear();
      document.documentElement.lang = "";
    }
  });

  it("closes the confirmation and shows the form error when create fails", async () => {
    mockMutate.mockImplementation(() => {
      mockError = new Error("Namespace already exists");
      capturedOnError?.();
    });
    const user = await fillNameAndSubmit();
    await user.click(screen.getByRole("button", { name: /create namespace/i }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: /confirm and create/i,
      }),
    );

    expect(mockMutate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("Error creating namespace")).toBeInTheDocument();
    expect(
      screen.getAllByText("Namespace already exists").length,
    ).toBeGreaterThan(0);
  });

  it("does not open the confirmation when validation fails", async () => {
    const user = userEvent.setup();
    render(<CreateNamespace />, { wrapper });
    await user.click(screen.getByRole("button", { name: /create namespace/i }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockMutate).not.toHaveBeenCalled();
  });
});
