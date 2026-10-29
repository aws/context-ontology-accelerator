// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Free-text match for a Cloudscape `useCollection` `filteringFunction`.
 *
 * Keeps Cloudscape's default behavior (case-insensitive substring match on
 * any primitive field of the item, so searching for a raw value such as
 * `ACTIVE` still works) and also matches the texts the table shows for the
 * row, such as a status or type translated into the reader's language.
 * Without the second part a user cannot find a row by the word in the cell.
 */
export function matchesFilteringText(
  item: object,
  filteringText: string,
  shownTexts: ReadonlyArray<string | number | undefined>,
): boolean {
  if (filteringText.length === 0) return true;
  const needle = filteringText.toLowerCase();
  const contains = (value: unknown): boolean =>
    String(value).toLowerCase().includes(needle);
  const fieldValues: unknown[] = Object.values(item);
  if (
    fieldValues.some(
      (value) => !(value && typeof value === "object") && contains(value),
    )
  ) {
    return true;
  }
  return shownTexts.some((text) => text !== undefined && contains(text));
}
