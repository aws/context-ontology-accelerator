#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Print a coverage summary table from all coverage reports in the repo.

Supports:
  - Cobertura XML  (packages/*/coverage.xml, libs/*/coverage.xml,
                    infra/coverage/cobertura-coverage.xml)
  - Vitest JSON summary (packages/*/coverage/coverage-summary.json)
  - Clover XML fallback (packages/*/coverage/clover.xml)

Run after 'make test' to see per-package coverage at a glance:
    make coverage
"""

import glob
import json
import os
from datetime import datetime

# XXE hardening: defusedxml refuses entity declarations. Coverage XMLs are
# test-suite output rather than attacker input, but the same parser is what
# tier3.py uses, and the finding filed both sites together — no reason to keep
# a divergent import here. `defusedxml` is a workspace transitive dep via
# ontology-engine (uv sync installs it into the workspace .venv).
try:
    from defusedxml import ElementTree as ET
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "defusedxml is required. Run `uv sync` from the repo root."
    ) from exc

rows = []


def _mtime(f: str) -> str:
    return datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M")


def _add_cobertura(f: str, pkg: str) -> None:
    try:
        root = ET.parse(f).getroot()
    except ET.ParseError as e:
        print(f"Warning: Could not parse {f}: {e}")
        return
    rate = float(root.attrib.get("line-rate", 0)) * 100
    lines = int(root.attrib.get("lines-valid", 0))
    covered = int(root.attrib.get("lines-covered", 0))
    rows.append((pkg, lines, covered, rate, _mtime(f)))


def _add_clover(f: str, pkg: str) -> None:
    try:
        root = ET.parse(f).getroot()
    except ET.ParseError as e:
        print(f"Warning: Could not parse {f}: {e}")
        return
    metrics = root.find(".//project/metrics")
    if metrics is None:
        return
    total = int(metrics.attrib.get("statements", 0))
    covered = int(metrics.attrib.get("coveredstatements", 0))
    rate = (covered / total * 100) if total else 0
    rows.append((pkg, total, covered, rate, _mtime(f)))


def _add_json_summary(f: str, pkg: str) -> None:
    try:
        with open(f) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: Could not parse {f}: {e}")
        return
    total = data.get("total", {})
    lines = total.get("lines", total.get("statements", {}))
    covered = lines.get("covered", 0)
    total_count = lines.get("total", 0)
    rate = lines.get("pct", 0)
    rows.append((pkg, total_count, covered, rate, _mtime(f)))


# --- Python packages: Cobertura XML ---
for f in sorted(glob.glob("packages/*/coverage.xml") + glob.glob("libs/*/coverage.xml")):
    parts = f.split("/")
    pkg = f"{parts[0]}/{parts[1]}" if parts[0] == "libs" else parts[1]
    _add_cobertura(f, pkg)

# --- Infra: Jest cobertura (named cobertura-coverage.xml or coverage.xml) ---
for f in glob.glob("infra/coverage/cobertura-coverage.xml") + glob.glob("infra/coverage/coverage.xml"):
    _add_cobertura(f, "infra")
    break  # only add once

# --- Web-app: prefer json-summary, fall back to clover ---
json_f = "packages/web-app/coverage/coverage-summary.json"
clover_f = "packages/web-app/coverage/clover.xml"
if glob.glob(json_f):
    _add_json_summary(json_f, "web-app")
elif glob.glob(clover_f):
    _add_clover(clover_f, "web-app")

if not rows:
    print("No coverage reports found. Run 'make test' first.")
    raise SystemExit(1)

rows.sort(key=lambda r: r[0])

print(f"{'Package':<35} {'Lines':>8} {'Covered':>8} {'Coverage':>10}  Generated")
print("-" * 75)

total_lines = total_covered = 0
for pkg, lines, covered, rate, mtime in rows:
    total_lines += lines
    total_covered += covered
    print(f"{pkg:<35} {lines:>8} {covered:>8} {rate:>9.1f}%  {mtime}")

print("-" * 75)
overall = (total_covered / total_lines * 100) if total_lines else 0
print(f"{'TOTAL':<35} {total_lines:>8} {total_covered:>8} {overall:>9.1f}%")
