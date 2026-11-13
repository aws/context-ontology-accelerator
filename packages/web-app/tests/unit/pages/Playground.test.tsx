// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi, describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import React from "react";
import { Playground } from "../../../src/pages/Playground";

vi.mock("../../../src/components/RuntimeContext", () => ({
  RuntimeConfigContext: React.createContext({
    region: "us-east-1",
    serveRuntimeArn:
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
    oidcConfig: { authority: "https://mock", clientId: "c" },
  }),
}));

vi.mock("../../../src/components/NamespaceProvider", () => ({
  useCurrentNamespace: () => ({
    namespaceId: "ns-1",
    current: { namespaceId: "ns-1", name: "test", displayName: "Test" },
    namespaces: [],
    setNamespace: vi.fn(),
    isLoading: false,
  }),
}));

vi.mock("../../../src/api-hooks/use-playground-chat-sse", () => ({
  usePlaygroundChatSSE: () => ({
    state: { messages: [] },
    sessionId: null,
    isRestoring: false,
    isStreaming: false,
    sendQuery: vi.fn(),
    newChat: vi.fn(),
    retryQuery: vi.fn(),
    hasMoreHistory: false,
    isLoadingOlderHistory: false,
    requestOlderHistory: vi.fn(),
  }),
}));

vi.mock("../../../src/components/SplitPanelProvider", () => ({
  useSplitPanel: () => ({
    state: { content: null, isOpen: false, header: "" },
    openPanel: vi.fn(),
    closePanel: vi.fn(),
  }),
}));

vi.mock("../../../src/components/ToolsPanelProvider", () => ({
  useToolsPanel: () => ({
    state: { drawers: [], activeDrawerId: null },
    registerDrawer: vi.fn(),
    unregisterDrawer: vi.fn(),
    openDrawer: vi.fn(),
    closeDrawer: vi.fn(),
  }),
}));

function renderPlayground() {
  return render(
    <MemoryRouter initialEntries={["/playground"]}>
      <Routes>
        <Route path="/playground" element={<Playground />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Playground", () => {
  it("renders the playground header", () => {
    renderPlayground();
    expect(screen.getByText("Playground")).toBeInTheDocument();
  });

  it("renders the chat container with empty state", () => {
    renderPlayground();
    expect(
      screen.getByText("Ask a question against the governed surface..."),
    ).toBeInTheDocument();
  });
});
