// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@coa/shared": path.resolve(__dirname, "../../libs/ts-shared/src/index.ts"),
      "@auth": path.resolve(__dirname, "src/auth"),
      "@app-types": path.resolve(__dirname, "src/app-types"),
      "@api-hooks": path.resolve(__dirname, "src/api-hooks"),
      "@components": path.resolve(__dirname, "src/components"),
      "@pages": path.resolve(__dirname, "src/pages"),
      "@utils": path.resolve(__dirname, "src/utils"),
      "@constants": path.resolve(__dirname, "src/constants"),
    },
  },
  server: {
    proxy: {
      "/ws": {
        target: "http://localhost:8080",
        ws: true,
      },
      "/api/ontology": {
        target: "http://localhost:8001",
        rewrite: (path) => path.replace(/^\/api\/ontology/, ""),
      },
    },
  },
  // Vitest reads vitest.config.ts, so a `test` block here is ignored.
});
