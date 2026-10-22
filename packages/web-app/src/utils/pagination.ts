// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Upper bound on pages the list-everything helpers will follow. Far
 * beyond any real namespace; it only stops a misbehaving cursor from looping
 * the browser forever.
 */
export const LIST_ALL_MAX_PAGES = 100;

/**
 * Collect every item of a paginated list by following ``nextToken``.
 *
 * A page may be short or even empty while still carrying a ``nextToken`` (the
 * server filters after reading a bounded slice), so the loop stops only when the
 * token is absent, not on an empty page. Stops early, with a warning, if the
 * server repeats a token or after {@link LIST_ALL_MAX_PAGES} pages.
 */
export async function collectAllPages<T>(
  what: string,
  fetchPage: (
    nextToken: string | undefined,
  ) => Promise<{ items: T[]; nextToken?: string }>,
): Promise<T[]> {
  const all: T[] = [];
  const seen = new Set<string>();
  let nextToken: string | undefined;
  for (let i = 0; i < LIST_ALL_MAX_PAGES; i++) {
    const page = await fetchPage(nextToken);
    all.push(...page.items);
    nextToken = page.nextToken || undefined;
    if (!nextToken) return all;
    if (seen.has(nextToken)) {
      console.warn(`${what} repeated a page token; stopping`);
      return all;
    }
    seen.add(nextToken);
  }
  console.warn(`${what} page limit reached; the list may be incomplete`, {
    maxPages: LIST_ALL_MAX_PAGES,
  });
  return all;
}
