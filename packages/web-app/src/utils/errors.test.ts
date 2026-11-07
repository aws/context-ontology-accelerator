// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { ControlPlaneServiceServiceException } from "@coa/control-plane-client";
import { extractErrorMessage, isForbiddenError } from "./errors";

// Build an instance whose prototype satisfies `instanceof` without invoking
// the Smithy constructor.
function makeException(
  props: Record<string, unknown>,
): ControlPlaneServiceServiceException {
  return Object.assign(
    Object.create(ControlPlaneServiceServiceException.prototype),
    props,
  );
}

describe("extractErrorMessage", () => {
  it("returns the message of a plain Error", () => {
    expect(extractErrorMessage(new Error("boom"))).toBe("boom");
  });

  it("returns the fallback for non-error values", () => {
    expect(extractErrorMessage(null, "fallback")).toBe("fallback");
  });

  it("returns a real Smithy exception message", () => {
    expect(
      extractErrorMessage(makeException({ message: "real failure" })),
    ).toBe("real failure");
  });

  it("reads the `error` field copied onto an UnknownError exception", () => {
    expect(
      extractErrorMessage(
        makeException({ message: "UnknownError", error: "validation failed" }),
      ),
    ).toBe("validation failed");
  });

  it("parses the `error` field from the raw response body", () => {
    expect(
      extractErrorMessage(
        makeException({
          message: "UnknownError",
          $response: { body: JSON.stringify({ error: "bad column" }) },
        }),
      ),
    ).toBe("bad column");
  });

  it("falls back to the `message` field in the response body", () => {
    expect(
      extractErrorMessage(
        makeException({
          message: "UnknownError",
          $response: { body: JSON.stringify({ message: "from body" }) },
        }),
      ),
    ).toBe("from body");
  });

  it("returns the fallback when an UnknownError carries no usable detail", () => {
    expect(
      extractErrorMessage(makeException({ message: "UnknownError" }), "fb"),
    ).toBe("fb");
  });
});

describe("isForbiddenError", () => {
  it("recognises a Smithy 403", () => {
    expect(
      isForbiddenError(makeException({ $metadata: { httpStatusCode: 403 } })),
    ).toBe(true);
  });
  it("recognises an ApiError-shaped 403", () => {
    expect(
      isForbiddenError(Object.assign(new Error("x"), { status: 403 })),
    ).toBe(true);
  });
  it("is false for other statuses and non-errors", () => {
    expect(
      isForbiddenError(Object.assign(new Error("x"), { status: 404 })),
    ).toBe(false);
    expect(isForbiddenError(new Error("x"))).toBe(false);
    expect(isForbiddenError(null)).toBe(false);
    expect(isForbiddenError("403")).toBe(false);
  });
});
