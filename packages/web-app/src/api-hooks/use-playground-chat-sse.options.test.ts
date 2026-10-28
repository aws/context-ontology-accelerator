// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Request-shaping for the Playground's `options` object.
 *
 * The Playground page test mocks `usePlaygroundChatSSE` wholesale, so without
 * these the request shape has no coverage at all — and a regression here would
 * silently drop `mode`, which worked before `strategy` existed, not just the new
 * field.
 */
import { describe, it, expect } from "vitest";
import { buildOptions } from "./use-playground-chat-sse";

describe("buildOptions", () => {
  it("sends includeDebugInfo:false even when nothing else is set", () => {
    // The Playground does NOT opt into the compiled SQL/SPARQL — those expose
    // internal schema and are withheld by the AppSec gate (594f9c85). Keeping
    // the flag explicitly false preserves the default-withhold posture. #1166.
    expect(buildOptions()).toEqual({ options: { includeDebugInfo: false } });
  });

  it("sends mode alongside includeDebugInfo", () => {
    expect(buildOptions("deep-reasoning")).toEqual({
      options: { mode: "deep-reasoning", includeDebugInfo: false },
    });
  });

  it("sends strategy alongside includeDebugInfo", () => {
    expect(buildOptions(undefined, "ontop")).toEqual({
      options: { strategy: "ontop", includeDebugInfo: false },
    });
  });

  it("sends mode + strategy without one clobbering the other", () => {
    // The reason this is a helper rather than two conditional spreads: two
    // spreads of `options` would leave only the second.
    expect(buildOptions("standard", "ontop_first")).toEqual({
      options: {
        mode: "standard",
        strategy: "ontop_first",
        includeDebugInfo: false,
      },
    });
  });

  it("treats an empty string as unset, still sending includeDebugInfo", () => {
    // Guards the falsy-check: `""` must not reach the API as a pin, which serve
    // would fail to match and silently resolve to its default.
    expect(buildOptions("" as never, "" as never)).toEqual({
      options: { includeDebugInfo: false },
    });
  });
});
