// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useRef, useCallback } from "react";
import { Box, Container, SpaceBetween } from "@cloudscape-design/components";
import { ChatInput } from "../ChatInput";
import { findScrollParent } from "@utils/scroll";
import { useAppI18n } from "../../../i18n/LocaleProvider";

export interface ChatContainerProps {
  children: React.ReactNode;
  isEmpty: boolean;
  inputValue: string;
  onInputChange: (value: string) => void;
  onSubmit: () => void;
  inputDisabled?: boolean;
  submitDisabled?: boolean;
  placeholder?: string;
  /** Rendered in place of the default empty-state text when the chat is empty. */
  emptyStateContent?: React.ReactNode;
}

export const ChatContainer: React.FC<ChatContainerProps> = ({
  children,
  isEmpty,
  inputValue,
  onInputChange,
  onSubmit,
  inputDisabled = false,
  submitDisabled = false,
  placeholder,
  emptyStateContent,
}) => {
  const { t } = useAppI18n();
  const observerRef = useRef<MutationObserver | null>(null);
  const rafRef = useRef<number | null>(null);

  const setListRef = useCallback((node: HTMLDivElement | null) => {
    if (observerRef.current) {
      observerRef.current.disconnect();
      observerRef.current = null;
    }
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    if (!node) return;

    let scrollParent: HTMLElement | null = null;
    let prevScrollHeight = 0;

    const getScrollParentCached = (): HTMLElement | null => {
      if (scrollParent) return scrollParent;
      scrollParent = findScrollParent(node);
      return scrollParent;
    };

    // Initial scroll to bottom
    setTimeout(() => {
      const sp = getScrollParentCached();
      if (sp) {
        sp.scrollTop = sp.scrollHeight;
        prevScrollHeight = sp.scrollHeight;
      }
    }, 0);

    observerRef.current = new MutationObserver(() => {
      if (rafRef.current) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        const sp = getScrollParentCached();
        if (!sp) return;

        const newScrollHeight = sp.scrollHeight;
        const heightDelta = newScrollHeight - prevScrollHeight;
        const distanceFromBottom =
          prevScrollHeight - sp.scrollTop - sp.clientHeight;

        if (distanceFromBottom < 200) {
          // User is at/near bottom: scroll to bottom (normal chat behavior)
          sp.scrollTop = newScrollHeight;
        } else if (heightDelta > 0 && sp.scrollTop > 0) {
          // Content was added while user is scrolled up: preserve position
          // (compensate for height added above the viewport)
          sp.scrollTop += heightDelta;
        }

        prevScrollHeight = newScrollHeight;
      });
    });
    observerRef.current.observe(node, {
      childList: true,
      subtree: true,
    });
  }, []);

  return (
    <Container
      fitHeight
      footer={
        <ChatInput
          value={inputValue}
          onChange={onInputChange}
          onSubmit={onSubmit}
          disabled={inputDisabled}
          submitDisabled={submitDisabled}
          placeholder={placeholder}
        />
      }
    >
      {isEmpty ? (
        (emptyStateContent ?? (
          <Box textAlign="center" color="text-body-secondary" padding="xl">
            {t("Ask a question against the governed surface...")}
          </Box>
        ))
      ) : (
        <div ref={setListRef}>
          <SpaceBetween size="l">{children}</SpaceBetween>
        </div>
      )}
    </Container>
  );
};
