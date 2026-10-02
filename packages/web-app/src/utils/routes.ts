// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Build the table-detail route, encoding every dynamic segment exactly once.
 *
 * namespaceId and sourceId are server-generated UUIDs today, so encoding
 * them is a no-op in practice, but tableId is user-supplied (a catalog
 * table name) and can contain reserved characters (#, %, spaces) or
 * non-ASCII text that would otherwise corrupt the route.
 */
export function tableDetailPath(
  namespaceId: string,
  sourceId: string,
  tableId: string,
): string {
  return (
    `/namespaces/${encodeURIComponent(namespaceId)}` +
    `/sources/${encodeURIComponent(sourceId)}` +
    `/tables/${encodeURIComponent(tableId)}`
  );
}
