// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Display helpers for ontology records, shared by the Ontology page, the
 * Explorer inventory and the induced-ontology detail page.
 *
 * These lived in `OntologyPage.tsx`; importing them from there pulled that
 * whole module — and its Cloudscape tree — into every consumer's chunk.
 */

import type { AppMessage } from "../i18n/appMessages";

const ONTOLOGY_TYPE_DISPLAY: Record<
  string,
  { label: string; color: "blue" | "green" | "grey" }
> = {
  induced: { label: "Induced", color: "grey" },
  foundational: { label: "Foundational", color: "green" },
  user_created: { label: "Uploaded", color: "blue" },
  user_uploaded: { label: "Uploaded", color: "blue" },
};

export function ontologyTypeDisplay(type?: string | null): {
  label: string;
  color: "blue" | "green" | "grey";
} {
  if (!type) return { label: "Unknown", color: "grey" };
  return ONTOLOGY_TYPE_DISPLAY[type] ?? { label: type, color: "grey" };
}

/** Catalogue message for each label `ontologyTypeDisplay` can return. */
export const ONTOLOGY_TYPE_MESSAGES: Readonly<Record<string, AppMessage>> = {
  Induced: "Induced",
  Foundational: "Foundational",
  Uploaded: "Uploaded",
  Unknown: "Unknown",
};

/**
 * The ontology type label in the reader's locale. A type this console does not
 * know is shown raw, as `ontologyTypeDisplay` does.
 */
export function localizeOntologyType(
  type: string | null | undefined,
  t: (message: AppMessage) => string,
): string {
  const { label } = ontologyTypeDisplay(type);
  const message = Object.prototype.hasOwnProperty.call(
    ONTOLOGY_TYPE_MESSAGES,
    label,
  )
    ? ONTOLOGY_TYPE_MESSAGES[label]
    : undefined;
  return message ? t(message) : label;
}

/** Which segment of the type filter a given ontology_type belongs to. */
export function ontologyTypeGroup(
  type?: string | null,
): "induced" | "foundational" | "uploaded" | "other" {
  if (type === "induced") return "induced";
  if (type === "foundational") return "foundational";
  if (type === "user_created" || type === "user_uploaded") return "uploaded";
  return "other";
}

/** Copy for the delete-confirm modal, keyed on ontology type.
 *
 *  Foundational ontologies are curated references that can be re-loaded from the
 *  catalog, so their removal is framed as reversible ("remove"). User-uploaded
 *  and induced ontologies are the user's own content — permanent once deleted.
 *  Exported as a pure function so the type-aware wording is unit-tested directly
 *  (driving the Cloudscape Modal open in jsdom is flaky). The matching body copy
 *  lives in `OntologyDeleteWarning`, which renders this header. */
export function deleteOntologyCopy(ontologyType: string): {
  header: string;
  reloadable: boolean;
} {
  if (ontologyTypeGroup(ontologyType) === "foundational") {
    return { header: "Remove this foundational ontology?", reloadable: true };
  }
  return { header: "This action cannot be undone", reloadable: false };
}
