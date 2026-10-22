// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { RUNTIME_CONFIG_FILENAME } from "@coa/shared";
import { Spinner } from "@cloudscape-design/components";
import React, {
  createContext,
  PropsWithChildren,
  useEffect,
  useState,
} from "react";
import { OidcConfig } from "@auth";
import { useAppI18n } from "../../i18n/LocaleProvider";

export interface RuntimeContext {
  readonly region: string;
  readonly authority: string;
  readonly clientId: string;
  readonly apiEndpoint?: string;
  /** AgentCore Runtime ARN. Frontend builds the invocations URL from this + region. */
  readonly serveRuntimeArn?: string;
  /** Monorepo version (from VERSION), rendered as a badge in the UI. */
  readonly version?: string;
  readonly oidcConfig: OidcConfig;
}

export const RuntimeConfigContext = createContext<RuntimeContext | undefined>(
  undefined,
);

/**
 * Loads `/runtime-config.json` at mount and provides it via {@link RuntimeConfigContext}.
 *
 * Required fields: `region`, `authority`, `clientId`.
 * Optional fields: `apiEndpoint`.
 *
 * Renders an error message if the config is missing or invalid.
 * Should be the outermost provider wrapping {@link Auth} in `main.tsx`.
 */
export const RuntimeContextProvider: React.FC<PropsWithChildren> = ({
  children,
}) => {
  const { t } = useAppI18n();
  const [runtimeContext, setRuntimeContext] = useState<
    RuntimeContext | undefined
  >();
  const [error, setError] = useState<string | undefined>();

  useEffect(() => {
    fetch(`/${RUNTIME_CONFIG_FILENAME}`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((config: Record<string, string>) => {
        if (!config.region || !config.authority || !config.clientId) {
          setError(
            t("runtime-config.json must have region, authority, and clientId."),
          );
          return;
        }
        const oidcConfig: OidcConfig = {
          authority: config.authority,
          clientId: config.clientId,
        };
        setRuntimeContext({
          ...config,
          oidcConfig,
        } as unknown as RuntimeContext);
      })
      .catch(() => setError(t("No runtime-config.json detected")));
  }, [t]);

  if (error) {
    return <div style={{ padding: "2rem", color: "#d13212" }}>{error}</div>;
  }

  return runtimeContext ? (
    <RuntimeConfigContext.Provider value={runtimeContext}>
      {children}
    </RuntimeConfigContext.Provider>
  ) : (
    <Spinner />
  );
};
