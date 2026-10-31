// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Minimal ZIP reader for tests: walks the central directory and returns each
 * entry's name and uncompressed bytes. Supports the two methods the pack
 * script writes (0 = stored, 8 = deflate). No external `unzip` binary needed.
 *
 * It checks what `unzip` checks for these archives and throws on a mismatch:
 * the local header signature, that the local header agrees with the central
 * directory (method, CRC-32, sizes), and the CRC-32 and size of the
 * uncompressed data. A wrong CRC in the pack script therefore fails the tests
 * instead of reaching users as `bad CRC` from `unzip`.
 */
import { crc32, inflateRawSync } from "node:zlib";

export interface ZipEntry {
  name: string;
  data: Buffer;
}

const LOCAL_SIG = 0x04034b50;
const CENTRAL_SIG = 0x02014b50;

export function readZip(buf: Buffer): ZipEntry[] {
  const eocd = buf.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  if (eocd < 0) throw new Error("not a zip: no end-of-central-directory");
  const count = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  const entries: ZipEntry[] = [];
  for (let i = 0; i < count; i++) {
    if (buf.readUInt32LE(p) !== CENTRAL_SIG) {
      throw new Error(`bad central directory header at ${p}`);
    }
    const method = buf.readUInt16LE(p + 10);
    const crc = buf.readUInt32LE(p + 16);
    const compressed = buf.readUInt32LE(p + 20);
    const size = buf.readUInt32LE(p + 24);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const localOffset = buf.readUInt32LE(p + 42);
    const name = buf.subarray(p + 46, p + 46 + nameLen).toString("utf8");

    if (buf.readUInt32LE(localOffset) !== LOCAL_SIG) {
      throw new Error(`bad local header signature for ${name}`);
    }
    if (
      buf.readUInt16LE(localOffset + 8) !== method ||
      buf.readUInt32LE(localOffset + 14) !== crc ||
      buf.readUInt32LE(localOffset + 18) !== compressed ||
      buf.readUInt32LE(localOffset + 22) !== size
    ) {
      throw new Error(`local header disagrees with central directory: ${name}`);
    }
    const localNameLen = buf.readUInt16LE(localOffset + 26);
    const localExtraLen = buf.readUInt16LE(localOffset + 28);
    const start = localOffset + 30 + localNameLen + localExtraLen;
    const body = buf.subarray(start, start + compressed);
    let data: Buffer;
    if (method === 0) data = Buffer.from(body);
    else if (method === 8) data = inflateRawSync(body);
    else throw new Error(`unsupported zip method ${method} for ${name}`);
    if (data.length !== size) {
      throw new Error(
        `size mismatch for ${name}: ${data.length} bytes, header says ${size}`,
      );
    }
    if (crc32(data) !== crc) {
      throw new Error(`CRC-32 mismatch for ${name}`);
    }
    entries.push({ name, data });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}
