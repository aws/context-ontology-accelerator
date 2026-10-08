// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { execFileSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

/**
 * The vkg, serve and mcp stacks build linux/arm64 image assets. On an x86_64
 * host with no arm64 emulation the deploy fails ~20 minutes in with
 * `exec format error`; preflight-deploy.sh §3b catches that up front. Run its
 * bash detection functions against fixture input.
 */

const SCRIPT = path.join(
  __dirname,
  "..",
  "..",
  "scripts",
  "preflight-deploy.sh",
);

/** Exit status of a preflight bash function (0 = detected). */
function callBash(fn: string, args: string[], stdin = ""): number {
  try {
    execFileSync(
      "bash",
      [
        "-c",
        // eval rather than `source <(...)`: macOS ships bash 3.2, where
        // sourcing a process substitution does not define the function.
        `eval "$(sed -n "/^${fn}()/,/^}/p" "$0")"; ${fn} "$@"`,
        SCRIPT,
        ...args,
      ],
      { input: stdin, stdio: ["pipe", "pipe", "pipe"] },
    );
    return 0;
  } catch (e: unknown) {
    // Not `instanceof Error`: child_process errors come from a different realm
    // than jest's sandboxed globals.
    if (
      typeof e === "object" &&
      e !== null &&
      "status" in e &&
      typeof e.status === "number"
    ) {
      return e.status;
    }
    throw e;
  }
}

describe("_buildx_lists_runnable_arm64", () => {
  const header =
    "NAME/NODE       DRIVER/ENDPOINT             STATUS  BUILDKIT    PLATFORMS\n";

  it("rejects a stopped builder configured for linux/arm64* (field-reported host)", () => {
    // Verbatim from the x86_64 Amazon Linux 2023 host with no binfmt handler.
    const out =
      header +
      "arm64builder *  docker-container                                \n" +
      "  arm64builder0 unix:///var/run/docker.sock stopped             linux/arm64*\n" +
      "default         docker                                          \n" +
      "  default       default                     running v0.12.6-m.1 linux/amd64, linux/amd64/v2, linux/amd64/v3, linux/amd64/v4, linux/386\n";
    expect(callBash("_buildx_lists_runnable_arm64", [], out)).not.toBe(0);
  });

  it("rejects a running builder where arm64 is only configured (*)", () => {
    const out =
      header +
      "  b0 unix:///var/run/docker.sock running v0.12.6 linux/arm64*, linux/amd64\n";
    expect(callBash("_buildx_lists_runnable_arm64", [], out)).not.toBe(0);
  });

  it.each([
    "  default default running v0.12.6 linux/amd64, linux/arm64, linux/386\n",
    "  default default running v0.12.6 linux/amd64, linux/arm64\n",
    "  desktop-linux desktop-linux running v0.16.1 linux/amd64, linux/arm64/v8, linux/386\n",
  ])("accepts a running node that detected arm64: %s", (line) => {
    expect(callBash("_buildx_lists_runnable_arm64", [], header + line)).toBe(0);
  });

  it("rejects empty output (engine has no buildx)", () => {
    expect(callBash("_buildx_lists_runnable_arm64", [], "")).not.toBe(0);
  });
});

describe("_arm64_binfmt_registered", () => {
  let dir: string;
  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "binfmt-"));
    // What /proc/sys/fs/binfmt_misc held on the failing host.
    fs.writeFileSync(path.join(dir, "register"), "");
    fs.writeFileSync(path.join(dir, "status"), "enabled\n");
    fs.writeFileSync(
      path.join(dir, "kshcomp"),
      "enabled\ninterpreter /bin/ksh\n",
    );
  });
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  it("rejects a registry with no qemu-aarch64 handler", () => {
    expect(callBash("_arm64_binfmt_registered", [dir])).not.toBe(0);
  });

  it("rejects a disabled qemu-aarch64 handler", () => {
    fs.writeFileSync(
      path.join(dir, "qemu-aarch64"),
      "disabled\ninterpreter /usr/bin/qemu-aarch64\n",
    );
    expect(callBash("_arm64_binfmt_registered", [dir])).not.toBe(0);
  });

  it.each(["qemu-aarch64", "qemu-aarch64-static"])(
    "accepts an enabled %s handler",
    (name) => {
      fs.writeFileSync(
        path.join(dir, name),
        "enabled\ninterpreter /usr/bin/qemu-aarch64\nflags: POCF\n",
      );
      expect(callBash("_arm64_binfmt_registered", [dir])).toBe(0);
    },
  );
});
