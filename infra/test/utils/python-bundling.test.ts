// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import {
  _computeSourceHash,
  _isHashLocked,
  _uvInstallCommand,
} from "../../lib/utils/python-bundling";

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

  describe("portability and build artifacts", () => {
    it("is identical for the same tree at a different checkout path", () => {
      // The hash folded in ABSOLUTE paths, so CI, a local clone, and every
      // `git worktree` disagreed on the hash for the same commit — re-bundling
      // every Python Lambda on each deploy from a different path.
      const a = srcDir("checkoutA/pkg/src", { "mod.py": "x = 1" });
      const b = srcDir("checkoutB/pkg/src", { "mod.py": "x = 1" });

      expect(_computeSourceHash({ srcDirs: [a] })).toEqual(
        _computeSourceHash({ srcDirs: [b] }),
      );
    });

    it("still changes when a file is renamed", () => {
      // Portability must not cost invalidation: the relative path is still hashed.
      const dir = srcDir("a", { "mod.py": "x = 1" });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.renameSync(path.join(dir, "mod.py"), path.join(dir, "renamed.py"));

      expect(_computeSourceHash({ srcDirs: [dir] })).not.toEqual(before);
    });

    it("ignores *.egg-info, so a setuptools build does not change the hash", () => {
      // packages/ontology-engine/src carries one; it is untracked and regenerated
      // by tooling, so hashing it made the same commit hash differently in CI.
      const dir = srcDir("a", {
        "mod.py": "x = 1",
        "pkg.egg-info/SOURCES.txt": "mod.py",
      });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.writeFileSync(
        path.join(dir, "pkg.egg-info/SOURCES.txt"),
        "mod.py\nother.py",
      );

      expect(_computeSourceHash({ srcDirs: [dir] })).toEqual(before);
    });

    it("ignores tool caches", () => {
      const dir = srcDir("a", {
        "mod.py": "x = 1",
        ".mypy_cache/x.json": "{}",
        "__pycache__/mod.cpython-312.pyc": "bytecode",
      });
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.writeFileSync(path.join(dir, ".mypy_cache/x.json"), '{"a":1}');

      expect(_computeSourceHash({ srcDirs: [dir] })).toEqual(before);
    });

    it("tracks a symlinked file's target content", () => {
      // `cp -r ${d}/*` expands the glob, so a top-level symlink is a command-line
      // argument and cp DEREFERENCES it: the target content ships in the bundle.
      // isFile() is false for symlinks, so that content was previously unhashed.
      const dir = srcDir("a", { "mod.py": "x = 1" });
      const target = path.join(root, "outside.txt");
      fs.writeFileSync(target, "v1");
      fs.symlinkSync(target, path.join(dir, "linked.txt"));
      const before = _computeSourceHash({ srcDirs: [dir] });

      fs.writeFileSync(target, "v2");

      expect(_computeSourceHash({ srcDirs: [dir] })).not.toEqual(before);
    });

    it("does not throw on a dangling symlink", () => {
      const dir = srcDir("a", { "mod.py": "x = 1" });
      fs.symlinkSync(path.join(root, "nope.txt"), path.join(dir, "dead.txt"));

      expect(() => _computeSourceHash({ srcDirs: [dir] })).not.toThrow();
    });
  });

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

/**
 * The local bundle path must NOT use the host `pip`/`pip3`: installing a newer
 * Python on distros like Amazon Linux 2023 does not repoint the default
 * `python3` symlink, so the host pip targets the wrong interpreter.
 * uv is a standalone binary that cross-resolves for the 3.12 Lambda target
 * regardless of the host Python.
 */
describe("_uvInstallCommand (local install uses uv, not host pip)", () => {
  it("uses uv and never the host pip/pip3", () => {
    const cmd = _uvInstallCommand({ srcDirs: [], pipDeps: ["structlog"] });
    expect(cmd).not.toBeNull();
    expect(cmd!).toMatch(/^uv pip install /);
    // Guard against a regression to a bare pip/pip3 at a command boundary
    // (start of string or after `&&`). The `pip` inside `uv pip install` is
    // fine — it is preceded by `uv `, not a boundary.
    expect(cmd!).not.toMatch(/(^|&&\s*)pip3? install/);
  });

  it("cross-targets the 3.12 Lambda runtime with wheels only", () => {
    const cmd = _uvInstallCommand({ srcDirs: [], pipDeps: ["structlog"] })!;
    expect(cmd).toContain("--python-version 3.12");
    expect(cmd).toContain("--python-platform x86_64-manylinux2014");
    expect(cmd).toContain("--only-binary :all:");
    expect(cmd).toContain("--target /asset-output");
  });

  it("selects the aarch64 platform tag for arm64", () => {
    const cmd = _uvInstallCommand({
      srcDirs: [],
      pipDeps: ["structlog"],
      architecture: "arm64",
    })!;
    expect(cmd).toContain("--python-platform aarch64-manylinux2014");
    expect(cmd).not.toContain("x86_64-manylinux2014");
  });

  it("installs from a requirements file when provided", () => {
    const cmd = _uvInstallCommand({
      srcDirs: [],
      requirementsFile: "/repo/packages/x/requirements.txt",
    })!;
    expect(cmd).toContain("-r /repo/packages/x/requirements.txt");
    // A plain requirements file is not hash-checked.
    expect(cmd).not.toContain("--require-hashes");
    expect(cmd).not.toContain("--no-config");
  });

  it("installs a lock with --require-hashes and without the workspace config", () => {
    const cmd = _uvInstallCommand({
      srcDirs: [],
      requirementsFile: "/repo/packages/x/requirements.lock.txt",
      architecture: "arm64",
    })!;
    expect(cmd).toMatch(/^uv pip install --no-config --require-hashes /);
    expect(cmd).toContain("-r /repo/packages/x/requirements.lock.txt");
    expect(cmd).toContain("--python-platform aarch64-manylinux2014");
  });

  it("quotes each explicit dependency (version specifiers are shell-safe)", () => {
    const cmd = _uvInstallCommand({
      srcDirs: [],
      pipDeps: ["pydantic>=2", "structlog"],
    })!;
    expect(cmd).toContain("'pydantic>=2'");
    expect(cmd).toContain("'structlog'");
  });

  it("returns null when there are no dependencies to install", () => {
    expect(_uvInstallCommand({ srcDirs: [] })).toBeNull();
  });
});

describe("hash-locked requirements", () => {
  const repoRoot = path.resolve(__dirname, "../../..");
  const lock = path.join(repoRoot, "packages/sources/requirements.lock.txt");

  it("recognizes a lock by its file name only", () => {
    expect(_isHashLocked("/r/packages/sources/requirements.lock.txt")).toBe(
      true,
    );
    expect(_isHashLocked("/r/packages/sources/requirements.txt")).toBe(false);
  });

  it("pins every requirement of the sources lock with a hash", () => {
    // --require-hashes refuses the whole install if one entry is unpinned or
    // unhashed, so a hand edit that breaks this would fail every deploy.
    const text = fs.readFileSync(lock, "utf-8");
    const entries = text
      .split(/\n(?=[A-Za-z0-9])/)
      .map((block) => block.trim())
      .filter((block) => block && !block.startsWith("#"));
    expect(entries.length).toBeGreaterThan(0);
    for (const entry of entries) {
      expect(entry).toMatch(/^[A-Za-z0-9_.-]+==[^\s\\]+ \\/);
      expect(entry).toMatch(/--hash=sha256:[0-9a-f]{64}/);
    }
  });

  it("keeps every direct dependency of requirements.txt in the lock", () => {
    const direct = fs
      .readFileSync(
        path.join(repoRoot, "packages/sources/requirements.txt"),
        "utf-8",
      )
      .split("\n")
      .map((line) => line.replace(/#.*/, "").trim())
      .filter(Boolean)
      .map((line) =>
        line
          .split(/[<>=!~;\s[]/)[0]
          .toLowerCase()
          .replace(/_/g, "-"),
      );
    const pinned = new Set(
      [
        ...fs.readFileSync(lock, "utf-8").matchAll(/^([A-Za-z0-9_.-]+)==/gm),
      ].map((m) => m[1].toLowerCase().replace(/_/g, "-")),
    );
    for (const name of direct) {
      expect(pinned.has(name)).toBe(true);
    }
  });
});
