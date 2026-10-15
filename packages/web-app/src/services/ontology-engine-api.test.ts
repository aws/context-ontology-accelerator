// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  triggerInduction,
  getInductionJob,
  listProposals,
  getProposal,
  getProposalUploadUrls,
  updateProposal,
  acceptProposal,
  cancelProposal,
  rejectProposals,
  validateProposal,
  getProposalValidationJob,
  listOntologies,
  deleteOntology,
  searchEntities,
  getClass,
  getOntologyOverview,
  getObjectProperty,
  getDatatypeProperty,
  fetchTurtleFromUrl,
  fetchJsonFromUrl,
  putToPresignedUrl,
} from "./ontology-engine";
import type { ApiClient } from "@components/ApiClientProvider";

describe("ontology-engine API wrappers", () => {
  let api: ApiClient;
  let get: ReturnType<typeof vi.fn>;
  let post: ReturnType<typeof vi.fn>;
  let del: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    get = vi.fn().mockResolvedValue({ ok: true });
    post = vi.fn().mockResolvedValue({ ok: true });
    del = vi.fn().mockResolvedValue(undefined);
    api = { get, post, put: vi.fn(), del };
  });

  it("triggerInduction POSTs to the induce endpoint", async () => {
    await triggerInduction(api, "ns a", { some: "input" } as never);
    expect(post).toHaveBeenCalledWith("/namespaces/ns%20a/induce", {
      some: "input",
    });
  });

  it("getInductionJob GETs the job by id (encoded)", async () => {
    await getInductionJob(api, "ns", "job/1");
    expect(get).toHaveBeenCalledWith("/namespaces/ns/induce/jobs/job%2F1");
  });

  it("listProposals appends a query string from opts", async () => {
    await listProposals(api, "ns", { status: "pending", limit: 5 });
    const url = get.mock.calls[0][0] as string;
    expect(url.startsWith("/namespaces/ns/proposals")).toBe(true);
    expect(url).toContain("status=pending");
    expect(url).toContain("limit=5");
  });

  it("listProposals with no opts hits the bare endpoint", async () => {
    await listProposals(api, "ns");
    expect(get).toHaveBeenCalledWith("/namespaces/ns/proposals");
  });

  it("getProposal GETs the proposal by id", async () => {
    await getProposal(api, "ns", "p1");
    expect(get).toHaveBeenCalledWith("/namespaces/ns/proposals/p1");
  });

  it("getProposalUploadUrls POSTs to the upload-url endpoint with an empty body", async () => {
    await getProposalUploadUrls(api, "ns", "p1");
    expect(post).toHaveBeenCalledWith(
      "/namespaces/ns/proposals/p1/upload-url",
      {},
    );
  });

  it("updateProposal forwards non-null grounding and novel table fields", async () => {
    await updateProposal(api, "ns", "p1", {
      grounding_overrides: { orders: "https://schema.org/Order" },
      novel_tables: ["customers"],
    });
    expect(post).toHaveBeenCalledWith("/namespaces/ns/proposals/update", {
      proposal_id: "p1",
      grounding_overrides: { orders: "https://schema.org/Order" },
      novel_tables: ["customers"],
    });
  });

  it("acceptProposal without opts POSTs an empty body", async () => {
    await acceptProposal(api, "ns", "p1");
    expect(post).toHaveBeenCalledWith("/namespaces/ns/proposals/p1/accept", {});
  });

  it("acceptProposal with ontology_id forwards it", async () => {
    await acceptProposal(api, "ns", "p1", { ontology_id: "o1" });
    expect(post).toHaveBeenCalledWith("/namespaces/ns/proposals/p1/accept", {
      ontology_id: "o1",
    });
  });

  it("cancelProposal POSTs to the cancel endpoint", async () => {
    await cancelProposal(api, "ns", "p1");
    expect(post).toHaveBeenCalledWith(
      expect.stringContaining("/namespaces/ns/proposals/p1/cancel"),
      expect.anything(),
    );
  });

  it("rejectProposals POSTs the id list", async () => {
    await rejectProposals(api, "ns", ["p1", "p2"]);
    expect(post).toHaveBeenCalledTimes(1);
    const [, body] = post.mock.calls[0];
    expect(JSON.stringify(body)).toContain("p1");
    expect(JSON.stringify(body)).toContain("p2");
  });

  it("validateProposal + getProposalValidationJob hit validation endpoints", async () => {
    await validateProposal(api, "ns", "p1");
    expect(post).toHaveBeenCalledWith(
      expect.stringContaining("/namespaces/ns/proposals/p1/validate"),
      expect.anything(),
    );
    await getProposalValidationJob(api, "ns", "p1", "vjob");
    expect(get).toHaveBeenCalledWith(expect.stringContaining("vjob"));
  });

  it("listOntologies appends filter opts", async () => {
    await listOntologies(api, "ns", { ontology_type: "domain", limit: 10 });
    const url = get.mock.calls[0][0] as string;
    expect(url).toContain("ontology_type=domain");
    expect(url).toContain("limit=10");
  });

  // ── ListOntologies wire contract ──
  // The mirror of packages/ontology-engine/tests/unit/test_list_ontologies_wire_shape.py.
  // The endpoint used to return a bare snake_case array while Smithy declared a
  // camelCase `{ontologies: [...]}` envelope, so the client read `undefined`,
  // `?? []` swallowed it, and the UI silently showed nothing. These pin the
  // client half of that contract, using the exact JSON the backend now emits.

  it("listOntologies unwraps the {ontologies} envelope", async () => {
    get.mockResolvedValue({
      ontologies: [
        { ontologyId: "http://ex.org/o#", uri: "http://ex.org/o#" },
        { ontologyId: "http://ex.org/p#", uri: "http://ex.org/p#" },
      ],
    });
    const rows = await listOntologies(api, "ns");
    expect(rows).toHaveLength(2);
    expect(rows[0].ontologyId).toBe("http://ex.org/o#");
  });

  it("listOntologies surfaces the camelCase fields the UI depends on", async () => {
    // parseStatus drives the Ingesting/Ready/Failed indicator; deleteError tells
    // a stuck teardown from one still running. Both were absent from the old
    // contract, so both are asserted here rather than assumed.
    get.mockResolvedValue({
      ontologies: [
        {
          ontologyId: "http://ex.org/o#",
          uri: "http://ex.org/o#",
          ontologyType: "induced",
          parseStatus: "ok",
          status: "deleting",
          deleteError: "Neptune DROP timed out",
          classCount: 5,
          embeddingCount: 10,
          createdAt: "2026-07-01T00:00:00Z",
        },
      ],
    });
    const [row] = await listOntologies(api, "ns");
    expect(row.parseStatus).toBe("ok");
    expect(row.status).toBe("deleting");
    expect(row.deleteError).toBe("Neptune DROP timed out");
    expect(row.ontologyType).toBe("induced");
    expect(row.classCount).toBe(5);
    expect(row.embeddingCount).toBe(10);
    expect(row.createdAt).toBe("2026-07-01T00:00:00Z");
  });

  it("listOntologies yields [] for an empty namespace, not undefined", async () => {
    get.mockResolvedValue({ ontologies: [] });
    await expect(listOntologies(api, "ns")).resolves.toEqual([]);
  });

  it("listOntologies degrades to [] if a legacy deployment omits the envelope", async () => {
    // Guards the rollout window where the web app is ahead of the API: callers
    // get an empty list rather than a crash mid-render.
    get.mockResolvedValue([{ ontology_id: "legacy" }]);
    await expect(listOntologies(api, "ns")).resolves.toEqual([]);
  });

  it("deleteOntology DELs with the ontology_id query param (IRI-encoded)", async () => {
    await deleteOntology(api, "ns", "http://ex.org/o#Thing");
    expect(del).toHaveBeenCalledWith(
      "/namespaces/ns/ontologies?ontology_id=" +
        encodeURIComponent("http://ex.org/o#Thing"),
    );
  });

  it("searchEntities encodes the query and opts", async () => {
    get.mockResolvedValueOnce({ hits: [], total_count: 0 });
    await searchEntities(api, "ns", "cust omer", { kind: "class", limit: 3 });
    const url = get.mock.calls[0][0] as string;
    // qs() uses URLSearchParams → spaces render as "+"
    expect(url).toContain("q=cust+omer");
    expect(url).toContain("kind=class");
    expect(url).toContain("limit=3");
  });

  it("searchEntities unwraps hits from the {hits, total_count} envelope", async () => {
    const hits = [
      { uri: "http://ex/C", label: "C", kind: "class", graph_uris: ["g"] },
    ];
    get.mockResolvedValueOnce({ hits, total_count: 42 });
    const result = await searchEntities(api, "ns", "c");
    expect(result).toEqual(hits);
  });

  it("searchEntitiesPaged forwards offset and returns the full envelope", async () => {
    const { searchEntitiesPaged } = await import("./ontology-engine");
    const envelope = {
      hits: [
        { uri: "http://ex/C", label: "C", kind: "class", graph_uris: ["g"] },
      ],
      total_count: 7,
    };
    get.mockResolvedValueOnce(envelope);
    const result = await searchEntitiesPaged(api, "ns", "*", {
      kind: "class",
      limit: 100,
      offset: 100,
    });
    const url = get.mock.calls[0][0] as string;
    expect(url).toContain("offset=100");
    expect(url).toContain("limit=100");
    expect(result).toEqual(envelope);
  });

  it("getClass / getObjectProperty / getDatatypeProperty pass the uri param", async () => {
    await getClass(api, "ns", "http://ex/C");
    await getObjectProperty(api, "ns", "http://ex/p");
    await getDatatypeProperty(api, "ns", "http://ex/d");
    const urls = get.mock.calls.map((c) => c[0] as string);
    expect(urls[0]).toContain("/graph/class?");
    expect(urls[0]).toContain("uri=" + encodeURIComponent("http://ex/C"));
    expect(urls[1]).toContain("/graph/object-property?");
    expect(urls[2]).toContain("/graph/datatype-property?");
  });

  it("getOntologyOverview passes ontology_id", async () => {
    await getOntologyOverview(api, "ns", "http://ex/o");
    const url = get.mock.calls[0][0] as string;
    expect(url).toContain("/graph/ontology-overview?");
    expect(url).toContain("ontology_id=" + encodeURIComponent("http://ex/o"));
  });

  describe("direct-to-S3 helpers", () => {
    it("fetchTurtleFromUrl returns the body text on ok", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          text: () => Promise.resolve("@prefix ex: <> ."),
        }),
      );
      expect(await fetchTurtleFromUrl("https://s3/x")).toBe("@prefix ex: <> .");
    });

    it("fetchTurtleFromUrl throws on non-ok", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: false,
          status: 403,
          statusText: "Forbidden",
        }),
      );
      await expect(fetchTurtleFromUrl("https://s3/x")).rejects.toThrow(
        /403 Forbidden/,
      );
    });

    it("fetchJsonFromUrl returns the parsed body on ok", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          json: () => Promise.resolve({ matches: [{ source_table: "t" }] }),
        }),
      );
      expect(await fetchJsonFromUrl("https://s3/matches.json")).toEqual({
        matches: [{ source_table: "t" }],
      });
    });

    it("fetchJsonFromUrl throws on non-ok", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: false,
          status: 404,
          statusText: "Not Found",
        }),
      );
      await expect(fetchJsonFromUrl("https://s3/matches.json")).rejects.toThrow(
        /404 Not Found/,
      );
    });

    it("putToPresignedUrl PUTs with the signed create-only header", async () => {
      const f = vi.fn().mockResolvedValue({ ok: true });
      vi.stubGlobal("fetch", f);
      await putToPresignedUrl("https://s3/put", "body");
      expect(f).toHaveBeenCalledWith("https://s3/put", {
        method: "PUT",
        body: "body",
        headers: {
          "Content-Type": "text/turtle",
          "If-None-Match": "*",
        },
      });
    });

    it.each([409, 412])(
      "putToPresignedUrl treats %i as a failed conflicting write",
      async (status) => {
        const url = "https://s3/secret-presigned-query";
        vi.stubGlobal(
          "fetch",
          vi.fn().mockResolvedValue({
            ok: false,
            status,
            statusText: status === 409 ? "Conflict" : "Precondition Failed",
          }),
        );

        try {
          await putToPresignedUrl(url, "body");
          expect.unreachable("a conflicting write must fail");
        } catch (error: unknown) {
          expect(error).toBeInstanceOf(Error);
          if (error instanceof Error) {
            expect(error.message).toMatch(
              /revision already has an upload or the write outcome conflicted/,
            );
            expect(error.message).toContain(
              "Save again to create a fresh revision and re-upload.",
            );
            expect(error.message).not.toContain(url);
          }
        }
      },
    );

    it("putToPresignedUrl explains a 403 without leaking the URL", async () => {
      const url = "https://s3/secret-presigned-query";
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: false,
          status: 403,
          statusText: "Forbidden",
        }),
      );

      try {
        await putToPresignedUrl(url, "body");
        expect.unreachable("a forbidden write must fail");
      } catch (error: unknown) {
        expect(error).toBeInstanceOf(Error);
        if (error instanceof Error) {
          expect(error.message).toMatch(/expired or a required signed header/);
          expect(error.message).not.toContain(url);
        }
      }
    });

    it("putToPresignedUrl keeps generic handling for other failures", async () => {
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue({ ok: false, status: 500, statusText: "Err" }),
      );
      await expect(putToPresignedUrl("https://s3/put", "b")).rejects.toThrow(
        /500 Err/,
      );
    });
  });
});
