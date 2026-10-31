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
import { Paths } from "../paths";

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
}

/**
 * Build the local `uv pip install` command for the bundle, or `null` when there
 * are no dependencies to install.
 *
 * Why uv and not the host `pip`: installing a newer Python on many Linux
 * distributions does NOT repoint the default `python3`/`pip` symlink. Amazon
 * Linux 2023 ships Python 3.9 and keeps `/usr/bin/python3` on it because `dnf`
 * itself depends on that exact version, so `dnf install python3.12` adds
 * `/usr/bin/python3.12` but leaves `python3` (and therefore the `pip`/`pip3` on
 * PATH) bound to 3.9. A `pip`/`pip3` probe then finds the wrong interpreter and
 * the bundle either fails outright or targets the wrong runtime.
 *
 * uv sidesteps the symlink entirely: it is a standalone binary that does not use
 * the system Python to run. `--python-platform` + `--python-version` cross-resolve
 * wheels for the Lambda target from any host (macOS or an older-Python Linux)
 * without executing a 3.12 interpreter, and `--only-binary :all:` forces wheels
 * so a native sdist can never silently build against the host toolchain.
 *
 * The `--target` is `/asset-output`; the caller rewrites it to the real output
 * dir for local bundling (and the requirements path stays an absolute host path,
 * since the local step runs on the host and reads the real file).
 */
/**
 * True for a hash-pinned lock (`requirements.lock.txt`, written by
 * scripts/relock-python-requirements.sh), which is installed with
 * `--require-hashes` so a deploy can only ship the locked files.
 */
export function _isHashLocked(requirementsFile: string): boolean {
  return requirementsFile.endsWith(".lock.txt");
}

export function _uvInstallCommand(opts: PythonBundlingOptions): string | null {
  const uvPlatform =
    opts.architecture === "arm64"
      ? "aarch64-manylinux2014"
      : "x86_64-manylinux2014";
  const flags = `--target /asset-output --python-platform ${uvPlatform} --python-version 3.12 --only-binary :all: -q --no-cache`;
  if (opts.requirementsFile) {
    const reqFile = opts.requirementsFile.replace(/\\/g, "/");
    // A lock already pins the whole tree with hashes. --no-config keeps the
    // workspace's [tool.uv] constraint-dependencies out of the install: they
    // are `>=` floors for dev tooling, which hash mode rejects as unpinned, and
    // the relock script already applied them when it compiled the lock.
    const lockFlags = _isHashLocked(reqFile)
      ? " --no-config --require-hashes"
      : "";
    return `uv pip install${lockFlags} ${flags} -r ${reqFile}`;
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

  // Normalize all source paths to forward slashes for Docker compatibility
  const srcDirs = opts.srcDirs.map((d) => d.replace(/\\/g, "/"));
  const copies = srcDirs.map((d) => `cp -r ${d}/* /asset-output/`);
  const platform =
    opts.architecture === "arm64"
      ? "manylinux2014_aarch64"
      : "manylinux2014_x86_64";

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

  // Docker path installs inside the 3.12 bundling image, whose `pip` IS the
  // target interpreter, so it keeps the classic pip cross-flags.
  const pipFlags = `--target /asset-output --platform ${platform} --only-binary=:all: --python-version 3.12 -q --no-cache-dir --disable-pip-version-check`;
  // Local path uses uv, which does not depend on the host `python3` symlink and
  // so bundles for 3.12 even where the distro default Python is older and cannot
  // be repointed. See _uvInstallCommand.
  const localInstall = _uvInstallCommand(opts);

  let localPipSteps: string[] = [];
  let dockerPipSteps: string[] = [];
  if (opts.requirementsFile) {
    const reqFile = opts.requirementsFile.replace(/\\/g, "/");
    localPipSteps = localInstall ? [localInstall, cleanup] : [];
    const hashFlag = _isHashLocked(reqFile) ? " --require-hashes" : "";
    dockerPipSteps = [
      `pip install${hashFlag} -r ${reqFile} ${pipFlags}`,
      cleanup,
    ];
  }

  const localSteps = [...copies, ...localPipSteps];
  const bundleCmd = localSteps.join(" && ");

  const dockerSteps = [...copies, ...dockerPipSteps];
  const rootForward = Paths.root.replace(/\\/g, "/");
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

/**
 * Compute a stable SHA-256 hash over every input that affects this Lambda
 * bundle. Used as a custom CDK asset hash so unrelated repo changes (test
 * outputs, coverage files, …) don't trigger needless re-bundles.
 *
 * Hashes ALL files under `srcDirs` — not just `*.py`. The bundle command copies
 * everything (`cp -r ${d}/*`), so JSON/schema/template files shipped alongside
 * the code are part of the output and must invalidate the asset when edited.
 *
 * Also folds in `architecture`, which changes the produced bundle without
 * touching any file: it selects the pip `--platform` tag and therefore which
 * binary wheels land in the bundle, so flipping x86_64 ↔ arm64 reused an asset
 * built for the wrong architecture.
 *
 * Reads files via Node on every platform. The previous `find … || true` shell
 * pipeline was unquoted and failure-suppressed, so a repo path containing a space
 * degraded every hash to the empty-input hash — permanently disabling
 * invalidation — and any `find` failure passed silently.
 */
export function _computeSourceHash(opts: PythonBundlingOptions): string {
  const hash = crypto.createHash("sha256");

  // Bundle-shaping inputs first, so they are covered even if a srcDir is absent.
  hash.update(`architecture:${opts.architecture ?? "x86_64"}\n`);

  for (const dir of opts.srcDirs) {
    const normalizedDir = dir.replace(/\\/g, "/");
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
      // Path is included so a rename alone invalidates the asset — but RELATIVE to
      // its srcDir, for two reasons:
      //
      //  1. Portability. An absolute path made the hash depend on where the repo
      //     was checked out, so CI, a local clone, and every `git worktree`
      //     produced a different asset hash for the SAME commit — re-bundling on
      //     every deploy from a different path, which is exactly what a stable
      //     hash is supposed to prevent.
      //  2. It models the bundle. `cp -r ${d}/* /asset-output/` flattens every
      //     srcDir into one directory, so what determines the asset's content is
      //     the path WITHIN that output, not which absolute dir it came from.
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

/**
 * `target` relative to `from`, with forward slashes.
 *
 * Forward slashes are forced so Windows and POSIX agree on the hash for an
 * identical tree (they previously could not, since the two platforms took
 * different code paths entirely).
 */
function _relative(from: string, target: string): string {
  return path.relative(from, target).replace(/\\/g, "/");
}

/**
 * Recursively collect every file path in a directory.
 *
 * Deliberately NOT limited to `*.py`: the bundle copies the whole tree, so any
 * non-Python file it ships (JSON, schemas, templates, Cedar policies) must
 * invalidate the asset when edited.
 *
 * Build output is skipped — it is not input, and hashing it would make the asset
 * depend on whether tests, mypy, or a setuptools build happened to run first.
 * `*.egg-info` matters in practice: `packages/ontology-engine/src` carries one,
 * it is untracked, and it is regenerated by tooling — so including it made the
 * same commit hash differently in CI than locally.
 */
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

function _collectFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  const results: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = `${dir}/${entry.name}`;
    if (entry.isDirectory()) {
      if (_isBuildArtifactDir(entry.name)) continue;
      results.push(..._collectFiles(fullPath));
    } else if (entry.isSymbolicLink()) {
      // `cp -r ${d}/*` expands the glob, so a top-level symlink arrives as a
      // command-line argument and `cp` DEREFERENCES it — the target's content
      // lands in the bundle. `isFile()` is false for a symlink, so without this
      // branch that content shipped without contributing to the hash. Hash the
      // resolved target (matching what the bundle gets); skip broken links and
      // directory symlinks, which would otherwise risk a traversal cycle.
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
