// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import CloudscapeChatBubble from "@cloudscape-design/chat-components/chat-bubble";
import { ChatAvatar } from "../ChatAvatar";
import { ChatBubble, ChatBubbleProps } from "../ChatBubble";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import "../chat.css";

export interface ChatMessageProps extends ChatBubbleProps {
  initials?: string;
}

export const ChatMessage: React.FC<ChatMessageProps> = ({
  initials,
  ...bubbleProps
}) => {
  const { t } = useAppI18n();
  const isLoading =
    bubbleProps.role === "assistant" && bubbleProps.state === "loading";
  const isUser = bubbleProps.role === "user";

  if (isUser) {
    return (
      <div className="chat-message-user">
        <CloudscapeChatBubble
          type="outgoing"
          ariaLabel={t("user message")}
          avatar={<ChatAvatar role="user" initials={initials} />}
        >
          <ChatBubble {...bubbleProps} />
        </CloudscapeChatBubble>
      </div>
    );
  }

  return (
    <div style={{ maxWidth: "900px" }}>
      <CloudscapeChatBubble
        type="incoming"
        ariaLabel={t("assistant message")}
        avatar={<ChatAvatar role="assistant" isLoading={isLoading} />}
      >
        <ChatBubble {...bubbleProps} />
      </CloudscapeChatBubble>
    </div>
  );
};
