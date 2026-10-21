// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import {
  I18nProvider,
  importMessages,
  type I18nProviderProps,
} from "@cloudscape-design/components/i18n";
import enMessages from "@cloudscape-design/components/i18n/messages/all.en";
import {
  formatAppMessage,
  type AppMessage,
  type AppMessageValues,
} from "./appMessages";

const LOCALE_KEY = "scl.locale";

export type SupportedLocale = "en" | "ja";
export const SUPPORTED_LOCALES: ReadonlyArray<SupportedLocale> = ["en", "ja"];
const SUPPORTED_LOCALE_SET: ReadonlySet<string> = new Set(SUPPORTED_LOCALES);

// The default locale's bundle is imported statically (~45 KB uncompressed, a
// few KB gzipped against a ~2 MB app) so the default path is synchronous: no
// frame in which pagination, filter and upload strings render empty while a
// chunk loads. Other locales load on demand via importMessages.
const DEFAULT_MESSAGES: ReadonlyArray<I18nProviderProps.Messages> = [
  enMessages,
];

/** Self-named labels for the language switcher — deliberately not translated. */
export const LOCALE_LABELS: Record<SupportedLocale, string> = {
  en: "English",
  ja: "日本語",
};

export function isSupported(value: string | null): value is SupportedLocale {
  return value !== null && SUPPORTED_LOCALE_SET.has(value);
}

/**
 * Resolve the initial UI locale: an explicit stored preference wins, then the
 * browser language (primary subtag), then English. Mirrors how the dark-mode
 * and density preferences are read in App.tsx.
 */
export function readLocale(): SupportedLocale {
  if (typeof window === "undefined") return "en";
  try {
    const stored = window.localStorage.getItem(LOCALE_KEY);
    if (isSupported(stored)) return stored;
  } catch {
    // fall through to browser language
  }
  const browser = (navigator.language ?? "").split("-")[0];
  return isSupported(browser) ? browser : "en";
}

interface LocaleContextValue {
  locale: SupportedLocale;
  setLocale: (locale: SupportedLocale) => void;
}

export type AppTranslate = (
  message: AppMessage,
  values?: AppMessageValues,
) => string;

const LocaleContext = createContext<LocaleContextValue>({
  locale: "en",
  setLocale: () => {},
});

interface ActiveLocale {
  locale: SupportedLocale;
  messages: ReadonlyArray<I18nProviderProps.Messages>;
}

const DEFAULT_ACTIVE_LOCALE: ActiveLocale = {
  locale: "en",
  messages: DEFAULT_MESSAGES,
};

function persistLocale(locale: SupportedLocale): void {
  try {
    window.localStorage.setItem(LOCALE_KEY, locale);
  } catch {
    // preference just won't persist
  }
}

export function useLocale(): LocaleContextValue {
  return useContext(LocaleContext);
}

export function useAppI18n(): {
  locale: SupportedLocale;
  t: AppTranslate;
} {
  const { locale } = useLocale();
  const t = useCallback(
    (message: AppMessage, values?: AppMessageValues) =>
      formatAppMessage(locale, message, values),
    [locale],
  );
  return { locale, t };
}

/**
 * Owns the UI locale preference and provides Cloudscape's built-in component
 * strings (pagination, property filter, file upload, wizard, split panel, …)
 * in that locale via Cloudscape's I18nProvider. English is available
 * synchronously. Another locale's bundle is loaded on demand and activated
 * atomically; until it resolves, components keep the previous locale and its
 * strings. A failed load also keeps the previous locale usable.
 *
 * The I18nProvider is always mounted. Mounting it only once messages arrive
 * would change the element type at the root of the subtree and remount the
 * whole app (router, auth, query client) on first load.
 */
export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [requestedLocale, setRequestedLocale] =
    useState<SupportedLocale>(readLocale);
  const [activeLocale, setActiveLocale] = useState<ActiveLocale>(
    DEFAULT_ACTIVE_LOCALE,
  );

  useEffect(() => {
    if (requestedLocale === activeLocale.locale) {
      return;
    }

    if (requestedLocale === "en") {
      setActiveLocale(DEFAULT_ACTIVE_LOCALE);
      persistLocale("en");
      return;
    }

    let cancelled = false;
    importMessages(requestedLocale).then(
      (loaded) => {
        if (cancelled) return;
        if (loaded.length === 0) {
          console.error(
            `Failed to load Cloudscape messages for ${requestedLocale}: bundle was empty`,
          );
          persistLocale(activeLocale.locale);
          setRequestedLocale(activeLocale.locale);
          return;
        }
        setActiveLocale({ locale: requestedLocale, messages: loaded });
        persistLocale(requestedLocale);
      },
      (err) => {
        if (cancelled) return;
        console.error(
          `Failed to load Cloudscape messages for ${requestedLocale}:`,
          err,
        );
        persistLocale(activeLocale.locale);
        setRequestedLocale(activeLocale.locale);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [activeLocale.locale, requestedLocale]);

  useEffect(() => {
    document.documentElement.lang = activeLocale.locale;
  }, [activeLocale.locale]);

  return (
    <LocaleContext.Provider
      value={{ locale: activeLocale.locale, setLocale: setRequestedLocale }}
    >
      <I18nProvider
        locale={activeLocale.locale}
        messages={activeLocale.messages}
      >
        {children}
      </I18nProvider>
    </LocaleContext.Provider>
  );
}
