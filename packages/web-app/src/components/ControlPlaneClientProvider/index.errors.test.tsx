// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The sources service answers a refusal as `{error}`, while the generated client reads
 * `message`, so a refused Re-verify showed "UnknownError". The console's client reads both; the server is unchanged.
 */

import React from "react";
import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  type ControlPlaneServiceClient,
  RescanSourceCommand,
} from "@coa/control-plane-client";
import { ControlPlaneClientProvider, useControlPlaneClient } from "./index";
import { RuntimeConfigContext } from "../RuntimeContext";
import { OIDCProvider } from "@auth";

/** The one HTTP answer the client receives: the real generated client, with only its transport replaced. */
let reply: { status: number; body: object; headers: Record<string, string> } = {
  status: 200,
  body: {},
  headers: {},
};

vi.mock("@coa/control-plane-client", async (importOriginal) => {
  const real =
    await importOriginal<typeof import("@coa/control-plane-client")>();
  class Client extends real.ControlPlaneServiceClient {
    constructor(
      config: ConstructorParameters<typeof real.ControlPlaneServiceClient>[0],
    ) {
      super({
        ...config,
        requestHandler: {
          handle: async () => ({
            response: {
              statusCode: reply.status,
              headers: { "content-type": "application/json", ...reply.headers },
              body: new TextEncoder().encode(JSON.stringify(reply.body)),
            },
          }),
        },
      } as typeof config);
    }
  }
  return { ...real, ControlPlaneServiceClient: Client };
});

vi.mock("@auth", () => ({
  OIDCProvider: { build: vi.fn() },
  UserProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const runtimeContext = {
  apiEndpoint: "https://api.example.com/",
  oidcConfig: { authority: "https://auth", clientId: "client" },
} as unknown as React.ContextType<typeof RuntimeConfigContext>;

const SENTENCE = "Source 'ab2b5c4f' is already being verified.";

let captured: ControlPlaneServiceClient | undefined;

function Probe() {
  captured = useControlPlaneClient();
  return null;
}

function client(): ControlPlaneServiceClient {
  render(
    <RuntimeConfigContext.Provider value={runtimeContext}>
      <ControlPlaneClientProvider>
        <Probe />
      </ControlPlaneClientProvider>
    </RuntimeConfigContext.Provider>,
  );
  if (!captured) throw new Error("provider did not supply a client");
  return captured;
}

function answer(
  status: number,
  body: object,
  headers: Record<string, string> = {},
) {
  reply = { status, body, headers };
}

async function refusal(): Promise<Error> {
  const error = await client()
    .send(new RescanSourceCommand({ namespaceId: "ns-1", sourceId: "src-1" }))
    .catch((e: unknown) => e);
  if (!(error instanceof Error))
    throw new Error("expected the call to be refused");
  return error;
}

describe("the control-plane client reads a refusal's sentence from `error` as well as `message`", () => {
  beforeEach(() => {
    captured = undefined;
    vi.mocked(OIDCProvider.build).mockReturnValue({
      getIdToken: vi.fn().mockResolvedValue("id-token"),
      getUser: vi.fn().mockResolvedValue(null),
    } as unknown as OIDCProvider);
  });

  it("a 409 {error, sourceId, status}, as the sources service sends it, carries the service's sentence", async () => {
    answer(409, { error: SENTENCE, sourceId: "src-1", status: "APPROVED" });
    expect((await refusal()).message).toBe(SENTENCE);
  });

  it("the same when the error type is named (the modeled ConflictError)", async () => {
    answer(409, { error: SENTENCE }, { "x-amzn-errortype": "ConflictError" });
    expect((await refusal()).message).toBe(SENTENCE);
  });

  it("a modeled `message` is kept as it is", async () => {
    answer(
      409,
      { message: "modeled sentence", error: "other" },
      { "x-amzn-errortype": "ConflictError" },
    );
    expect((await refusal()).message).toBe("modeled sentence");
  });

  it("a refusal with neither keeps the client's own fallback", async () => {
    answer(409, { sourceId: "src-1" });
    expect((await refusal()).message).toBe("UnknownError");
  });
});
