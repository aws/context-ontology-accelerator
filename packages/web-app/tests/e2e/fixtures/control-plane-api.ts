// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Control-plane API teardown.
 *
 * Token is read fresh from the live page (the OIDC provider renews it there; a
 * token from persisted storageState goes stale on long runs). Cleanup calls run
 * from Node — no CORS, far more robust than fetch loops in `page.evaluate`.
 *
 * Cleanup is a strict state machine that enforces delete order and never
 * archives over a live source:
 *   DELETING                               → wait
 *   sources remain (confirmed list)        → reactivate if needed, then delete
 *   sources == 0 & ACTIVE                  → archive
 *   sources == 0 & ARCHIVED/DELETE_FAILED  → delete
 *   DELETED / gone (404)                   → done
 */
import type { Page } from "@playwright/test";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Read the app's current (auto-renewed) id token from the live page. */
async function readLiveToken(page: Page): Promise<string | null> {
  try {
    const hasToken = await page.evaluate(() =>
      Object.keys(localStorage).some((k) => k.startsWith("oidc.user:")),
    );
    if (!hasToken) {
      await page.goto("/").catch(() => undefined);
      await page
        .waitForFunction(
          () =>
            Object.keys(localStorage).some((k) => k.startsWith("oidc.user:")),
          undefined,
          { timeout: 15_000 },
        )
        .catch(() => undefined);
    }
    return await page.evaluate(() => {
      const k = Object.keys(localStorage).find((key) =>
        key.startsWith("oidc.user:"),
      );
      if (!k) return null;
      return JSON.parse(localStorage.getItem(k) ?? "{}").id_token ?? null;
    });
  } catch {
    return null;
  }
}

let cachedEndpoint: string | null | undefined;
async function getApiEndpoint(baseURL: string): Promise<string | null> {
  if (cachedEndpoint !== undefined) return cachedEndpoint;
  try {
    const res = await fetch(
      `${baseURL.replace(/\/$/, "")}/runtime-config.json`,
    );
    const cfg: { apiEndpoint?: string } = await res.json();
    cachedEndpoint = (cfg.apiEndpoint ?? "").replace(/\/$/, "") || null;
  } catch {
    cachedEndpoint = null;
  }
  return cachedEndpoint;
}

interface Api {
  base: string;
  headers: Record<string, string>;
}

/** GET-by-id status, or "DELETED" on 404, or null when unreadable. */
async function getStatus(api: Api, id: string): Promise<string | null> {
  const res = await fetch(`${api.base}/namespaces/${id}`, {
    headers: api.headers,
  });
  if (res.status === 404) return "DELETED";
  if (!res.ok) return null;
  const j = await res.json();
  return j.namespace?.status ?? j.status ?? null;
}

/** Source ids, or null when the list could not be read. */
async function listSourceIds(api: Api, id: string): Promise<string[] | null> {
  const res = await fetch(`${api.base}/namespaces/${id}/sources`, {
    headers: api.headers,
  });
  if (!res.ok) return null;
  const b = await res.json();
  return (b.items ?? [])
    .map((s: { sourceId?: string }) => s.sourceId ?? "")
    .filter(Boolean);
}

async function forceDeleteOne(api: Api, id: string): Promise<void> {
  const deadline = Date.now() + 6 * 60_000;
  while (Date.now() < deadline) {
    const status = await getStatus(api, id);
    if (status === "DELETED" || status === null) return;

    if (status === "DELETING") {
      await sleep(8_000);
      continue;
    }

    // Only a *successful* list is trusted (a failed list must not read as
    // "sources remain" and reactivate a mid-delete namespace).
    const sources = await listSourceIds(api, id);

    if (sources && sources.length > 0) {
      if (status !== "ACTIVE") {
        await fetch(`${api.base}/namespaces/${id}/status`, {
          method: "PATCH",
          headers: api.headers,
          body: JSON.stringify({ status: "ACTIVE" }),
        }).catch(() => undefined);
      } else {
        for (const sid of sources) {
          await fetch(`${api.base}/namespaces/${id}/sources/${sid}`, {
            method: "DELETE",
            headers: api.headers,
          }).catch(() => undefined);
        }
      }
      await sleep(8_000);
      continue;
    }

    if (status === "ACTIVE") {
      // Archive only once a successful list confirms no sources.
      if (sources === null) {
        await sleep(8_000);
        continue;
      }
      await fetch(`${api.base}/namespaces/${id}/status`, {
        method: "PATCH",
        headers: api.headers,
        body: JSON.stringify({ status: "ARCHIVED" }),
      }).catch(() => undefined);
    } else {
      await fetch(`${api.base}/namespaces/${id}`, {
        method: "DELETE",
        headers: api.headers,
      }).catch(() => undefined);
    }
    await sleep(8_000);
  }
}

/**
 * Best-effort force-delete of namespaces by display name. Safe with names that
 * no longer exist (no-op). Never throws — teardown must not fail the test.
 */
export async function forceDeleteNamespaces(
  page: Page,
  names: string[],
  knownIds: string[] = [],
): Promise<void> {
  if (names.length === 0 && knownIds.length === 0) return;
  const baseURL = process.env.E2E_BASE_URL;
  if (!baseURL) return;
  try {
    const token = await readLiveToken(page);
    if (!token) return;
    const base = await getApiEndpoint(baseURL);
    if (!base) return;
    const api: Api = {
      base,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
    };

    // Start from known ids (reliable even when a namespace is mid-delete and
    // omitted from the list).
    const ids = new Set<string>(knownIds);

    if (names.length > 0) {
      const wanted = new Set(names);
      let nextToken: string | undefined;
      do {
        const u = new URL(`${base}/namespaces`);
        u.searchParams.set("maxResults", "100");
        if (nextToken) u.searchParams.set("nextToken", nextToken);
        const res = await fetch(u, { headers: api.headers });
        if (!res.ok) break;
        const b = await res.json();
        for (const n of b.namespaces ?? b.items ?? []) {
          const nm = n.displayName ?? n.name ?? "";
          if (wanted.has(nm) && n.namespaceId) ids.add(n.namespaceId);
        }
        nextToken = b.nextToken;
      } while (nextToken);
    }

    for (const id of ids) {
      await forceDeleteOne(api, id).catch(() => undefined);
    }
  } catch {
    // Best-effort: teardown must never fail the test.
  }
}
