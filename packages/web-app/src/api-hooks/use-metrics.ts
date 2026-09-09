// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import {
  useQuery,
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { useApiClient } from "@components/ApiClientProvider";
import type {
  MetricDefinition as _MetricDefinition,
  MetricDialect as _MetricDialect,
  MetricAiContext as _MetricAiContext,
  CreateMetricInput as _CreateMetricInput,
  CreateMetricOutput as _CreateMetricOutput,
  ListMetricsOutput as _ListMetricsOutput,
  ImportOsiOutput as _ImportOsiOutput,
  ExportOsiOutput as _ExportOsiOutput,
} from "@coa/control-plane-client";

// Re-export Smithy types with required fields narrowed for UI usage.
// The generated types mark @required output fields as T | undefined
// because the OpenAPI generator is conservative. We know the API always
// returns these fields, so we narrow them here for ergonomic UI code.

export type MetricDialect = Required<_MetricDialect>;
export type MetricAiContext = _MetricAiContext;

export interface MetricDefinition extends Required<
  Pick<
    _MetricDefinition,
    "name" | "description" | "expression" | "dataSourceId" | "sourceTable"
  >
> {
  defaultTimeGrain?: string;
  unit?: string;
  returnType?: string;
  aiContext?: MetricAiContext;
  ontologyConcepts?: string[];
  definedBy?: string;
  effectiveFrom?: string;
}

/** Input for create/update — excludes namespaceId (passed via URL). */
export type CreateMetricInput = Omit<_CreateMetricInput, "namespaceId">;

export interface CreateMetricOutput {
  metric: MetricDefinition;
  warnings?: Array<{ field: string; message: string; severity: string }>;
}

export interface ListMetricsOutput {
  metrics: MetricDefinition[];
  nextToken?: string;
}

export interface ImportOsiOutput {
  datasetsResolved: number;
  metricsCreated: number;
  metricsUpdated: number;
  warnings?: string[];
  jobId?: string;
  status?: "COMPLETED" | "IN_PROGRESS" | "FAILED";
}

export interface ImportJobOutput {
  jobId: string;
  status: "IN_PROGRESS" | "COMPLETED" | "FAILED";
  metricsTotal: number;
  metricsProcessed: number;
  metricsCreated: number;
  metricsUpdated: number;
  errors?: string[];
  warnings?: string[];
}

export interface ExportOsiOutput {
  content: string;
}

export function useListMetrics(namespaceId: string, maxResults = 50) {
  const client = useApiClient();

  return useInfiniteQuery<ListMetricsOutput, Error>({
    queryKey: ["metrics", namespaceId],
    queryFn: async ({ pageParam }) => {
      const params = new URLSearchParams();
      params.set("maxResults", String(maxResults));
      if (pageParam) params.set("nextToken", pageParam as string);
      return client.get<ListMetricsOutput>(
        `/namespaces/${namespaceId}/metrics?${params.toString()}`,
      );
    },
    getNextPageParam: (lastPage) => lastPage.nextToken,
    initialPageParam: undefined as string | undefined,
    enabled: !!namespaceId,
  });
}

export function useGetMetric(namespaceId: string, name: string) {
  const client = useApiClient();

  return useQuery<{ metric: MetricDefinition }, Error>({
    queryKey: ["metrics", namespaceId, name],
    queryFn: () =>
      client.get<{ metric: MetricDefinition }>(
        `/namespaces/${namespaceId}/metrics/${name}`,
      ),
    enabled: !!namespaceId && !!name,
  });
}

export function useCreateMetric(namespaceId: string) {
  const client = useApiClient();
  const queryClient = useQueryClient();

  return useMutation<CreateMetricOutput, Error, CreateMetricInput>({
    mutationFn: (input) =>
      client.post<CreateMetricOutput>(
        `/namespaces/${namespaceId}/metrics`,
        input,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["metrics", namespaceId] });
    },
  });
}

export function useUpdateMetric(namespaceId: string, name: string) {
  const client = useApiClient();
  const queryClient = useQueryClient();

  return useMutation<CreateMetricOutput, Error, CreateMetricInput>({
    mutationFn: (input) =>
      client.put<CreateMetricOutput>(
        `/namespaces/${namespaceId}/metrics/${name}`,
        input,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["metrics", namespaceId] });
      queryClient.invalidateQueries({
        queryKey: ["metrics", namespaceId, name],
      });
    },
  });
}

export function useDeleteMetric(namespaceId: string) {
  const client = useApiClient();
  const queryClient = useQueryClient();

  return useMutation<void, Error, string>({
    mutationFn: (metricName: string) =>
      client.del<void>(`/namespaces/${namespaceId}/metrics/${metricName}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["metrics", namespaceId] });
    },
  });
}

export interface BulkDeleteOutput {
  deleted: number;
  notFound?: string[];
}

export function useBulkDeleteMetrics(namespaceId: string) {
  const client = useApiClient();
  const queryClient = useQueryClient();

  return useMutation<
    BulkDeleteOutput,
    Error,
    {
      names?: string[];
      filter?: {
        dataSourceId?: string;
        sourceTable?: string;
        namePrefix?: string;
      };
    }
  >({
    mutationFn: (input) =>
      client.post<BulkDeleteOutput>(
        `/namespaces/${namespaceId}/bulk-delete-metrics`,
        input,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["metrics", namespaceId] });
    },
  });
}

export function useImportOsi(namespaceId: string) {
  const client = useApiClient();
  const queryClient = useQueryClient();

  return useMutation<ImportOsiOutput, Error, string>({
    mutationFn: (content: string) =>
      client.post<ImportOsiOutput>(`/namespaces/${namespaceId}/import-osi`, {
        content,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["metrics", namespaceId] });
    },
  });
}

/** Upload file to S3 via pre-signed URL, then call import with s3Key. */
export function useImportOsiViaS3(namespaceId: string) {
  const client = useApiClient();
  const queryClient = useQueryClient();

  return useMutation<ImportOsiOutput, Error, File>({
    mutationFn: async (file: File) => {
      // Step 1: Get pre-signed upload URL
      const { uploadUrl, s3Key } = await client.post<{
        uploadUrl: string;
        s3Key: string;
      }>(`/namespaces/${namespaceId}/import-osi/upload-url`, {
        filename: file.name,
      });

      // Step 2: Upload file to S3
      const uploadResponse = await fetch(uploadUrl, {
        method: "PUT",
        body: file,
        headers: { "Content-Type": "application/x-yaml" },
      });
      if (!uploadResponse.ok) {
        throw new Error(`S3 upload failed: ${uploadResponse.status}`);
      }

      // Step 3: Call import with s3Key
      return client.post<ImportOsiOutput>(
        `/namespaces/${namespaceId}/import-osi`,
        { s3Key },
      );
    },
    onSuccess: (result) => {
      if (result.status === "COMPLETED") {
        queryClient.invalidateQueries({ queryKey: ["metrics", namespaceId] });
      }
    },
  });
}

/** Import an OSI file through the pre-signed S3 upload flow.
 *
 *  Always routes via S3 (upload-url → browser PUT → import by `s3Key`), never an
 *  inline `{content}` POST. The inline path went through API Gateway, whose WAF
 *  (`AWSManagedRulesCommonRuleSet`, `SizeRestrictions_BODY`) rejects any request
 *  body over 8 KB — so a typical metrics YAML (~10-15 KB) failed with 403
 *  (issue 103). The S3 object bypasses the WAF; only a small `{s3Key}` reaches
 *  the API.
 *
 *  Requires the browser→S3 PUT to be permitted: the OSI bucket's CORS allows it
 *  and the SPA's CSP `connect-src` includes the region's S3 origin (see
 *  `public-ui-construct.ts`). The backend still accepts an inline `{content}`
 *  body for non-UI callers under the 8 KB limit — only the UI stopped using it. */
export function useImportOsiFile(namespaceId: string) {
  const client = useApiClient();
  const queryClient = useQueryClient();

  return useMutation<ImportOsiOutput, Error, File>({
    mutationFn: async (file: File) => {
      const { uploadUrl, s3Key } = await client.post<{
        uploadUrl: string;
        s3Key: string;
      }>(`/namespaces/${namespaceId}/import-osi/upload-url`, {
        filename: file.name,
      });
      const uploadResponse = await fetch(uploadUrl, {
        method: "PUT",
        body: file,
        headers: { "Content-Type": "application/x-yaml" },
      });
      if (!uploadResponse.ok) {
        throw new Error(`S3 upload failed: ${uploadResponse.status}`);
      }
      return client.post<ImportOsiOutput>(
        `/namespaces/${namespaceId}/import-osi`,
        { s3Key },
      );
    },
    onSuccess: (result) => {
      if (result.status === "COMPLETED") {
        queryClient.invalidateQueries({ queryKey: ["metrics", namespaceId] });
      }
    },
  });
}

/** Poll an import job until it completes. */
export function useGetImportJob(namespaceId: string, jobId: string | null) {
  const client = useApiClient();
  const queryClient = useQueryClient();
  const hasInvalidated = React.useRef(false);

  // Reset ref when jobId changes
  React.useEffect(() => {
    hasInvalidated.current = false;
  }, [jobId]);

  return useQuery<ImportJobOutput, Error>({
    queryKey: ["importJob", namespaceId, jobId],
    queryFn: () =>
      client.get<ImportJobOutput>(
        `/namespaces/${namespaceId}/import-jobs/${jobId}`,
      ),
    enabled: !!namespaceId && !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "IN_PROGRESS") return 5000;
      // Job finished — invalidate metrics list once
      if (
        (status === "COMPLETED" || status === "FAILED") &&
        !hasInvalidated.current
      ) {
        hasInvalidated.current = true;
        queryClient.invalidateQueries({ queryKey: ["metrics", namespaceId] });
      }
      return false;
    },
  });
}

export interface ExportOsiInput {
  /** Optional subset of metric names to export. Omit to export every metric
   *  in the namespace (default, unchanged behavior). */
  names?: string[];
}

export function useExportOsi(namespaceId: string) {
  const client = useApiClient();

  return useMutation<ExportOsiOutput, Error, ExportOsiInput | void>({
    mutationFn: (input) => {
      const params = new URLSearchParams();
      for (const name of input?.names ?? []) {
        params.append("names", name);
      }
      const query = params.toString();
      return client.get<ExportOsiOutput>(
        `/namespaces/${namespaceId}/export-osi${query ? `?${query}` : ""}`,
      );
    },
  });
}
