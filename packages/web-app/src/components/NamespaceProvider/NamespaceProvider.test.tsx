// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { NamespaceProvider, useCurrentNamespace } from "./index";

// ---------------------------------------------------------------------------
// Mocks: the real list/get hooks run against a fake control-plane client.
// ---------------------------------------------------------------------------

interface FakeCommand {
  kind: "list" | "get";
  input: { nextToken?: string; namespaceId?: string };
}

const mockSend = vi.fn<(command: FakeCommand) => Promise<unknown>>();

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: mockSend }),
}));

vi.mock("@coa/control-plane-client", async (importOriginal) => {
  const actual = await importOriginal<object>();
  return {
    ...actual,
    ListNamespacesCommand: vi.fn().mockImplementation(function (input) {
      return { kind: "list", input };
    }),
    GetNamespaceCommand: vi.fn().mockImplementation(function (input) {
      return { kind: "get", input };
    }),
  };
});

// ---------------------------------------------------------------------------
// Fixtures: a 2-page list, 25 on page 1 (the API's page size) and 3 on page 2.
// ---------------------------------------------------------------------------

function ns(i: number) {
  const id = `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
  return {
    namespaceId: id,
    name: `ns-${String(i).padStart(2, "0")}`,
    status: "ACTIVE",
    language: "en",
  };
}

const PAGE_1 = Array.from({ length: 25 }, (_, i) => ns(i + 1));
const PAGE_2 = Array.from({ length: 3 }, (_, i) => ns(i + 26));
const ON_PAGE_2 = PAGE_2[1]!;
const TOKEN = "page-2-token";

const never = () => new Promise<never>(() => undefined);

/** Default backend: both pages and GetNamespace for any listed id. */
function serveBothPages(command: FakeCommand): Promise<unknown> {
  if (command.kind === "list") {
    return Promise.resolve(
      command.input.nextToken === TOKEN
        ? { namespaces: PAGE_2 }
        : { namespaces: PAGE_1, nextToken: TOKEN },
    );
  }
  const found = [...PAGE_1, ...PAGE_2].find(
    (n) => n.namespaceId === command.input.namespaceId,
  );
  return found
    ? Promise.resolve({ namespace: { ...found, owner: "o", createdAt: "t" } })
    : Promise.reject(new Error("NotFoundError"));
}

function listCalls(nextToken: string | undefined) {
  return mockSend.mock.calls.filter(
    ([c]) => c.kind === "list" && c.input.nextToken === nextToken,
  ).length;
}

function getCalls(namespaceId: string) {
  return mockSend.mock.calls.filter(
    ([c]) => c.kind === "get" && c.input.namespaceId === namespaceId,
  ).length;
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

function Probe() {
  const { current, namespaceId, namespaces, isLoading } = useCurrentNamespace();
  const navigate = useNavigate();
  return (
    <div>
      <span data-testid="namespace-id">{namespaceId}</span>
      <span data-testid="current-name">{current?.name ?? ""}</span>
      <span data-testid="count">{namespaces.length}</span>
      <span data-testid="loading">{String(isLoading)}</span>
      <button onClick={() => navigate("/playground")}>to playground</button>
    </div>
  );
}

function renderAt(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <NamespaceProvider>
          <Probe />
        </NamespaceProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

const text = (id: string) => screen.getByTestId(id).textContent;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("NamespaceProvider", () => {
  beforeEach(() => {
    mockSend.mockReset();
    mockSend.mockImplementation(serveBothPages);
  });

  it("loads every page of the list, so the switcher can reach page 2", async () => {
    renderAt("/");
    await waitFor(() => expect(text("count")).toBe("28"));
    expect(listCalls(undefined)).toBe(1);
    expect(listCalls(TOKEN)).toBe(1);
    // With nothing chosen, the first namespace is still the default.
    expect(text("namespace-id")).toBe(PAGE_1[0]!.namespaceId);
  });

  it("makes a page-2 namespace named in the URL the current one", async () => {
    renderAt(`/namespaces/${ON_PAGE_2.namespaceId}/sources`);
    await waitFor(() => expect(text("count")).toBe("28"));
    expect(text("namespace-id")).toBe(ON_PAGE_2.namespaceId);
    expect(text("current-name")).toBe(ON_PAGE_2.name);
  });

  it("keeps the page-2 namespace after leaving its URL for the Playground", async () => {
    renderAt(`/namespaces/${ON_PAGE_2.namespaceId}`);
    await waitFor(() =>
      expect(text("namespace-id")).toBe(ON_PAGE_2.namespaceId),
    );
    await act(async () => screen.getByText("to playground").click());
    expect(text("namespace-id")).toBe(ON_PAGE_2.namespaceId);
  });

  it("resolves the URL namespace by id before the later pages arrive", async () => {
    mockSend.mockImplementation((command) =>
      command.kind === "list" && command.input.nextToken === TOKEN
        ? never()
        : serveBothPages(command),
    );
    renderAt(`/namespaces/${ON_PAGE_2.namespaceId}`);
    await waitFor(() =>
      expect(text("namespace-id")).toBe(ON_PAGE_2.namespaceId),
    );
    expect(text("current-name")).toBe(ON_PAGE_2.name);
    expect(text("count")).toBe("25");
    expect(getCalls(ON_PAGE_2.namespaceId)).toBe(1);
  });

  it("keeps a namespace found only by id after leaving its URL for the Playground", async () => {
    mockSend.mockImplementation((command) =>
      command.kind === "list" && command.input.nextToken === TOKEN
        ? never()
        : serveBothPages(command),
    );
    renderAt(`/namespaces/${ON_PAGE_2.namespaceId}`);
    await waitFor(() =>
      expect(text("namespace-id")).toBe(ON_PAGE_2.namespaceId),
    );
    await act(async () => screen.getByText("to playground").click());
    expect(text("namespace-id")).toBe(ON_PAGE_2.namespaceId);
    expect(text("current-name")).toBe(ON_PAGE_2.name);
    expect(text("count")).toBe("25");
  });

  it("does not interrupt a list refetch to load the next page", async () => {
    // Page 2 is still loading when the list is refreshed (as after a create).
    let refetching = false;
    let releasePage1: () => void = () => {};
    const added = ns(99);
    mockSend.mockImplementation((command) => {
      if (command.kind !== "list") return serveBothPages(command);
      if (command.input.nextToken === TOKEN) {
        return refetching ? serveBothPages(command) : never();
      }
      if (!refetching) return serveBothPages(command);
      return new Promise((resolve) => {
        releasePage1 = () =>
          resolve({ namespaces: [added, ...PAGE_1], nextToken: TOKEN });
      });
    });
    const client = renderAt("/");
    await waitFor(() => expect(listCalls(TOKEN)).toBe(1));
    expect(text("count")).toBe("25");

    refetching = true;
    await act(async () => {
      void client.invalidateQueries({ queryKey: ["namespaces"] });
    });
    await waitFor(() => expect(listCalls(undefined)).toBe(2));
    // Give the page loader a chance to interrupt the refetch.
    await new Promise((r) => setTimeout(r, 50));
    await act(async () => releasePage1());

    await waitFor(() => expect(text("count")).toBe("29"));
    // Page 1 was fetched once for the first load and once for the refetch.
    expect(listCalls(undefined)).toBe(2);
  });

  it("does not fall back to another namespace while the URL one is unresolved", async () => {
    mockSend.mockImplementation((command) =>
      command.kind === "list" && !command.input.nextToken
        ? serveBothPages(command)
        : never(),
    );
    renderAt(`/namespaces/${ON_PAGE_2.namespaceId}`);
    await waitFor(() => expect(text("count")).toBe("25"));
    expect(text("namespace-id")).toBe("");
    expect(text("current-name")).toBe("");
    expect(text("loading")).toBe("true");
  });

  it("does not fall back when the URL names a namespace the server rejects", async () => {
    const missing = "00000000-0000-4000-8000-999999999999";
    renderAt(`/namespaces/${missing}`);
    await waitFor(() => expect(text("count")).toBe("28"));
    await waitFor(() => expect(text("loading")).toBe("false"));
    expect(text("namespace-id")).toBe("");
  });

  it("falls back to the first namespace when a remembered one is gone", async () => {
    const missing = "00000000-0000-4000-8000-999999999999";
    renderAt(`/namespaces/${missing}`);
    await waitFor(() => expect(text("loading")).toBe("false"));
    await act(async () => screen.getByText("to playground").click());
    await waitFor(() =>
      expect(text("namespace-id")).toBe(PAGE_1[0]!.namespaceId),
    );
  });

  it("does not treat /namespaces/create as a namespace id", async () => {
    renderAt("/namespaces/create");
    await waitFor(() => expect(text("count")).toBe("28"));
    expect(getCalls("create")).toBe(0);
    expect(text("namespace-id")).toBe(PAGE_1[0]!.namespaceId);
  });

  it("stops loading pages after a next-page error instead of retrying in a loop", async () => {
    mockSend.mockImplementation((command) =>
      command.kind === "list" && command.input.nextToken === TOKEN
        ? Promise.reject(new Error("boom"))
        : serveBothPages(command),
    );
    renderAt("/");
    await waitFor(() => expect(listCalls(TOKEN)).toBe(1));
    // Give a retry loop time to show itself.
    await new Promise((r) => setTimeout(r, 50));
    expect(listCalls(TOKEN)).toBe(1);
    expect(text("count")).toBe("25");
  });
});
