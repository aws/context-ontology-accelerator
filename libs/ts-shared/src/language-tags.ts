// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Namespace-language tag canonicalizer — TypeScript twin of
 * `libs/common/src/coa_common/language_tags.py`.
 *
 * Accepts the BCP 47 subset `language[-script][-region]` (language 2-8
 * letters, script 4 letters, region 2 letters or 3 digits). Both
 * implementations run the vectors in
 * `libs/common/tests/data/language_tag_conformance.json`; change them together.
 */

/** Effective language of a namespace created without one (including legacy namespaces). */
export const DEFAULT_LANGUAGE = "en";

// Explicit ASCII classes and no `m` flag, so `$` is end of input only.
const LANGUAGE_TAG_PATTERN =
  /^([A-Za-z]{2,8})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?$/;
const SURROUNDING_SPACE_OR_TAB = /^[ \t]+|[ \t]+$/g;

/** Thrown when a value is not a language tag in the supported subset. */
export class LanguageTagError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LanguageTagError";
  }
}

/**
 * Return the canonical form of a supported language tag, e.g. `pt-BR` for
 * `" PT-br "`. Only surrounding ASCII spaces and tabs are trimmed.
 *
 * @throws LanguageTagError when the value is empty or outside the grammar.
 */
export function canonicalizeLanguageTag(value: string): string {
  const tag = value.replace(SURROUNDING_SPACE_OR_TAB, "");
  if (tag === "") {
    throw new LanguageTagError("language tag must not be empty");
  }
  const match = LANGUAGE_TAG_PATTERN.exec(tag);
  if (match === null) {
    throw new LanguageTagError(
      "language tag must have the form language[-Script][-REGION], e.g. 'en', 'ja', 'pt-BR'",
    );
  }
  const [, language, script, region] = match;
  const parts = [language.toLowerCase()];
  if (script) {
    parts.push(script.charAt(0).toUpperCase() + script.slice(1).toLowerCase());
  }
  if (region) {
    parts.push(region.toUpperCase());
  }
  return parts.join("-");
}

/**
 * Return the canonical primary language subtag of a supported tag.
 *
 * @throws LanguageTagError when the value is not a supported language tag.
 */
export function primaryLanguage(value: string): string {
  return canonicalizeLanguageTag(value).split("-", 1)[0];
}
