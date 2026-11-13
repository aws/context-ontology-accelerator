// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi, describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { ChatContainer } from "../../../src/components/Chat/ChatContainer/ChatContainer";

describe("ChatContainer", () => {
  it("renders empty state when isEmpty is true", () => {
    render(
      <ChatContainer
        isEmpty={true}
        inputValue=""
        onInputChange={vi.fn()}
        onSubmit={vi.fn()}
      >
        <div>child</div>
      </ChatContainer>,
    );

    expect(
      screen.getByText("Ask a question against the governed surface..."),
    ).toBeInTheDocument();
    expect(screen.queryByText("child")).not.toBeInTheDocument();
  });

  it("renders children when isEmpty is false", () => {
    render(
      <ChatContainer
        isEmpty={false}
        inputValue=""
        onInputChange={vi.fn()}
        onSubmit={vi.fn()}
      >
        <div>message content</div>
      </ChatContainer>,
    );

    expect(screen.getByText("message content")).toBeInTheDocument();
    expect(
      screen.queryByText("Ask a question against the governed surface..."),
    ).not.toBeInTheDocument();
  });

  it("renders the chat input", () => {
    render(
      <ChatContainer
        isEmpty={true}
        inputValue="hello"
        onInputChange={vi.fn()}
        onSubmit={vi.fn()}
        placeholder="Type here"
      >
        <div />
      </ChatContainer>,
    );

    expect(screen.getByPlaceholderText("Type here")).toBeInTheDocument();
  });
});
