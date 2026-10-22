// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LocaleProvider } from "../i18n/LocaleProvider";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MetricList } from "./MetricList";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let mockMetrics: Array<{
  name: string;
  description: string;
  dataSourceId: string;
  sourceTable: string;
  expression: { dialects: Array<{ dialect: string }> };
}> = [];

const mockBulkDeleteMutate = vi.fn();

const mockExportMutate = vi.fn();

// Pagination state for the useListMetrics mock — mutable so tests can
// simulate an infinite query with unloaded server pages.
let mockHasNextPage = false;
const mockFetchNextPage = vi.fn(() => Promise.resolve());

vi.mock("../api-hooks/use-metrics", () => ({
  useListMetrics: () => ({
    data: { pages: [{ metrics: mockMetrics, nextToken: undefined }] },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
    isFetching: false,
    hasNextPage: mockHasNextPage,
    isFetchingNextPage: false,
    fetchNextPage: mockFetchNextPage,
  }),
  useImportOsiFile: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  useExportOsi: () => ({
    mutate: mockExportMutate,
    isPending: false,
    isError: false,
  }),
  useGetImportJob: () => ({ data: undefined }),
  useBulkDeleteMetrics: () => ({
    mutate: mockBulkDeleteMutate,
    isPending: false,
    isError: false,
  }),
}));

vi.mock("../api-hooks/use-source-name-by-id", () => ({
  useSourceNameById: () => new Map<string, string>(),
}));

// LocaleProvider sits at the app root in production and supplies the
// property filter's built-in strings the page no longer hand-writes.
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <LocaleProvider>
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  </LocaleProvider>
);

function makeMetric(name: string) {
  return {
    name,
    description: `desc for ${name}`,
    dataSourceId: "ds-1",
    sourceTable: "orders",
    expression: { dialects: [{ dialect: "POSTGRESQL" }] },
  };
}

describe("MetricList export scope", () => {
  beforeEach(() => {
    mockExportMutate.mockReset();
  });

  /** Multiple Cloudscape Modals stay mounted in the DOM even when closed, so
   *  scope queries to the export modal specifically via its header text. */
  function getExportDialog(): HTMLElement {
    const heading = screen.getByRole("heading", {
      name: /export osi metrics/i,
    });
    return heading.closest('[role="dialog"]') as HTMLElement;
  }

  it("defaults to 'all' and exports with no names when nothing is selected/filtered", async () => {
    mockMetrics = [makeMetric("metric_a"), makeMetric("metric_b")];
    const user = userEvent.setup();
    render(<MetricList />, { wrapper });

    await user.click(screen.getByRole("button", { name: /export osi/i }));
    const dialog = getExportDialog();
    expect(
      within(dialog).getByText(/export all 2 metrics in this namespace/i),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /^export$/i }));

    expect(mockExportMutate).toHaveBeenCalledTimes(1);
    expect(mockExportMutate).toHaveBeenCalledWith(
      undefined,
      expect.objectContaining({
        onSuccess: expect.any(Function),
      }),
    );
  });

  it("defaults to 'selected' and exports only the selected names when a selection exists", async () => {
    mockMetrics = [makeMetric("metric_a"), makeMetric("metric_b")];
    const user = userEvent.setup();
    render(<MetricList />, { wrapper });

    const rowA = screen.getByRole("row", { name: /metric_a/i });
    await user.click(within(rowA).getByRole("checkbox"));

    await user.click(screen.getByRole("button", { name: /export osi/i }));
    const dialog = getExportDialog();
    expect(
      within(dialog).getByText(/export 1 selected metrics/i),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /^export$/i }));

    expect(mockExportMutate).toHaveBeenCalledWith(
      { names: ["metric_a"] },
      expect.objectContaining({
        onSuccess: expect.any(Function),
      }),
    );
  });

  it("disables the 'selected' option when nothing is selected", async () => {
    mockMetrics = [makeMetric("metric_a")];
    const user = userEvent.setup();
    render(<MetricList />, { wrapper });

    await user.click(screen.getByRole("button", { name: /export osi/i }));
    const dialog = getExportDialog();
    const selectedRadio = within(dialog).getByRole("radio", {
      name: /export 0 selected metrics/i,
    });
    expect(selectedRadio).toBeDisabled();
  });

  it("exports only the filtered subset, not all metrics, when 'filtered' is chosen", async () => {
    // Regression test: the filtered-export handler previously used
    // `allMetrics.filter(m => items.some(...) || tokens.length > 0)` — the
    // `||` made every metric match once any filter token existed, silently
    // exporting everything instead of the filtered subset.
    mockMetrics = [makeMetric("total_orders"), makeMetric("unique_customers")];
    const user = userEvent.setup();
    render(<MetricList />, { wrapper });

    const filterInput = screen.getByPlaceholderText(/find metrics/i);
    await user.type(filterInput, "total");
    await user.click(screen.getByText(/use: "total"/i));

    await user.click(screen.getByRole("button", { name: /export osi/i }));
    const dialog = getExportDialog();

    const filteredRadio = within(dialog).getByRole("radio", {
      name: /export all 1 metrics matching/i,
    });
    await user.click(filteredRadio);
    await user.click(within(dialog).getByRole("button", { name: /^export$/i }));

    expect(mockExportMutate).toHaveBeenCalledWith(
      { names: ["total_orders"] },
      expect.objectContaining({
        onSuccess: expect.any(Function),
      }),
    );
  });
});

describe("MetricList bulk-delete selection sync", () => {
  beforeEach(() => {
    mockBulkDeleteMutate.mockReset();
  });

  it("resets the 'Delete N selected' button to disabled once deleted rows disappear from the data", async () => {
    mockMetrics = [makeMetric("metric_a"), makeMetric("metric_b")];
    const user = userEvent.setup();
    const { rerender } = render(<MetricList />, { wrapper });

    // Select metric_a.
    const rowA = screen.getByRole("row", { name: /metric_a/i });
    await user.click(within(rowA).getByRole("checkbox"));
    expect(
      screen.getByRole("button", { name: /delete 1 selected/i }),
    ).toBeInTheDocument();

    // Simulate the bulk-delete completing: the query invalidation refetch
    // returns data with metric_a removed (React Query would normally drive
    // this via cache update — re-rendering with new mockMetrics + a fresh
    // render approximates the same "allMetrics changed" transition).
    mockMetrics = [makeMetric("metric_b")];
    rerender(<MetricList />);

    // The button must reset to disabled "Delete", not linger at "1 selected".
    // The exact "Delete" name can also match the bulk-delete modal's footer
    // button (Cloudscape keeps modal content mounted even when closed), so
    // pick the one that is NOT inside a dialog — that's the header action.
    expect(
      screen.queryByRole("button", { name: /delete 1 selected/i }),
    ).not.toBeInTheDocument();
    const deleteButtons = screen.getAllByRole("button", { name: /^delete$/i });
    const headerDeleteButton = deleteButtons.find(
      (btn) => !btn.closest('[role="dialog"]'),
    ) as HTMLElement;
    expect(headerDeleteButton).toBeDisabled();
  });

  it("preserves a selection that is still present after an unrelated refetch", async () => {
    mockMetrics = [makeMetric("metric_a"), makeMetric("metric_b")];
    const user = userEvent.setup();
    const { rerender } = render(<MetricList />, { wrapper });

    const rowA = screen.getByRole("row", { name: /metric_a/i });
    await user.click(within(rowA).getByRole("checkbox"));
    expect(
      screen.getByRole("button", { name: /delete 1 selected/i }),
    ).toBeInTheDocument();

    // Refetch returns the exact same set (e.g. a manual refresh) — the
    // selection should NOT be cleared just because `allMetrics` re-rendered.
    mockMetrics = [makeMetric("metric_a"), makeMetric("metric_b")];
    rerender(<MetricList />);

    expect(
      screen.getByRole("button", { name: /delete 1 selected/i }),
    ).toBeInTheDocument();
  });

  it("deletes only the filtered subset, not all metrics, when 'filtered' is chosen", async () => {
    // Regression test: the filtered-delete handler previously used the same
    // `items.some(...) || tokens.length > 0` `||` bug as the export handler —
    // once any filter token existed, every metric matched and got deleted
    // instead of just the filtered subset.
    mockMetrics = [makeMetric("total_orders"), makeMetric("unique_customers")];
    const user = userEvent.setup();
    render(<MetricList />, { wrapper });

    // Select a metric AND apply a filter so both the "selected" and
    // "filtered" radio options are available in the modal.
    const rowA = screen.getByRole("row", { name: /total_orders/i });
    await user.click(within(rowA).getByRole("checkbox"));

    const filterInput = screen.getByPlaceholderText(/find metrics/i);
    await user.type(filterInput, "total");
    await user.click(screen.getByText(/use: "total"/i));

    await user.click(
      screen.getByRole("button", { name: /delete 1 selected/i }),
    );

    const heading = screen.getByRole("heading", { name: /delete metrics/i });
    const dialog = heading.closest('[role="dialog"]') as HTMLElement;

    const filteredRadio = within(dialog).getByRole("radio", {
      name: /delete all 1 metrics matching/i,
    });
    await user.click(filteredRadio);
    await user.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(mockBulkDeleteMutate).toHaveBeenCalledTimes(1);
    expect(mockBulkDeleteMutate).toHaveBeenCalledWith(
      { names: ["total_orders"] },
      expect.objectContaining({
        onSuccess: expect.any(Function),
        onError: expect.any(Function),
      }),
    );
  });
});

describe("MetricList filtered scope covers the complete namespace", () => {
  beforeEach(() => {
    mockExportMutate.mockReset();
    mockBulkDeleteMutate.mockReset();
    mockFetchNextPage.mockReset();
    mockHasNextPage = false;
  });

  function getExportDialog(): HTMLElement {
    const heading = screen.getByRole("heading", {
      name: /export osi metrics/i,
    });
    return heading.closest('[role="dialog"]') as HTMLElement;
  }

  it("filtered export includes matches beyond the current client page (allPageItems, not items)", async () => {
    // 25 matching metrics with pageSize 20 — matches on client page 2 must
    // still be exported. The pre-#744 code derived names from `items`
    // (CURRENT page only) and would export just the first 20.
    mockMetrics = Array.from({ length: 25 }, (_, i) =>
      makeMetric(`total_${String(i).padStart(2, "0")}`),
    );
    const user = userEvent.setup();
    render(<MetricList />, { wrapper });

    const filterInput = screen.getByPlaceholderText(/find metrics/i);
    await user.type(filterInput, "total");
    await user.click(screen.getByText(/use: "total"/i));

    await user.click(screen.getByRole("button", { name: /export osi/i }));
    const dialog = getExportDialog();
    await user.click(
      within(dialog).getByRole("radio", {
        name: /export all 25 metrics matching/i,
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: /^export$/i }));

    expect(mockExportMutate).toHaveBeenCalledTimes(1);
    const exported = mockExportMutate.mock.calls[0][0] as { names: string[] };
    expect(exported.names).toHaveLength(25);
    expect(exported.names).toContain("total_24"); // beyond client page 1
  });

  it("filtered export drains unloaded server pages before executing", async () => {
    mockMetrics = [makeMetric("total_orders"), makeMetric("other_metric")];
    mockHasNextPage = true; // more server pages exist
    const user = userEvent.setup();
    const { rerender } = render(<MetricList />, { wrapper });

    const filterInput = screen.getByPlaceholderText(/find metrics/i);
    await user.type(filterInput, "total");
    await user.click(screen.getByText(/use: "total"/i));

    await user.click(screen.getByRole("button", { name: /export osi/i }));
    const dialog = getExportDialog();
    await user.click(
      within(dialog).getByRole("radio", { name: /metrics matching/i }),
    );
    await user.click(within(dialog).getByRole("button", { name: /^export$/i }));

    // Must NOT export yet — a server page is still unloaded.
    expect(mockExportMutate).not.toHaveBeenCalled();
    // Drain must have started.
    expect(mockFetchNextPage).toHaveBeenCalled();

    // Simulate the final page arriving with another matching metric.
    mockMetrics = [
      makeMetric("total_orders"),
      makeMetric("other_metric"),
      makeMetric("total_refunds"),
    ];
    mockHasNextPage = false;
    rerender(<MetricList />);

    expect(mockExportMutate).toHaveBeenCalledTimes(1);
    const exported = mockExportMutate.mock.calls[0][0] as { names: string[] };
    expect(exported.names).toEqual(
      expect.arrayContaining(["total_orders", "total_refunds"]),
    );
    expect(exported.names).not.toContain("other_metric");
  });

  it("filtered bulk-delete drains unloaded server pages before executing", async () => {
    mockMetrics = [makeMetric("total_orders"), makeMetric("other_metric")];
    mockHasNextPage = true;
    const user = userEvent.setup();
    const { rerender } = render(<MetricList />, { wrapper });

    // Select + filter so both radio options appear in the delete modal.
    const rowA = screen.getByRole("row", { name: /total_orders/i });
    await user.click(within(rowA).getByRole("checkbox"));
    const filterInput = screen.getByPlaceholderText(/find metrics/i);
    await user.type(filterInput, "total");
    await user.click(screen.getByText(/use: "total"/i));

    await user.click(
      screen.getByRole("button", { name: /delete 1 selected/i }),
    );
    const heading = screen.getByRole("heading", { name: /delete metrics/i });
    const dialog = heading.closest('[role="dialog"]') as HTMLElement;
    await user.click(
      within(dialog).getByRole("radio", { name: /metrics matching/i }),
    );
    await user.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    expect(mockBulkDeleteMutate).not.toHaveBeenCalled();
    expect(mockFetchNextPage).toHaveBeenCalled();

    mockMetrics = [
      makeMetric("total_orders"),
      makeMetric("other_metric"),
      makeMetric("total_refunds"),
    ];
    mockHasNextPage = false;
    rerender(<MetricList />);

    expect(mockBulkDeleteMutate).toHaveBeenCalledTimes(1);
    const deleted = mockBulkDeleteMutate.mock.calls[0][0] as {
      names: string[];
    };
    expect(deleted.names).toEqual(
      expect.arrayContaining(["total_orders", "total_refunds"]),
    );
    expect(deleted.names).not.toContain("other_metric");
  });

  it("recovers from a drain failure: cancels the pending export and shows an error", async () => {
    mockMetrics = [makeMetric("total_orders")];
    mockHasNextPage = true;
    mockFetchNextPage.mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );
    const user = userEvent.setup();
    render(<MetricList />, { wrapper });

    const filterInput = screen.getByPlaceholderText(/find metrics/i);
    await user.type(filterInput, "total");
    await user.click(screen.getByText(/use: "total"/i));

    await user.click(screen.getByRole("button", { name: /export osi/i }));
    const dialog = getExportDialog();
    await user.click(
      within(dialog).getByRole("radio", { name: /metrics matching/i }),
    );
    await user.click(within(dialog).getByRole("button", { name: /^export$/i }));

    // The rejection must cancel the pending op and surface an error —
    // NOT leave the button spinning forever.
    expect(
      await screen.findByText(/failed to load all metrics/i),
    ).toBeInTheDocument();
    expect(mockExportMutate).not.toHaveBeenCalled();
    const exportButton = within(dialog).getByRole("button", {
      name: /^export$/i,
    });
    expect(exportButton).not.toHaveAttribute("aria-disabled", "true");
  });

  it("does NOT execute a filtered export after the modal was dismissed mid-drain", async () => {
    mockMetrics = [makeMetric("total_orders")];
    mockHasNextPage = true;
    const user = userEvent.setup();
    const { rerender } = render(<MetricList />, { wrapper });

    const filterInput = screen.getByPlaceholderText(/find metrics/i);
    await user.type(filterInput, "total");
    await user.click(screen.getByText(/use: "total"/i));

    await user.click(screen.getByRole("button", { name: /export osi/i }));
    const dialog = getExportDialog();
    await user.click(
      within(dialog).getByRole("radio", { name: /metrics matching/i }),
    );
    await user.click(within(dialog).getByRole("button", { name: /^export$/i }));
    expect(mockExportMutate).not.toHaveBeenCalled(); // draining

    // User cancels while pages are still loading.
    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));

    // Drain completes afterwards — the cancelled export must NOT fire.
    mockMetrics = [makeMetric("total_orders"), makeMetric("total_refunds")];
    mockHasNextPage = false;
    rerender(<MetricList />);

    expect(mockExportMutate).not.toHaveBeenCalled();
  });
});
