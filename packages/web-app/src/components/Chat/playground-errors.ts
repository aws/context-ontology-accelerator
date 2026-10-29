// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { AppMessage } from "../../i18n/appMessages";
import type { AppTranslate, SupportedLocale } from "../../i18n/LocaleProvider";

/**
 * Catalogue message per Playground error type. The serve layer sends a stable
 * `error` type with every streaming error event, and its `message` text is
 * English; the web layer adds its own types for transport failures. The key is
 * the English summary, so for the fixed-text types it is the very sentence the
 * server sends.
 */
export const PLAYGROUND_ERROR_MESSAGES: Readonly<Record<string, AppMessage>> = {
  AccessDenied: "Access denied",
  AccessDeniedError: "Access denied",
  TimeoutError: "Query resolution timed out",
  InternalError: "Query resolution failed",
  NoResult: "No result for this question",
  NoResultError: "No result for this question",
  QueryTranslationError: "The question could not be turned into a query",
  AmbiguousReferenceError:
    "A table in the question matches more than one source",
  DataSourceUnavailableError: "A data source is unavailable",
  NamespaceNotFoundError: "Namespace not found",
  InvalidResponse: "Received malformed response from server",
  AuthError: "Your session has expired. Sign in again.",
  StreamError: "The request failed",
};

/**
 * Outcomes that repeat exactly on a retry (a permission deny, a deterministic
 * miss or refusal), so the bubble offers no Retry button for them.
 */
const NOT_RETRYABLE = new Set<string>([
  "AccessDenied",
  "AccessDeniedError",
  "AmbiguousReferenceError",
  "NamespaceNotFoundError",
  "NoResult",
  "NoResultError",
]);

export interface PlaygroundErrorText {
  /** The headline shown in the reader's locale. */
  summary: string;
  /** The server's own sentence, shown under the summary when it adds detail. */
  detail?: string;
  retryable: boolean;
}

/**
 * Turn a Playground error into display text.
 *
 * A known type gets its catalogue message as the summary. The server's text is
 * kept as secondary detail when it says more than the summary (for example the
 * table names in an ambiguous-reference refusal). In English the server text is
 * already readable, so it stays the headline, which leaves English unchanged.
 * An unknown type falls back to the server text.
 */
export function describePlaygroundError(
  errorType: string | undefined,
  serverMessage: string | undefined,
  locale: SupportedLocale,
  t: AppTranslate,
): PlaygroundErrorText {
  const server = serverMessage?.trim() ?? "";
  const message =
    errorType &&
    Object.prototype.hasOwnProperty.call(PLAYGROUND_ERROR_MESSAGES, errorType)
      ? PLAYGROUND_ERROR_MESSAGES[errorType]
      : undefined;
  const retryable = !(errorType && NOT_RETRYABLE.has(errorType));
  if (!message) {
    return { summary: server || t("The request failed"), retryable };
  }
  if (locale === "en" && server) {
    return { summary: server, retryable };
  }
  const summary = t(message);
  const detail = server && server !== message ? server : undefined;
  return { summary, detail, retryable };
}
