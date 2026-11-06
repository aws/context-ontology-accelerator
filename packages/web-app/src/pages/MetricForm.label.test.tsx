// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * UpdateMetric is a full replace, so a save that leaves `label` out clears it.
 * The form loads the stored label into its Display label field and sends it
 * back as it is, and omits the field when it is blank: an empty string would
 * make every save of an unlabelled metric fail with 400, and clearing the
 * field is how a steward removes a label. The 256 limit is counted after NFC,
 * in code points, as the service counts it.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import { MetricForm } from "./MetricForm";

interface MockState {
  data?: { metric: Record<string, unknown> };
  update: Mock;
  create: Mock;
  sources: Array<Record<string, unknown>>;
  tables: Array<Record<string, unknown>>;
}

// `data` must keep its identity across renders: the form copies it into state
// in an effect keyed on it, so a fresh object per render would loop forever.
const mocks = vi.hoisted(
  (): MockState => ({
    data: undefined,
    update: vi.fn(),
    create: vi.fn(),
    sources: [],
    tables: [],
  }),
);

vi.mock("@api-hooks/use-metrics", () => ({
  useGetMetric: () => ({
    data: mocks.data,
    isLoading: false,
    error: null,
  }),
  useCreateMetric: () => ({ mutate: mocks.create, isPending: false }),
  useUpdateMetric: () => ({ mutate: mocks.update, isPending: false }),
}));

vi.mock("@api-hooks/use-list-sources", () => ({
  useListSources: () => ({
    data: { pages: [{ items: mocks.sources }] },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@api-hooks/use-list-source-tables", () => ({
  useListSourceTables: () => ({
    data: { items: mocks.tables },
    isFetching: false,
    error: null,
  }),
}));

vi.mock("@components/ApiClientProvider", () => ({
  useApiClient: () => ({ get: vi.fn() }),
}));

vi.mock("../services/ontology-engine", () => ({
  searchEntities: vi.fn().mockResolvedValue([]),
}));

function storedMetric(extra: Record<string, unknown> = {}) {
  return {
    name: "revenue_total",
    description: "Total revenue",
    dataSourceId: "ds-1",
    sourceTable: "orders",
    expression: {
      dialects: [{ dialect: "TRINO", expression: "SELECT SUM(x) FROM orders" }],
    },
    ...extra,
  };
}

function renderEditForm() {
  return render(
    <MemoryRouter
      initialEntries={["/namespaces/ns/metrics/revenue_total/edit"]}
    >
      <Routes>
        <Route
          path="/namespaces/:namespaceId/metrics/:metricName/edit"
          element={<MetricForm />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function savedBody(): Record<string, unknown> {
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  expect(mocks.update).toHaveBeenCalledTimes(1);
  return mocks.update.mock.calls[0][0];
}

describe("MetricForm label pass-through", () => {
  beforeEach(() => {
    mocks.update.mockReset();
    mocks.create.mockReset();
    mocks.data = undefined;
  });

  it("sends the existing label back unchanged on save", () => {
    mocks.data = { metric: storedMetric({ label: " 売上合計 \u200d" }) };
    renderEditForm();

    expect(savedBody().label).toBe(" 売上合計 \u200d");
  });

  it("omits label for a metric that has none", () => {
    mocks.data = { metric: storedMetric() };
    renderEditForm();

    expect(savedBody()).not.toHaveProperty("label");
  });

  it("omits label rather than sending an empty string", () => {
    mocks.data = { metric: storedMetric({ label: "" }) };
    renderEditForm();

    expect(savedBody()).not.toHaveProperty("label");
  });
});

function labelInput(): HTMLInputElement {
  return screen.getByPlaceholderText("Monthly revenue") as HTMLInputElement;
}

function typeLabel(value: string) {
  fireEvent.change(labelInput(), { target: { value } });
}

describe("MetricForm Display label field", () => {
  beforeEach(() => {
    mocks.update.mockReset();
    mocks.create.mockReset();
    mocks.data = undefined;
  });

  it("shows the stored label", () => {
    mocks.data = { metric: storedMetric({ label: "売上合計" }) };
    renderEditForm();

    expect(labelInput().value).toBe("売上合計");
  });

  it("sends an edited label as typed, without trimming it", () => {
    mocks.data = { metric: storedMetric({ label: "売上合計" }) };
    renderEditForm();

    typeLabel(" 月次売上 ");

    expect(savedBody().label).toBe(" 月次売上 ");
  });

  it("removes the label when the field is cleared", () => {
    mocks.data = { metric: storedMetric({ label: "売上合計" }) };
    renderEditForm();

    typeLabel("");

    expect(savedBody()).not.toHaveProperty("label");
  });

  it("treats a whitespace-only field as no label", () => {
    mocks.data = { metric: storedMetric({ label: "売上合計" }) };
    renderEditForm();

    typeLabel("   ");

    expect(savedBody()).not.toHaveProperty("label");
  });

  it("accepts 256 code points and refuses 257 without sending", () => {
    mocks.data = { metric: storedMetric() };
    renderEditForm();

    typeLabel("𠮷".repeat(256));
    expect(
      screen.queryByText("Display label must be 256 characters or fewer"),
    ).toBeNull();
    expect(savedBody().label).toBe("𠮷".repeat(256));

    mocks.update.mockReset();
    typeLabel("𠮷".repeat(257));
    const tooLong = "Display label must be 256 characters or fewer";
    // The field shows the error as soon as the text is too long, and Save
    // adds the form-level error, so each count pins one of the two.
    expect(screen.getAllByText(tooLong)).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(mocks.update).not.toHaveBeenCalled();
    expect(screen.getAllByText(tooLong)).toHaveLength(2);
  });

  it("counts after NFC, so a decomposed character counts once", () => {
    mocks.data = { metric: storedMetric() };
    renderEditForm();

    // "か" + U+3099 (combining voiced mark) composes to "が" under NFC.
    typeLabel("か\u3099".repeat(256));

    expect(screen.getByText(/^256\/256 characters/)).toBeTruthy();
    expect(savedBody().label).toBe("か\u3099".repeat(256));
  });
});

function renderCreateForm() {
  return render(
    <MemoryRouter initialEntries={["/namespaces/ns/metrics/create"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/metrics/:metricName"
          element={<MetricForm />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

// A Cloudscape Select opens on mousedown on its trigger, and its option
// commits on mouseup.
async function pick(placeholder: string, option: RegExp) {
  const trigger = screen.getByText(placeholder).closest("button");
  expect(trigger).not.toBeNull();
  fireEvent.mouseDown(trigger!);
  const choice = await screen.findByRole("option", { name: option });
  fireEvent.mouseUp(choice);
  fireEvent.click(choice);
}

async function fillRequiredCreateFields() {
  fireEvent.change(screen.getByPlaceholderText("monthly_revenue"), {
    target: { value: "revenue_total" },
  });
  fireEvent.change(
    screen.getByPlaceholderText(
      "Total revenue from all orders in a calendar month",
    ),
    { target: { value: "Total revenue" } },
  );
  await pick("Select a data source", /Orders DB/);
  await pick("Select a table", /^orders$/);
  fireEvent.change(screen.getByPlaceholderText("SUM(orders.total_amount)"), {
    target: { value: "SUM(orders.amount)" },
  });
}

describe("MetricForm Display label on create", () => {
  beforeEach(() => {
    mocks.update.mockReset();
    mocks.create.mockReset();
    mocks.data = undefined;
    mocks.sources = [
      { sourceId: "ds-1", name: "Orders DB", status: "APPROVED" },
    ];
    mocks.tables = [{ name: "orders" }];
  });

  afterEach(() => {
    mocks.sources = [];
    mocks.tables = [];
  });

  it("sends a label typed on the create form", async () => {
    renderCreateForm();
    await fillRequiredCreateFields();

    typeLabel("売上合計");
    fireEvent.click(screen.getByRole("button", { name: "Create metric" }));

    expect(mocks.create).toHaveBeenCalledTimes(1);
    expect(mocks.create.mock.calls[0][0]).toMatchObject({
      name: "revenue_total",
      dataSourceId: "ds-1",
      sourceTable: "orders",
      label: "売上合計",
    });
  });

  it("refuses a 257-code-point label without creating", async () => {
    renderCreateForm();
    await fillRequiredCreateFields();

    typeLabel("𠮷".repeat(257));
    fireEvent.click(screen.getByRole("button", { name: "Create metric" }));

    expect(mocks.create).not.toHaveBeenCalled();
    expect(
      screen.getAllByText("Display label must be 256 characters or fewer"),
    ).toHaveLength(2);
  });
});
