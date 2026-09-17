// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, {
  PropsWithChildren,
  useContext,
  useEffect,
  useState,
} from "react";
import { Spinner } from "@cloudscape-design/components";
import { RuntimeConfigContext } from "../RuntimeContext";
import { OIDCProvider, UserProvider } from "@auth";
import { AuthRoutes } from "./AuthRoutes";

interface AuthProps extends PropsWithChildren {
  applicationName: string;
}

/**
 * Authentication wrapper. Checks OIDC auth state and renders login routes
 * when unauthenticated, or wraps children in {@link UserProvider} when authenticated.
 *
 * @param applicationName - Name shown on the login page.
 */
export const Auth: React.FC<AuthProps> = ({ children, applicationName }) => {
  const runtimeContext = useContext(RuntimeConfigContext);
  const [authenticated, setAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  // Dev bypass: skip auth in local dev mode (Vite dev server only).
  // import.meta.env.DEV is tree-shaken to `false` in production builds,
  // so this code path is completely eliminated from deployed bundles.
  const isDevBypass =
    import.meta.env.DEV &&
    runtimeContext?.oidcConfig?.authority?.includes("LOCAL");

  useEffect(() => {
    if (isDevBypass) {
      setAuthenticated(true);
      setIsLoading(false);
      return;
    }
    if (!runtimeContext?.oidcConfig) return;
    const provider = OIDCProvider.build(runtimeContext.oidcConfig);

    let cancelled = false;

    provider
      .getUser()
      .then(async (user) => {
        if (!user?.profile?.sub) {
          if (!cancelled) setAuthenticated(false);
          return;
        }
        if (user.expires_at && user.expires_at < Date.now() / 1000) {
          try {
            await provider.userManager.signinSilent();
          } catch {
            await provider.userManager.removeUser();
            if (!cancelled) setAuthenticated(false);
            return;
          }
        }
        if (!cancelled) setAuthenticated(true);
      })
      .catch(() => {
        if (!cancelled) setAuthenticated(false);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    // Mid-session expiry handling. oidc-client-ts fires these events when the
    // access token expires or a silent renewal fails (e.g. the refresh token
    // is also expired/invalid). When the session can no longer be renewed we
    // drop the user and flip to unauthenticated, which makes this component
    // render <AuthRoutes>, whose catch-all redirects to /login — mirroring the
    // OIDC "null user" gate used in catalog-console-commons.
    const events = provider.userManager?.events;

    const handleSessionLost = async () => {
      try {
        // A successful automatic silent renew leaves a still-valid user, so
        // only treat the session as lost when no unexpired token remains.
        const current = await provider.getUser();
        if (current?.expires_at && current.expires_at > Date.now() / 1000) {
          return;
        }
        await provider.userManager.signinSilent();
      } catch {
        await provider.userManager.removeUser().catch(() => {});
        if (!cancelled) setAuthenticated(false);
      }
    };

    events?.addAccessTokenExpired(handleSessionLost);
    events?.addSilentRenewError(handleSessionLost);

    // A dropped user fires UserUnloaded. Unlike accessTokenExpired /
    // silentRenewError — which may still leave a valid token after a
    // successful background renew, hence handleSessionLost's re-check — a
    // UserUnloaded means the session is definitively gone. This is the event
    // the ad-hoc renewal path in getIdToken/getAccessToken now raises via
    // removeUser() (issue #136), where the background events never fired. Flip
    // straight to unauthenticated WITHOUT another signinSilent(): re-renewing
    // here would also run during signOut()'s removeUser().
    const handleUserUnloaded = () => {
      if (!cancelled) setAuthenticated(false);
    };
    events?.addUserUnloaded(handleUserUnloaded);

    return () => {
      cancelled = true;
      events?.removeAccessTokenExpired(handleSessionLost);
      events?.removeSilentRenewError(handleSessionLost);
      events?.removeUserUnloaded(handleUserUnloaded);
    };
  }, [runtimeContext, isDevBypass]);

  if (!runtimeContext?.oidcConfig) return null;
  if (isLoading) return <Spinner />;

  if (!authenticated) {
    return <AuthRoutes title={applicationName} />;
  }

  // In dev bypass mode, render children directly without UserProvider
  if (isDevBypass) {
    return <>{children}</>;
  }

  return (
    <UserProvider oidcConfig={runtimeContext.oidcConfig}>
      {children}
    </UserProvider>
  );
};
