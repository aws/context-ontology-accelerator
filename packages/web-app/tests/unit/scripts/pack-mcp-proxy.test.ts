// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// The MCP page tells users to unzip coa-mcp-proxy-src.zip and run
// `npm install && npm run build`. These tests pin what the zip must contain for
// that to work with the npm that ships with Node 22: a build-only package.json
// (no test toolchain, whose dev tree npm 10 cannot resolve) that still has the
// `build` script, with every direct dependency pinned to the version in
// pnpm-lock.yaml, and a byte-stable, valid archive so the published SHA-256
// only changes when the source or the pinned versions do.

import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import {
  BUILD_DEV_DEPENDENCIES,
  BUNDLE_SCRIPTS,
  DEFAULT_LOCKFILE,
  DEFAULT_PROXY_ROOT,
  ZIP_NAME,
  buildBundlePackageJson,
  isMainModule,
  packMcpProxy,
  readLockedVersions,
} from "../../../scripts/pack-mcp-proxy.mjs";
import { readZip } from "../../helpers/read-zip";

const tmpDirs: string[] = [];
function tmp(): string {
  const dir = mkdtempSync(join(tmpdir(), "pack-mcp-proxy-test-"));
  tmpDirs.push(dir);
  return dir;
}
afterEach(() => {
  for (const dir of tmpDirs.splice(0))
    rmSync(dir, { recursive: true, force: true });
});

const sourcePkg = JSON.parse(
  readFileSync(join(DEFAULT_PROXY_ROOT, "package.json"), "utf8"),
);
const lockText = readFileSync(DEFAULT_LOCKFILE, "utf8");
const locked = readLockedVersions(lockText);

/**
 * The version pnpm-lock.yaml records for `name` under the mcp-proxy importer,
 * found with a plain text search (independent of readLockedVersions).
 */
function lockedVersionByText(name: string): string {
  const lines = lockText.split(/\r?\n/);
  const start = lines.indexOf("  packages/mcp-proxy:");
  const end = lines.findIndex((l, i) => i > start && /^ {2}\S/.test(l));
  const key = name.startsWith("@") ? `'${name}'` : name;
  const at = lines.indexOf(`      ${key}:`, start);
  expect(start, "mcp-proxy importer").toBeGreaterThan(-1);
  expect(at, `${name} in pnpm-lock.yaml`).toBeGreaterThan(start);
  expect(at, `${name} in pnpm-lock.yaml`).toBeLessThan(end);
  const versionLine = lines[at + 2] ?? "";
  expect(versionLine).toMatch(/^ {8}version: /);
  return versionLine.trim().replace("version: ", "").split("(")[0]!;
}

/** A synthetic mcp-proxy package with the real package.json. */
function fakeProxyRoot(): string {
  const root = tmp();
  mkdirSync(join(root, "src"));
  writeFileSync(join(root, "src", "index.ts"), "export {};\n");
  writeFileSync(join(root, "package.json"), JSON.stringify(sourcePkg));
  writeFileSync(join(root, "tsconfig.json"), "{}\n");
  writeFileSync(join(root, "README.md"), "# x\n");
  return root;
}

function bundledPackageJson(zipBuf: Buffer) {
  const entry = readZip(zipBuf).find(
    (e) => e.name === "coa-mcp-proxy/package.json",
  );
  expect(entry).toBeDefined();
  return JSON.parse(entry!.data.toString("utf8"));
}

describe("pack-mcp-proxy: the bundled package.json", () => {
  it("keeps only the dev dependencies the build needs", () => {
    const { zipBuf } = packMcpProxy({ outDir: tmp() });
    const pkg = bundledPackageJson(zipBuf);
    expect(Object.keys(pkg.devDependencies).sort()).toEqual(
      [...BUILD_DEV_DEPENDENCIES].sort(),
    );
    expect(pkg.devDependencies).not.toHaveProperty("vitest");
    expect(pkg.devDependencies).not.toHaveProperty("@vitest/coverage-v8");
  });

  it("pins every direct dependency to the exact version in pnpm-lock.yaml", () => {
    const pkg = bundledPackageJson(packMcpProxy({ outDir: tmp() }).zipBuf);
    expect(Object.keys(pkg.dependencies)).toEqual(
      Object.keys(sourcePkg.dependencies),
    );
    const pinned = { ...pkg.dependencies, ...pkg.devDependencies };
    for (const [name, version] of Object.entries(pinned)) {
      expect(version, name).toMatch(/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/);
      expect(version, name).toBe(lockedVersionByText(name));
    }
  });

  it("keeps the version and exactly the build and start scripts of the source", () => {
    const pkg = bundledPackageJson(packMcpProxy({ outDir: tmp() }).zipBuf);
    expect(pkg.name).toBe(sourcePkg.name);
    expect(pkg.version).toBe(sourcePkg.version);
    expect(pkg.type).toBe(sourcePkg.type);
    expect(Object.keys(pkg.scripts).sort()).toEqual(
      BUNDLE_SCRIPTS.filter((k) => k in sourcePkg.scripts).sort(),
    );
    expect(pkg.scripts.build).toBe(sourcePkg.scripts.build);
    expect(pkg.scripts.start).toBe(sourcePkg.scripts.start);
    // `test` needs vitest, which the bundle does not install.
    expect(pkg.scripts).not.toHaveProperty("test");
  });

  it("refuses a source package.json the documented build cannot work with", () => {
    expect(() =>
      buildBundlePackageJson(
        { ...sourcePkg, scripts: { start: "tsx src/index.ts" } },
        locked,
      ),
    ).toThrow(/build/);
    expect(() =>
      buildBundlePackageJson(
        { ...sourcePkg, devDependencies: { "@types/node": "^26.0.0" } },
        locked,
      ),
    ).toThrow(/typescript/);
  });
});

describe("pack-mcp-proxy: pinning to pnpm-lock.yaml", () => {
  const fixture = [
    "lockfileVersion: '9.0'",
    "",
    "importers:",
    "",
    "  .:",
    "    devDependencies:",
    "      nx:",
    "        specifier: 1.0.0",
    "        version: 1.0.0",
    "",
    "  packages/mcp-proxy:",
    "    dependencies:",
    "      '@scope/a':",
    "        specifier: ^1.2.0",
    "        version: 1.4.2",
    "      b:",
    "        specifier: '>=2'",
    "        version: 2.0.1(peer@3.0.0)",
    "    devDependencies:",
    "      typescript:",
    "        specifier: ^6.0.3",
    "        version: 6.0.3",
    "",
    "  packages/web-app:",
    "    dependencies:",
    "      b:",
    "        specifier: ^9.0.0",
    "        version: 9.9.9",
    "",
    "packages:",
    "",
  ].join("\r\n");

  it("reads only the mcp-proxy importer, with quoted keys, peers and CRLF", () => {
    expect(readLockedVersions(fixture)).toEqual({
      dependencies: {
        "@scope/a": { specifier: "^1.2.0", version: "1.4.2" },
        b: { specifier: ">=2", version: "2.0.1(peer@3.0.0)" },
      },
      devDependencies: {
        typescript: { specifier: "^6.0.3", version: "6.0.3" },
      },
    });
  });

  const pkgWith = (dependencies: Record<string, string>) => ({
    ...sourcePkg,
    dependencies,
    devDependencies: { "@types/node": "^26.1.1", typescript: "^6.0.3" },
  });
  const fixtureLocked = {
    ...readLockedVersions(fixture),
    devDependencies: {
      "@types/node": { specifier: "^26.1.1", version: "26.1.1" },
      typescript: { specifier: "^6.0.3", version: "6.0.3" },
    },
  };

  it("strips the peer suffix from a locked version", () => {
    const pkg = buildBundlePackageJson(
      pkgWith({ "@scope/a": "^1.2.0", b: ">=2" }),
      fixtureLocked,
    );
    expect(pkg.dependencies).toEqual({ "@scope/a": "1.4.2", b: "2.0.1" });
    expect(pkg.devDependencies).toEqual({
      "@types/node": "26.1.1",
      typescript: "6.0.3",
    });
  });

  it("refuses a lockfile that does not match package.json", () => {
    expect(() =>
      buildBundlePackageJson(pkgWith({ "@scope/a": "^1.3.0" }), fixtureLocked),
    ).toThrow(/out of date for @scope\/a/);
    expect(() =>
      buildBundlePackageJson(pkgWith({ c: "^1.0.0" }), fixtureLocked),
    ).toThrow(/no dependencies entry for c/);
  });

  it("refuses a locked version that is not a registry version", () => {
    const linked = {
      ...fixtureLocked,
      dependencies: { d: { specifier: "workspace:*", version: "link:../d" } },
    };
    expect(() =>
      buildBundlePackageJson(pkgWith({ d: "workspace:*" }), linked),
    ).toThrow(/not a registry version/);
  });

  it("refuses a lockfile without the mcp-proxy importer", () => {
    expect(() => readLockedVersions("importers:\n\n  .:\n")).toThrow(
      /no importer packages\/mcp-proxy/,
    );
    expect(() => readLockedVersions("lockfileVersion: '9.0'\n")).toThrow(
      /no importers section/,
    );
  });
});

describe("pack-mcp-proxy: errors", () => {
  it("names a missing required file", () => {
    const root = fakeProxyRoot();
    rmSync(join(root, "README.md"));
    expect(() => packMcpProxy({ proxyRoot: root, outDir: tmp() })).toThrow(
      /required file missing from mcp-proxy: README.md/,
    );
  });

  it("names a missing package directory", () => {
    expect(() =>
      packMcpProxy({ proxyRoot: join(tmp(), "nope"), outDir: tmp() }),
    ).toThrow(/mcp-proxy package not found/);
  });

  it("names a missing lockfile", () => {
    expect(() =>
      packMcpProxy({
        proxyRoot: fakeProxyRoot(),
        outDir: tmp(),
        lockfile: join(tmp(), "pnpm-lock.yaml"),
      }),
    ).toThrow(/pnpm-lock.yaml not found/);
  });

  it("names the package.json that does not parse", () => {
    const root = fakeProxyRoot();
    writeFileSync(join(root, "package.json"), '{ "name": }');
    expect(() => packMcpProxy({ proxyRoot: root, outDir: tmp() })).toThrow(
      `cannot parse ${join(root, "package.json")}`,
    );
  });

  it("builds without a tests/ folder", () => {
    const names = readZip(
      packMcpProxy({ proxyRoot: fakeProxyRoot(), outDir: tmp() }).zipBuf,
    ).map((e) => e.name);
    expect(names.some((n) => n.startsWith("coa-mcp-proxy/tests/"))).toBe(false);
    expect(names).toContain("coa-mcp-proxy/README.md");
  });
});

describe("pack-mcp-proxy: the archive", () => {
  it("holds the source files under coa-mcp-proxy/ and nothing generated", () => {
    const names = readZip(packMcpProxy({ outDir: tmp() }).zipBuf).map(
      (e) => e.name,
    );
    expect(names).toEqual([...names].sort());
    expect(names.every((n) => n.startsWith("coa-mcp-proxy/"))).toBe(true);
    for (const required of [
      "coa-mcp-proxy/README.md",
      "coa-mcp-proxy/package.json",
      "coa-mcp-proxy/tsconfig.json",
      "coa-mcp-proxy/src/index.ts",
      "coa-mcp-proxy/src/auth.ts",
      "coa-mcp-proxy/src/config.ts",
    ]) {
      expect(names).toContain(required);
    }
    expect(
      names.some((n) => /node_modules|\/dist\/|coverage|lock/.test(n)),
    ).toBe(false);
  });

  it("skips node_modules, dist and coverage inside the package", () => {
    const root = fakeProxyRoot();
    for (const dir of ["node_modules", "dist", "coverage"]) {
      mkdirSync(join(root, "src", dir));
      writeFileSync(join(root, "src", dir, "x.js"), "x\n");
    }
    const names = readZip(
      packMcpProxy({ proxyRoot: root, outDir: tmp() }).zipBuf,
    ).map((e) => e.name);
    expect(names).toEqual([
      "coa-mcp-proxy/README.md",
      "coa-mcp-proxy/package.json",
      "coa-mcp-proxy/src/index.ts",
      "coa-mcp-proxy/tsconfig.json",
    ]);
  });

  it("is byte-identical across builds and the sidecar matches it", () => {
    const a = packMcpProxy({ outDir: tmp() });
    const outDir = tmp();
    const b = packMcpProxy({ outDir });
    expect(b.zipBuf.equals(a.zipBuf)).toBe(true);
    expect(b.sha).toBe(a.sha);
    expect(readFileSync(join(outDir, `${ZIP_NAME}.sha256`), "utf8")).toBe(
      `${b.sha}  ${ZIP_NAME}\n`,
    );
    expect(readFileSync(join(outDir, ZIP_NAME)).equals(b.zipBuf)).toBe(true);
  });

  it.runIf(spawnSync("unzip", ["-v"]).status === 0)(
    "passes the system unzip's integrity test",
    () => {
      const outDir = tmp();
      packMcpProxy({ outDir });
      const r = spawnSync("unzip", ["-tq", join(outDir, ZIP_NAME)], {
        encoding: "utf8",
      });
      expect(r.status, r.stdout + r.stderr).toBe(0);
    },
  );
});

describe("readZip test helper", () => {
  // Offsets into the first central directory entry, from the EOCD record.
  function firstCentral(buf: Buffer): number {
    const eocd = buf.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
    return buf.readUInt32LE(eocd + 16);
  }
  const zip = () => Buffer.from(packMcpProxy({ outDir: tmp() }).zipBuf);

  it("rejects a wrong CRC-32", () => {
    const buf = zip();
    const cd = firstCentral(buf);
    const local = buf.readUInt32LE(cd + 42);
    const bad = (buf.readUInt32LE(cd + 16) ^ 1) >>> 0;
    buf.writeUInt32LE(bad, cd + 16);
    buf.writeUInt32LE(bad, local + 14);
    expect(() => readZip(buf)).toThrow(/CRC-32 mismatch/);
  });

  it("rejects a wrong uncompressed size", () => {
    const buf = zip();
    const cd = firstCentral(buf);
    const local = buf.readUInt32LE(cd + 42);
    const bad = buf.readUInt32LE(cd + 24) + 1;
    buf.writeUInt32LE(bad, cd + 24);
    buf.writeUInt32LE(bad, local + 22);
    expect(() => readZip(buf)).toThrow(/size mismatch/);
  });

  it("rejects a bad local header signature", () => {
    const buf = zip();
    buf.writeUInt32LE(0, buf.readUInt32LE(firstCentral(buf) + 42));
    expect(() => readZip(buf)).toThrow(/bad local header signature/);
  });

  it("rejects a local header that disagrees with the central directory", () => {
    const buf = zip();
    const cd = firstCentral(buf);
    buf.writeUInt32LE(0, buf.readUInt32LE(cd + 42) + 14);
    expect(() => readZip(buf)).toThrow(/local header disagrees/);
  });
});

describe("pack-mcp-proxy: running as a script", () => {
  it("recognises itself when started through a symlinked path", () => {
    const dir = tmp();
    const real = join(dir, "real.mjs");
    const link = join(dir, "link.mjs");
    writeFileSync(real, "\n");
    symlinkSync(real, link);
    const url = pathToFileURL(real).href;
    expect(isMainModule(real, url)).toBe(true);
    expect(isMainModule(link, url)).toBe(true);
    expect(isMainModule(join(dir, "other.mjs"), url)).toBe(false);
    expect(isMainModule(undefined, url)).toBe(false);
  });
});
