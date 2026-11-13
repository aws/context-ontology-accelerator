// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Utility for bundling Python Lambda code from monorepo `src/` layout packages.
 *
 * Handles:
 *   - Flattening `src/` layout so modules are importable at the Lambda root
 *   - Merging multiple source directories (e.g. package + shared lib)
 *   - Installing pip dependencies into the asset (locally via uv — independent
 *     of the host `python3` symlink; in the Docker fallback via the image `pip`)
 *   - Local bundling first (no Docker needed), with Docker fallback
 */
import { execSync } from "child_process";
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { fromRoot, Paths } from "../paths";

/**
 * The Lambda runtime botocore's bounds, applied to every unlocked bundle install
 * and compiled into every lock by scripts/relock-python-requirements.sh.
 */
export const LAMBDA_RUNTIME_CONSTRAINTS = fromRoot(
  "infra/lib/utils/lambda-runtime-constraints.txt",
);

export interface PythonBundlingOptions {
  /** Absolute paths to `src/` directories whose contents are copied to the asset root. */
  readonly srcDirs: string[];
  /**
   * Path to a `requirements.txt`, or to a hash-pinned `requirements.lock.txt`,
   * which is installed with `--require-hashes`. If omitted, no pip install step
   * runs.
   */
  readonly requirementsFile?: string;
  /**
   * Target Lambda architecture. Determines the pip platform tag.
   * @default "x86_64"
   */
  readonly architecture?: "x86_64" | "arm64";
  /**
   * Constraints applied to an unlocked dependency install and folded into the
   * asset hash. A hash-pinned lock is installed without them, since hash mode
   * rejects any requirement that is not `==`-pinned; the relock script compiles
   * them into the lock instead.
   * @default LAMBDA_RUNTIME_CONSTRAINTS
   */
  readonly constraintsFile?: string;
}

/** A path with forward slashes, as Docker and the shell commands need. */
function toPosix(p: string): string {
  return p.replace(/\\/g, "/");
}

/** The constraints file for this bundle, with forward slashes (Docker-safe). */
function _constraintsPath(opts: PythonBundlingOptions): string {
  return toPosix(opts.constraintsFile ?? LAMBDA_RUNTIME_CONSTRAINTS);
}

/**
 * True for a hash-pinned lock (`requirements.lock.txt`, written by
 * scripts/relock-python-requirements.sh), which is installed with
 * `--require-hashes` so a deploy can only ship the locked files.
 */
export function _isHashLocked(requirementsFile: string): boolean {
  return requirementsFile.endsWith(".lock.txt");
}

/**
 * Build the local `uv pip install` command for the bundle, or `null` when there
 * are no dependencies to install.
 *
 * uv rather than the host `pip`: Amazon Linux 2023 keeps `/usr/bin/python3` on 3.9
 * because `dnf` depends on it, so `dnf install python3.12` leaves `pip`/`pip3` bound
 * to the wrong interpreter. uv is a standalone binary; `--python-platform` +
 * `--python-version` cross-resolve wheels for the Lambda target from any host, and
 * `--only-binary :all:` stops a native sdist from building against the host toolchain.
 *
 * The `--target` is `/asset-output`; the caller rewrites it to the real output dir
 * for local bundling.
 */
export function _uvInstallCommand(opts: PythonBundlingOptions): string | null {
  const uvPlatform =
    opts.architecture === "arm64"
      ? "aarch64-manylinux2014"
      : "x86_64-manylinux2014";
  const constraints = _constraintsPath(opts);
  const flags = `--target /asset-output --python-platform ${uvPlatform} --python-version 3.12 --only-binary :all: -q --no-cache`;
  if (opts.requirementsFile) {
    const reqFile = toPosix(opts.requirementsFile);
    // A lock already pins the whole tree with hashes. --no-config keeps the
    // workspace's [tool.uv] constraint-dependencies out of the install: they
    // are `>=` floors for dev tooling, which hash mode rejects as unpinned, and
    // the relock script already applied them when it compiled the lock.
    if (_isHashLocked(reqFile)) {
      return `uv pip install --no-config --require-hashes ${flags} -r ${reqFile}`;
    }
    return `uv pip install ${flags} --constraint ${constraints} -r ${reqFile}`;
  }
  return null;
}

/**
 * Build the Docker fallback's `pip install` command (inside the 3.12 bundling image,
 * whose `pip` IS the target interpreter, so it keeps the classic pip cross-flags),
 * or `null` when there are no dependencies to install.
 */
export function _pipInstallCommand(opts: PythonBundlingOptions): string | null {
  const platform =
    opts.architecture === "arm64"
      ? "manylinux2014_aarch64"
      : "manylinux2014_x86_64";
  const flags = `--target /asset-output --platform ${platform} --only-binary=:all: --python-version 3.12 -q --no-cache-dir --disable-pip-version-check`;
  if (opts.requirementsFile) {
    const reqFile = toPosix(opts.requirementsFile);
    if (_isHashLocked(reqFile)) {
      return `pip install --require-hashes -r ${reqFile} ${flags}`;
    }
    return `pip install -r ${reqFile} ${flags} -c ${_constraintsPath(opts)}`;
  }
  return null;
}

/**
 * Returns a bundled `lambda.Code` that merges one or more Python `src/`
 * directories and installs pip dependencies.
 *
 * @example
 * ```ts
 * bundlePython({
 *   srcDirs: [Paths.controlPlaneSrc, fromRoot("libs/common/src")],
 *   requirementsFile: fromRoot("packages/control-plane/requirements.lock.txt"),
 * });
 * ```
 */
export function bundlePython(opts: PythonBundlingOptions): lambda.Code {
  // Compute a stable hash over EVERY input that affects the produced bundle.
  // Omitting one means CDK reuses a stale cached asset and the Lambda keeps
  // running old code or old dependencies, with no error at deploy time.
  const assetHash = _computeSourceHash(opts);

  const copies = opts.srcDirs.map(
    (d) => `cp -r ${toPosix(d)}/* /asset-output/`,
  );

  // Packages provided by the Lambda runtime — must not be overridden.
  const runtimePkgs = [
    "boto3",
    "botocore",
    "urllib3",
    "s3transfer",
    "jmespath",
  ];
  // Anchor the dist-info glob on the version separator (`-`) so it cannot match a
  // DIFFERENT package that merely starts with the same name: `boto3*dist-info`
  // also matched `boto3_stubs-1.2.3.dist-info`, deleting that package's metadata
  // while leaving its code in place — a half-removed install. A real dist-info is
  // always `{name}-{version}.dist-info`.
  const cleanup = runtimePkgs
    .map(
      (p) =>
        `rm -rf /asset-output/${p} /asset-output/${p.replace("-", "_")}-*.dist-info`,
    )
    .join(" && ");

  // Local path uses uv, which does not depend on the host `python3` symlink and
  // so bundles for 3.12 even where the distro default Python is older and cannot
  // be repointed. See _uvInstallCommand.
  const localInstall = _uvInstallCommand(opts);

  const dockerInstall = _pipInstallCommand(opts);
  const localPipSteps = localInstall ? [localInstall, cleanup] : [];
  const dockerPipSteps = dockerInstall ? [dockerInstall, cleanup] : [];

  const localSteps = [...copies, ...localPipSteps];
  const bundleCmd = localSteps.join(" && ");

  const dockerSteps = [...copies, ...dockerPipSteps];
  const rootForward = toPosix(Paths.root);
  const dockerCmd = dockerSteps
    .join(" && ")
    .replace(new RegExp(escapeRegExp(rootForward), "g"), "/asset-input");

  return lambda.Code.fromAsset(Paths.root, {
    assetHashType: cdk.AssetHashType.CUSTOM,
    assetHash,
    bundling: {
      image: lambda.Runtime.PYTHON_3_12.bundlingImage,
      command: ["bash", "-c", dockerCmd],
      local: {
        tryBundle(_outputDir: string) {
          // Skip local bundling on Windows — always use Docker.
          // Local bundling requires Unix commands (cp, rm) that don't exist in CMD.
          if (process.platform === "win32") {
            return false;
          }
          try {
            execSync(bundleCmd.replace(/\/asset-output/g, _outputDir), {
              stdio: "inherit",
            });
            return true;
          } catch {
            return false;
          }
        },
      },
    },
  });
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Every line break Python's str.splitlines() honours, which is how pip reads a requirements file.
const PIP_LINE_BREAK = /\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/;

/**
 * Compute a stable SHA-256 hash over every input that affects this Lambda bundle.
 * Used as a custom CDK asset hash so unrelated repo changes (test outputs, coverage
 * files, …) do not trigger needless re-bundles.
 *
 * Hashes ALL files under `srcDirs`, not just `*.py`: the bundle command copies
 * everything (`cp -r ${d}/*`), so JSON/schema/template files are part of the output.
 *
 * Also folds in `architecture`, which selects the pip `--platform` tag and therefore
 * which binary wheels land in the bundle, without touching any file.
 *
 * Reads files via Node on every platform; a shell `find` pipeline degrades to the
 * empty-input hash when the repo path contains a space.
 */
export function _computeSourceHash(opts: PythonBundlingOptions): string {
  const hash = crypto.createHash("sha256");

  // Bundle-shaping inputs first, so they are covered even if a srcDir is absent.
  hash.update(`architecture:${opts.architecture ?? "x86_64"}\n`);
  // The constraints change which versions land in the bundle without touching
  // any source file, so they are hashed; a missing file, or one with no
  // constraint line, fails closed rather than bundling unconstrained.
  const constraints = opts.constraintsFile ?? LAMBDA_RUNTIME_CONSTRAINTS;
  if (!fs.existsSync(constraints)) {
    throw new Error(
      `bundlePython: the constraints file ${constraints} is missing; refusing to bundle unconstrained`,
    );
  }
  const constraintText = fs.readFileSync(constraints, "utf8");
  const lines = constraintText.split(PIP_LINE_BREAK);
  // pip's rule: `#` starts a comment only at a line start or after whitespace.
  const uncommented = lines.map((line) => line.replace(/(^|\s+)#.*$/, ""));
  // pip joins a line ending in `\` to the next and expands `${VAR}`, so what it
  // reads would not be what is hashed.
  const rewritten = lines.findIndex(
    (line, i) =>
      (line.endsWith("\\") && !/^\s*#/.test(line)) ||
      uncommented[i].includes("${"),
  );
  if (rewritten !== -1) {
    throw new Error(
      `bundlePython: the constraints file ${constraints} has a line pip rewrites (${lines[rewritten].trim()}); write each bound on one plain line`,
    );
  }
  const bounds = uncommented.map((line) => line.trim()).filter(Boolean);
  if (bounds.length === 0) {
    throw new Error(
      `bundlePython: the constraints file ${constraints} has no constraint; refusing to bundle unconstrained`,
    );
  }
  // An option line changes the install in ways this hash cannot see: `-r other.txt`
  // pulls in another file's bounds, `--index-url` or `--pre` changes what resolves.
  const option = bounds.find((bound) => bound.startsWith("-"));
  if (option) {
    throw new Error(
      `bundlePython: the constraints file ${constraints} carries an option line (${option}); only bounds are allowed`,
    );
  }
  // The bounds, not the raw text: a comment edit must not re-bundle every Lambda.
  hash.update(`constraints:${bounds.join("\n")}\n`);

  for (const dir of opts.srcDirs) {
    const normalizedDir = toPosix(dir);
    if (!fs.existsSync(normalizedDir)) {
      // A source dir that does not exist yet still has to affect the hash, or
      // creating it later would not invalidate the asset. Keyed on the basename
      // rather than the full path, for the portability reason below.
      hash.update(`missing:${path.basename(normalizedDir)}\n`);
      continue;
    }
    for (const file of _collectFiles(normalizedDir).sort()) {
      const fileHash = crypto
        .createHash("sha256")
        .update(fs.readFileSync(file))
        .digest("hex");
      // Path is included so a rename alone invalidates the asset. It is relative to its
      // srcDir so the hash does not depend on checkout location; `cp -r ${d}/*` flattens
      // every srcDir into one output dir, so the path within the output is what matters.
      hash.update(`${fileHash}  ${_relative(normalizedDir, file)}\n`);
    }
  }

  if (opts.requirementsFile) {
    if (fs.existsSync(opts.requirementsFile)) {
      hash.update(fs.readFileSync(opts.requirementsFile));
    } else {
      hash.update(`missing:${path.basename(opts.requirementsFile)}\n`);
    }
  }
  return hash.digest("hex");
}

/** `target` relative to `from`, with forward slashes. */
function _relative(from: string, target: string): string {
  return toPosix(path.relative(from, target));
}

const _IGNORED_DIRS = new Set([
  "__pycache__",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
]);

function _isBuildArtifactDir(name: string): boolean {
  return (
    _IGNORED_DIRS.has(name) ||
    name.endsWith(".egg-info") ||
    name.endsWith(".dist-info")
  );
}

/**
 * Recursively collect every file path in a directory, skipping build output
 * (`__pycache__`, tool caches, `*.egg-info`, `*.dist-info`), which is not input and
 * would make the hash depend on what ran first. Not limited to `*.py`: the bundle
 * copies the whole tree.
 */
function _collectFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  const results: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = `${dir}/${entry.name}`;
    if (entry.isDirectory()) {
      if (_isBuildArtifactDir(entry.name)) continue;
      results.push(..._collectFiles(fullPath));
    } else if (entry.isSymbolicLink()) {
      // `cp -r ${d}/*` expands the glob, so a top-level symlink arrives as an argument and
      // `cp` dereferences it. `isFile()` is false for a symlink, so hash the resolved target;
      // skip broken links and directory symlinks, which risk a traversal cycle.
      try {
        if (fs.statSync(fullPath).isFile() && !entry.name.endsWith(".pyc")) {
          results.push(fullPath);
        }
      } catch {
        // Dangling symlink — nothing to hash.
      }
    } else if (entry.isFile() && !entry.name.endsWith(".pyc")) {
      results.push(fullPath);
    }
  }
  return results;
}
