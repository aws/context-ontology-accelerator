// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { AppMessage } from "../i18n/appMessages";

/**
 * Namespace languages the console offers at creation. The API accepts any tag
 * in the `language[-Script][-REGION]` subset, but the console only advertises
 * the languages exercised by product UAT (namespace-language LLD §2.2.2).
 */
export type NamespaceLanguage = "en" | "ja";

export const NAMESPACE_LANGUAGE_OPTIONS: ReadonlyArray<{
  value: NamespaceLanguage;
  label: AppMessage;
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

/**
 * `formatNamespaceLanguage` in the reader's locale: the offered languages go
 * through the app catalogue, any other tag is shown raw (it is not text the
 * console authored), and an absent field stays "—".
 */
export function localizeNamespaceLanguage(
  tag: string | undefined,
  t: (message: AppMessage) => string,
): string {
  const option = NAMESPACE_LANGUAGE_OPTIONS.find((o) => o.value === tag);
  return option ? t(option.label) : formatNamespaceLanguage(tag);
}
