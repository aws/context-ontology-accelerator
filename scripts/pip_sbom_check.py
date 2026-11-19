# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fail an image build when the image's python path holds a pip that ships a vendored SBOM.

Usage: python pip_sbom_check.py [--absent]

pip vendors setuptools' pkg_resources, and recent releases (26.2 and later at least) list it in
pip/_vendor/bom.cdx.json as setuptools 70.3.0. Image scanners report that entry as CVE-2025-47273
and CVE-2026-59890 even when the installed setuptools is newer, and upgrading pip does not help.

So the deployed AL2023 images (context-manager, ontology-engine, sources database enrichment and
documents kg-build) install with the python3.12-pip RPM and never upgrade pip. That pip is 23.2.1
and has no SBOM. Amazon Linux backports its fixes through ALAS: its tar unpacking already applies
``tarfile.data_filter`` (CVE-2025-8869), and CVE-2023-5752 only affects ``hg+`` VCS installs,
which no build uses. Nothing at runtime runs pip. The preprocessing Lambda image instead uninstalls
its only pip after the last install, and runs this check with ``--absent``.

Default: fail if any ``pip/_vendor/bom.cdx.json`` sits on sys.path. ``--absent``: also fail if pip
is importable at all. pip is located with importlib.util.find_spec and never imported.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def check(absent: bool) -> str | None:
    """Return why the image fails the check, or None when it passes."""
    if absent and importlib.util.find_spec("pip") is not None:
        return "pip is still installed"
    for entry in sys.path:
        sbom = Path(entry or ".") / "pip" / "_vendor" / "bom.cdx.json"
        if sbom.is_file():
            return f"pip with a vendored SBOM is on the path: {sbom}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--absent", action="store_true", help="also require that pip is not importable at all")
    error = check(parser.parse_args(argv).absent)
    if error:
        print(f"pip SBOM check: {error}", file=sys.stderr)
        return 1
    print("pip SBOM check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
