// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, {
  createContext,
  PropsWithChildren,
  useContext,
  useMemo,
} from "react";
import { RuntimeConfigContext } from "../RuntimeContext";
import { OIDCProvider } from "@auth";

export interface ApiClient {
  get: <T>(path: string) => Promise<T>;
  post: <T>(path: string, body: unknown) => Promise<T>;
  put: <T>(path: string, body: unknown) => Promise<T>;
  del: <T>(path: string) => Promise<T>;
  /** Fetch a non-JSON `text/*` response body as a raw string (e.g. an
   *  ontology exported as `text/turtle` from `/download`). Optional so the
   *  many JSON-only mock clients in tests stay valid. */
  getText?: (path: string) => Promise<string>;
}

/** Structured API error with status code and optional server-provided details. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const ApiClientContext = createContext<ApiClient | undefined>(undefined);

export const useApiClient = (): ApiClient => {
  const ctx = useContext(ApiClientContext);
  if (!ctx)
    throw new Error("useApiClient must be used within ApiClientProvider");
  return ctx;
};

export const ApiClientProvider: React.FC<PropsWithChildren> = ({
  children,
}) => {
  const runtimeContext = useContext(RuntimeConfigContext);

  const client = useMemo<ApiClient | undefined>(() => {
    if (!runtimeContext?.apiEndpoint || !runtimeContext?.oidcConfig)
      return undefined;
    const baseUrl = runtimeContext.apiEndpoint.replace(/\/$/, "");
    const provider = OIDCProvider.build(runtimeContext.oidcConfig);

    const request = async <T,>(
      method: string,
      path: string,
      body?: unknown,
    ): Promise<T> => {
      const token = await provider.getIdToken();
      const headers: Record<string, string> = {
        Authorization: `Bearer ${token}`,
      };
      if (body) {
        headers["Content-Type"] = "application/json";
      }
      const res = await fetch(`${baseUrl}${path}`, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        const rawFallback = `API ${method} ${path} failed (${res.status}): ${text}`;
        // Undefined until the body supplies them, so the 403 default below
        // applies when the server said nothing.
        let code: string | undefined;
        let message: string | undefined;

        try {
          const parsed = JSON.parse(text);
          code = parsed.code ?? code;
          // Smithy-modeled errors carry `message`; FastAPI uses `detail`. A
          // non-string `detail` keeps the fallback.
          const detail =
            typeof parsed.detail === "string" ? parsed.detail : undefined;
          message = parsed.message ?? detail;
          // Fold string entries of a structured `errors` array (e.g. OSI
          // import parse errors) into the message.
          if (Array.isArray(parsed.errors)) {
            const details = parsed.errors.filter(
              (e: unknown): e is string => typeof e === "string",
            );
            if (details.length > 0) {
              message = `${message ?? rawFallback}: ${details.join("; ")}`;
            }
          }
        } catch {
          // not JSON, use defaults
        }

        if (res.status === 403) {
          throw new ApiError(
            403,
            code ?? "FORBIDDEN",
            message ?? "You are not authorized to perform this action.",
          );
        }

        throw new ApiError(
          res.status,
          code ?? "UNKNOWN_ERROR",
          message ?? rawFallback,
        );
      }
      if (res.status === 204 || res.headers.get("content-length") === "0") {
        return undefined as T;
      }
      return res.json();
    };

    return {
      get: <T,>(path: string) => request<T>("GET", path),
      post: <T,>(path: string, body: unknown) => request<T>("POST", path, body),
      put: <T,>(path: string, body: unknown) => request<T>("PUT", path, body),
      del: <T,>(path: string) => request<T>("DELETE", path),
      getText: async (path: string): Promise<string> => {
        const token = await provider.getIdToken();
        const res = await fetch(`${baseUrl}${path}`, {
          method: "GET",
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          let code = "UNKNOWN_ERROR";
          let message = `API GET ${path} failed (${res.status}): ${text}`;
          try {
            const parsed = JSON.parse(text);
            code = parsed.code ?? code;
            // Same two envelopes as `request`; see the comment there.
            const detail =
              typeof parsed.detail === "string" ? parsed.detail : undefined;
            message = parsed.message ?? detail ?? message;
          } catch {
            // not JSON, use defaults
          }
          throw new ApiError(res.status, code, message);
        }
        return res.text();
      },
    };
  }, [runtimeContext]);

  return (
    <ApiClientContext.Provider value={client}>
      {children}
    </ApiClientContext.Provider>
  );
};
