// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, {
  createContext,
  PropsWithChildren,
  useContext,
  useMemo,
} from "react";
import { ControlPlaneServiceClient } from "@coa/control-plane-client";
import { RuntimeConfigContext } from "../RuntimeContext";
import { OIDCProvider } from "@auth";

const ControlPlaneClientContext = createContext<
  ControlPlaneServiceClient | undefined
>(undefined);

export const useControlPlaneClient = (): ControlPlaneServiceClient => {
  const ctx = useContext(ControlPlaneClientContext);
  if (!ctx)
    throw new Error(
      "useControlPlaneClient must be used within ControlPlaneClientProvider",
    );
  return ctx;
};

export const ControlPlaneClientProvider: React.FC<PropsWithChildren> = ({
  children,
}) => {
  const runtimeContext = useContext(RuntimeConfigContext);
  const apiEndpoint = runtimeContext?.apiEndpoint;
  const oidcConfig = runtimeContext?.oidcConfig;

  const client = useMemo(() => {
    if (!apiEndpoint || !oidcConfig) return undefined;
    const provider = OIDCProvider.build(oidcConfig);

    return new ControlPlaneServiceClient({
      endpoint: apiEndpoint.replace(/\/$/, ""),
      token: async () => {
        const token = await provider.getIdToken();
        if (!token) throw new Error("Failed to retrieve authentication token");
        // Return the token's own expiry. The smithy httpBearerAuth client wraps
        // this provider in memoizeIdentityProvider, which only re-invokes it
        // when the cached identity reports it is expiring. WITHOUT an
        // `expiration`, the SDK treats the token as non-expiring and caches the
        // first one for the client's lifetime — so getIdToken()'s refresh never
        // runs here and every request keeps sending the initial ID token until
        // it expires (403 "Signature has expired"; only a page reload recovered
        // it). Sourced from the ID token's `exp` claim because that is the token
        // sent as the bearer. See issue #136.
        const user = await provider.getUser();
        const expiration =
          user?.profile?.exp !== undefined
            ? new Date(user.profile.exp * 1000)
            : undefined;
        return { token, expiration };
      },
    });
  }, [apiEndpoint, oidcConfig]);

  return (
    <ControlPlaneClientContext.Provider value={client}>
      {children}
    </ControlPlaneClientContext.Provider>
  );
};
