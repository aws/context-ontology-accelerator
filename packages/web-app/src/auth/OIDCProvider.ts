// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { UserManager, WebStorageStateStore, User } from "oidc-client-ts";

/**
 * Refresh a token this many seconds BEFORE its own expiry. A proactive buffer:
 * the token is renewed ahead of lapsing rather than after, so a request never
 * goes out with an about-to-expire token. Firing early is cheap and harmless;
 * firing late means a spurious 401/403. Two minutes comfortably covers a
 * request round-trip plus client/server clock skew.
 */
const TOKEN_REFRESH_BUFFER_SECONDS = 120;

/** OIDC provider configuration for the Authorization Code flow with PKCE. */
export interface OidcConfig {
  /** OIDC provider URL (e.g. `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXX`). */
  authority: string;
  /** OAuth client ID from the OIDC provider. */
  clientId: string;
  /** Path to redirect after login. @default "/authenticate/" */
  onLoginRedirectPath?: string;
  /** Path to redirect after logout. @default "/" */
  onLogoutRedirectPath?: string;
}

let instance: OIDCProvider | undefined;

/**
 * Singleton OIDC client wrapper using `oidc-client-ts`.
 * Use {@link OIDCProvider.build} to obtain the instance.
 */
export class OIDCProvider {
  readonly userManager: UserManager;
  private readonly logoutRedirectUri: string;
  private readonly clientId: string;

  /** In-memory cache of the latest User, updated via the UserLoaded event. */
  private cachedUser: User | null = null;

  private constructor(config: OidcConfig) {
    const loginRedirect = `${window.location.origin}${config.onLoginRedirectPath ?? "/authenticate/"}`;
    const logoutRedirect = `${window.location.origin}${config.onLogoutRedirectPath ?? "/"}`;
    this.logoutRedirectUri = logoutRedirect;
    this.clientId = config.clientId;

    this.userManager = new UserManager({
      authority: config.authority,
      client_id: config.clientId,
      redirect_uri: loginRedirect,
      post_logout_redirect_uri: logoutRedirect,
      response_type: "code",
      scope: "email openid profile",
      // Proactively renew the token before it expires so an expired/invalid
      // session is detected promptly. When renewal fails, oidc-client-ts emits
      // `silentRenewError` (and ultimately `accessTokenExpired`), which the
      // Auth gate listens for to route the user back to the login page.
      automaticSilentRenew: true,
      userStore: new WebStorageStateStore(),
      stateStore: new WebStorageStateStore(),
    });

    // Keep in-memory cache in sync with background silent renew.
    // When automaticSilentRenew refreshes the token, this fires immediately
    // with the fresh User, so subsequent getIdToken/getAccessToken calls
    // always return the latest tokens without hitting storage.
    this.userManager.events.addUserLoaded((user) => {
      this.cachedUser = user;
    });

    this.userManager.events.addUserUnloaded(() => {
      this.cachedUser = null;
    });
  }

  /** Returns the singleton OIDCProvider, creating it on first call. */
  static build(config: OidcConfig): OIDCProvider {
    if (!instance) {
      instance = new OIDCProvider(config);
    }
    return instance;
  }

  /** Initiates the OIDC login redirect to the identity provider. */
  async authenticate(): Promise<void> {
    await this.userManager.signinRedirect();
  }

  async authenticationCallback(url?: string): Promise<User | undefined> {
    return (await this.userManager.signinCallback(url)) ?? undefined;
  }

  async getUser(): Promise<User | null> {
    return this.userManager.getUser();
  }

  /**
   * Returns the current ID token, silently refreshing it once it is at (or
   * within {@link TOKEN_REFRESH_BUFFER_SECONDS} of) its own expiry.
   *
   * The renewal decision keys off the ID token's own `exp` claim
   * (`user.profile.exp`) — NOT `user.expires_at`, which oidc-client-ts derives
   * from the OAuth `expires_in` (the ACCESS token lifetime). This is the token
   * this method returns and the one the API sends as the bearer, so when a
   * deployment sets the ID-token TTL shorter than the access-token TTL, gating
   * on `expires_at` would let an already-expired ID token through with no
   * renewal attempt (403s, no refresh request) — see the tab-switch report.
   */
  async getIdToken(): Promise<string> {
    // Prefer in-memory cache (kept fresh by UserLoaded event) over storage.
    let user = this.cachedUser ?? (await this.userManager.getUser());

    // Refresh once the ID token itself is essentially expired.
    const idTokenExp = user?.profile?.exp;
    if (
      user &&
      idTokenExp &&
      idTokenExp < Date.now() / 1000 + TOKEN_REFRESH_BUFFER_SECONDS
    ) {
      try {
        user = await this.userManager.signinSilent();
        this.cachedUser = user;
      } catch {
        // This ad-hoc renewal path is separate from oidc-client-ts's
        // background `automaticSilentRenew` timer, so a failure here does NOT
        // raise `silentRenewError`/`accessTokenExpired`. Drop the user so
        // `UserUnloaded` fires and the Auth gate flips to unauthenticated —
        // otherwise the UI keeps showing "signed in" while every token-bearing
        // call throws individually (issue #136). Best-effort: never let a
        // removeUser() failure mask the sign-in-again error.
        await this.userManager.removeUser().catch(() => {});
        throw new Error("Token refresh failed. Please sign in again.");
      }
    }

    const token = user?.id_token;
    if (!token) throw new Error("Auth token is empty.");
    return token;
  }

  /**
   * Returns the current access token, silently refreshing it once it is at (or
   * within {@link TOKEN_REFRESH_BUFFER_SECONDS} of) expiry. Gates on
   * `user.expires_at`, which is the access token's lifetime — the right clock
   * for the token this method returns.
   */
  async getAccessToken(): Promise<string> {
    let user = this.cachedUser ?? (await this.userManager.getUser());

    // Refresh once the access token is essentially expired.
    if (
      user &&
      user.expires_at &&
      user.expires_at < Date.now() / 1000 + TOKEN_REFRESH_BUFFER_SECONDS
    ) {
      try {
        user = await this.userManager.signinSilent();
        this.cachedUser = user;
      } catch {
        // See getIdToken: drop the user so `UserUnloaded` fires and the Auth
        // gate flips to unauthenticated (issue #136). Best-effort.
        await this.userManager.removeUser().catch(() => {});
        throw new Error("Token refresh failed. Please sign in again.");
      }
    }

    const token = user?.access_token;
    if (!token) throw new Error("Access token is empty.");
    return token;
  }

  /**
   * Revokes the current refresh token at the identity provider so it can no
   * longer be used to mint new access/ID tokens after logout. This closes the
   * gap where a stolen refresh token (via XSS, storage compromise, or device
   * access) stays usable until natural expiry despite the user logging out.
   *
   * Best-effort: revocation failures are logged (without token material) and
   * do not block the logout redirect, so the user is always signed out locally.
   *
   * Cognito does not advertise a `revocation_endpoint` in its OIDC discovery
   * document; the revoke endpoint lives on the same Hosted-UI domain as the
   * token endpoint (`/oauth2/revoke`), so we derive it as a fallback. Revoking
   * a refresh token also invalidates the access tokens issued from it.
   */
  private async revokeRefreshToken(): Promise<void> {
    const user = this.cachedUser ?? (await this.userManager.getUser());
    const refreshToken = user?.refresh_token;
    if (!refreshToken) return;

    let revocationUrl = await this.userManager.metadataService
      .getRevocationEndpoint(true)
      .catch(() => undefined);

    if (!revocationUrl) {
      const tokenEndpoint = await this.userManager.metadataService
        .getTokenEndpoint(true)
        .catch(() => undefined);
      if (tokenEndpoint) {
        revocationUrl = tokenEndpoint.replace(
          /\/oauth2\/token\/?$/,
          "/oauth2/revoke",
        );
      }
    }
    if (!revocationUrl) {
      console.error("Token revocation skipped: no revocation endpoint found.");
      return;
    }

    const body = new URLSearchParams({
      token: refreshToken,
      client_id: this.clientId,
    });
    const response = await fetch(revocationUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    // Never log token material — status only.
    if (!response.ok) {
      console.error(`Token revocation failed: HTTP ${response.status}`);
    }
  }

  /**
   * Signs the user out and redirects to the post-logout URI.
   *
   * Not every OIDC-compliant IdP advertises an `end_session_endpoint` in its
   * discovery document — some enterprise SSO providers do not expose one:
   * there is no IdP-hosted logout page to redirect through when session state
   * lives at the browser/network layer, not as a per-relying-app IdP session to
   * explicitly end). `signoutRedirect()` needs that endpoint to build its
   * redirect URL and throws `Error("No end session endpoint")` when it's
   * absent — uncaught, that leaves local state cleared (tokens removed) but
   * the browser never navigates anywhere, so sign-out silently appears to do
   * nothing instead of returning the user to the app.
   *
   * Checking `getEndSessionEndpoint()` first (the same optional-metadata
   * helper `revokeRefreshToken()` already uses for the revocation endpoint)
   * lets this fall back to a plain local redirect when the IdP has no
   * end-session endpoint, while still using the standard IdP-redirect flow
   * for any IdP that does advertise one (e.g. Cognito's hosted-UI logout).
   */
  async signOut(): Promise<void> {
    // Revoke the refresh token at the IdP BEFORE clearing local state so a
    // stolen token cannot mint new tokens after logout. Best-effort — never
    // block the local sign-out on a revocation error.
    try {
      await this.revokeRefreshToken();
    } catch (error) {
      console.error("Token revocation error during logout:", error);
    }
    await this.userManager.clearStaleState();
    await this.userManager.removeUser();

    const endSessionEndpoint = await this.userManager.metadataService
      .getEndSessionEndpoint()
      .catch(() => undefined);

    if (!endSessionEndpoint) {
      // No IdP-hosted logout page to redirect through (e.g. Federate) — local
      // state is already cleared above, so go straight to the post-logout URI.
      window.location.href = this.logoutRedirectUri;
      return;
    }

    await this.userManager.signoutRedirect({
      post_logout_redirect_uri: this.logoutRedirectUri,
      extraQueryParams: {
        logout_uri: this.logoutRedirectUri,
        redirect_uri: this.logoutRedirectUri,
        response_type: "code",
      },
    });
  }
}
