// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type {
  GraphContextEntity,
  GraphContextItem,
  GraphContextRelationship,
  LegacyGraphContextRelationship,
} from "@app-types/playground";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isGraphContextEntity(value: unknown): value is GraphContextEntity {
  return (
    isRecord(value) &&
    typeof value.uri === "string" &&
    typeof value.label === "string"
  );
}

function isGraphContextRelationship(
  value: unknown,
): value is GraphContextRelationship {
  return (
    isRecord(value) &&
    typeof value.sourceUri === "string" &&
    typeof value.predicateUri === "string" &&
    typeof value.targetUri === "string"
  );
}

/** Keep only string-valued fields, so no relationship field renders as a non-string child. */
function stringFields(value: object): LegacyGraphContextRelationship {
  return Object.fromEntries(
    Object.entries(value).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
}

/** Sanitize the fields the renderer reads off an entity; the guard only checks uri/label. */
function toEntity(entity: GraphContextEntity): GraphContextEntity {
  const { type, properties, relationships, ...rest } = entity;
  return {
    ...rest,
    ...(typeof type === "string" && { type }),
    ...(isRecord(properties) && { properties }),
    ...(Array.isArray(relationships) && {
      relationships: relationships.filter(isRecord).map(stringFields),
    }),
  };
}

function toRelationship(
  relationship: GraphContextRelationship,
): GraphContextRelationship {
  const { predicateLabel, ...rest } = relationship;
  return typeof predicateLabel === "string" ? relationship : rest;
}

/**
 * Normalize canonical GraphContext and the legacy top-level entity-array shape.
 *
 * `entities` and `relationships` are assigned UNCONDITIONALLY. A conditional
 * spread only overwrites the key when the field was already an array, so
 * `{ entities: "abc" }` survived `...value` untouched: `hasGraphContext` read
 * `"abc".length === 3` as truthy, `GraphContextSection` called `.map` on a string,
 * and the error boundary replaced the whole chat panel — losing the conversation
 * view over a malformed field. Making the shape safe is this function's entire
 * job, so a non-array is replaced with `[]`, never passed through.
 */
export function normalizeGraphContext(
  value: unknown,
): GraphContextItem | undefined {
  // Legacy top-level entity array: `entities` is an array by construction here, so
  // this branch was never the unsafe one.
  if (Array.isArray(value)) {
    return { entities: value.filter(isGraphContextEntity).map(toEntity) };
  }
  if (!isRecord(value)) return undefined;

  return {
    ...value,
    entities: Array.isArray(value.entities)
      ? value.entities.filter(isGraphContextEntity).map(toEntity)
      : [],
    relationships: Array.isArray(value.relationships)
      ? value.relationships
          .filter(isGraphContextRelationship)
          .map(toRelationship)
      : [],
  };
}
