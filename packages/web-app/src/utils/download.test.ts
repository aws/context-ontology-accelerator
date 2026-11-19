// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest";
import { downloadTextFile } from "./download";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("downloadTextFile", () => {
  it("triggers an anchor click with the given download name and cleans up", () => {
    const createObjectURL = vi.fn().mockReturnValue("blob:mock");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });

    const anchor = document.createElement("a");
    const clickSpy = vi.spyOn(anchor, "click").mockImplementation(() => {});
    const createElement = vi
      .spyOn(document, "createElement")
      .mockReturnValue(anchor);

    downloadTextFile("policy.shapes.ttl", "@prefix sh: <> .");

    expect(createElement).toHaveBeenCalledWith("a");
    expect(anchor.download).toBe("policy.shapes.ttl");
    expect(anchor.href).toContain("blob:mock");
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    // Object URL is revoked after the click to avoid leaking blobs.
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock");
  });

  it("defaults to the turtle mime type and honors an override", () => {
    const blobs: Blob[] = [];
    const OriginalBlob = Blob;
    vi.stubGlobal(
      "Blob",
      class extends OriginalBlob {
        constructor(parts?: BlobPart[], options?: BlobPropertyBag) {
          super(parts, options);
          blobs.push(this);
        }
      },
    );
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn().mockReturnValue("blob:mock"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(document, "createElement").mockReturnValue(
      Object.assign(document.createElement("a"), { click: () => {} }),
    );

    downloadTextFile("a.ttl", "x");
    downloadTextFile("b.json", "y", "application/json");

    expect(blobs[0].type).toBe("text/turtle");
    expect(blobs[1].type).toBe("application/json");
  });
});
