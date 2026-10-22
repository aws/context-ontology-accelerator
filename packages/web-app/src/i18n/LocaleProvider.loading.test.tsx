// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import Pagination from "@cloudscape-design/components/pagination";
import jaMessages from "@cloudscape-design/components/i18n/messages/all.ja";
import type { I18nProviderProps } from "@cloudscape-design/components/i18n";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

type Messages = ReadonlyArray<I18nProviderProps.Messages>;

const { importMessagesMock } = vi.hoisted(() => ({
  importMessagesMock: vi.fn<(locale: string) => Promise<Messages>>(),
}));

vi.mock("@cloudscape-design/components/i18n", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("@cloudscape-design/components/i18n")>();
  return { ...original, importMessages: importMessagesMock };
});

import { LocaleProvider, useLocale } from "./LocaleProvider";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  const resolvers: Array<(value: T) => void> = [];
  const promise = new Promise<T>((resolve) => {
    resolvers.push(resolve);
  });
  const resolve = resolvers[0];
  if (!resolve) throw new Error("Promise executor did not run");
  return { promise, resolve };
}

function Probe() {
  const { locale } = useLocale();
  return (
    <>
      <span data-testid="locale">{locale}</span>
      <Pagination currentPageIndex={1} pagesCount={3} />
    </>
  );
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = "";
  importMessagesMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

it("keeps English active until a stored Japanese bundle finishes loading", async () => {
  const pending = deferred<Messages>();
  importMessagesMock.mockReturnValue(pending.promise);
  window.localStorage.setItem("scl.locale", "ja");

  render(
    <LocaleProvider>
      <Probe />
    </LocaleProvider>,
  );

  expect(screen.getByTestId("locale")).toHaveTextContent("en");
  expect(screen.getByLabelText("Next page")).toBeInTheDocument();
  expect(document.documentElement.lang).toBe("en");

  await act(async () => {
    pending.resolve([jaMessages]);
    await pending.promise;
  });

  await waitFor(() => {
    expect(screen.getByTestId("locale")).toHaveTextContent("ja");
    expect(screen.getByLabelText("次のページ")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("ja");
  });
});

it("falls back to English when a stored Japanese bundle fails to load", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  importMessagesMock.mockRejectedValue(new Error("chunk unavailable"));
  window.localStorage.setItem("scl.locale", "ja");

  render(
    <LocaleProvider>
      <Probe />
    </LocaleProvider>,
  );

  await waitFor(() => {
    expect(consoleError).toHaveBeenCalledWith(
      "Failed to load Cloudscape messages for ja:",
      expect.any(Error),
    );
  });
  expect(screen.getByTestId("locale")).toHaveTextContent("en");
  expect(screen.getByLabelText("Next page")).toBeInTheDocument();
  expect(document.documentElement.lang).toBe("en");
  expect(window.localStorage.getItem("scl.locale")).toBe("en");
});

it("falls back to English when the message loader returns no bundle", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  importMessagesMock.mockResolvedValue([]);
  window.localStorage.setItem("scl.locale", "ja");

  render(
    <LocaleProvider>
      <Probe />
    </LocaleProvider>,
  );

  await waitFor(() => {
    expect(consoleError).toHaveBeenCalledWith(
      "Failed to load Cloudscape messages for ja: bundle was empty",
    );
  });
  expect(screen.getByTestId("locale")).toHaveTextContent("en");
  expect(screen.getByLabelText("Next page")).toBeInTheDocument();
  expect(document.documentElement.lang).toBe("en");
  expect(window.localStorage.getItem("scl.locale")).toBe("en");
});
