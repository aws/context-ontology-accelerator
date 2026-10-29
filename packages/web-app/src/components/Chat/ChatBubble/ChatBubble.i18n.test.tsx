// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import { ChatBubble } from "./ChatBubble";

// A Playground error carries the server's stable error type. The bubble shows
// the catalogue message for it, so a Japanese user does not see the server's
// English sentence as the whole story.

function renderError(
  locale: "en" | "ja",
  error: string,
  message: string,
  onRetry = vi.fn(),
) {
  window.localStorage.setItem("scl.locale", locale);
  return render(
    <LocaleProvider>
      <ChatBubble
        role="assistant"
        state="error"
        content={message}
        timestamp={new Date("2026-01-01T12:00:00Z")}
        errorMessage={message}
        errorDetails={{ error, statusCode: 0 }}
        onRetry={onRetry}
      />
    </LocaleProvider>,
  );
}

describe("ChatBubble — typed Playground errors", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("shows the access deny in Japanese with no Retry button", async () => {
    renderError("ja", "AccessDenied", "Access denied");
    expect(
      await screen.findByText("アクセスが拒否されました"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Access denied")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "再試行" })).toBeNull();
  });

  it("keeps the English deny text and drops Retry in English", async () => {
    renderError("en", "AccessDenied", "Access denied");
    expect(await screen.findByText("Access denied")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("shows a Japanese summary with the server sentence as detail", async () => {
    const server =
      "table reference 'customers' is ambiguous: it could be any of customers";
    renderError("ja", "AmbiguousReferenceError", server);
    expect(
      await screen.findByText(
        "質問内のテーブルが複数のデータソースに一致します",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(server)).toBeInTheDocument();
  });

  it("offers Retry for a timeout, in Japanese", async () => {
    renderError("ja", "TimeoutError", "Query resolution timed out");
    expect(
      await screen.findByText("質問の処理がタイムアウトしました"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "再試行" })).toBeInTheDocument();
  });

  it("leaves an English timeout exactly as the server wrote it", async () => {
    renderError("en", "TimeoutError", "Query resolution timed out");
    expect(
      await screen.findByText("Query resolution timed out"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
