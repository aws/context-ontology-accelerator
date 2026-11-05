// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { matchesEventPattern } from "./event-pattern";

describe("matchesEventPattern", () => {
  const event = {
    source: "aws.ecs",
    detail: {
      stopCode: "EssentialContainerExited",
      containers: [
        { name: "app", exitCode: 0 },
        { name: "sidecar", exitCode: 137, reason: "OutOfMemoryError: x" },
      ],
    },
  };

  test("matches exact values, wildcards and any element of an array", () => {
    expect(
      matchesEventPattern(
        {
          source: ["aws.ecs"],
          detail: {
            stopCode: ["EssentialContainerExited"],
            containers: { reason: [{ wildcard: "*OutOfMemory*" }] },
          },
        },
        event,
      ),
    ).toBe(true);
  });

  test("needs every field and at least one $or branch to match", () => {
    expect(
      matchesEventPattern(
        { source: ["aws.ecs"], detail: { stopCode: ["UserInitiated"] } },
        event,
      ),
    ).toBe(false);
    expect(
      matchesEventPattern(
        {
          detail: {
            $or: [
              { stopCode: ["UserInitiated"] },
              { containers: { exitCode: [1] } },
            ],
          },
        },
        event,
      ),
    ).toBe(false);
    expect(
      matchesEventPattern(
        {
          detail: {
            $or: [
              { stopCode: ["UserInitiated"] },
              { containers: { exitCode: [137] } },
            ],
          },
        },
        event,
      ),
    ).toBe(true);
  });

  test("a missing field does not match", () => {
    expect(
      matchesEventPattern(
        { detail: { stoppedReason: [{ wildcard: "*" }] } },
        event,
      ),
    ).toBe(false);
  });

  test("an operator it does not evaluate throws", () => {
    expect(() =>
      matchesEventPattern(
        { detail: { stopCode: [{ "anything-but": ["UserInitiated"] }] } },
        event,
      ),
    ).toThrow("unsupported operator 'anything-but'");
    expect(() =>
      matchesEventPattern(
        { detail: { stopCode: [{ wildcards: "*" }] } },
        event,
      ),
    ).toThrow("unsupported operator 'wildcards'");
  });
});
