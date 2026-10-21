// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { LocaleProvider } from "../i18n/LocaleProvider";
import { TraceDetail } from "./TraceDetail";
import type { TraceStep } from "@app-types";

// Regression guards for bug #836:
//   1. Guardrails badge renders "passed" when the t3.guardrail step succeeded
//      (previously always rendered "n/a" because the UI only had a boolean).
//   2. A "Back to Playground" affordance is present on the success path (was
//      missing entirely — only the error branch had one). Navigation calls
//      setNamespace(metadata.namespace) so the playground doesn't fall back
//      to the first namespace on reload/return.

const useTraceDataMock = vi.fn();
vi.mock("../api-hooks/use-trace-data", () => ({
  useTraceData: () => useTraceDataMock(),
}));

const setNamespaceMock = vi.fn();
vi.mock("../components/NamespaceProvider", () => ({
  useCurrentNamespace: () => ({
    setNamespace: setNamespaceMock,
    namespaceId: "",
    namespaces: [],
    current: undefined,
    isLoading: false,
  }),
}));

function traceData(overrides: {
  trace?: TraceStep[];
  guardrailBlocked?: boolean;
  namespace?: string;
  tier?: number;
}) {
  return {
    trace: overrides.trace ?? [],
    tier: overrides.tier ?? 3,
    content: "Answer",
    guardrailBlocked: overrides.guardrailBlocked ?? false,
    metadata: overrides.namespace ? { namespace: overrides.namespace } : {},
    requestId: "req-abc",
    userQuery: "What is the SLA?",
  };
}

function renderTraceDetail() {
  return render(
    <MemoryRouter initialEntries={["/playground/trace/session-1/req-abc"]}>
      <Routes>
        <Route
          path="/playground/trace/:sessionId/:requestId"
          element={<TraceDetail />}
        />
        <Route path="/playground" element={<div>PLAYGROUND</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("TraceDetail — Guardrails boundary badge (bug #836)", () => {
  beforeEach(() => {
    useTraceDataMock.mockReset();
  });

  it("renders Guardrails: passed when the t3.guardrail step succeeded", () => {
    useTraceDataMock.mockReturnValue({
      data: traceData({
        trace: [
          { step: "t3.synthesize", status: "success", durationMs: 800 },
          { step: "t3.guardrail", status: "success", durationMs: 0 },
        ],
      }),
      loading: false,
      error: null,
      retry: () => {},
    });
    renderTraceDetail();
    expect(screen.getByText("Guardrails: passed")).toBeInTheDocument();
  });

  it("renders Guardrails: blocked when the t3.guardrail step was denied", () => {
    useTraceDataMock.mockReturnValue({
      data: traceData({
        trace: [{ step: "t3.guardrail", status: "denied", durationMs: 0 }],
        guardrailBlocked: true,
      }),
      loading: false,
      error: null,
      retry: () => {},
    });
    renderTraceDetail();
    expect(screen.getByText("Guardrails: blocked")).toBeInTheDocument();
  });

  it("renders Guardrails: n/a when no guardrail step is present", () => {
    useTraceDataMock.mockReturnValue({
      data: traceData({
        trace: [{ step: "t3.synthesize", status: "success", durationMs: 100 }],
      }),
      loading: false,
      error: null,
      retry: () => {},
    });
    renderTraceDetail();
    expect(screen.getByText("Guardrails: n/a")).toBeInTheDocument();
  });

  it("falls back to guardrailBlocked=true → blocked for legacy traces without the step", () => {
    // Traces recorded before the t3.guardrail step existed should still
    // render "blocked" on the block path so the boundary indicator remains
    // meaningful for old sessions.
    useTraceDataMock.mockReturnValue({
      data: traceData({
        trace: [{ step: "t3.synthesize", status: "success", durationMs: 100 }],
        guardrailBlocked: true,
      }),
      loading: false,
      error: null,
      retry: () => {},
    });
    renderTraceDetail();
    expect(screen.getByText("Guardrails: blocked")).toBeInTheDocument();
  });
});

describe("TraceDetail — Back to Playground (bug #836)", () => {
  beforeEach(() => {
    useTraceDataMock.mockReset();
    setNamespaceMock.mockReset();
  });

  it("renders a back-navigation control on the success path", () => {
    useTraceDataMock.mockReturnValue({
      data: traceData({ trace: [] }),
      loading: false,
      error: null,
      retry: () => {},
    });
    renderTraceDetail();
    expect(
      screen.getByRole("button", { name: /Back to Playground/i }),
    ).toBeInTheDocument();
  });

  it("preserves the namespace via setNamespace before navigating to /playground", async () => {
    useTraceDataMock.mockReturnValue({
      data: traceData({ trace: [], namespace: "insurance-prod" }),
      loading: false,
      error: null,
      retry: () => {},
    });
    renderTraceDetail();
    await userEvent.click(
      screen.getByRole("button", { name: /Back to Playground/i }),
    );
    expect(setNamespaceMock).toHaveBeenCalledWith("insurance-prod");
    expect(screen.getByText("PLAYGROUND")).toBeInTheDocument();
  });

  it("navigates to /playground without setNamespace when metadata.namespace is missing", async () => {
    useTraceDataMock.mockReturnValue({
      data: traceData({ trace: [] }),
      loading: false,
      error: null,
      retry: () => {},
    });
    renderTraceDetail();
    await userEvent.click(
      screen.getByRole("button", { name: /Back to Playground/i }),
    );
    expect(setNamespaceMock).not.toHaveBeenCalled();
    expect(screen.getByText("PLAYGROUND")).toBeInTheDocument();
  });
});

const NL_TO_SQL_TRACE: TraceStep[] = [
  { step: "t1.metric_match", status: "miss", durationMs: 20 },
  { step: "t2.sql.generate", status: "success", durationMs: 5800 },
  { step: "t2.sql.execute", status: "success", durationMs: 1900 },
];

const VKG_TRACE: TraceStep[] = [
  { step: "t2.vkg.translate", status: "success", durationMs: 900 },
  { step: "t2.vkg.execute", status: "success", durationMs: 700 },
];

function renderWith(data: ReturnType<typeof traceData>) {
  useTraceDataMock.mockReturnValue({
    data,
    loading: false,
    error: null,
    retry: () => {},
  });
  renderTraceDetail();
}

describe("TraceDetail — Tier outcome names the Tier 2 strategy that ran", () => {
  beforeEach(() => {
    useTraceDataMock.mockReset();
  });

  it("labels an NL→SQL answer as a structured query, not ontology", () => {
    renderWith(traceData({ tier: 2, trace: NL_TO_SQL_TRACE }));
    expect(
      screen.getByText("Tier 2 — Structured Query (NL→SQL)"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Tier 2 — ontology/)).not.toBeInTheDocument();
  });

  it("labels an Ontop answer with the VKG strategy", () => {
    renderWith(traceData({ tier: 2, trace: VKG_TRACE }));
    expect(
      screen.getByText("Tier 2 — Structured Query (VKG (Ontop))"),
    ).toBeInTheDocument();
  });

  it("names both strategies when NL→SQL and VKG both ran", () => {
    renderWith(
      traceData({ tier: 2, trace: [...NL_TO_SQL_TRACE, ...VKG_TRACE] }),
    );
    expect(
      screen.getByText("Tier 2 — Structured Query (NL→SQL + VKG (Ontop))"),
    ).toBeInTheDocument();
  });

  it("keeps the Tier 1 label", () => {
    renderWith(traceData({ tier: 1 }));
    expect(screen.getByText("Tier 1 — metric")).toBeInTheDocument();
  });

  it("keeps the Tier 3 label", () => {
    renderWith(traceData({ tier: 3 }));
    expect(screen.getByText("Tier 3 — knowledge")).toBeInTheDocument();
  });
});

describe("TraceDetail — Tier outcome in Japanese", () => {
  beforeEach(() => {
    useTraceDataMock.mockReset();
    window.localStorage.setItem("scl.locale", "ja");
  });

  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  function renderJapanese(data: ReturnType<typeof traceData>) {
    useTraceDataMock.mockReturnValue({
      data,
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

  it("names the Tier 2 strategies through the shared localized title", async () => {
    renderJapanese(
      traceData({ tier: 2, trace: [...NL_TO_SQL_TRACE, ...VKG_TRACE] }),
    );
    expect(
      await screen.findByText("Tier 2 — 構造化クエリ（NL→SQL + VKG (Ontop)）"),
    ).toBeInTheDocument();
  });

  it("localizes the Tier 1 and Tier 3 labels", async () => {
    const { unmount } = renderJapanese(traceData({ tier: 1 }));
    expect(await screen.findByText("Tier 1 — メトリクス")).toBeInTheDocument();
    unmount();
    renderJapanese(traceData({ tier: 3 }));
    expect(await screen.findByText("Tier 3 — ナレッジ")).toBeInTheDocument();
  });
});
