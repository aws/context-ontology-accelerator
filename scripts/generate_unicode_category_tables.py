# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate pinned Unicode category ranges shared by Python and TypeScript.

Run with the repository's Python 3.12 environment:

    uv run python scripts/generate_unicode_category_tables.py

Use ``--check`` in CI to verify that the committed outputs are current.
"""

from __future__ import annotations

import argparse
import unicodedata
from collections.abc import Callable
from pathlib import Path

PINNED_UNICODE_VERSION = "15.0.0"
MAX_CODE_POINT = 0x10FFFF
REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_OUTPUT = REPO_ROOT / "packages/ontology-engine/src/coa_ontology/inducer/services/unicode_category_ranges.py"
TYPESCRIPT_OUTPUT = REPO_ROOT / "packages/web-app/src/pages/ontology/induce/unicodeCategoryRanges.ts"


def _ranges_for(predicate: Callable[[str], bool]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    range_start: int | None = None
    previous = -1
    for code_point in range(MAX_CODE_POINT + 1):
        if predicate(unicodedata.category(chr(code_point))):
            if range_start is None:
                range_start = code_point
            previous = code_point
        elif range_start is not None:
            ranges.append((range_start, previous))
            range_start = None
    if range_start is not None:
        ranges.append((range_start, previous))
    return ranges


def _python_ranges(name: str, ranges: list[tuple[int, int]]) -> str:
    lines = [f"{name}: tuple[tuple[int, int], ...] = ("]
    lines.extend(f"    (0x{start:X}, 0x{end:X})," for start, end in ranges)
    lines.append(")")
    return "\n".join(lines)


def _typescript_ranges(name: str, ranges: list[tuple[int, int]]) -> str:
    lines = [f"export const {name}: ReadonlyArray<readonly [number, number]> = ["]
    lines.extend(f"  [0x{start:x}, 0x{end:x}]," for start, end in ranges)
    lines.append("];")
    return "\n".join(lines)


def _render_python(
    letter_number_ranges: list[tuple[int, int]],
    symbol_ranges: list[tuple[int, int]],
    mark_ranges: list[tuple[int, int]],
) -> str:
    return (
        "\n\n".join(
            [
                "# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n"
                "# SPDX-License-Identifier: Apache-2.0\n\n"
                '"""Generated Unicode 15.0 category ranges. Do not edit by hand."""\n\n'
                f'UNICODE_CATEGORY_TABLE_VERSION = "{PINNED_UNICODE_VERSION}"',
                _python_ranges("LETTER_NUMBER_RANGES", letter_number_ranges),
                _python_ranges("SYMBOL_RANGES", symbol_ranges),
                _python_ranges("MARK_RANGES", mark_ranges),
            ]
        )
        + "\n"
    )


def _render_typescript(
    letter_number_ranges: list[tuple[int, int]],
    symbol_ranges: list[tuple[int, int]],
    mark_ranges: list[tuple[int, int]],
) -> str:
    return (
        "\n\n".join(
            [
                "// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n"
                "// SPDX-License-Identifier: Apache-2.0\n\n"
                "// Generated Unicode 15.0 category ranges. Do not edit by hand.\n"
                f'export const UNICODE_CATEGORY_TABLE_VERSION = "{PINNED_UNICODE_VERSION}";',
                _typescript_ranges("LETTER_NUMBER_RANGES", letter_number_ranges),
                _typescript_ranges("SYMBOL_RANGES", symbol_ranges),
                _typescript_ranges("MARK_RANGES", mark_ranges),
            ]
        )
        + "\n"
    )


def _write_or_check(path: Path, content: str, *, check: bool) -> bool:
    if check:
        return path.is_file() and path.read_text(encoding="utf-8") == content
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if unicodedata.unidata_version != PINNED_UNICODE_VERSION:
        parser.error(
            f"Unicode {PINNED_UNICODE_VERSION} is required; this Python provides {unicodedata.unidata_version}"
        )

    letter_number_ranges = _ranges_for(lambda category: category[0] in {"L", "N"})
    symbol_ranges = _ranges_for(lambda category: category[0] == "S")
    mark_ranges = _ranges_for(lambda category: category[0] == "M")

    outputs = {
        PYTHON_OUTPUT: _render_python(letter_number_ranges, symbol_ranges, mark_ranges),
        TYPESCRIPT_OUTPUT: _render_typescript(letter_number_ranges, symbol_ranges, mark_ranges),
    }
    stale = [path for path, content in outputs.items() if not _write_or_check(path, content, check=args.check)]
    if stale:
        parser.error("generated Unicode tables are stale: " + ", ".join(str(path) for path in stale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
