// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { fromRoot } from "../../lib/paths";
import {
  _computeSourceHash,
  _isHashLocked,
  _uvInstallCommand,
  type PythonBundlingOptions,
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
    const cmd = _uvInstallCommand({
      srcDirs: [],
      requirementsFile: "/repo/packages/x/requirements.txt",
    });
    expect(cmd).not.toBeNull();
    expect(cmd!).toMatch(/^uv pip install /);
    // Guard against a regression to a bare pip/pip3 at a command boundary
    // (start of string or after `&&`). The `pip` inside `uv pip install` is
    // fine — it is preceded by `uv `, not a boundary.
    expect(cmd!).not.toMatch(/(^|&&\s*)pip3? install/);
  });

  it("cross-targets the 3.12 Lambda runtime with wheels only", () => {
    const cmd = _uvInstallCommand({
      srcDirs: [],
      requirementsFile: "/repo/packages/x/requirements.txt",
    })!;
    expect(cmd).toContain("--python-version 3.12");
    expect(cmd).toContain("--python-platform x86_64-manylinux2014");
    expect(cmd).toContain("--only-binary :all:");
    expect(cmd).toContain("--target /asset-output");
  });

  it("selects the aarch64 platform tag for arm64", () => {
    const cmd = _uvInstallCommand({
      srcDirs: [],
      requirementsFile: "/repo/packages/x/requirements.txt",
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

  it("returns null when there are no dependencies to install", () => {
    expect(_uvInstallCommand({ srcDirs: [] })).toBeNull();
  });
});

/**
 * A bundle installs its requirements without botocore (the Lambda
 * runtime provides it), so the resolver never sees botocore's own bounds. On
 * 2026-10-22 that put python-dateutil 3.0.0 in four bundles while the runtime's
 * botocore requires python-dateutil>=2.1,<3.0.0; the bundle precedes the runtime
 * on sys.path, so botocore would have imported the out-of-range copy.
 */
describe("Lambda runtime constraints", () => {
  const CONSTRAINTS = fromRoot(
    "infra/lib/utils/lambda-runtime-constraints.txt",
  );
  let root: string;

  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), "coa-bundle-constraints-"));
  });

  afterEach(() => {
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("bounds python-dateutil below 3, as the runtime's botocore requires", () => {
    expect(fs.existsSync(CONSTRAINTS)).toBe(true);
    const specs = fs
      .readFileSync(CONSTRAINTS, "utf8")
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#"));
    expect(specs).toContain("python-dateutil>=2.1,<3.0.0");
  });

  it("passes the constraints to the local uv install of a requirements file", () => {
    expect(
      _uvInstallCommand({
        srcDirs: [],
        requirementsFile: "/repo/pkg/requirements.txt",
      }),
    ).toContain(`--constraint ${CONSTRAINTS}`);
  });

  it("passes the constraints to the Docker fallback's pip install", () => {
    const bundling = require("../../lib/utils/python-bundling");
    expect(typeof bundling._pipInstallCommand).toBe("function");
    expect(
      bundling._pipInstallCommand({
        srcDirs: [],
        requirementsFile: "/repo/pkg/requirements.txt",
      }),
    ).toContain(`-c ${CONSTRAINTS}`);
  });

  it("installs a hash-pinned lock without the range constraints, which hash mode rejects", () => {
    const bundling = require("../../lib/utils/python-bundling");
    const opts = {
      srcDirs: [],
      requirementsFile: "/repo/pkg/requirements.lock.txt",
    };
    const uv = _uvInstallCommand(opts);
    const pip = bundling._pipInstallCommand(opts);
    expect(uv).toContain("--require-hashes");
    expect(pip).toContain("--require-hashes");
    expect(uv).not.toContain(CONSTRAINTS);
    expect(pip).not.toContain(CONSTRAINTS);
  });

  it.each([
    ["an empty", ""],
    ["a comment-only", "# no bounds here\n\n"],
  ])(
    "refuses to hash with %s constraints file (never unconstrained)",
    (_, text) => {
      const dir = path.join(root, "src");
      fs.mkdirSync(dir);
      const constraints = path.join(root, "constraints.txt");
      fs.writeFileSync(constraints, text);
      const opts = {
        srcDirs: [dir],
        constraintsFile: constraints,
      } as unknown as PythonBundlingOptions;
      expect(() => _computeSourceHash(opts)).toThrow(/has no constraint/);
    },
  );

  it("changes the asset hash when the constraints change", () => {
    const dir = path.join(root, "src");
    fs.mkdirSync(dir);
    fs.writeFileSync(path.join(dir, "mod.py"), "x = 1");
    const constraints = path.join(root, "constraints.txt");
    fs.writeFileSync(constraints, "python-dateutil>=2.1,<3.0.0\n");
    const opts = {
      srcDirs: [dir],
      constraintsFile: constraints,
    } as unknown as PythonBundlingOptions;
    const before = _computeSourceHash(opts);

    fs.writeFileSync(constraints, "python-dateutil>=2.1,<4.0.0\n");

    expect(_computeSourceHash(opts)).not.toEqual(before);
  });

  it("keeps the asset hash when only the constraints file's comments or blank lines change", () => {
    const dir = path.join(root, "src");
    fs.mkdirSync(dir);
    fs.writeFileSync(path.join(dir, "mod.py"), "x = 1");
    const constraints = path.join(root, "constraints.txt");
    fs.writeFileSync(
      constraints,
      "# botocore bounds\npython-dateutil>=2.1,<3.0.0\n",
    );
    const opts = {
      srcDirs: [dir],
      constraintsFile: constraints,
    } as unknown as PythonBundlingOptions;
    const before = _computeSourceHash(opts);

    fs.writeFileSync(
      constraints,
      "# Source: a newer botocore\n\npython-dateutil>=2.1,<3.0.0  # runtime bound\n",
    );

    expect(_computeSourceHash(opts)).toEqual(before);
  });

  it("treats `#` as a comment only at a line start or after whitespace, as pip does", () => {
    const dir = path.join(root, "src");
    fs.mkdirSync(dir);
    const constraints = path.join(root, "constraints.txt");
    const opts = {
      srcDirs: [dir],
      constraintsFile: constraints,
    } as unknown as PythonBundlingOptions;
    fs.writeFileSync(
      constraints,
      "pkg @ https://example.test/pkg-1.0-py3-none-any.whl#sha256=aaaa\n",
    );
    const before = _computeSourceHash(opts);

    fs.writeFileSync(
      constraints,
      "pkg @ https://example.test/pkg-1.0-py3-none-any.whl#sha256=bbbb\n",
    );

    expect(_computeSourceHash(opts)).not.toEqual(before);
  });

  it("hashes a CRLF constraints file the same as its LF copy", () => {
    const dir = path.join(root, "src");
    fs.mkdirSync(dir);
    const constraints = path.join(root, "constraints.txt");
    const opts = {
      srcDirs: [dir],
      constraintsFile: constraints,
    } as unknown as PythonBundlingOptions;
    fs.writeFileSync(constraints, "# bounds\npython-dateutil<3  # runtime\n");
    const lf = _computeSourceHash(opts);

    fs.writeFileSync(
      constraints,
      "# bounds\r\npython-dateutil<3  # runtime\r\n",
    );

    expect(_computeSourceHash(opts)).toEqual(lf);
  });

  it.each([
    ["-r other.txt"],
    ["-c more.txt"],
    ["--requirement=other.txt"],
    ["--index-url https://example.test/simple"],
    ["--pre"],
    ["-e ."],
  ])(
    "refuses a constraints file that carries an option line (%s), which the hash cannot account for",
    (option) => {
      const dir = path.join(root, "src");
      fs.mkdirSync(dir);
      const constraints = path.join(root, "constraints.txt");
      fs.writeFileSync(constraints, `python-dateutil<3\n${option}\n`);
      const opts = {
        srcDirs: [dir],
        constraintsFile: constraints,
      } as unknown as PythonBundlingOptions;
      expect(() => _computeSourceHash(opts)).toThrow(
        `carries an option line (${option}); only bounds are allowed`,
      );
    },
  );

  it.each([
    [
      "a lone CR",
      "python-dateutil<3\r-r other.txt\n",
      "carries an option line (-r other.txt)",
    ],
    [
      "a form feed",
      "python-dateutil<3\f-r other.txt\n",
      "carries an option line (-r other.txt)",
    ],
    [
      "a line continuation",
      "python-dateutil>=2.1,\\\n<3\n",
      "has a line pip rewrites (python-dateutil>=2.1,\\)",
    ],
    [
      "an environment variable",
      "python-dateutil<${MAX}\n",
      "has a line pip rewrites (python-dateutil<${MAX})",
    ],
  ])(
    "refuses a constraints file that pip reads differently from its lines (%s)",
    (_, text, message) => {
      const dir = path.join(root, "src");
      fs.mkdirSync(dir);
      const constraints = path.join(root, "constraints.txt");
      fs.writeFileSync(constraints, text);
      const opts = {
        srcDirs: [dir],
        constraintsFile: constraints,
      } as unknown as PythonBundlingOptions;
      expect(() => _computeSourceHash(opts)).toThrow(message);
    },
  );

  it("refuses to hash a bundle whose constraints file is missing (fail closed, never unconstrained)", () => {
    const dir = path.join(root, "src");
    fs.mkdirSync(dir);
    const opts = {
      srcDirs: [dir],
      constraintsFile: path.join(root, "absent.txt"),
    } as unknown as PythonBundlingOptions;
    expect(() => _computeSourceHash(opts)).toThrow(/is missing/);
  });
});

const repoRoot = path.resolve(__dirname, "../../..");

// The packages whose bundles install from a lock, as the relock script, the
// Renovate rule and the CI check all see them. The script is the source of truth.
const lockedPackages = (
  /PACKAGES=\(([^)]*)\)/.exec(
    fs.readFileSync(
      path.join(repoRoot, "scripts/relock-python-requirements.sh"),
      "utf-8",
    ),
  )?.[1] ?? ""
)
  .split(/\s+/)
  .filter(Boolean);

// Renovate runs only in the source repository, so the public mirror ships no
// renovate.json. The mirror has no ci/ either: skip only when both are absent,
// so a source checkout that loses renovate.json still fails.
const renovateJson = path.join(repoRoot, "renovate.json");
const itWithRenovate =
  fs.existsSync(renovateJson) || fs.existsSync(path.join(repoRoot, "ci"))
    ? it
    : it.skip;

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null;

const stringList = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];

/** True when dotted-numeric version `v` is at least `floor` (suffixes ignored). */
function versionAtLeast(v: string, floor: string): boolean {
  const nums = (x: string) =>
    (/^\d+(\.\d+)*/.exec(x)?.[0] ?? "0").split(".").map(Number);
  const [a, b] = [nums(v), nums(floor)];
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (a[i] ?? 0) - (b[i] ?? 0);
    if (d !== 0) return d > 0;
  }
  return true;
}

/** Compares the dotted-numeric release parts of two versions (suffixes ignored). */
function compareVersions(v: string, w: string): number {
  const nums = (x: string) =>
    (/^\d+(\.\d+)*/.exec(x)?.[0] ?? "0").split(".").map(Number);
  const [a, b] = [nums(v), nums(w)];
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (a[i] ?? 0) - (b[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

/** True when `version` meets every clause of a specifier such as `>=2.1,<3.0.0`. */
function satisfies(version: string, specifier: string): boolean {
  return specifier.split(",").every((clause) => {
    const [, op, bound] =
      /^\s*(>=|<=|==|!=|>|<)\s*(\S+)\s*$/.exec(clause) ?? [];
    const c = compareVersions(version, bound ?? "");
    switch (op) {
      case ">=":
        return c >= 0;
      case "<=":
        return c <= 0;
      case ">":
        return c > 0;
      case "<":
        return c < 0;
      case "==":
        return c === 0;
      case "!=":
        return c !== 0;
      default:
        throw new Error(`unsupported specifier clause: ${clause}`);
    }
  });
}

describe("every Python bundle installs from a lock", () => {
  const stacksDir = path.join(repoRoot, "infra/lib/stacks");
  const stackFiles = fs
    .readdirSync(stacksDir, { recursive: true, encoding: "utf-8" })
    .filter((f) => f.endsWith(".ts"))
    .map((f) => path.join(stacksDir, f));
  const used = new Set<string>();
  let literalUses = 0;
  let allUses = 0;
  for (const file of stackFiles) {
    const text = fs.readFileSync(file, "utf-8");
    allUses += [...text.matchAll(/\brequirementsFile:/g)].length;
    for (const m of text.matchAll(
      /requirementsFile:\s*fromRoot\(\s*"([^"]+)"/g,
    )) {
      used.add(m[1]);
      literalUses += 1;
    }
  }

  it("lists the packages to lock", () => {
    expect(lockedPackages.length).toBeGreaterThan(0);
  });

  it("writes every requirementsFile as a fromRoot string literal", () => {
    // The checks below only see literals, so any other form (a constant, a
    // path.join) would escape them and could point at an unhashed file.
    expect(allUses).toBe(literalUses);
  });

  it("points every bundle at a lock that the relock script maintains", () => {
    expect(used.size).toBeGreaterThan(0);
    for (const req of used) {
      expect(req).toMatch(/\/requirements\.lock\.txt$/);
      expect(lockedPackages).toContain(path.posix.dirname(req));
    }
  });

  it("has a bundle for every package the relock script locks", () => {
    for (const pkg of lockedPackages) {
      expect(used.has(`${pkg}/requirements.lock.txt`)).toBe(true);
    }
  });

  itWithRenovate("has the Renovate rule relock exactly those packages", () => {
    const parsed: unknown = JSON.parse(fs.readFileSync(renovateJson, "utf-8"));
    const rules: unknown[] =
      isRecord(parsed) && Array.isArray(parsed.packageRules)
        ? parsed.packageRules
        : [];
    expect(rules.length).toBeGreaterThan(0);
    const rule = rules
      .filter(isRecord)
      .find((r) =>
        stringList(r.matchFileNames).includes(
          "packages/sources/requirements.lock.txt",
        ),
      );
    expect(rule).toBeDefined();
    const tasks = rule?.postUpgradeTasks;
    const locks = lockedPackages.map((p) => `${p}/requirements.lock.txt`);
    const reqs = lockedPackages.map((p) => `${p}/requirements.txt`);
    expect(stringList(rule?.matchFileNames).sort()).toEqual(
      [...locks, ...reqs].sort(),
    );
    expect(
      stringList(isRecord(tasks) ? tasks.fileFilters : undefined).sort(),
    ).toEqual([...locks].sort());
  });
});

describe.each(lockedPackages)("hash-locked requirements: %s", (pkg) => {
  const lock = path.join(repoRoot, pkg, "requirements.lock.txt");

  it("pins every requirement of the lock with a hash", () => {
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

  it("pins only versions the Lambda runtime's botocore accepts", () => {
    // Hash mode cannot take the runtime constraints at install time, so the
    // lock itself must honour them: python-dateutil 3.0.0 in a bundle shadows
    // the runtime's copy, and its botocore requires <3.0.0.
    const normalize = (n: string) => n.toLowerCase().replace(/_/g, "-");
    const bounds = fs
      .readFileSync(
        path.join(repoRoot, "infra/lib/utils/lambda-runtime-constraints.txt"),
        "utf-8",
      )
      .split("\n")
      .map((line) => line.replace(/#.*/, "").trim())
      .filter(Boolean)
      .map((line) => {
        const [, name, spec] = /^([A-Za-z0-9_.-]+)(.*)$/.exec(line) ?? [];
        return { name: normalize(name ?? ""), spec: spec ?? "" };
      });
    expect(bounds.length).toBeGreaterThan(0);
    const pinned = new Map(
      [
        ...fs
          .readFileSync(lock, "utf-8")
          .matchAll(/^([A-Za-z0-9_.-]+)==([^\s\\]+)/gm),
      ].map((m) => [normalize(m[1]), m[2]]),
    );
    const outside = bounds
      .filter(({ name, spec }) => {
        const version = pinned.get(name);
        return version !== undefined && !satisfies(version, spec);
      })
      .map(({ name, spec }) => `${name}==${pinned.get(name)} (needs ${spec})`);
    expect(outside).toEqual([]);
  });

  it("keeps every direct dependency of requirements.txt in the lock, at or above its floor", () => {
    const normalize = (n: string) => n.toLowerCase().replace(/_/g, "-");
    const direct = fs
      .readFileSync(path.join(repoRoot, pkg, "requirements.txt"), "utf-8")
      .split("\n")
      .map((line) => line.replace(/#.*/, "").trim())
      .filter(Boolean)
      .map((line) => ({
        name: normalize(line.split(/[<>=!~;\s[]/)[0]),
        floor: /^[^<>=!~;\s[]+(?:\[[^\]]*\])?>=([0-9][^,;\s]*)/.exec(line)?.[1],
      }));
    const pinned = new Map(
      [
        ...fs
          .readFileSync(lock, "utf-8")
          .matchAll(/^([A-Za-z0-9_.-]+)==([^\s\\]+)/gm),
      ].map((m) => [normalize(m[1]), m[2]]),
    );
    const missing = direct.filter(({ name }) => !pinned.has(name));
    expect(missing).toEqual([]);
    // e.g. the certifi security floor: a hand-edited older pin must fail here
    // as well as in the CI relock check.
    const belowFloor = direct.filter(({ name, floor }) => {
      const version = pinned.get(name);
      return floor && version && !versionAtLeast(version, floor);
    });
    expect(belowFloor).toEqual([]);
  });
});

describe("_isHashLocked", () => {
  it("recognizes a lock by its file name only", () => {
    expect(_isHashLocked("/r/packages/sources/requirements.lock.txt")).toBe(
      true,
    );
    expect(_isHashLocked("/r/packages/sources/requirements.txt")).toBe(false);
  });
});
