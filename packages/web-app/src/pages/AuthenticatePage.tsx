// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  AppLayout,
  Box,
  SpaceBetween,
  TextContent,
} from "@cloudscape-design/components";
import { useSearchParams } from "react-router-dom";
import { RuntimeConfigContext } from "@components/RuntimeContext";
import { OIDCProvider } from "@auth";
import { useAppI18n } from "../i18n/LocaleProvider";

interface AuthenticatePageProps {
  title: string;
}

export const AuthenticatePage: React.FC<AuthenticatePageProps> = ({
  title,
}) => {
  const { t } = useAppI18n();
  const [status, setStatus] = useState<"processing" | "success" | "failure">(
    "processing",
  );
  const [searchParams] = useSearchParams();
  const runtimeContext = useContext(RuntimeConfigContext);
  const provider = useMemo(
    () =>
      runtimeContext?.oidcConfig
        ? OIDCProvider.build(runtimeContext.oidcConfig)
        : undefined,
    [runtimeContext],
  );
  const called = useRef(false);

  useEffect(() => {
    if (called.current || !provider) return;
    called.current = true;

    const code = searchParams.get("code");
    const state = searchParams.get("state");
    if (!code || !state) {
      setStatus("failure");
      return;
    }

    provider
      .authenticationCallback()
      .then((user) => {
        setStatus(user ? "success" : "failure");
      })
      .catch(() => setStatus("failure"));
  }, [provider, searchParams]);

  useEffect(() => {
    if (status === "success") window.location.replace("/");
    else if (status === "failure") window.location.replace("/login");
  }, [status]);

  const message =
    status === "processing"
      ? t("Authenticating with the configured Identity Provider...")
      : status === "success"
        ? t("Authentication successful. Redirecting...")
        : t("Authentication failed. Redirecting...");

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
          <TextContent>
            <Box variant="h5" textAlign="center" padding={{ top: "s" }}>
              {message}
            </Box>
          </TextContent>
        </SpaceBetween>
      }
    />
  );
};
