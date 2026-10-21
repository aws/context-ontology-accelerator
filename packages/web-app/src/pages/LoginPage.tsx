// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useContext, useState } from "react";
import {
  AppLayout,
  Box,
  Button,
  Container,
  Flashbar,
  SpaceBetween,
  TextContent,
} from "@cloudscape-design/components";
import { RuntimeConfigContext } from "@components/RuntimeContext";
import { OIDCProvider } from "@auth";
import { useAppI18n } from "../i18n/LocaleProvider";

interface LoginPageProps {
  title: string;
}

export const LoginPage: React.FC<LoginPageProps> = ({ title }) => {
  const runtimeContext = useContext(RuntimeConfigContext);
  const [error, setError] = useState<string | null>(null);
  const { t } = useAppI18n();

  const handleSignIn = async () => {
    if (!runtimeContext?.oidcConfig) return;
    try {
      setError(null);
      const provider = OIDCProvider.build(runtimeContext.oidcConfig);
      await provider.authenticate();
    } catch {
      setError(t("Sign in failed. Please try again."));
    }
  };

  return (
    <AppLayout
      navigationHide
      toolsHide
      contentType="cards"
      content={
        <SpaceBetween size="xxl" direction="vertical" alignItems="center">
          <TextContent>
            <Box variant="h1" textAlign="center" padding={{ top: "xxxl" }}>
              {title}
            </Box>
          </TextContent>
          {error && <Flashbar items={[{ content: error, type: "error" }]} />}
          <Container>
            <SpaceBetween size="s" direction="vertical">
              <Button fullWidth variant="primary" onClick={handleSignIn}>
                {t("Sign in")}
              </Button>
            </SpaceBetween>
          </Container>
        </SpaceBetween>
      }
    />
  );
};
