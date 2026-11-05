# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Digest of a synthesized cloud assembly's templates.

``exact``: ``sha256sum *.template.json | sort | sha256sum``, byte for byte.
``normalized``: the same over templates with the three values that change on every synth masked: the ISO synth
timestamps of the authnz seed items (their ``createdAt``, ``updatedAt`` and ``grantedAt``), the web stack's
``api-redeploy-<clock>`` id, and container image fingerprints (they hash untracked files of the build tree).
Nothing else is masked: a timestamp anywhere else is a real value.

Run: ``python3 -I cdkout_digest.py exact|normalized <cdk.out dir>``.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

MASKS = (
    # A seed item is an AwsCustomResource call, so it sits in the template as an escaped JSON string:
    # \"createdAt\":{\"S\":\"2026-01-01T00:00:00.000Z\"}
    (
        re.compile(
            r'((?:createdAt|updatedAt|grantedAt)\\?"\s*:\s*\{\s*\\?"S\\?"\s*:\s*\\?")'
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
        ),
        r"\1<synth-time>",
    ),
    (re.compile(r"api-redeploy-\d+"), "api-redeploy-<clock>"),
    (re.compile(r"(container-assets-\$\{AWS::AccountId\}-\$\{AWS::Region\}:)[0-9a-f]{64}"), r"\1<image>"),
)


def normalize(text: str) -> str:
    """The template text with only the per-synth values masked."""
    for pattern, replacement in MASKS:
        text = pattern.sub(replacement, text)
    return text


def digest(folder: Path, normalized: bool) -> str:
    """sha256 over the sorted ``<sha256>  <name>`` lines, as ``sha256sum *.template.json | sort | sha256sum``."""
    lines = []
    for template in folder.glob("*.template.json"):
        data = template.read_bytes()
        if normalized:
            data = normalize(data.decode()).encode()
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {template.name}\n")
    return hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()


def main() -> None:
    """Print the requested digest."""
    if len(sys.argv) != 3 or sys.argv[1] not in ("exact", "normalized"):
        print("usage: cdkout_digest.py exact|normalized <cdk.out dir>", file=sys.stderr)
        raise SystemExit(2)
    mode, folder = sys.argv[1], Path(sys.argv[2])
    if not folder.is_dir() or not any(folder.glob("*.template.json")):
        print(f"cdkout_digest.py: {folder} is not an assembly: no *.template.json in it", file=sys.stderr)
        raise SystemExit(2)
    print(digest(folder, normalized=mode == "normalized"))


if __name__ == "__main__":
    main()
