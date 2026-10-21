// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  Box,
  Button,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
} from "@cloudscape-design/components";
import { useAppI18n } from "../i18n/LocaleProvider";

interface Props {
  children: React.ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

function PageErrorFallback({ error }: { error: Error | null }) {
  const { t } = useAppI18n();

  return (
    <Box padding="xl">
      <Container
        header={<Header variant="h2">{t("Something went wrong")}</Header>}
      >
        <SpaceBetween size="m">
          <StatusIndicator type="error">
            {t("An unexpected error occurred while rendering this page.")}
          </StatusIndicator>
          {error && (
            <Box variant="code" fontSize="body-s">
              {error.message}
            </Box>
          )}
          <Button iconName="refresh" onClick={() => window.location.reload()}>
            {t("Reload page")}
          </Button>
        </SpaceBetween>
      </Container>
    </Box>
  );
}

export class PageErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("Page render error:", error, info.componentStack);
  }

  render() {
    if (!this.state.hasError) {
      return this.props.children;
    }

    return <PageErrorFallback error={this.state.error} />;
  }
}
