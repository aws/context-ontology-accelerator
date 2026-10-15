// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientProvider, ApiError, useApiClient } from "./index";
import { RuntimeConfigContext } from "../RuntimeContext";
import { OIDCProvider } from "@auth";

vi.mock("@auth", () => ({
  OIDCProvider: { build: vi.fn() },
  UserProvider: ({ children }: { children: React.ReactNode }) => children,
}));

const runtimeContext = {
  apiEndpoint: "https://api.example.com/",
  oidcConfig: { authority: "https://auth", clientId: "client" },
} as unknown as React.ContextType<typeof RuntimeConfigContext>;

let captured: ReturnType<typeof useApiClient> | undefined;

function Probe() {
  captured = useApiClient();
  return null;
}

/** Render the provider and hand back the client its children would receive. */
function renderClient(): NonNullable<typeof captured> {
  render(
    <RuntimeConfigContext.Provider value={runtimeContext}>
      <ApiClientProvider>
        <Probe />
      </ApiClientProvider>
    </RuntimeConfigContext.Provider>,
  );
  if (!captured) throw new Error("provider did not supply a client");
  return captured;
}

/** Stub one failing fetch response with the given body. */
function mockFailure(status: number, body: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: false,
      status,
      headers: new Headers(),
      text: () => Promise.resolve(body),
    }),
  );
}

describe("ApiClientProvider error envelopes", () => {
  beforeEach(() => {
    captured = undefined;
    vi.clearAllMocks();
    vi.mocked(OIDCProvider.build).mockReturnValue({
      getIdToken: vi.fn().mockResolvedValue("id-token"),
    } as unknown as OIDCProvider);
  });

  it("uses a FastAPI `detail` as the error message", async () => {
    // Most ontology-engine 4xx responses are a bare `HTTPException`, whose
    // envelope is `detail` rather than the Smithy `message`. Reading only
    // `message` made every one of those fall through to the raw fallback, so a
    // page that rendered `error.message` showed the JSON envelope to the user
    // and the sentence the backend wrote was buried inside it.
    mockFailure(
      409,
      JSON.stringify({
        detail:
          "Grounding override could not be applied: no class matching '請求明細' in ontology Turtle",
      }),
    );
    const client = renderClient();

    await expect(client.post("/proposals/update", {})).rejects.toThrow(
      ApiError,
    );
    await expect(client.post("/proposals/update", {})).rejects.toThrow(
      "Grounding override could not be applied: no class matching '請求明細' in ontology Turtle",
    );
  });

  it("preserves the status and code alongside the detail", async () => {
    mockFailure(503, JSON.stringify({ code: "BUSY", detail: "try later" }));
    const client = renderClient();

    const error = await client.get("/whatever").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    if (!(error instanceof ApiError)) throw new Error("unreachable");
    expect(error.status).toBe(503);
    expect(error.code).toBe("BUSY");
    expect(error.message).toBe("try later");
  });

  it("prefers `message` when a response carries both", async () => {
    // The update route deliberately re-envelopes its 503 as `message` to match
    // the modeled ServiceUnavailableError, so `message` has to keep winning.
    mockFailure(
      503,
      JSON.stringify({ message: "modeled sentence", detail: "raw detail" }),
    );
    const client = renderClient();

    await expect(client.post("/x", {})).rejects.toThrow("modeled sentence");
  });

  it("falls back to the raw body when `detail` is not a string", async () => {
    // Some routes send a structured detail object. Interpolating it would
    // render "[object Object]" and lose the status the fallback carries.
    mockFailure(422, JSON.stringify({ detail: [{ loc: ["body"] }] }));
    const client = renderClient();

    await expect(client.post("/x", {})).rejects.toThrow(
      'API POST /x failed (422): {"detail":[{"loc":["body"]}]}',
    );
  });

  it("still folds a structured `errors` array into the message", async () => {
    mockFailure(
      400,
      JSON.stringify({
        message: "OSI import failed",
        errors: ["line 3: bad IRI", { ignored: true }, "line 9: bad IRI"],
      }),
    );
    const client = renderClient();

    await expect(client.post("/x", {})).rejects.toThrow(
      "OSI import failed: line 3: bad IRI; line 9: bad IRI",
    );
  });

  it("keeps the authorization wording for a 403", async () => {
    mockFailure(403, "");
    const client = renderClient();

    const error = await client.get("/x").catch((e: unknown) => e);
    if (!(error instanceof ApiError)) throw new Error("expected an ApiError");
    expect(error.status).toBe(403);
    expect(error.code).toBe("FORBIDDEN");
    expect(error.message).toContain("not authorized");
  });
});
