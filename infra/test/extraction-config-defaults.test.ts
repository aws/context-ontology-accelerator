// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as fs from "fs";
import * as path from "path";
import { DEFAULT_EXTRACTION_CONFIG } from "../lib/constants";

/**
 * DEFAULT_EXTRACTION_CONFIG in lib/constants.ts is the extraction config the
 * CDK stacks provision; coa_common.constants.py is what the KG-build runtime
 * falls back to when an env var is missing. The two are maintained by hand and
 * the TS comment claims it "mirrors constants.py". If one side is flipped and
 * the other is not, the provisioned default and the runtime default silently
 * disagree about whether batch inference is used. This test pins the
 * batch-inference default together, source text against exported value.
 */

const REPO = path.join(__dirname, "..", "..");
const read = (...p: string[]) => fs.readFileSync(path.join(REPO, ...p), "utf8");

function match(source: string, re: RegExp, what: string): string {
  const m = source.match(re);
  if (!m) throw new Error(`could not find ${what} (${re})`);
  return m[1];
}

describe("extraction-config defaults agree across TypeScript and Python", () => {
  it("use_batch_inference: lib/constants.ts = coa_common.constants.DEFAULT_USE_BATCH_INFERENCE", () => {
    const constantsPy = read(
      "libs",
      "common",
      "src",
      "coa_common",
      "constants.py",
    );
    const pythonDefault = match(
      constantsPy,
      /^DEFAULT_USE_BATCH_INFERENCE:\s*str\s*=\s*"([^"]+)"/m,
      "DEFAULT_USE_BATCH_INFERENCE in constants.py",
    );
    expect(DEFAULT_EXTRACTION_CONFIG.use_batch_inference).toBe(pythonDefault);
    // Batch inference is the default ingestion path; guard the specific value
    // so a silent flip to "false" on either side fails here.
    expect(pythonDefault).toBe("true");
  });
});
