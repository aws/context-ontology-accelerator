// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { Button, StatusIndicator } from "@cloudscape-design/components";
import { useAppI18n } from "../../i18n/LocaleProvider";
import "./chat.css";

interface ChatErrorBoundaryProps {
  children: React.ReactNode;
  /** Called when the user clicks retry — typically re-sends the query. */
  onRetry?: () => void;
}

interface ChatErrorBoundaryState {
  hasError: boolean;
}

function ChatErrorFallback({
  onRetry,
}: {
  onRetry?: () => void;
}): React.ReactElement {
  const { t } = useAppI18n();

  return (
    <div className="chat-error-fallback">
      <StatusIndicator type="error">
        {t("Failed to render response")}
      </StatusIndicator>
      {onRetry && (
        <Button variant="inline-link" onClick={onRetry}>
          {t("Retry")}
        </Button>
      )}
    </div>
  );
}

/**
 * Error boundary that wraps individual chat messages so one broken
 * message doesn't crash the entire chat UI.
 */
export class ChatErrorBoundary extends React.Component<
  ChatErrorBoundaryProps,
  ChatErrorBoundaryState
> {
  constructor(props: ChatErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): ChatErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    console.error(
      "[ChatErrorBoundary] Render error in chat message:",
      error,
      errorInfo,
    );
  }

  private handleRetry = (): void => {
    this.setState({ hasError: false });
    this.props.onRetry?.();
  };

  render(): React.ReactNode {
    if (this.state.hasError) {
      return (
        <ChatErrorFallback
          onRetry={this.props.onRetry ? this.handleRetry : undefined}
        />
      );
    }

    return this.props.children;
  }
}
