// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi, describe, it, expect, beforeEach } from "vitest";
import { UserManager, User } from "oidc-client-ts";
import { OIDCProvider, OidcConfig } from "./OIDCProvider";

vi.mock("oidc-client-ts", () => {
  const eventHandlers: Record<string, ((...args: unknown[]) => void)[]> = {};
  const mockUserManager = {
    signinRedirect: vi.fn(),
    signinCallback: vi.fn(),
    signinSilent: vi.fn(),
    getUser: vi.fn(),
    clearStaleState: vi.fn(),
    removeUser: vi.fn(() => Promise.resolve()),
    signoutRedirect: vi.fn(),
    metadataService: {
      getRevocationEndpoint: vi.fn(),
      getTokenEndpoint: vi.fn(),
      getEndSessionEndpoint: vi.fn(),
    },
    events: {
      addUserLoaded: vi.fn((cb: (user: User) => void) => {
        eventHandlers["userLoaded"] = eventHandlers["userLoaded"] ?? [];
        eventHandlers["userLoaded"].push(cb as (...args: unknown[]) => void);
      }),
      addUserUnloaded: vi.fn((cb: () => void) => {
        eventHandlers["userUnloaded"] = eventHandlers["userUnloaded"] ?? [];
        eventHandlers["userUnloaded"].push(cb);
      }),
      // Helper to simulate events in tests
      _emit: (event: string, ...args: unknown[]) => {
        (eventHandlers[event] ?? []).forEach((cb) => cb(...args));
      },
    },
  };
  return {
    UserManager: vi.fn(() => mockUserManager),
    WebStorageStateStore: vi.fn(),
  };
});

const config: OidcConfig = {
  authority: "https://mock-authority",
  clientId: "mock-client-id",
};

// Build a partial User object for tests. The real `User` type from
// oidc-client-ts has many required fields we don't need for these mocks,
// so we cast through `unknown` to get a typed value without restating them.
const mockUser = (partial: Partial<User>): User => partial as unknown as User;

describe("OIDCProvider", () => {
  let provider: OIDCProvider;
  let mockUM: ReturnType<typeof vi.mocked<UserManager>>;

  beforeEach(() => {
    vi.clearAllMocks();
    provider = OIDCProvider.build(config);
    mockUM = vi.mocked(provider.userManager);
    // Reset cached user between tests to avoid singleton leakage
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (mockUM.events as any)._emit("userUnloaded");
    // Default to an IdP that advertises an end-session endpoint (e.g.
    // Cognito's hosted-UI logout) so existing signOut tests exercise the
    // signoutRedirect path unless a test explicitly overrides this to
    // simulate an IdP without one (e.g. Amazon Federate).
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (mockUM.metadataService as any).getEndSessionEndpoint.mockResolvedValue(
      "https://mock-authority/logout",
    );
  });

  describe("build (singleton)", () => {
    it("returns the same instance on subsequent calls", () => {
      const a = OIDCProvider.build(config);
      const b = OIDCProvider.build(config);
      expect(a).toBe(b);
    });
  });

  describe("authenticate", () => {
    it("calls signinRedirect", async () => {
      await provider.authenticate();
      expect(mockUM.signinRedirect).toHaveBeenCalled();
    });
  });

  describe("authenticationCallback", () => {
    it("returns user on success", async () => {
      const user = mockUser({ id_token: "tok" });
      mockUM.signinCallback.mockResolvedValue(user);
      const result = await provider.authenticationCallback();
      expect(result).toBe(user);
    });

    it("returns undefined when signinCallback returns null", async () => {
      mockUM.signinCallback.mockResolvedValue(null as unknown as User);
      const result = await provider.authenticationCallback();
      expect(result).toBeUndefined();
    });
  });

  describe("getUser", () => {
    it("delegates to userManager.getUser", async () => {
      const user = mockUser({ profile: { sub: "123" } as User["profile"] });
      mockUM.getUser.mockResolvedValue(user);
      const result = await provider.getUser();
      expect(result).toBe(user);
    });
  });

  describe("getIdToken", () => {
    it("returns token without refreshing when the ID token is still valid", async () => {
      mockUM.getUser.mockResolvedValue(
        mockUser({
          id_token: "my-token",
          expires_at: Date.now() / 1000 + 3600,
          profile: { exp: Date.now() / 1000 + 3600 } as User["profile"],
        }),
      );
      const token = await provider.getIdToken();
      expect(token).toBe("my-token");
      expect(mockUM.signinSilent).not.toHaveBeenCalled();
    });

    it("uses cached user from UserLoaded event instead of storage", async () => {
      // Simulate background silent renew updating the cached user
      const freshUser = mockUser({
        id_token: "fresh-token",
        expires_at: Date.now() / 1000 + 3600,
      });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (mockUM.events as any)._emit("userLoaded", freshUser);

      // getUser from storage would return stale, but cache takes precedence
      mockUM.getUser.mockResolvedValue(
        mockUser({
          id_token: "stale-token",
          expires_at: Date.now() / 1000 + 3600,
        }),
      );

      const token = await provider.getIdToken();
      expect(token).toBe("fresh-token");
      // Should NOT have called getUser since cache was populated
      expect(mockUM.getUser).not.toHaveBeenCalled();
    });

    it("refreshes when the ID token is expired even if the access token is still valid", async () => {
      // Gates on the ID token's own exp (profile.exp), NOT expires_at (the
      // access-token lifetime): a still-valid access token must not suppress
      // renewal of an expired ID token — that skew was the "403s, no refresh
      // request" report.
      const expiringUser = mockUser({
        id_token: "old-token",
        expires_at: Date.now() / 1000 + 3600, // access token still valid
        profile: { exp: Date.now() / 1000 - 1 } as User["profile"], // ID token expired
      });
      const refreshedUser = mockUser({ id_token: "new-token" });
      mockUM.getUser.mockResolvedValue(expiringUser);
      mockUM.signinSilent.mockResolvedValue(refreshedUser);

      const token = await provider.getIdToken();
      expect(mockUM.signinSilent).toHaveBeenCalled();
      expect(token).toBe("new-token");
    });

    it("throws when silent refresh fails", async () => {
      mockUM.getUser.mockResolvedValue(
        mockUser({
          id_token: "tok",
          profile: { exp: Date.now() / 1000 - 1 } as User["profile"],
        }),
      );
      mockUM.signinSilent.mockRejectedValue(new Error("network"));

      await expect(provider.getIdToken()).rejects.toThrow(
        "Token refresh failed. Please sign in again.",
      );
      // #136: the ad-hoc renewal path must drop the user so `UserUnloaded`
      // fires and the Auth gate flips to unauthenticated — otherwise the UI
      // keeps showing "signed in" while every call throws.
      expect(mockUM.removeUser).toHaveBeenCalled();
    });

    it("throws when id_token is empty", async () => {
      mockUM.getUser.mockResolvedValue(
        mockUser({
          id_token: "",
          expires_at: Date.now() / 1000 + 3600,
        }),
      );
      await expect(provider.getIdToken()).rejects.toThrow(
        "Auth token is empty.",
      );
    });

    it("throws when id_token is undefined", async () => {
      mockUM.getUser.mockResolvedValue(
        mockUser({
          id_token: undefined,
          expires_at: Date.now() / 1000 + 3600,
        }),
      );
      await expect(provider.getIdToken()).rejects.toThrow(
        "Auth token is empty.",
      );
    });

    it("throws when getUser returns null", async () => {
      mockUM.getUser.mockResolvedValue(null);
      await expect(provider.getIdToken()).rejects.toThrow(
        "Auth token is empty.",
      );
    });
  });

  describe("getAccessToken", () => {
    it("returns access token when user has valid token", async () => {
      mockUM.getUser.mockResolvedValue(
        mockUser({
          access_token: "my-access-token",
          expires_at: Date.now() / 1000 + 3600,
        }),
      );
      const token = await provider.getAccessToken();
      expect(token).toBe("my-access-token");
    });

    it("refreshes when the access token is expired", async () => {
      const expiringUser = mockUser({
        access_token: "old-at",
        expires_at: Date.now() / 1000 - 1,
      });
      const refreshedUser = mockUser({ access_token: "new-at" });
      mockUM.getUser.mockResolvedValue(expiringUser);
      mockUM.signinSilent.mockResolvedValue(refreshedUser);

      const token = await provider.getAccessToken();
      expect(mockUM.signinSilent).toHaveBeenCalled();
      expect(token).toBe("new-at");
    });

    it("throws when silent refresh fails", async () => {
      mockUM.getUser.mockResolvedValue(
        mockUser({ access_token: "at", expires_at: Date.now() / 1000 - 1 }),
      );
      mockUM.signinSilent.mockRejectedValue(new Error("network"));

      await expect(provider.getAccessToken()).rejects.toThrow(
        "Token refresh failed. Please sign in again.",
      );
      // #136: drop the user on ad-hoc renewal failure (see getIdToken).
      expect(mockUM.removeUser).toHaveBeenCalled();
    });

    it("throws when access_token is empty", async () => {
      mockUM.getUser.mockResolvedValue(
        mockUser({ access_token: "", expires_at: Date.now() / 1000 + 3600 }),
      );
      await expect(provider.getAccessToken()).rejects.toThrow(
        "Access token is empty.",
      );
    });

    it("throws when getUser returns null", async () => {
      mockUM.getUser.mockResolvedValue(null);
      await expect(provider.getAccessToken()).rejects.toThrow(
        "Access token is empty.",
      );
    });
  });

  describe("signOut", () => {
    it("clears state, removes user, and calls signoutRedirect", async () => {
      await provider.signOut();
      expect(mockUM.clearStaleState).toHaveBeenCalled();
      expect(mockUM.removeUser).toHaveBeenCalled();
      expect(mockUM.signoutRedirect).toHaveBeenCalledWith(
        expect.objectContaining({
          post_logout_redirect_uri: expect.any(String),
        }),
      );
    });

    it("revokes the refresh token at the IdP before clearing local state", async () => {
      mockUM.getUser.mockResolvedValue(
        mockUser({ refresh_token: "rt-secret" }),
      );
      // Cognito advertises no revocation_endpoint → derive from token endpoint.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (mockUM.metadataService as any).getRevocationEndpoint.mockResolvedValue(
        undefined,
      );
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (mockUM.metadataService as any).getTokenEndpoint.mockResolvedValue(
        "https://coa-pentest-auth.auth.us-east-1.amazoncognito.com/oauth2/token",
      );
      const fetchMock = vi
        .fn()
        .mockResolvedValue({ ok: true, status: 200 } as Response);
      vi.stubGlobal("fetch", fetchMock);

      await provider.signOut();

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe(
        "https://coa-pentest-auth.auth.us-east-1.amazoncognito.com/oauth2/revoke",
      );
      const body = String((init as RequestInit).body);
      expect(body).toContain("token=rt-secret");
      expect(body).toContain("client_id=mock-client-id");
      // Revocation happens; local sign-out still completes.
      expect(mockUM.removeUser).toHaveBeenCalled();
      expect(mockUM.signoutRedirect).toHaveBeenCalled();

      vi.unstubAllGlobals();
    });

    it("skips revocation but still logs out when there is no refresh token", async () => {
      mockUM.getUser.mockResolvedValue(mockUser({ refresh_token: undefined }));
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);

      await provider.signOut();

      expect(fetchMock).not.toHaveBeenCalled();
      expect(mockUM.removeUser).toHaveBeenCalled();
      expect(mockUM.signoutRedirect).toHaveBeenCalled();

      vi.unstubAllGlobals();
    });

    it("still logs out locally when revocation fails", async () => {
      mockUM.getUser.mockResolvedValue(mockUser({ refresh_token: "rt" }));
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (mockUM.metadataService as any).getRevocationEndpoint.mockResolvedValue(
        "https://idp.example.com/oauth2/revoke",
      );
      const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
      vi.stubGlobal("fetch", fetchMock);

      await expect(provider.signOut()).resolves.toBeUndefined();
      expect(mockUM.removeUser).toHaveBeenCalled();
      expect(mockUM.signoutRedirect).toHaveBeenCalled();

      vi.unstubAllGlobals();
    });

    it("falls back to a local redirect when the IdP has no end_session_endpoint (Federate)", async () => {
      // Amazon Federate's discovery document has no end_session_endpoint —
      // there's no IdP-hosted logout page to redirect through. Without this
      // fallback, signoutRedirect() throws "No end session endpoint"
      // uncaught: local state is cleared but the browser never navigates,
      // so sign-out silently appears to do nothing.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (mockUM.metadataService as any).getEndSessionEndpoint.mockResolvedValue(
        undefined,
      );
      const hrefSetter = vi.fn();
      // jsdom's window.location.href setter can't be spied on directly;
      // redefine the property to observe assignments without needing a full
      // Location mock.
      Object.defineProperty(window, "location", {
        value: {
          ...window.location,
          set href(v: string) {
            hrefSetter(v);
          },
        },
        writable: true,
      });

      await provider.signOut();

      expect(mockUM.removeUser).toHaveBeenCalled();
      expect(mockUM.signoutRedirect).not.toHaveBeenCalled();
      // logoutRedirectUri is computed once at construction time as
      // `${origin}${onLogoutRedirectPath ?? "/"}` — assert the fallback
      // navigates there, not to some other URL.
      expect(hrefSetter).toHaveBeenCalledWith(expect.stringMatching(/\/$/));
    });

    it("falls back to a local redirect when getEndSessionEndpoint rejects", async () => {
      // Defensive: a metadata-fetch failure must not surface as an uncaught
      // rejection from signOut() — still complete the local sign-out.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (mockUM.metadataService as any).getEndSessionEndpoint.mockRejectedValue(
        new Error("discovery fetch failed"),
      );

      await expect(provider.signOut()).resolves.toBeUndefined();
      expect(mockUM.removeUser).toHaveBeenCalled();
      expect(mockUM.signoutRedirect).not.toHaveBeenCalled();
    });
  });
});
