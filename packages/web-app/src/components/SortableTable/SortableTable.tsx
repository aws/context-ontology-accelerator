// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Cloudscape ``<Table>`` with client-side column sorting.
 *
 * Not ``useCollection``: Cloudscape tracks the active sort by the
 * ``sortingComparator`` function instance, and inline column definitions make a
 * new instance every render, so sorting silently stops. This tracks the active
 * column by its stable ``id`` and sorts itself.
 */
import { useMemo, useState } from "react";
import Table, { type TableProps } from "@cloudscape-design/components/table";

export interface SortableTableProps<T> extends Omit<
  TableProps<T>,
  "sortingColumn" | "sortingDescending" | "onSortingChange"
> {
  /** ``id`` of the column to sort by on first render (default: source order). */
  defaultSortingColumnId?: string;
  /** Whether the initial sort is descending (default false = ascending). */
  defaultSortingDescending?: boolean;
}

/** Type guard: narrows `unknown` to a string-indexable record (no `as` cast). */
function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

/** Cloudscape's ``sortingField`` is a plain string, not ``keyof T``. */
function readField(row: unknown, field: string): unknown {
  return isRecord(row) ? row[field] : undefined;
}

export function SortableTable<T>({
  items,
  columnDefinitions,
  defaultSortingColumnId,
  defaultSortingDescending,
  ...tableProps
}: SortableTableProps<T>): React.JSX.Element {
  const [sortingColumnId, setSortingColumnId] = useState<string | undefined>(
    defaultSortingColumnId,
  );
  const [isDescending, setIsDescending] = useState<boolean>(
    defaultSortingDescending ?? false,
  );

  const columns = columnDefinitions ?? [];
  const activeColumn = sortingColumnId
    ? columns.find((c) => c.id === sortingColumnId)
    : undefined;

  const sortedItems = useMemo(() => {
    const list = items ?? [];
    if (!activeColumn) return list;

    const { sortingComparator, sortingField } = activeColumn;
    if (!sortingComparator && !sortingField) return list;

    const cmp = (a: T, b: T): number => {
      if (sortingComparator) return sortingComparator(a, b);
      const av = readField(a, sortingField ?? "");
      const bv = readField(b, sortingField ?? "");
      if (typeof av === "number" && typeof bv === "number") return av - bv;
      return String(av ?? "").localeCompare(String(bv ?? ""));
    };

    // Copy before sort — never mutate the caller's array. A caller-provided
    // comparator that throws (e.g. on incomplete/malformed row data) must not
    // crash the render tree; fall back to the unsorted list on error.
    try {
      const out = [...list].sort(cmp);
      return isDescending ? out.reverse() : out;
    } catch (error) {
      console.error(
        "SortableTable: sorting comparator threw; showing unsorted",
        error,
      );
      return list;
    }
  }, [items, activeColumn, isDescending]);

  return (
    <Table
      {...tableProps}
      columnDefinitions={columns}
      items={sortedItems}
      sortingColumn={activeColumn}
      sortingDescending={isDescending}
      onSortingChange={({ detail }) => {
        // detail.sortingColumn is the column-definition object; resolve it back
        // to its stable id by reference identity (SortingColumn has no `id`).
        const clicked = columns.find((c) => c === detail.sortingColumn);
        setSortingColumnId(clicked?.id);
        setIsDescending(detail.isDescending ?? false);
      }}
    />
  );
}
