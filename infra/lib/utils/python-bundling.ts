// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Utility for bundling Python Lambda code from monorepo `src/` layout packages.
 *
 * Handles:
 *   - Flattening `src/` layout so modules are importable at the Lambda root
 *   - Merging multiple source directories (e.g. package + shared lib)
 *   - Installing pip dependencies into the asset
 *   - Local bundling first (no Docker needed), with Docker fallback
 */
import { execSync } from "child_process";
import * as crypto from "crypto";
import * as fs from "fs";
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Paths } from "../paths";

export interface PythonBundlingOptions {
  /** Absolute paths to `src/` directories whose contents are copied to the asset root. */
  readonly srcDirs: string[];
  /**
   * How to install pip dependencies. Provide exactly one of:
   *   - `requirementsFile` — path to a `requirements.txt`
   *   - `pipDeps` — explicit list of package names
   *
   * If neither is provided, no pip install step runs.
   */
  readonly requirementsFile?: string;
  readonly pipDeps?: string[];
  /**
   * Target Lambda architecture. Determines the pip platform tag.
   * @default "x86_64"
   */
  readonly architecture?: "x86_64" | "arm64";
}

/**
 * Resolve the local pip command. macOS often has `pip3` but not `pip` (or
 * has an Xcode stub at /usr/local/bin/pip that fails with xcode-select).
 * Docker bundling always uses `pip` (available in the Python container).
 */
function resolvePipCommand(): string {
  try {
    execSync("pip --version", { stdio: "pipe" });
    return "pip";
  } catch {
    try {
      execSync("pip3 --version", { stdio: "pipe" });
      return "pip3";
    } catch {
      return "pip";
    }
  }
}

/**
 * Returns a bundled `lambda.Code` that merges one or more Python `src/`
 * directories and installs pip dependencies.
 *
 * @example
 * ```ts
 * // Using a requirements file:
 * bundlePython({
 *   srcDirs: [Paths.controlPlaneSrc, fromRoot("libs/common/src")],
 *   requirementsFile: fromRoot("packages/control-plane/requirements.txt"),
 * });
 *
 * // Using an explicit list:
 * bundlePython({
 *   srcDirs: [Paths.controlPlaneSrc, fromRoot("libs/common/src")],
 *   pipDeps: ["pydantic", "structlog"],
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

  const localPip = resolvePipCommand();
  const pipFlags = `--target /asset-output --platform ${platform} --only-binary=:all: --python-version 3.12 -q --no-cache-dir --disable-pip-version-check`;

  let localPipSteps: string[] = [];
  let dockerPipSteps: string[] = [];
  if (opts.requirementsFile) {
    const reqFile = opts.requirementsFile.replace(/\\/g, "/");
    localPipSteps = [`${localPip} install -r ${reqFile} ${pipFlags}`, cleanup];
    dockerPipSteps = [`pip install -r ${reqFile} ${pipFlags}`, cleanup];
  } else if (opts.pipDeps?.length) {
    const deps = opts.pipDeps.map((d) => `'${d}'`).join(" ");
    localPipSteps = [`${localPip} install ${pipFlags} ${deps}`, cleanup];
    dockerPipSteps = [`pip install ${pipFlags} ${deps}`, cleanup];
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
 * Also folds in `pipDeps` and `architecture`, which change the produced bundle
 * without touching any file:
 *   - `pipDeps` is the alternative to `requirementsFile`; callers using it could
 *     add, remove, or re-pin a dependency with no hash change, so the deployed
 *     bundle kept the old dependency set (symptom: `ImportModuleError` for a
 *     freshly added dep).
 *   - `architecture` selects the pip `--platform` tag and therefore which binary
 *     wheels land in the bundle, so flipping x86_64 ↔ arm64 reused an asset built
 *     for the wrong architecture.
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
  if (opts.pipDeps?.length) {
    // Sorted: dependency ORDER does not change the installed set, so it must not
    // change the hash (an unsorted list would cause spurious re-bundles).
    hash.update(`pipDeps:${[...opts.pipDeps].sort().join(",")}\n`);
  }

  for (const dir of opts.srcDirs) {
    const normalizedDir = dir.replace(/\\/g, "/");
    if (!fs.existsSync(normalizedDir)) {
      // A source dir that does not exist yet still has to affect the hash, or
      // creating it later would not invalidate the asset.
      hash.update(`missing:${normalizedDir}\n`);
      continue;
    }
    for (const file of _collectFiles(normalizedDir).sort()) {
      const fileHash = crypto
        .createHash("sha256")
        .update(fs.readFileSync(file))
        .digest("hex");
      // Path is included so a rename alone invalidates the asset.
      hash.update(`${fileHash}  ${file}\n`);
    }
  }

  if (opts.requirementsFile) {
    if (fs.existsSync(opts.requirementsFile)) {
      hash.update(fs.readFileSync(opts.requirementsFile));
    } else {
      hash.update(`missing:${opts.requirementsFile}\n`);
    }
  }
  return hash.digest("hex");
}

/**
 * Recursively collect every file path in a directory.
 *
 * Deliberately NOT limited to `*.py`: the bundle copies the whole tree, so any
 * non-Python file it ships (JSON, schemas, templates, Cedar policies) must
 * invalidate the asset when edited.
 *
 * `__pycache__` is skipped — it is build output, not input, and hashing it would
 * make the asset depend on whether tests happened to run first.
 */
function _collectFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  const results: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = `${dir}/${entry.name}`;
    if (entry.isDirectory()) {
      if (entry.name === "__pycache__") continue;
      results.push(..._collectFiles(fullPath));
    } else if (entry.isFile() && !entry.name.endsWith(".pyc")) {
      results.push(fullPath);
    }
  }
  return results;
}
