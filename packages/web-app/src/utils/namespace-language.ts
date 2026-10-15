// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Namespace languages the console offers at creation. The API accepts any tag
 * in the `language[-Script][-REGION]` subset, but the console only advertises
 * the languages exercised by product UAT (namespace-language LLD §2.2.2).
 */
export type NamespaceLanguage = "en" | "ja";

export const NAMESPACE_LANGUAGE_OPTIONS: ReadonlyArray<{
  value: NamespaceLanguage;
  label: string;
}> = [
  { value: "en", label: "English (en)" },
  { value: "ja", label: "Japanese (ja)" },
];

/** Narrow a plain string (e.g. from RadioGroup) to an offered language. */
export const isNamespaceLanguage = (
  value: string,
): value is NamespaceLanguage =>
  NAMESPACE_LANGUAGE_OPTIONS.some((option) => option.value === value);

/**
 * Human label for a namespace language returned by the API: "English (en)"
 * and "Japanese (ja)" for the offered languages, the raw tag for any other
 * tag, and "—" when the field is absent.
 */
export function formatNamespaceLanguage(tag: string | undefined): string {
  if (!tag) return "—";
  const option = NAMESPACE_LANGUAGE_OPTIONS.find((o) => o.value === tag);
  return option ? option.label : tag;
}
