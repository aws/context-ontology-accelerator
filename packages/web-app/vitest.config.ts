// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@coa/shared": path.resolve(
        __dirname,
        "../../libs/ts-shared/src/index.ts",
      ),
      "@auth": path.resolve(__dirname, "src/auth"),
      "@app-types": path.resolve(__dirname, "src/app-types"),
      "@api-hooks": path.resolve(__dirname, "src/api-hooks"),
      "@components": path.resolve(__dirname, "src/components"),
      "@pages": path.resolve(__dirname, "src/pages"),
      "@utils": path.resolve(__dirname, "src/utils"),
      "@constants": path.resolve(__dirname, "src/constants"),
      "@test-fixtures": path.resolve(__dirname, "../../tests/unit/fixtures"),
    },
  },
  test: {
    globals: true,
    environment: "happy-dom",
    setupFiles: "./vitest-setup.ts",
    css: false,
    // Coverage must live here: Vitest does not merge vite.config.ts when this
    // file exists.
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary", "cobertura"],
      reportsDirectory: "coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/**/*.d.ts",
        "src/main.tsx",
        "src/**/index.ts",
        "src/App.tsx",
        "src/app-types/**",
        "src/components/Chat/index.tsx",
        "src/pages/index.ts",
        "src/utils/index.ts",
      ],
      // Floors are the measured vitest 4 values (its mandatory AST-aware v8
      // remapping lowered branch/function counts). Raise them toward 80% as
      // tests are added; never lower.
      thresholds: {
        lines: 58,
        statements: 58,
        functions: 55,
        branches: 50,
      },
    },
    // Playwright E2E specs live under tests/e2e and use their own runner; keep
    // Vitest from discovering them (they call test.describe from
    // @playwright/test, which throws under Vitest). Vitest's default excludes
    // (node_modules, dist, etc.) are inlined here since we override the option.
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      "**/cypress/**",
      "**/.{idea,git,cache,output,temp}/**",
      "**/{karma,rollup,webpack,vite,vitest,jest,ava,babel,nyc,cypress,tsup,build,eslint,prettier}.config.*",
      "tests/e2e/**",
    ],
    server: {
      deps: {
        // Bundle Cloudscape components that use ESM directory imports
        // incompatible with Node's native ESM resolution in happy-dom.
        inline: [/@cloudscape-design\/.*/],
      },
    },
  },
});
