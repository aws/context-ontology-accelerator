// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Type guards for validating SSE response payloads from AgentCore. */
import type { HistoryMessage, StreamingEvent } from "@app-types/playground";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export interface RecentSessionResponse {
  sessionId: string | null;
  namespace?: string;
  title?: string;
  messages?: HistoryMessage[];
  totalCount?: number;
  hasMore?: boolean;
  cursor?: number | null;
  requestId?: string;
  statusCode?: number;
  error?: string;
}

export function isRecentSessionResponse(
  value: unknown,
): value is RecentSessionResponse {
  if (!isRecord(value)) return false;
  // sessionId must be present (null for "no session" or string)
  if (!("sessionId" in value)) return false;
  if (value.sessionId !== null && typeof value.sessionId !== "string")
    return false;
  return true;
}

export interface SessionHistoryResponse {
  sessionId: string;
  messages: HistoryMessage[];
  hasMore: boolean;
  cursor: number | null;
  requestId?: string;
  statusCode?: number;
  error?: string;
  message?: string;
}

export function isSessionHistoryResponse(
  value: unknown,
): value is SessionHistoryResponse {
  if (!isRecord(value)) return false;
  if (value.error) return true; // Error responses are also valid
  if (typeof value.sessionId !== "string") return false;
  if (!Array.isArray(value.messages)) return false;
  if (typeof value.hasMore !== "boolean") return false;
  return true;
}

export interface CreateSessionResponse {
  action: string;
  sessionId: string;
  namespace: string;
  requestId?: string;
  statusCode?: number;
  error?: string;
}

export function isCreateSessionResponse(
  value: unknown,
): value is CreateSessionResponse {
  if (!isRecord(value)) return false;
  if (value.error) return true;
  if (typeof value.sessionId !== "string") return false;
  return true;
}

export function isStreamingEvent(value: unknown): value is StreamingEvent {
  if (!isRecord(value)) return false;
  if (typeof value.type !== "string") return false;
  if (typeof value.requestId !== "string") return false;
  const validTypes = ["step", "token", "done", "error", "rows"];
  return validTypes.includes(value.type);
}
