// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import {
  useBreadcrumbs,
  type BreadcrumbItem,
} from "@components/BreadcrumbProvider";
import { useNamespace } from "./use-namespace";

/**
 * Declarative description of one crumb a page wants to publish.
 *
 * A page describes *where it is*; this hook turns that into the final
 * `BreadcrumbItem[]`, prepending the `COA` root, resolving `href` into a
 * `navigate` click handler, and security-trimming namespace up-links.
 */
export interface TrailCrumb {
  /** Visible crumb label. */
  text: string;
  /**
   * Target path. When present the crumb becomes a navigable link; when
   * omitted the crumb is plain text (use for the current page — the last
   * crumb — and for non-navigable grouping labels like "Ontology").
   */
  href?: string;
  /**
   * Namespace this crumb's link belongs to. When set, the link is only kept
   * if the current user has access to that namespace; otherwise the crumb
   * falls back to plain text (security trim). Omit for namespace-agnostic
   * crumbs (e.g. "Namespaces", "Profile").
   */
  namespaceId?: string;
}

/**
 * Publish a context-aware, grant-trimmed breadcrumb trail for the current page.
 *
 * Every trail is implicitly rooted at a clickable `COA` crumb that navigates
 * home, so callers pass only the crumbs below the root. The trail is set on
 * mount / whenever it changes and cleared on unmount, mirroring the
 * `setBreadcrumbs(...) ... return () => clearBreadcrumbs()` pattern the
 * ontology pages established.
 *
 * Security trimming: any crumb carrying a `namespaceId` the current user
 * cannot open (per `GetNamespace(id)`) is rendered as plain text instead of
 * a link, so the breadcrumb never offers navigation into a namespace the
 * server would deny. This is a UX signal derived from that same
 * authorization check, not a separate client-side re-derivation of it.
 *
 * Pass `undefined` to intentionally publish nothing (fall back to the shell's
 * route-default breadcrumb) without violating the rules-of-hooks.
 */
export function useBreadcrumbTrail(trail: TrailCrumb[] | undefined): void {
  const navigate = useNavigate();
  const { setBreadcrumbs, clearBreadcrumbs } = useBreadcrumbs();

  // A trail references at most one namespace (every page is scoped to a
  // single route-level :namespaceId), so one GetNamespace(id) call per trail
  // answers the access check for every crumb in it. Dev-mode check makes that
  // guarantee explicit rather than unchecked folklore: a future trail mixing
  // two different namespace ids would otherwise silently gate some crumbs
  // against the wrong namespace's access result.
  const namespaceIds = trail
    ?.map((c) => c.namespaceId)
    .filter((id): id is string => id !== undefined);
  if (
    process.env.NODE_ENV !== "production" &&
    namespaceIds &&
    new Set(namespaceIds).size > 1
  ) {
    console.error(
      "useBreadcrumbTrail: trail references more than one distinct " +
        `namespaceId (${[...new Set(namespaceIds)].join(", ")}); only the ` +
        "first is used for access trimming, so crumbs for the others may " +
        "be gated incorrectly.",
    );
  }
  const trailNamespaceId = namespaceIds?.[0];
  const { canAccess: canAccessNamespace } = useNamespace(trailNamespaceId);

  // Serialize the trail so the effect only re-runs on a real change, not on
  // every render's fresh array identity.
  const trailKey = JSON.stringify(trail ?? null);

  const items = useMemo<BreadcrumbItem[] | null>(() => {
    if (!trail) return null;
    const root: BreadcrumbItem = { text: "COA", onClick: () => navigate("/") };
    const rest = trail.map((c) => {
      const linkable =
        c.href !== undefined &&
        (c.namespaceId === undefined || canAccessNamespace);
      const href = c.href;
      return linkable && href !== undefined
        ? { text: c.text, onClick: () => navigate(href) }
        : { text: c.text };
    });
    return [root, ...rest];
    // trailKey captures the trail's content, and navigate is a stable
    // identity. canAccessNamespace is included because it starts false while
    // GetNamespace is loading and flips once it resolves — a namespace crumb
    // trimmed on first render must re-link as soon as access is confirmed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trailKey, navigate, canAccessNamespace]);

  useEffect(() => {
    if (!items) return;
    setBreadcrumbs(items);
    return () => clearBreadcrumbs();
  }, [items, setBreadcrumbs, clearBreadcrumbs]);
}
