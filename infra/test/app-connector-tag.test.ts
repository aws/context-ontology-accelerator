// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import * as fs from "fs";
import * as path from "path";
import { CONNECTOR_TAG_KEY } from "../lib/constants";

/**
 * The build-time half of the connector invoke policy.
 *
 * `DenyAthenaInvokeOfUntaggedFunctions`, on both the serve and the discovery role,
 * denies an Athena-mediated `lambda:InvokeFunction` on every same-account function
 * that does NOT carry `coa:connector`. The exemption is what lets a connector
 * deployed alongside this stack be reached at all — see serve-stack.ts and
 * sources-stack.ts.
 *
 * Its residual is one of OUR OWN functions acquiring that tag: it would become
 * Athena-invocable, which is precisely the escalation the Deny exists to close. An
 * Athena UDF (`USING EXTERNAL FUNCTION ... LAMBDA '<arn>'`) needs only
 * StartQueryExecution — held for every user question — plus InvokeFunction, and
 * serve's SQL is LLM-generated, so the vector is plausibly reachable by prompt
 * injection.
 *
 * That residual is closed HERE rather than with a second runtime tag, because a
 * second tag applied at the same scope degrades to the first one and only moves the
 * problem. CDK's `Tags.of(scope).add(...)` propagates to every taggable child, so
 * the realistic path is one line in a stack — which is what this asserts against.
 *
 * A source assertion rather than a template one: `infra/bin/app.ts` is a script
 * that reads SSM at module scope (`void deploy()`), so it cannot be imported and
 * synthesised in a unit test. Reading the source covers every stack in the app
 * including ones added later, which per-stack template assertions would not.
 */
const INFRA_LIB = path.join(__dirname, "..", "lib");

/** Every `.ts` file under `infra/lib`, recursively. */
function sourceFiles(directory: string): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      return sourceFiles(full);
    }
    return entry.isFile() && entry.name.endsWith(".ts") ? [full] : [];
  });
}

/** `{file, line, text}` for every line in `infra/lib` naming the tag, however spelled. */
function linesNamingTheTag(): Array<{
  file: string;
  line: number;
  text: string;
}> {
  const found: Array<{ file: string; line: number; text: string }> = [];
  for (const file of sourceFiles(INFRA_LIB)) {
    const relative = path.relative(INFRA_LIB, file);
    fs.readFileSync(file, "utf8")
      .split("\n")
      .forEach((text, index) => {
        // Both spellings: the imported constant, and the literal value someone
        // could hard-code instead of importing it.
        if (
          text.includes("CONNECTOR_TAG_KEY") ||
          text.includes(CONNECTOR_TAG_KEY)
        ) {
          found.push({ file: relative, line: index + 1, text: text.trim() });
        }
      });
  }
  return found;
}

describe("the connector tag is never applied by this app", () => {
  // `Tags.of(...)` is the propagating form; a `tags:` prop on a construct is the
  // direct form. Neither may carry this key.
  it("applies coa:connector to no resource in infra/lib", () => {
    const applications = linesNamingTheTag().filter(({ text }) => {
      const isComment = text.startsWith("//") || text.startsWith("*");
      if (isComment) {
        return false;
      }
      // The only legitimate non-comment uses are importing/re-exporting the
      // constant and naming it inside an IAM condition key. Anything else that
      // mentions the tag is treated as an application until proven otherwise —
      // failing loudly on an unrecognised use is the safe direction here.
      const isImportOrReExport =
        /^\s*(import\b|export\b)/.test(text) ||
        /^\s*CONNECTOR_TAG_KEY,?$/.test(text);
      const isConditionKey = text.includes("aws:ResourceTag/");
      return !isImportOrReExport && !isConditionKey;
    });

    expect(applications).toEqual([]);
  });

  // Guards the assertion above against silently passing because the tag stopped
  // being mentioned at all — which would mean the Allow and the Deny had lost
  // their condition and the invoke policy no longer scopes anything.
  it("still uses the tag as an IAM condition key on both roles", () => {
    const conditionKeys = linesNamingTheTag().filter(
      ({ text }) => text.includes("aws:ResourceTag/") && !text.startsWith("//"),
    );
    const files = new Set(conditionKeys.map(({ file }) => file));

    // Two statements per role — the tag-scoped Allow and the tag-exempted Deny.
    expect(conditionKeys).toHaveLength(4);
    expect([...files].sort()).toEqual([
      path.join("stacks", "services", "serve-stack.ts"),
      path.join("stacks", "services", "sources-stack.ts"),
    ]);
  });
});
