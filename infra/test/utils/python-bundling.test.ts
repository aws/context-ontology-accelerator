// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { _computeSourceHash } from "../../lib/utils/python-bundling";

/**
 * The asset hash is what CDK uses to decide whether to re-bundle. Any input that
 * changes the produced bundle but NOT the hash means a stale cached asset is
 * silently reused — the Lambda keeps running old code or old dependencies with no
 * error at deploy time.
 */
describe("bundlePython asset hash", () => {
  let root: string;

  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), "coa-bundle-hash-"));
  });

  afterEach(() => {
    fs.rmSync(root, { recursive: true, force: true });
  });

  /** Create a src dir with the given relative files, return its absolute path. */
  function srcDir(name: string, files: Record<string, string>): string {
    const dir = path.join(root, name);
    for (const [rel, content] of Object.entries(files)) {
      const full = path.join(dir, rel);
      fs.mkdirSync(path.dirname(full), { recursive: true });
      fs.writeFileSync(full, content);
    }
    fs.mkdirSync(dir, { recursive: true });
    return dir;
  }

  describe("source files", () => {
    it("changes when a .py file changes", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.writeFileSync(path.join(dir, "mod.py"), "x = 2");

      expect(_computeSourceHash({ srcDirs: [dir] })).not.toEqual(before);
    });

    it("changes when a NON-.py file changes", () => {
      // The bundle copies everything (`cp -r ${d}/*`), so a schema/JSON file
      // shipped alongside the code is part of the output.
      const dir = srcDir("a", { "mod.py": "x = 1", "schema.json": '{"v":1}' });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.writeFileSync(path.join(dir, "schema.json"), '{"v":2}');

      expect(_computeSourceHash({ srcDirs: [dir] })).not.toEqual(before);
    });

    it("changes when a nested file changes", () => {
      const dir = srcDir("a", { "pkg/sub/mod.py": "x = 1" });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.writeFileSync(path.join(dir, "pkg/sub/mod.py"), "x = 2");

      expect(_computeSourceHash({ srcDirs: [dir] })).not.toEqual(before);
    });

    it("changes when a file is renamed but content is identical", () => {
      const dir = srcDir("a", { "old.py": "x = 1" });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.renameSync(path.join(dir, "old.py"), path.join(dir, "new.py"));

      expect(_computeSourceHash({ srcDirs: [dir] })).not.toEqual(before);
    });

    it("is stable across calls with no changes", () => {
      const dir = srcDir("a", { "mod.py": "x = 1", "data.json": "{}" });

      expect(_computeSourceHash({ srcDirs: [dir] })).toEqual(
        _computeSourceHash({ srcDirs: [dir] }),
      );
    });

    it("ignores __pycache__ so a prior test run cannot change the hash", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.mkdirSync(path.join(dir, "__pycache__"), { recursive: true });
      fs.writeFileSync(
        path.join(dir, "__pycache__/mod.cpython-312.pyc"),
        "\x00",
      );

      expect(_computeSourceHash({ srcDirs: [dir] })).toEqual(before);
    });

    it("handles a path containing spaces", () => {
      // The old shell pipeline was unquoted and failure-suppressed (`|| true`),
      // so a path with a space degraded every hash to the empty-input hash —
      // permanently disabling invalidation.
      const dir = srcDir("dir with space", { "mod.py": "x = 1" });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.writeFileSync(path.join(dir, "mod.py"), "x = 2");
      const after = _computeSourceHash({ srcDirs: [dir] });

      expect(after).not.toEqual(before);
      expect(before).not.toEqual(_computeSourceHash({ srcDirs: [] }));
    });

    it("distinguishes a missing src dir from an empty one", () => {
      const missing = path.join(root, "not-created");
      const empty = srcDir("empty", {});

      expect(_computeSourceHash({ srcDirs: [missing] })).not.toEqual(
        _computeSourceHash({ srcDirs: [empty] }),
      );
    });
  });

  describe("pipDeps", () => {
    it("changes when a dependency is added", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });
      const before = _computeSourceHash({
        srcDirs: [dir],
        pipDeps: ["pydantic"],
      });

      const after = _computeSourceHash({
        srcDirs: [dir],
        pipDeps: ["pydantic", "structlog"],
      });

      expect(after).not.toEqual(before);
    });

    it("changes when a dependency is removed", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });
      const before = _computeSourceHash({
        srcDirs: [dir],
        pipDeps: ["pydantic", "structlog"],
      });

      expect(
        _computeSourceHash({ srcDirs: [dir], pipDeps: ["pydantic"] }),
      ).not.toEqual(before);
    });

    it("changes when a dependency is re-pinned", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });
      const before = _computeSourceHash({
        srcDirs: [dir],
        pipDeps: ["pydantic==2.1.0"],
      });

      expect(
        _computeSourceHash({ srcDirs: [dir], pipDeps: ["pydantic==2.2.0"] }),
      ).not.toEqual(before);
    });

    it("does NOT change when only the order differs", () => {
      // Order does not change the installed set, so it must not cause a re-bundle.
      const dir = srcDir("a", { "mod.py": "x = 1" });

      expect(
        _computeSourceHash({ srcDirs: [dir], pipDeps: ["a", "b"] }),
      ).toEqual(_computeSourceHash({ srcDirs: [dir], pipDeps: ["b", "a"] }));
    });
  });

  describe("architecture", () => {
    it("changes between x86_64 and arm64", () => {
      // Selects the pip --platform tag, and therefore which binary wheels are in
      // the bundle. Reusing an asset across architectures ships wrong wheels.
      const dir = srcDir("a", { "mod.py": "x = 1" });

      expect(
        _computeSourceHash({ srcDirs: [dir], architecture: "arm64" }),
      ).not.toEqual(
        _computeSourceHash({ srcDirs: [dir], architecture: "x86_64" }),
      );
    });

    it("treats an omitted architecture as the x86_64 default", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });

      expect(_computeSourceHash({ srcDirs: [dir] })).toEqual(
        _computeSourceHash({ srcDirs: [dir], architecture: "x86_64" }),
      );
    });
  });

  describe("requirementsFile", () => {
    it("changes when the requirements content changes", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });
      const req = path.join(root, "requirements.txt");
      fs.writeFileSync(req, "pydantic==2.1.0\n");
      const before = _computeSourceHash({
        srcDirs: [dir],
        requirementsFile: req,
      });

      fs.writeFileSync(req, "pydantic==2.2.0\n");

      expect(
        _computeSourceHash({ srcDirs: [dir], requirementsFile: req }),
      ).not.toEqual(before);
    });

    it("distinguishes a missing requirements file from none at all", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });

      expect(
        _computeSourceHash({
          srcDirs: [dir],
          requirementsFile: path.join(root, "absent.txt"),
        }),
      ).not.toEqual(_computeSourceHash({ srcDirs: [dir] }));
    });
  });
});
