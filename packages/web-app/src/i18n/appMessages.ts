// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { SupportedLocale } from "./LocaleProvider";
import { MESSAGES_JA } from "./messages.ja";

/**
 * Application-authored copy. Cloudscape's own strings live in its
 * I18nProvider bundle; this catalogue is only for text owned by COA.
 *
 * English text is the key on purpose. It keeps call sites readable, gives the
 * default locale a synchronous zero-copy path, and lets TypeScript reject a
 * message that has not been added to a non-default locale's catalogue.
 */

/**
 * The set of English message keys — the source of truth for what copy exists.
 * Because `en` renders its key verbatim, the catalogue keys ARE the canonical
 * English copy. Every non-default locale is typed `Record<AppMessage, string>`
 * (see `CATALOGUES`), so TypeScript forces each one to cover every key.
 */
export type AppMessage = keyof typeof MESSAGES_JA;
export type AppMessageValues = Readonly<Record<string, string | number>>;

/**
 * Per-locale translation tables, keyed by every locale except the default
 * `en` (which renders its key verbatim). Each table must supply every
 * `AppMessage`, so adding a language is a single file-add — a new
 * `messages.<locale>.ts` registered here — with no change to the formatter,
 * the types, or the parity test.
 */
export const CATALOGUES: Record<
  Exclude<SupportedLocale, "en">,
  Record<AppMessage, string>
> = {
  ja: MESSAGES_JA,
};

function interpolate(
  message: string,
  values: AppMessageValues | undefined,
): string {
  if (!values) return message;
  return message.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (token, name) =>
    Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name])
      : token,
  );
}

export function formatAppMessage(
  locale: SupportedLocale,
  message: AppMessage,
  values?: AppMessageValues,
): string {
  const template = locale === "en" ? message : CATALOGUES[locale][message];
  return interpolate(template, values);
}
