// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Evaluates a rendered EventBridge event pattern against a sample event, for the
// subset of the pattern language the stacks use: exact values, `wildcard` and
// `$or`. Arrays in the event are flattened the way EventBridge flattens them, so
// a field pattern matches when any element matches. Any other operator throws
// rather than being evaluated wrongly. An escaped `\*` is not supported.

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function wildcardRegExp(glob: string): RegExp {
  const body = glob
    .split("*")
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${body}$`, "s");
}

function isCloudFormationToken(value: Record<string, unknown>): boolean {
  const keys = Object.keys(value);
  return keys.length === 1 && (keys[0] === "Ref" || keys[0].startsWith("Fn::"));
}

function matchesValue(matcher: unknown, value: unknown): boolean {
  if (isRecord(matcher) && !isCloudFormationToken(matcher)) {
    const operators = Object.keys(matcher);
    if (operators.length !== 1 || operators[0] !== "wildcard") {
      throw new Error(
        `matchesEventPattern: unsupported operator '${operators.join(",")}'`,
      );
    }
    return (
      typeof matcher.wildcard === "string" &&
      typeof value === "string" &&
      wildcardRegExp(matcher.wildcard).test(value)
    );
  }
  // Exact values, and CloudFormation tokens such as { "Fn::GetAtt": [...] }.
  return JSON.stringify(matcher) === JSON.stringify(value);
}

function fieldValues(nodes: readonly unknown[], key: string): unknown[] {
  return nodes.flatMap((node) => {
    if (!isRecord(node) || node[key] === undefined) return [];
    const value = node[key];
    return Array.isArray(value) ? value : [value];
  });
}

function matchesNodes(
  pattern: Record<string, unknown>,
  nodes: readonly unknown[],
): boolean {
  return Object.entries(pattern).every(([key, sub]) => {
    if (key === "$or") {
      return (
        Array.isArray(sub) &&
        sub.some((branch) => isRecord(branch) && matchesNodes(branch, nodes))
      );
    }
    const values = fieldValues(nodes, key);
    if (Array.isArray(sub)) {
      return values.some((value) => sub.some((m) => matchesValue(m, value)));
    }
    if (isRecord(sub)) return matchesNodes(sub, values);
    throw new Error(
      `matchesEventPattern: the pattern for '${key}' must be an array or an object`,
    );
  });
}

export function matchesEventPattern(pattern: unknown, event: unknown): boolean {
  if (!isRecord(pattern)) {
    throw new Error("matchesEventPattern: the pattern must be an object");
  }
  return matchesNodes(pattern, [event]);
}
