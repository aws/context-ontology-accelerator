#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Classify why a stack is in DELETE_FAILED, for scripts/destroy.sh.

`classify` reads `aws cloudformation list-stack-resources --output json` on stdin and
prints `<action> <logical id> <physical id> <type>` per DELETE_FAILED resource. Actions:
retain-runtime-sg, retain-deleted-runtime, empty-bucket, eni-pinned, unknown.
`stacks <manifest.json>` prints `<artifact id> <stack name> <region>` per stack.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from typing import TextIO

RUNTIME_SG_LOGICAL_PREFIXES = ("AgentCoreSG", "McpSG")

_DEPENDENCY_MARKERS = ("dependent object", "dependencyviolation", "has dependencies")
_NOT_EMPTY_MARKERS = ("not empty", "bucketnotempty")


def _has_marker(reason: str, markers: Iterable[str]) -> bool:
    text = reason.lower()
    return any(marker in text for marker in markers)


def classify(resource: dict, runtime_sgs: set[str], deleted_runtimes: frozenset[str] = frozenset()) -> str:
    """Return the action for one DELETE_FAILED resource."""
    rtype = resource.get("ResourceType", "")
    logical = resource.get("LogicalResourceId", "")
    physical = resource.get("PhysicalResourceId", "")
    reason = resource.get("ResourceStatusReason", "") or ""

    if rtype == "AWS::BedrockAgentCore::Runtime" and physical in deleted_runtimes:
        return "retain-deleted-runtime"
    if rtype == "AWS::EC2::SecurityGroup" and _has_marker(reason, _DEPENDENCY_MARKERS):
        if physical in runtime_sgs or logical.startswith(RUNTIME_SG_LOGICAL_PREFIXES):
            return "retain-runtime-sg"
        return "eni-pinned"
    if rtype in ("AWS::EC2::Subnet", "AWS::EC2::VPC") and _has_marker(reason, _DEPENDENCY_MARKERS):
        return "eni-pinned"
    if rtype == "AWS::S3::Bucket" and "AccessLogs" in logical and _has_marker(reason, _NOT_EMPTY_MARKERS):
        return "empty-bucket"
    return "unknown"


def failed_resources(document: dict) -> list[dict]:
    resources = document.get("StackResourceSummaries") or document.get("StackResources") or []
    return [
        r
        for r in resources
        if r.get("ResourceStatus") == "DELETE_FAILED" and r.get("ResourceType") != "AWS::CloudFormation::Stack"
    ]


def run(stdin: TextIO, stdout: TextIO, runtime_sgs: set[str], deleted_runtimes: frozenset[str] = frozenset()) -> int:
    try:
        document = json.load(stdin)
    except json.JSONDecodeError:
        # The list call failed (throttling, credentials): nothing to act on safely.
        stdout.write("unknown\t-\t-\tunreadable stack resources\n")
        return 0
    for resource in failed_resources(document):
        ids = [resource.get(key, "") for key in ("LogicalResourceId", "PhysicalResourceId", "ResourceType")]
        stdout.write("\t".join([classify(resource, runtime_sgs, deleted_runtimes), *ids]) + "\n")
    return 0


def assembly_stacks(manifest: dict, default_region: str = "") -> list[tuple[str, str, str]]:
    """(artifact id, stack name, region) per stack; env-agnostic stacks get `default_region`."""
    stacks = []
    for artifact_id, artifact in manifest.get("artifacts", {}).items():
        if artifact.get("type") != "aws:cloudformation:stack":
            continue
        name = artifact.get("properties", {}).get("stackName") or artifact_id
        region = artifact.get("environment", "").rsplit("/", 1)[-1]
        if region in ("", "unknown-region"):
            region = default_region
        stacks.append((artifact_id, name, region))
    return stacks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify DELETE_FAILED stack resources for scripts/destroy.sh.")
    sub = parser.add_subparsers(dest="command", required=True)
    classify_cmd = sub.add_parser("classify")
    classify_cmd.add_argument("--runtime-sgs", default="", help="space-separated runtime security group ids")
    classify_cmd.add_argument("--deleted-runtimes", default="", help="space-separated ids of runtimes it deleted")
    stacks_cmd = sub.add_parser("stacks")
    stacks_cmd.add_argument("manifest", help="cdk.out/manifest.json")
    stacks_cmd.add_argument("--default-region", default="", help="region of environment-agnostic stacks")
    args = parser.parse_args(argv)
    if args.command == "stacks":
        with open(args.manifest, encoding="utf-8") as handle:
            for artifact_id, name, region in assembly_stacks(json.load(handle), args.default_region):
                sys.stdout.write(f"{artifact_id}\t{name}\t{region}\n")
        return 0
    return run(sys.stdin, sys.stdout, set(args.runtime_sgs.split()), frozenset(args.deleted_runtimes.split()))


if __name__ == "__main__":
    sys.exit(main())
