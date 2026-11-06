# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Assembly digests distinguish resource changes from known synth noise."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
SCRIPT = Path(__file__).resolve().parents[2] / "infra/scripts/cdkout_digest.py"


def _digest(mode: str, folder: Path) -> str:
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), mode, str(folder)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _seed(stamp: str) -> str:
    """An authnz seed item as AwsCustomResource writes it: a JSON string inside the template."""
    return json.dumps({"service": "DynamoDB", "parameters": {"Item": {"PK": {"S": "x"}, "createdAt": {"S": stamp}}}})


def _image(fingerprint: str) -> str:
    registry = "${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com"
    return f"{registry}/cdk-hnb659fds-container-assets-${{AWS::AccountId}}-${{AWS::Region}}:{fingerprint}"


def test_normalized_digest_ignores_only_synth_noise(tmp_path: Path) -> None:
    template = tmp_path / "probe.template.json"
    template.write_text(
        json.dumps({"Create": _seed("2026-01-01T00:00:00.000Z"), "id": "api-redeploy-100", "capacity": 1})
    )
    exact, normalized = _digest("exact", tmp_path), _digest("normalized", tmp_path)
    template.write_text(
        json.dumps({"Create": _seed("2026-01-02T00:00:00.000Z"), "id": "api-redeploy-200", "capacity": 1})
    )
    assert _digest("exact", tmp_path) != exact
    assert _digest("normalized", tmp_path) == normalized
    template.write_text(
        json.dumps({"Create": _seed("2026-01-02T00:00:00.000Z"), "id": "api-redeploy-200", "capacity": 2})
    )
    assert _digest("normalized", tmp_path) != normalized


def test_normalized_digest_keeps_a_timestamp_outside_the_seed_items(tmp_path: Path) -> None:
    template = tmp_path / "probe.template.json"
    template.write_text(json.dumps({"Properties": {"StartDate": "2026-01-01T00:00:00Z"}}))
    normalized = _digest("normalized", tmp_path)
    template.write_text(json.dumps({"Properties": {"StartDate": "2027-06-30T12:00:00Z"}}))
    assert _digest("normalized", tmp_path) != normalized


def test_normalized_digest_ignores_the_container_image_fingerprint(tmp_path: Path) -> None:
    template = tmp_path / "probe.template.json"
    template.write_text(json.dumps({"ImageUri": {"Fn::Sub": _image("a" * 64)}}))
    exact, normalized = _digest("exact", tmp_path), _digest("normalized", tmp_path)
    template.write_text(json.dumps({"ImageUri": {"Fn::Sub": _image("b" * 64)}}))
    assert _digest("exact", tmp_path) != exact
    assert _digest("normalized", tmp_path) == normalized


def test_digest_refuses_an_empty_assembly(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "exact", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "not an assembly" in result.stderr


@pytest.mark.parametrize("args", [[], ["exact"], ["fast", "cdk.out"], ["exact", "cdk.out", "extra"]])
def test_missing_arguments_print_the_usage(args: list[str]) -> None:
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True)
    assert result.returncode == 2
    assert "usage: cdkout_digest.py exact|normalized <cdk.out dir>" in result.stderr
    assert "Traceback" not in result.stderr
