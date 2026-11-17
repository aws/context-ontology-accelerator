# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""CLI for the third-party NOTICE. Run via ``uv run python -m scripts.supply_chain.cli``.

Subcommands:
  notice  — write the root NOTICE (default), or --check that the committed copy
            is current with the resolved dependency set.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.supply_chain import licenses, notice


def _cmd_notice(args: argparse.Namespace) -> int:
    pkgs = licenses.iter_installed()
    body = notice.render_notice(pkgs)
    target = Path(args.output)
    if args.check:
        if not target.exists():
            print(f"NOTICE check FAILED: {target} does not exist.")
            return 1
        current = target.read_text(encoding="utf-8")
        if current.strip() != body.strip():
            print(
                f"NOTICE check FAILED: {target} is stale. "
                "Regenerate with `uv run python -m scripts.supply_chain.cli notice`."
            )
            return 1
        print(f"NOTICE up to date ({len(pkgs)} components).")
        return 0
    target.write_text(body, encoding="utf-8")
    print(f"Wrote NOTICE with {len(pkgs)} third-party components to {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="supply-chain", description="Context Ontology Accelerator third-party NOTICE")
    sub = p.add_subparsers(dest="command", required=True)

    no = sub.add_parser("notice", help="write or --check the root NOTICE")
    no.add_argument("-o", "--output", default="NOTICE")
    no.add_argument("--check", action="store_true", help="fail if NOTICE is missing or stale")
    no.set_defaults(func=_cmd_notice)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
