// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider } from "../i18n/LocaleProvider";
import { TraceDetail } from "./TraceDetail";

// The Trace detail breadcrumb and the Status column of the trace table go
// through the catalogue: no "Trace <id>" literal and no raw step status such
// as "allow" in either language.

const useTraceDataMock = vi.fn();
vi.mock("../api-hooks/use-trace-data", () => ({
  useTraceData: () => useTraceDataMock(),
}));

vi.mock("../components/NamespaceProvider", () => ({
  useCurrentNamespace: () => ({
    setNamespace: vi.fn(),
    namespaceId: "",
    namespaces: [],
    current: undefined,
    isLoading: false,
  }),
}));

const crumbs: Array<Array<{ text: string }>> = [];
vi.mock("@api-hooks/use-breadcrumb-trail", () => ({
  useBreadcrumbTrail: (items: Array<{ text: string }>) => {
    crumbs.push(items);
  },
}));

function renderIn(locale: "en" | "ja") {
  window.localStorage.setItem("scl.locale", locale);
  useTraceDataMock.mockReturnValue({
    data: {
      trace: [
        { step: "t2.sql_authz", status: "allow", durationMs: 3 },
        { step: "t2.execute", status: "succeeded", durationMs: 40 },
      ],
      tier: 2,
      content: "Answer",
      guardrailBlocked: false,
      metadata: {},
      requestId: "req-abc",
      userQuery: "How many orders?",
    },
    loading: false,
    error: null,
    retry: () => {},
  });
  return render(
    <LocaleProvider>
      <MemoryRouter initialEntries={["/playground/trace/session-1/req-abc"]}>
        <Routes>
          <Route
            path="/playground/trace/:sessionId/:requestId"
            element={<TraceDetail />}
          />
        </Routes>
      </MemoryRouter>
    </LocaleProvider>,
  );
}

describe("TraceDetail — localized breadcrumb and step status", () => {
  beforeEach(() => {
    crumbs.length = 0;
    useTraceDataMock.mockReset();
  });

  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("shows step statuses and the trace crumb in Japanese", async () => {
    renderIn("ja");
    expect((await screen.findAllByText("解決済み")).length).toBe(2);
    expect(screen.queryByText("allow")).not.toBeInTheDocument();
    expect(crumbs.at(-1)?.at(-1)?.text).toBe("トレース req-abc");
  });

  it("uses the shared English words in English", async () => {
    renderIn("en");
    expect((await screen.findAllByText("Resolved")).length).toBe(2);
    expect(screen.queryByText("allow")).not.toBeInTheDocument();
    expect(crumbs.at(-1)?.at(-1)?.text).toBe("Trace req-abc");
  });
});
