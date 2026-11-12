// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as path from "path";

// Deployment facts for a connector's CDK app. What a connector is (handler, jar, timeout) lives in
// its stack, committed. Where it is going differs per deployment and some of it must never be
// committed, so it comes from the environment. Real environment variables beat `.env`.

/** File holding local, uncommitted environment facts. Ignored by the repository's gitignore. */
export const ENV_FILE = ".env";

/**
 * Loads `.env` from each directory given, most specific first. Missing files are skipped — a
 * pipeline has none.
 *
 * <p>Directories are named, never searched upwards: a walk from `connectors/<id>/cdk` reaches the
 * repository root and then `/`, so an unrelated file could silently supply deployment settings.
 */
export function loadEnvFiles(...directories: string[]): string[] {
  const loaded: string[] = [];
  for (const directory of directories) {
    const candidate = path.join(path.resolve(directory), ENV_FILE);
    if (!fs.existsSync(candidate)) {
      continue;
    }
    // Node's own loader keeps this package dependency-free, uses the same parser as --env-file,
    // and does not overwrite an already-set variable. It writes to the real process environment,
    // underneath anything wrapping process.env — Jest's per-file copy cannot see it, so tests
    // assert on the returned paths. Needs Node 20.12+.
    try {
      process.loadEnvFile(candidate);
    } catch (cause) {
      throw new Error(`Could not read ${candidate}: ${String(cause)}`);
    }
    loaded.push(candidate);
  }
  return loaded;
}

/**
 * @param name the variable.
 * @param hint appended to the error; say where the value comes from, not just that it is missing.
 * @returns its trimmed value.
 * @throws Error when unset or blank.
 */
export function requiredEnv(name: string, hint?: string): string {
  const value = optionalEnv(name);
  if (value === undefined) {
    throw new Error(
      `${name} is not set. Put it in ${ENV_FILE} (see ${ENV_FILE}.example) or export it.` +
        (hint === undefined ? "" : `\n${hint}`),
    );
  }
  return value;
}

/** @returns the trimmed value, or undefined when unset or blank — an empty `.env` line is absent. */
export function optionalEnv(name: string): string | undefined {
  const raw = process.env[name];
  if (raw === undefined) {
    return undefined;
  }
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

/** @returns the value, or undefined when unset. @throws Error if not a positive integer. */
export function optionalIntEnv(name: string): number | undefined {
  const value = optionalEnv(name);
  if (value === undefined) {
    return undefined;
  }
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer; got "${value}".`);
  }
  return parsed;
}

/** Environment variables naming the COA roles that reach a connector. */
export const QUERY_ROLE_VARS = ["SERVE_ROLE_ARN", "DISCOVERY_ROLE_ARN"] as const;

/**
 * Every COA role that reaches this connector through Athena. Two, because two components do:
 * `SERVE_ROLE_ARN` runs the queries, and `DISCOVERY_ROLE_ARN` runs `DESCRIBE` — which is how the
 * `@pk` / `@fk` tags are read at all. Miss discovery and `SELECT` works while no key is ever found.
 *
 * <p>Each is comma-separated, for serving more than one COA deployment. Duplicates collapse, since
 * one role may do both jobs.
 *
 * @param options `{ required: false }` allows unset, granting nobody; useful only for probing.
 * @returns the ARNs, deduplicated.
 * @throws Error when required and unset, or an entry is not an ARN.
 */
export function queryRoleArns(options: { required?: boolean } = {}): string[] {
  const required = options.required ?? true;
  const arns: string[] = [];
  for (const name of QUERY_ROLE_VARS) {
    const raw = required
      ? requiredEnv(name, hintFor(name))
      : optionalEnv(name);
    if (raw === undefined) {
      continue;
    }
    for (const entry of raw.split(",")) {
      const arn = entry.trim();
      if (arn.length === 0) {
        continue;
      }
      if (!arn.startsWith("arn:")) {
        throw new Error(`${name} entry "${arn}" is not an ARN.`);
      }
      if (!arns.includes(arn)) {
        arns.push(arn);
      }
    }
  }
  return arns;
}

/** @returns advice for finding the role named by `variable`. */
function hintFor(variable: string): string {
  const component = variable === "DISCOVERY_ROLE_ARN" ? "discovery" : "serve";
  return (
    `It is the role COA's ${component} component calls Athena as` +
    (component === "discovery"
      ? " when it runs DESCRIBE to read column comments."
      : " when it runs queries.") +
    "\nFind it with:\n" +
    `  aws iam list-roles --query "Roles[?contains(RoleName,'${component}')].Arn"`
  );
}

/**
 * From `FUNCTION_NAME_PREFIX`. Tells two deployments of the same connector apart when they share an
 * account. Unset is the normal case.
 */
export function functionNamePrefix(): string | undefined {
  return optionalEnv("FUNCTION_NAME_PREFIX");
}

/**
 * The target account and region for a stack's `env`. Region comes from the environment, never from
 * committed code — a connector deployed into the wrong region succeeds and is then never queried.
 *
 * @returns either possibly undefined, leaving CDK to resolve from the active profile.
 */
export function deploymentEnv(): { account?: string; region?: string } {
  return {
    account: optionalEnv("CDK_DEFAULT_ACCOUNT") ?? optionalEnv("AWS_ACCOUNT_ID"),
    region:
      optionalEnv("CDK_DEFAULT_REGION") ??
      optionalEnv("AWS_REGION") ??
      optionalEnv("AWS_DEFAULT_REGION"),
  };
}
