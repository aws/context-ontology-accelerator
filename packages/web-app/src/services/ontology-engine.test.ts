// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  uploadOntologyFile,
  downloadOntology,
  inferConstraints,
  getInferConstraintsJob,
  loadFoundationalOntology,
  getOntologyIngestStatus,
} from "./ontology-engine";
import type { ApiClient } from "@components/ApiClientProvider";

describe("uploadOntologyFile", () => {
  let mockApiClient: ApiClient;
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockApiClient = {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
      del: vi.fn(),
    };
    fetchSpy = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchSpy);
  });

  it("calls upload-url, PUTs to S3, then calls ingest-from-s3", async () => {
    (mockApiClient.post as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({
        uploadUrl: "https://s3.amazonaws.com/presigned-put",
        s3Key: "ontology-uploads/ns/abc123/test.ttl",
      })
      .mockResolvedValueOnce({ ontology_id: "http://example.org/o" });

    const file = new File(["@prefix ex: <http://ex.org/> ."], "test.ttl", {
      type: "text/turtle",
    });

    await uploadOntologyFile(
      mockApiClient,
      "ns",
      "http://example.org/o",
      "My Ontology",
      file,
      "turtle",
    );

    // Step 1: get presigned URL
    expect(mockApiClient.post).toHaveBeenCalledWith(
      "/namespaces/ns/ontologies/upload-url",
      expect.objectContaining({
        filename: expect.any(String),
        content_type: "text/turtle",
      }),
    );

    // Step 2: PUT to S3 with File object
    expect(fetchSpy).toHaveBeenCalledWith(
      "https://s3.amazonaws.com/presigned-put",
      expect.objectContaining({
        method: "PUT",
        body: file,
        headers: { "Content-Type": "text/turtle" },
      }),
    );

    // Step 3: ingest-from-s3
    expect(mockApiClient.post).toHaveBeenCalledWith(
      "/namespaces/ns/ontologies/http%3A%2F%2Fexample.org%2Fo/ingest-from-s3",
      expect.objectContaining({
        s3_key: "ontology-uploads/ns/abc123/test.ttl",
        title: "My Ontology",
        format: "turtle",
        ontology_type: "user_uploaded",
      }),
    );
  });

  it("throws when S3 PUT fails", async () => {
    (mockApiClient.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      uploadUrl: "https://s3.amazonaws.com/presigned-put",
      s3Key: "ontology-uploads/ns/abc/test.ttl",
    });
    fetchSpy.mockResolvedValueOnce({
      ok: false,
      status: 403,
      statusText: "Forbidden",
    });

    const file = new File(["content"], "test.ttl");

    await expect(
      uploadOntologyFile(mockApiClient, "ns", "id", "Title", file, "turtle"),
    ).rejects.toThrow("Failed to upload file to S3");
  });

  it("uses correct content-type for rdf+xml", async () => {
    (mockApiClient.post as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({
        uploadUrl: "https://s3.amazonaws.com/presigned",
        s3Key: "ontology-uploads/ns/abc/test.rdf",
      })
      .mockResolvedValueOnce({});
    fetchSpy.mockResolvedValueOnce({ ok: true });

    const file = new File(["<rdf/>"], "test.rdf", {
      type: "application/rdf+xml",
    });

    await uploadOntologyFile(mockApiClient, "ns", "id", "T", file, "rdf+xml");

    expect(mockApiClient.post).toHaveBeenCalledWith(
      expect.stringContaining("/upload-url"),
      expect.objectContaining({ content_type: "application/rdf+xml" }),
    );
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        headers: { "Content-Type": "application/rdf+xml" },
      }),
    );
  });
});

describe("downloadOntology", () => {
  it("gets a presigned URL from the download endpoint and fetches the Turtle from it", async () => {
    const get = vi.fn().mockResolvedValue({
      downloadUrl: "https://s3.example/presigned",
    });
    const mockApiClient: ApiClient = {
      get,
      post: vi.fn(),
      put: vi.fn(),
      del: vi.fn(),
    };
    const fetchSpy = vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => "@prefix ex: <http://e/> .",
    } as Response);

    const ttl = await downloadOntology(mockApiClient, "ns", "http://ex.org/o#");

    // Fetches the (JSON) download endpoint for a presigned URL — no inline body.
    expect(get).toHaveBeenCalledWith(
      "/namespaces/ns/ontologies/http%3A%2F%2Fex.org%2Fo%23/download",
    );
    // Then downloads the Turtle directly from the presigned URL.
    expect(fetchSpy).toHaveBeenCalledWith("https://s3.example/presigned");
    expect(ttl).toContain("@prefix");

    fetchSpy.mockRestore();
  });

  it("throws when the download response has no download URL", async () => {
    const mockApiClient: ApiClient = {
      get: vi.fn().mockResolvedValue({}),
      post: vi.fn(),
      put: vi.fn(),
      del: vi.fn(),
    };
    await expect(downloadOntology(mockApiClient, "ns", "id")).rejects.toThrow(
      "did not include a download URL",
    );
  });
});

describe("infer-constraints (async 202 + job poll)", () => {
  it("POSTs infer-constraints and returns the 202 job handle", async () => {
    const post = vi.fn().mockResolvedValue({
      job_id: "job-1",
      proposal_id: "p-1",
      status: "pending",
      created_at: "now",
      result: null,
      error: null,
    });
    const mockApiClient: ApiClient = {
      get: vi.fn(),
      post,
      put: vi.fn(),
      del: vi.fn(),
    };

    const job = await inferConstraints(mockApiClient, "ns", "p-1");

    expect(post).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p-1/infer-constraints",
      {},
    );
    expect(job.status).toBe("pending");
    expect(job.job_id).toBe("job-1");
  });

  it("polls the infer-constraints job GET by proposal + job id", async () => {
    const get = vi.fn().mockResolvedValue({
      job_id: "job-1",
      proposal_id: "p-1",
      status: "completed",
      created_at: "now",
      result: { inferred_constraints: { classes: [] } },
      error: null,
    });
    const mockApiClient: ApiClient = {
      get,
      post: vi.fn(),
      put: vi.fn(),
      del: vi.fn(),
    };

    const job = await getInferConstraintsJob(
      mockApiClient,
      "ns",
      "p-1",
      "job-1",
    );

    expect(get).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p-1/infer-constraints/jobs/job-1",
    );
    expect(job.status).toBe("completed");
    expect(job.result?.inferred_constraints).toBeDefined();
  });
});

describe("foundational load (async 202 + ingest-status poll)", () => {
  it("unwraps the 202 IngestJob keyed by the ontology URI", async () => {
    const post = vi.fn().mockResolvedValue({
      result: {
        jobId: "job-9",
        ontologyId: "https://schema.org/",
        status: "pending",
      },
    });
    const mockApiClient: ApiClient = {
      get: vi.fn(),
      post,
      put: vi.fn(),
      del: vi.fn(),
    };

    const job = await loadFoundationalOntology(
      mockApiClient,
      "ns",
      "schema-org",
    );

    expect(post).toHaveBeenCalledWith(
      "/namespaces/ns/ontology/foundational/schema-org/load",
      {},
    );
    // The service unwraps ``result`` so the caller gets the job directly.
    expect(job.jobId).toBe("job-9");
    expect(job.ontologyId).toBe("https://schema.org/");
    expect(job.status).toBe("pending");
  });

  it("polls ingest-status by URL-encoded ontology URI + job id", async () => {
    const get = vi.fn().mockResolvedValue({
      result: {
        jobId: "job-9",
        ontologyId: "https://schema.org/",
        status: "completed",
        result: { classCount: 800, embeddingCount: 800 },
      },
    });
    const mockApiClient: ApiClient = {
      get,
      post: vi.fn(),
      put: vi.fn(),
      del: vi.fn(),
    };

    const job = await getOntologyIngestStatus(
      mockApiClient,
      "ns",
      "https://schema.org/",
      "job-9",
    );

    expect(get).toHaveBeenCalledWith(
      "/namespaces/ns/ontologies/https%3A%2F%2Fschema.org%2F/ingest-status/job-9",
    );
    expect(job.status).toBe("completed");
    expect(job.result?.classCount).toBe(800);
  });
});
