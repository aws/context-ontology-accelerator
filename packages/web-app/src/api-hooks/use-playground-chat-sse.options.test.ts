// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Request-shaping for the Playground's `options` object. The Playground page
 *  test mocks the hook, so this is the only coverage of the request shape. */
import { describe, it, expect } from "vitest";
import { buildOptions } from "./use-playground-chat-sse";

describe("buildOptions", () => {
  it("requests the compiled artifacts even when nothing else is set", () => {
    expect(buildOptions()).toEqual({ options: { includeDebugInfo: true } });
  });

  it("sends mode alongside includeDebugInfo", () => {
    expect(buildOptions("deep-reasoning")).toEqual({
      options: { mode: "deep-reasoning", includeDebugInfo: true },
    });
  });

  it("sends strategy alongside includeDebugInfo", () => {
    expect(buildOptions(undefined, "ontop")).toEqual({
      options: { strategy: "ontop", includeDebugInfo: true },
    });
  });

  it("sends mode + strategy without one clobbering the other", () => {
    expect(buildOptions("standard", "ontop_first")).toEqual({
      options: {
        mode: "standard",
        strategy: "ontop_first",
        includeDebugInfo: true,
      },
    });
  });

  it("treats an empty string as unset, still sending includeDebugInfo", () => {
    // Guards the falsy-check: `""` must not reach the API as a pin, which serve
    // would fail to match and silently resolve to its default.
    expect(buildOptions("" as never, "" as never)).toEqual({
      options: { includeDebugInfo: true },
    });
  });
});
