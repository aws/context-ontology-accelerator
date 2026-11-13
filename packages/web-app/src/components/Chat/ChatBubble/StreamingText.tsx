// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Progressive streaming text display with memoized markdown and blinking cursor.
 */
import React, { memo } from "react";
import { MemoizedMarkdown } from "./MemoizedMarkdown";
import "../chat.css";

export interface StreamingTextProps {
  /** Accumulated token text. */
  text: string;
  /** Whether tokens are still arriving (shows cursor). */
  isStreaming: boolean;
  /** Stable message ID for memoization keys. */
  messageId: string;
}

/**
 * Renders streaming AI text with a blinking cursor.
 * Uses memoized markdown blocks so only the trailing block re-renders on token append.
 */
export const StreamingText: React.FC<StreamingTextProps> = memo(
  ({ text, isStreaming, messageId }) => {
    if (!text) return null;

    return (
      <div aria-live="polite" aria-atomic={false} style={{ lineHeight: 1.6 }}>
        <MemoizedMarkdown content={text} id={messageId} />
        {isStreaming && <span className="streaming-cursor" />}
      </div>
    );
  },
  (prev, next) =>
    prev.text === next.text && prev.isStreaming === next.isStreaming,
);

StreamingText.displayName = "StreamingText";
