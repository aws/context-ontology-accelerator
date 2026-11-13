// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { columnDenylistRowsToMap } from "../../../src/utils/grant-overrides";

describe("columnDenylistRowsToMap", () => {
  it("converts rows into a wire map", () => {
    expect(
      columnDenylistRowsToMap([
        { table: "customers", columns: ["ssn", "credit_card"] },
        { table: "orders", columns: ["internal_cost"] },
      ]),
    ).toEqual({
      customers: ["ssn", "credit_card"],
      orders: ["internal_cost"],
    });
  });

  it("trims table names and columns", () => {
    expect(
      columnDenylistRowsToMap([
        { table: "  customers  ", columns: [" ssn ", " credit_card "] },
      ]),
    ).toEqual({ customers: ["ssn", "credit_card"] });
  });

  it("drops rows with a blank table name", () => {
    expect(
      columnDenylistRowsToMap([{ table: "  ", columns: ["ssn"] }]),
    ).toEqual({});
  });

  it("drops rows with no non-empty columns", () => {
    expect(
      columnDenylistRowsToMap([{ table: "customers", columns: ["", "  "] }]),
    ).toEqual({});
  });

  it("de-duplicates columns within a row", () => {
    expect(
      columnDenylistRowsToMap([
        { table: "customers", columns: ["ssn", "ssn", "credit_card"] },
      ]),
    ).toEqual({ customers: ["ssn", "credit_card"] });
  });

  it("last write wins for duplicate table names", () => {
    expect(
      columnDenylistRowsToMap([
        { table: "customers", columns: ["ssn"] },
        { table: "customers", columns: ["credit_card"] },
      ]),
    ).toEqual({ customers: ["credit_card"] });
  });
});
