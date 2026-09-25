// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { mergeOpenApiPaths } from "../../lib/utils/api-utils";

describe("mergeOpenApiPaths", () => {
  it("throws when both specs define the same path and method", () => {
    // The regression this guards: ControlPlaneService#ListMetrics and
    // DataLayerService#ServeListMetrics were both GET on
    // /namespaces/{namespaceId}/metrics. A plain spread let the overlay win, so
    // API Gateway published the overlay's parameters and response schema in
    // front of the base operation's integration, with a green build.
    const base = {
      paths: {
        "/namespaces/{namespaceId}/metrics": {
          get: { operationId: "ListMetrics" },
        },
      },
    };
    const overlay = {
      paths: {
        "/namespaces/{namespaceId}/metrics": {
          get: { operationId: "ServeListMetrics" },
        },
      },
    };

    expect(() => mergeOpenApiPaths(base, overlay)).toThrow(
      /merge conflict on "\/namespaces\/\{namespaceId\}\/metrics": \[get\]/,
    );
  });

  it("merges different methods on a shared path", () => {
    const base = {
      paths: { "/things": { post: { operationId: "CreateThing" } } },
    };
    const overlay = {
      paths: { "/things": { get: { operationId: "ListThings" } } },
    };

    const merged = mergeOpenApiPaths(base, overlay);

    expect(Object.keys(merged.paths["/things"]).sort()).toEqual([
      "get",
      "post",
    ]);
    expect(merged.paths["/things"].post.operationId).toBe("CreateThing");
    expect(merged.paths["/things"].get.operationId).toBe("ListThings");
  });

  it("adds paths the base spec does not have", () => {
    const base = { paths: { "/a": { get: { operationId: "GetA" } } } };
    const overlay = { paths: { "/b": { get: { operationId: "GetB" } } } };

    const merged = mergeOpenApiPaths(base, overlay);

    expect(Object.keys(merged.paths).sort()).toEqual(["/a", "/b"]);
  });

  it("tolerates a base spec with no paths", () => {
    const base = {};
    const overlay = { paths: { "/a": { get: { operationId: "GetA" } } } };

    expect(mergeOpenApiPaths(base, overlay).paths["/a"].get.operationId).toBe(
      "GetA",
    );
  });
});
