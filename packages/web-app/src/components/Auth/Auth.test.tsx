// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi, describe, it, expect, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";
import { Auth } from "./index";
import { RuntimeConfigContext, RuntimeContext } from "../RuntimeContext";
import { OIDCProvider } from "@auth";

vi.mock("../../auth", async () => {
  const actual = await vi.importActual("../../auth");
  return {
    ...actual,
    OIDCProvider: { build: vi.fn() },
  };
});

const mockProvider = {
  getUser: vi.fn(),
  getIdToken: vi.fn(),
  signOut: vi.fn(),
};

const runtimeContext: RuntimeContext = {
  region: "us-east-1",
  authority: "https://mock",
  clientId: "mock-id",
  oidcConfig: { authority: "https://mock", clientId: "mock-id" },
};

describe("Auth", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(OIDCProvider.build).mockReturnValue(
      mockProvider as unknown as OIDCProvider,
    );
  });

  it("renders nothing when runtimeContext is undefined", async () => {
    mockProvider.getUser.mockResolvedValue(null);

    let container: HTMLElement;
    await act(async () => {
      const result = render(
        <RuntimeConfigContext.Provider value={undefined}>
          <MemoryRouter>
            <Auth applicationName="Test App">
              <div>Protected</div>
            </Auth>
          </MemoryRouter>
        </RuntimeConfigContext.Provider>,
      );
      container = result.container;
    });

    expect(container!.innerHTML).toBe("");
  });

  it("renders children when user is authenticated", async () => {
    mockProvider.getUser.mockResolvedValue({ profile: { sub: "user1" } });
    mockProvider.getIdToken.mockResolvedValue("tok");

    await act(async () => {
      render(
        <RuntimeConfigContext.Provider value={runtimeContext}>
          <MemoryRouter>
            <Auth applicationName="Test App">
              <div data-testid="child">Protected Content</div>
            </Auth>
          </MemoryRouter>
        </RuntimeConfigContext.Provider>,
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId("child")).toHaveTextContent(
        "Protected Content",
      );
    });
  });

  it("renders login route when user is not authenticated", async () => {
    mockProvider.getUser.mockResolvedValue(null);

    await act(async () => {
      render(
        <RuntimeConfigContext.Provider value={runtimeContext}>
          <MemoryRouter initialEntries={["/login"]}>
            <Auth applicationName="Test App">
              <div data-testid="child">Protected</div>
            </Auth>
          </MemoryRouter>
        </RuntimeConfigContext.Provider>,
      );
    });

    await waitFor(() => {
      expect(screen.queryByTestId("child")).not.toBeInTheDocument();
      expect(screen.getByText("Sign in")).toBeInTheDocument();
    });
  });

  it("renders login route when getUser throws", async () => {
    mockProvider.getUser.mockRejectedValue(new Error("fail"));

    await act(async () => {
      render(
        <RuntimeConfigContext.Provider value={runtimeContext}>
          <MemoryRouter initialEntries={["/login"]}>
            <Auth applicationName="Test App">
              <div data-testid="child">Protected</div>
            </Auth>
          </MemoryRouter>
        </RuntimeConfigContext.Provider>,
      );
    });

    await waitFor(() => {
      expect(screen.queryByTestId("child")).not.toBeInTheDocument();
    });
  });

  it("routes back to login when the session is lost mid-session", async () => {
    const handlers: { expired?: () => void; renewError?: () => void } = {};
    const userManager = {
      events: {
        addAccessTokenExpired: (cb: () => void) => {
          handlers.expired = cb;
        },
        addSilentRenewError: (cb: () => void) => {
          handlers.renewError = cb;
        },
        removeAccessTokenExpired: vi.fn(),
        removeSilentRenewError: vi.fn(),
        addUserUnloaded: vi.fn(),
        removeUserUnloaded: vi.fn(),
      },
      signinSilent: vi.fn().mockRejectedValue(new Error("refresh failed")),
      removeUser: vi.fn().mockResolvedValue(undefined),
    };
    const provider = {
      getUser: vi
        .fn()
        // Initial load: a valid, unexpired session -> authenticated.
        .mockResolvedValueOnce({
          profile: { sub: "user1" },
          expires_at: Date.now() / 1000 + 3600,
        })
        // Subsequent checks (UserProvider + session-lost handler): expired.
        .mockResolvedValue({
          profile: { sub: "user1" },
          expires_at: Date.now() / 1000 - 10,
        }),
      getIdToken: vi.fn().mockResolvedValue("tok"),
      signOut: vi.fn(),
      userManager,
    };
    vi.mocked(OIDCProvider.build).mockReturnValue(
      provider as unknown as OIDCProvider,
    );

    await act(async () => {
      render(
        <RuntimeConfigContext.Provider value={runtimeContext}>
          <MemoryRouter initialEntries={["/"]}>
            <Auth applicationName="Test App">
              <div data-testid="child">Protected Content</div>
            </Auth>
          </MemoryRouter>
        </RuntimeConfigContext.Provider>,
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId("child")).toBeInTheDocument();
    });

    // Simulate the access token expiring with a failed silent renewal.
    await act(async () => {
      handlers.expired?.();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.queryByTestId("child")).not.toBeInTheDocument();
      expect(screen.getByText("Sign in")).toBeInTheDocument();
    });
    expect(userManager.removeUser).toHaveBeenCalled();
  });

  it("routes back to login on UserUnloaded without re-attempting silent renew", async () => {
    // The ad-hoc renewal path in getIdToken/getAccessToken drops the user on
    // failure, firing UserUnloaded (issue #136). Auth must flip to
    // unauthenticated directly — NOT call signinSilent again, which would also
    // run during signOut()'s removeUser().
    const handlers: { unloaded?: () => void } = {};
    const userManager = {
      events: {
        addAccessTokenExpired: vi.fn(),
        addSilentRenewError: vi.fn(),
        addUserUnloaded: (cb: () => void) => {
          handlers.unloaded = cb;
        },
        removeAccessTokenExpired: vi.fn(),
        removeSilentRenewError: vi.fn(),
        removeUserUnloaded: vi.fn(),
      },
      signinSilent: vi.fn().mockResolvedValue(undefined),
      removeUser: vi.fn().mockResolvedValue(undefined),
    };
    const provider = {
      getUser: vi.fn().mockResolvedValue({
        profile: { sub: "user1" },
        expires_at: Date.now() / 1000 + 3600,
      }),
      getIdToken: vi.fn().mockResolvedValue("tok"),
      signOut: vi.fn(),
      userManager,
    };
    vi.mocked(OIDCProvider.build).mockReturnValue(
      provider as unknown as OIDCProvider,
    );

    await act(async () => {
      render(
        <RuntimeConfigContext.Provider value={runtimeContext}>
          <MemoryRouter initialEntries={["/"]}>
            <Auth applicationName="Test App">
              <div data-testid="child">Protected Content</div>
            </Auth>
          </MemoryRouter>
        </RuntimeConfigContext.Provider>,
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId("child")).toBeInTheDocument();
    });

    await act(async () => {
      handlers.unloaded?.();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.queryByTestId("child")).not.toBeInTheDocument();
      expect(screen.getByText("Sign in")).toBeInTheDocument();
    });
    expect(userManager.signinSilent).not.toHaveBeenCalled();
  });
});
