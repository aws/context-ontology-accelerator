// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Converts grant-override editor rows into the canonical wire shape. */

/**
 * A single editable column-denylist row, as used by the `ColumnDenylistEditor`
 * UI. Kept structurally compatible with the component's `ColumnDenylistRow`.
 */
export interface ColumnDenylistRowValue {
  table: string;
  columns: string[];
}

/**
 * Converts editor rows into the canonical wire map (`Record<string, string[]>`).
 *
 * The table name is trimmed and each column is trimmed; rows with a blank
 * table name or with no non-empty columns are dropped, and duplicate columns
 * within a row are removed (first occurrence wins). Duplicate table names
 * overwrite earlier entries (last-write-wins).
 */
export function columnDenylistRowsToMap(
  rows: ColumnDenylistRowValue[],
): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const { table, columns } of rows) {
    const trimmedTable = table.trim();
    if (!trimmedTable) continue;
    const cols = Array.from(
      new Set(columns.map((c) => c.trim()).filter(Boolean)),
    );
    if (cols.length) out[trimmedTable] = cols;
  }
  return out;
}
