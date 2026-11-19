// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Assertions that a Python image never ships a pip with a vendored SBOM
 * (pip/_vendor/bom.cdx.json), which scanners misreport as setuptools CVEs.
 * The rationale lives in scripts/pip_sbom_check.py, and the build-time run of
 * that script is the real gate; these catch the Dockerfile drifting from it.
 */

/** Any pip install (pip, pip3, pip3.12, python -m pip, uv pip). */
const PIP_INSTALL = /\bpip[\d.]*\s+install\b/g;
/** A pip install that upgrades pip itself: `-U`/`--upgrade` with a bare or quoted `pip` (with or without a version) argument. */
const PIP_UPGRADE =
  /\bpip[\d.]*\s+install\b[^\n]*\s(?:-U|--upgrade)\b[^\n]*\s"?pip(?:[<>=!~][^\s"]*)?"?(?=\s|$)/m;
const COPY_CHECK = /^COPY\s+scripts\/pip_sbom_check\.py\s+\/tmp\/\s*$/m;

/** The Dockerfile's instructions with comment lines removed, so a comment can't satisfy a check. */
function instructions(dockerfile: string): string {
  return dockerfile
    .split("\n")
    .filter((line) => !/^\s*#/.test(line))
    .join("\n");
}

/** Index just past the last match of a global regex, or -1. */
function endOfLast(code: string, re: RegExp): number {
  let end = -1;
  for (const m of code.matchAll(re)) end = (m.index ?? 0) + m[0].length;
  return end;
}

/** The SBOM check is copied in and run after every pip install. */
function expectCheckRunsLast(code: string, run: RegExp): void {
  expect(code).toMatch(COPY_CHECK);
  expect(code).toMatch(run);
  expect(code.search(run)).toBeGreaterThan(endOfLast(code, PIP_INSTALL));
}

/** AL2023 images: pip is never upgraded, and the build runs the SBOM check after the last install. */
export function expectNoVendoredPipSbom(dockerfile: string): void {
  const code = instructions(dockerfile);
  expect(code).not.toMatch(PIP_UPGRADE);
  expectCheckRunsLast(code, /^RUN\s+python\s+\/tmp\/pip_sbom_check\.py\s*$/m);
}

/** Images whose only pip is removed: it is uninstalled after the last install, and the build checks it is gone. */
export function expectPipUninstalledLast(dockerfile: string): void {
  const code = instructions(dockerfile);
  expect(code).not.toMatch(PIP_UPGRADE);
  const uninstall =
    /\bpip[\d.]*\s+uninstall\s+-y\b[^\n]*\spip(?=\s*(?:&&|\\|$))/m;
  expect(code).toMatch(uninstall);
  expect(code.search(uninstall)).toBeGreaterThan(endOfLast(code, PIP_INSTALL));
  expectCheckRunsLast(
    code,
    /^RUN\s+python\s+\/tmp\/pip_sbom_check\.py\s+--absent\s*$/m,
  );
}
