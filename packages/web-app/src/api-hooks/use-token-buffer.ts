// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Hook that encapsulates token buffering logic: accumulates streaming tokens
 * in a ref-backed Map, and flushes them to state every 75ms via dispatch.
 */
import { useCallback, useEffect, useRef } from "react";
import type { PlaygroundAction } from "./playground-reducer";

const FLUSH_INTERVAL_MS = 75;

export interface UseTokenBufferReturn {
  append: (requestId: string, text: string) => void;
  flush: (requestId: string) => string | undefined;
  clear: (requestId: string) => void;
}

export function useTokenBuffer(
  dispatch: (action: PlaygroundAction) => void,
): UseTokenBufferReturn {
  const bufferRef = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    const intervalId = setInterval(() => {
      bufferRef.current.forEach((text, requestId) => {
        if (text) {
          dispatch({ type: "TOKEN", requestId, text });
          bufferRef.current.delete(requestId);
        }
      });
    }, FLUSH_INTERVAL_MS);

    return () => {
      clearInterval(intervalId);
    };
  }, [dispatch]);

  const append = useCallback((requestId: string, text: string) => {
    bufferRef.current.set(
      requestId,
      (bufferRef.current.get(requestId) ?? "") + text,
    );
  }, []);

  const flush = useCallback((requestId: string): string | undefined => {
    const text = bufferRef.current.get(requestId);
    bufferRef.current.delete(requestId);
    return text;
  }, []);

  const clear = useCallback((requestId: string) => {
    bufferRef.current.delete(requestId);
  }, []);

  return { append, flush, clear };
}
