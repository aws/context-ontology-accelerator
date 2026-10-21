// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { LocaleProvider } from "../../i18n/LocaleProvider";
import { ChatBubble } from "./ChatBubble";
import { RationalePanel } from "./RationalePanel";
import type { ChatMessage } from "@app-types";

function renderJapanese(ui: React.ReactNode) {
  return render(
    <LocaleProvider>
      <MemoryRouter>{ui}</MemoryRouter>
    </LocaleProvider>,
  );
}

describe("chat application copy in Japanese", () => {
  beforeEach(() => {
    window.localStorage.setItem("scl.locale", "ja");
  });

  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("localizes chat progress and retry controls", async () => {
    const { rerender } = renderJapanese(
      <ChatBubble
        role="assistant"
        state="loading"
        content=""
        timestamp={new Date("2026-01-01T00:00:00Z")}
        liveTraceSteps={[
          {
            step: "t1.metric_match",
            status: "success",
            durationMs: 1,
          },
        ]}
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByText("クエリを解決中 · メトリクスカタログを検索中"),
      ).toBeInTheDocument();
    });

    rerender(
      <LocaleProvider>
        <MemoryRouter>
          <ChatBubble
            role="assistant"
            state="error"
            content="失敗"
            timestamp={new Date("2026-01-01T00:00:00Z")}
            onRetry={() => {}}
          />
        </MemoryRouter>
      </LocaleProvider>,
    );
    expect(screen.getByText("再試行")).toBeInTheDocument();
  });

  it("localizes rationale metadata, step names, and fallback details", async () => {
    const message: ChatMessage = {
      id: "msg-ja",
      role: "assistant",
      content: "回答",
      timestamp: new Date("2026-01-01T00:00:00Z"),
      response: {
        requestId: "req-ja",
        result: {
          tier: 1,
          confidence: { score: 0.9, rationale: "高い信頼度" },
          trace: [
            {
              step: "t1.metric_match",
              status: "success",
              durationMs: 10,
            },
          ],
          partial: false,
        },
      },
    };

    renderJapanese(<RationalePanel message={message} />);

    await waitFor(() => {
      expect(screen.getByText("解決方法")).toBeInTheDocument();
      expect(screen.getAllByText("Tier 1 — メトリクス照合").length).toBe(2);
      expect(screen.getByText("メトリクスを照合")).toBeInTheDocument();
      expect(
        screen.getByText(/メトリクスカタログを確認しました。/),
      ).toBeInTheDocument();
    });
  });
});
