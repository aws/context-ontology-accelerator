// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ControlPlaneClientProvider } from "./index";
import { RuntimeConfigContext } from "../RuntimeContext";
import { OIDCProvider } from "@auth";

// Capture the config the generated client is constructed with so we can drive
// its `token` provider directly.
let capturedConfig:
  | { token: () => Promise<{ token: string; expiration?: Date }> }
  | undefined;

vi.mock("@coa/control-plane-client", () => ({
  ControlPlaneServiceClient: vi.fn().mockImplementation(function (config) {
    capturedConfig = config;
    return { config };
  }),
}));

vi.mock("@auth", () => ({
  OIDCProvider: { build: vi.fn() },
  UserProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const runtimeContext = {
  apiEndpoint: "https://api.example.com/",
  oidcConfig: { authority: "https://auth", clientId: "client" },
} as unknown as React.ContextType<typeof RuntimeConfigContext>;

function renderProvider() {
  return render(
    <RuntimeConfigContext.Provider value={runtimeContext}>
      <ControlPlaneClientProvider>
        <div />
      </ControlPlaneClientProvider>
    </RuntimeConfigContext.Provider>,
  );
}

describe("ControlPlaneClientProvider", () => {
  beforeEach(() => {
    capturedConfig = undefined;
    vi.clearAllMocks();
  });

  it("returns the ID token's expiry so the SDK refreshes instead of caching it forever (#136)", async () => {
    const exp = Math.floor(Date.now() / 1000) + 300;
    vi.mocked(OIDCProvider.build).mockReturnValue({
      getIdToken: vi.fn().mockResolvedValue("id-token"),
      getUser: vi
        .fn()
        .mockResolvedValue({ profile: { exp } } as unknown as never),
    } as unknown as OIDCProvider);

    renderProvider();

    const identity = await capturedConfig!.token();
    expect(identity.token).toBe("id-token");
    // Without an expiration the smithy memoize layer never re-invokes this
    // provider, so a stale token is sent until page reload. Assert it is set
    // and matches the ID token's own exp claim.
    expect(identity.expiration).toEqual(new Date(exp * 1000));
  });

  it("throws when no token is available", async () => {
    vi.mocked(OIDCProvider.build).mockReturnValue({
      getIdToken: vi.fn().mockResolvedValue(""),
      getUser: vi.fn().mockResolvedValue(null),
    } as unknown as OIDCProvider);

    renderProvider();

    await expect(capturedConfig!.token()).rejects.toThrow(
      "Failed to retrieve authentication token",
    );
  });
});
