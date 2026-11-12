// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { loadEnvFiles, queryRoleArns } from "../src/env";

const TOUCHED = ["SERVE_ROLE_ARN", "DISCOVERY_ROLE_ARN", "PROBE_VALUE"];

let saved: Record<string, string | undefined>;

beforeEach(() => {
  saved = {};
  for (const name of TOUCHED) {
    saved[name] = process.env[name];
    delete process.env[name];
  }
});

afterEach(() => {
  for (const name of TOUCHED) {
    if (saved[name] === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = saved[name];
    }
  }
});

describe("COA role ARNs", () => {
  const SERVE = "arn:aws:iam::111122223333:role/serve";
  const DISCOVERY = "arn:aws:iam::111122223333:role/discovery";

  it("collects both serve and discovery: DESCRIBE is how the tags reach COA", () => {
    process.env.SERVE_ROLE_ARN = SERVE;
    process.env.DISCOVERY_ROLE_ARN = DISCOVERY;
    expect(queryRoleArns()).toEqual([SERVE, DISCOVERY]);
  });

  it("splits a comma-separated list so one connector can serve several deployments", () => {
    process.env.SERVE_ROLE_ARN = `${SERVE}, arn:aws:iam::444455556666:role/serve2`;
    process.env.DISCOVERY_ROLE_ARN = DISCOVERY;
    expect(queryRoleArns()).toEqual([
      SERVE,
      "arn:aws:iam::444455556666:role/serve2",
      DISCOVERY,
    ]);
  });

  it("collapses a role that does both jobs, so it is granted once", () => {
    process.env.SERVE_ROLE_ARN = SERVE;
    process.env.DISCOVERY_ROLE_ARN = SERVE;
    expect(queryRoleArns()).toEqual([SERVE]);
  });

  it("requires discovery too, since without it no declared key is ever found", () => {
    process.env.SERVE_ROLE_ARN = SERVE;
    expect(() => queryRoleArns()).toThrow(/DISCOVERY_ROLE_ARN is not set/);
  });

  it("tolerates the punctuation a hand-edited .env picks up", () => {
    // Trailing comma, a doubled one, and padding — all plausible in a file someone pastes ARNs
    // into. An empty entry must be dropped rather than reaching CloudFormation as a blank
    // principal, which fails there with a message that names nothing useful.
    process.env.SERVE_ROLE_ARN = `  ${SERVE} ,, `;
    process.env.DISCOVERY_ROLE_ARN = `${DISCOVERY},`;
    expect(queryRoleArns()).toEqual([SERVE, DISCOVERY]);
  });

  it("treats a whitespace-only value as unset rather than as an empty grant", () => {
    process.env.SERVE_ROLE_ARN = SERVE;
    process.env.DISCOVERY_ROLE_ARN = "   ";
    expect(() => queryRoleArns()).toThrow(/DISCOVERY_ROLE_ARN is not set/);
  });

  it("rejects something that is not an ARN", () => {
    // A role *name* is the plausible mistake here, and it would otherwise reach
    // CloudFormation and fail there instead.
    process.env.SERVE_ROLE_ARN = "scl-dev-serve-role";
    expect(() => queryRoleArns()).toThrow(/is not an ARN/);
  });
});

describe("loading .env", () => {
  let root: string;
  let app: string;

  beforeEach(() => {
    root = fs.mkdtempSync(path.join(os.tmpdir(), "connectors-env-"));
    app = path.join(root, "example", "cdk");
    fs.mkdirSync(app, { recursive: true });
  });

  it("loads only the directories it is given, never their parents", () => {
    // Walking upwards could reach an unrelated .env and silently supply deployment settings.
    fs.writeFileSync(path.join(root, ".env"), "PROBE_VALUE=from_parent\n");
    expect(loadEnvFiles(app)).toEqual([]);
  });

  it("loads several directories in the order given, most specific first", () => {
    fs.writeFileSync(path.join(app, ".env"), "PROBE_VALUE=from_app\n");
    fs.writeFileSync(path.join(root, ".env"), "PROBE_VALUE=from_shared\n");
    expect(loadEnvFiles(app, root)).toEqual([
      path.join(app, ".env"),
      path.join(root, ".env"),
    ]);
  });

  it("skips directories with no .env, because a pipeline has no file at all", () => {
    expect(loadEnvFiles(app, root)).toEqual([]);
  });
});
