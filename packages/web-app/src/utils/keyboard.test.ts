// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { isSubmitEnter } from "./keyboard";

describe("isSubmitEnter", () => {
  it("accepts a plain Enter", () => {
    expect(
      isSubmitEnter({ key: "Enter", keyCode: 13, isComposing: false }),
    ).toBe(true);
  });

  it("accepts Enter when the detail carries only the key", () => {
    expect(isSubmitEnter({ key: "Enter" })).toBe(true);
  });

  it("rejects the Enter that confirms an IME conversion", () => {
    expect(
      isSubmitEnter({ key: "Enter", keyCode: 13, isComposing: true }),
    ).toBe(false);
  });

  it("rejects the IME's process key code even when composition has ended", () => {
    expect(
      isSubmitEnter({ key: "Enter", keyCode: 229, isComposing: false }),
    ).toBe(false);
  });

  it("rejects other keys", () => {
    expect(isSubmitEnter({ key: "a", keyCode: 65, isComposing: false })).toBe(
      false,
    );
  });
});
