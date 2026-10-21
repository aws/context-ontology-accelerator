// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import React from "react";
import Pagination from "@cloudscape-design/components/pagination";
import {
  LocaleProvider,
  useAppI18n,
  useLocale,
  readLocale,
} from "./LocaleProvider";

const LOCALE_KEY = "scl.locale";

function Probe() {
  const { locale, setLocale } = useLocale();
  const { t } = useAppI18n();
  return (
    <div>
      <span data-testid="locale">{locale}</span>
      <span data-testid="app-copy">{t("Sources")}</span>
      <button onClick={() => setLocale("ja")}>to-ja</button>
      {/* A Cloudscape component whose aria strings come from the provider. */}
      <Pagination currentPageIndex={1} pagesCount={3} />
    </div>
  );
}

describe("LocaleProvider", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    document.documentElement.lang = "";
    vi.restoreAllMocks();
  });

  describe("readLocale", () => {
    it("prefers a stored preference over the browser language", () => {
      window.localStorage.setItem(LOCALE_KEY, "ja");
      expect(readLocale()).toBe("ja");
    });

    it("falls back to the browser language's primary subtag", () => {
      vi.spyOn(navigator, "language", "get").mockReturnValue("ja-JP");
      expect(readLocale()).toBe("ja");
    });

    it("defaults to en for unsupported browser languages", () => {
      vi.spyOn(navigator, "language", "get").mockReturnValue("fr-FR");
      expect(readLocale()).toBe("en");
    });

    it("ignores an unsupported stored value", () => {
      window.localStorage.setItem(LOCALE_KEY, "klingon");
      vi.spyOn(navigator, "language", "get").mockReturnValue("en-US");
      expect(readLocale()).toBe("en");
    });
  });

  it("persists locale changes and sets the document language", async () => {
    render(
      <LocaleProvider>
        <Probe />
      </LocaleProvider>,
    );
    expect(screen.getByTestId("locale")).toHaveTextContent("en");
    expect(screen.getByTestId("app-copy")).toHaveTextContent("Sources");

    act(() => {
      screen.getByText("to-ja").click();
    });

    await waitFor(() => {
      expect(screen.getByTestId("locale")).toHaveTextContent("ja");
      expect(screen.getByTestId("app-copy")).toHaveTextContent("データソース");
      expect(window.localStorage.getItem(LOCALE_KEY)).toBe("ja");
      expect(document.documentElement.lang).toBe("ja");
    });
  });

  it("keeps working when the locale preference cannot be persisted", async () => {
    const setItem = vi
      .spyOn(window.localStorage, "setItem")
      .mockImplementation(() => {
        throw new Error("storage disabled");
      });
    render(
      <LocaleProvider>
        <Probe />
      </LocaleProvider>,
    );

    act(() => {
      screen.getByText("to-ja").click();
    });

    await waitFor(() => {
      expect(screen.getByTestId("locale")).toHaveTextContent("ja");
      expect(document.documentElement.lang).toBe("ja");
      expect(screen.getByLabelText("次のページ")).toBeInTheDocument();
    });
    expect(setItem).toHaveBeenCalledWith(LOCALE_KEY, "ja");
    // Restore this spy here rather than leaving it to `vi.restoreAllMocks()`.
    // happy-dom exposes `window.localStorage` through a Proxy, and vitest 4's
    // bulk restore does not put back a spied property on one — the throwing
    // `setItem` stayed installed and the two later tests that seed a stored
    // locale failed with this test's "storage disabled" error. `mockRestore()`
    // on the spy itself does work.
    setItem.mockRestore();
  });

  it("serves Cloudscape component strings in the selected locale", async () => {
    window.localStorage.setItem(LOCALE_KEY, "ja");
    render(
      <LocaleProvider>
        <Probe />
      </LocaleProvider>,
    );
    // Message bundles load asynchronously; the ja pagination aria label
    // appears once the provider has them.
    await waitFor(() => {
      expect(screen.getByLabelText("次のページ")).toBeInTheDocument();
    });
  });

  it("serves English component strings synchronously on first render", () => {
    // No waitFor: the default bundle is statically imported, so there is no
    // frame in which the strings are empty while a chunk loads.
    render(
      <LocaleProvider>
        <Probe />
      </LocaleProvider>,
    );
    expect(screen.getByLabelText("Next page")).toBeInTheDocument();
  });

  it("does not remount the subtree when the locale bundle arrives", async () => {
    // A child with local state: if the provider changed the element type at
    // the subtree root once messages loaded, this state would reset to 0.
    function Counter() {
      const [n, setN] = React.useState(0);
      return (
        <button data-testid="counter" onClick={() => setN((v) => v + 1)}>
          {n}
        </button>
      );
    }
    window.localStorage.setItem(LOCALE_KEY, "ja");
    render(
      <LocaleProvider>
        <Counter />
        <Probe />
      </LocaleProvider>,
    );
    act(() => {
      screen.getByTestId("counter").click();
    });
    expect(screen.getByTestId("counter")).toHaveTextContent("1");

    await waitFor(() => {
      expect(screen.getByLabelText("次のページ")).toBeInTheDocument();
    });
    expect(screen.getByTestId("counter")).toHaveTextContent("1");
  });
});
